from __future__ import annotations

import json
import os
import sys
import unittest
import base64
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.authorization import AuthorizationDenied, RequestContext, VerifiedIdentity, build_request_context
from legaldesk.documents import Document, DocumentStatus, InMemoryDocumentMetadataRepository
from legaldesk.mcp_server import (
    GET_DOCUMENT_METADATA,
    LIST_MATTER_DOCUMENTS,
    MCPServer,
    handle_metadata_request_for_identity,
    mcp_lambda_handler,
)
from legaldesk.gateway_interceptor import (
    GatewayAuthorizationGrant,
    InMemoryGatewayGrantRepository,
)


ALICE = test_identity("idp|alice-fictional")
CORRELATION_ID = "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"


def context(matter_id: str = "mat_sundial") -> RequestContext:
    return build_request_context(ALICE, matter_id, load_authorization_store(), correlation_id=CORRELATION_ID)


def document(document_id: str = "doc-sundial", matter_id: str = "mat_sundial") -> Document:
    return Document(
        document_id=document_id,
        matter_id=matter_id,
        tenant_id="tnt_aurora",
        name="Sundial notice.pdf",
        s3_key=f"tenants/tnt_aurora/matters/{matter_id}/documents/{document_id}/original.pdf",
        media_type="application/pdf",
        jurisdiction="fictional",
        document_date="2099-01-01",
        confidentiality="fictional-internal",
        status=DocumentStatus.INDEXED,
        file_size_bytes=123,
    )


class MCPMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = InMemoryDocumentMetadataRepository()
        self.repository.save(document())
        self.server = MCPServer(self.repository)

    def test_tools_list_exposes_only_two_strict_metadata_tools(self) -> None:
        response = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            request_context=context(),
        )
        self.assertEqual([tool["name"] for tool in response["result"]["tools"]], [LIST_MATTER_DOCUMENTS, GET_DOCUMENT_METADATA])
        self.assertEqual(response["result"]["_meta"], {"correlationId": CORRELATION_ID})
        self.assertTrue(all(tool["inputSchema"]["additionalProperties"] is False for tool in response["result"]["tools"]))
        self.assertEqual(response["result"]["tools"][0]["inputSchema"]["required"], ["matterId"])
        self.assertEqual(
            response["result"]["tools"][1]["inputSchema"]["required"],
            ["documentId", "matterId"],
        )

    def test_tools_list_accepts_pagination_cursor_and_reserved_meta(self) -> None:
        response = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "page-2",
                "method": "tools/list",
                "params": {"cursor": "opaque-next-page", "_meta": {"trace": "opaque"}},
            }
        )
        self.assertIn("result", response)
        malformed_meta = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "bad-meta",
                "method": "tools/list",
                "params": {"cursor": "opaque-next-page", "_meta": "not-an-object"},
            }
        )
        self.assertEqual(malformed_meta["error"]["code"], -32602)
        invalid_cursor = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "bad-cursor",
                "method": "tools/list",
                "params": {"cursor": 7, "_meta": {}},
            }
        )
        self.assertEqual(invalid_cursor["error"]["code"], -32602)

    def test_lifecycle_rejects_unknown_params_and_ping_cursor(self) -> None:
        unknown = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "unknown",
                "method": "tools/list",
                "params": {"_meta": {}, "unexpected": True},
            }
        )
        self.assertEqual(unknown["error"]["code"], -32602)
        ping_cursor = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "ping-cursor",
                "method": "ping",
                "params": {"cursor": "not-valid-for-ping"},
            }
        )
        self.assertEqual(ping_cursor["error"]["code"], -32602)

    def test_jsonrpc_notifications_never_respond_or_execute_tools(self) -> None:
        for method in (
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
            "unknown/notification",
        ):
            with self.subTest(method=method):
                self.assertIsNone(
                    self.server.handle_jsonrpc(
                        {"jsonrpc": "2.0", "method": method, "params": {}}
                    )
                )

        self.assertIsNone(
            self.server.handle_jsonrpc(
                {
                    "jsonrpc": "2.0",
                    "method": "tools/call",
                    "params": {
                        "name": GET_DOCUMENT_METADATA,
                        "arguments": {"documentId": "doc-sundial"},
                    },
                },
                request_context=context(),
            )
        )

    def test_list_current_matter_returns_metadata_without_s3_or_body(self) -> None:
        response = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": "list", "method": "tools/call", "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {}}},
            request_context=context(),
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["documents"][0]["documentId"], "doc-sundial")
        self.assertNotIn("s3Key", payload["documents"][0])
        self.assertNotIn("body", payload["documents"][0])

    def test_get_metadata_current_matter_and_invalid_arguments(self) -> None:
        response = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": GET_DOCUMENT_METADATA, "arguments": {"documentId": "doc-sundial"}}},
            request_context=context(),
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual(payload["document"]["mediaType"], "application/pdf")
        self.assertNotIn("s3Key", payload["document"])
        invalid = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": GET_DOCUMENT_METADATA, "arguments": {"documentId": "../other"}}},
            request_context=context(),
        )
        self.assertEqual(invalid["error"], {"code": -32602, "message": "invalid parameters"})

    def test_tools_call_allows_reserved_meta_without_relaxing_arguments(self) -> None:
        response = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "meta",
                "method": "tools/call",
                "params": {
                    "name": GET_DOCUMENT_METADATA,
                    "arguments": {"documentId": "doc-sundial"},
                    "_meta": {"trace": "opaque-client-metadata"},
                },
            },
            request_context=context(),
        )
        self.assertIn("result", response)
        invalid = self.server.handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "meta-invalid",
                "method": "tools/call",
                "params": {
                    "name": GET_DOCUMENT_METADATA,
                    "arguments": {"documentId": "doc-sundial", "matterId": "mat_sundial"},
                    "_meta": {},
                },
            },
            request_context=context(),
        )
        self.assertEqual(invalid["error"]["code"], -32602)

    def test_repository_results_are_scope_checked_and_cross_matter_auth_denies_first(self) -> None:
        class LeakyRepository:
            def list_for_scope(self, **_: object) -> tuple[Document, ...]:
                return (document("doc-other", "mat_glacier"),)

            def get_for_scope(self, **_: object) -> Document:
                return document("doc-other", "mat_glacier")

        response = MCPServer(LeakyRepository()).handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {}}},
            request_context=context(),
        )
        self.assertEqual(json.loads(response["result"]["content"][0]["text"]), {"documents": []})
        with self.assertRaises(AuthorizationDenied):
            handle_metadata_request_for_identity(
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {}}},
                ALICE,
                "mat_glacier",
                authorization_store=load_authorization_store(),
                metadata_repository=self.repository,
            )

    def test_malformed_protocol_and_unauthorized_context_do_not_reach_repository(self) -> None:
        response = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {"matterId": "mat_glacier"}}},
            request_context=context(),
        )
        self.assertEqual(response["error"]["code"], -32602)
        denied = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {}}},
            request_context=None,  # type: ignore[arg-type]
        )
        self.assertEqual(denied["error"]["code"], -32001)

    def test_mcp_lifecycle_methods_do_not_require_business_scope(self) -> None:
        initialized = self.server.handle_jsonrpc(
            {"jsonrpc": "2.0", "id": 10, "method": "initialize", "params": {}},
        )
        self.assertEqual(initialized["result"]["serverInfo"]["name"], "legaldesk-metadata")
        self.assertEqual(
            self.server.handle_jsonrpc(
                {"jsonrpc": "2.0", "id": 11, "method": "ping", "params": {}},
            )["result"],
            {},
        )
        self.assertIsNone(
            self.server.handle_jsonrpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        invalid_version = self.server.handle_jsonrpc(
            {"jsonrpc": "1.0", "id": 12, "method": "ping", "params": {}},
        )
        self.assertEqual(invalid_version["error"]["code"], -32600)

    def test_function_url_adapter_requires_selector_headers_and_json_body(self) -> None:
        response = mcp_lambda_handler({"body": "{}", "headers": {}}, object())
        self.assertIn(response["statusCode"], {403, 503})
        with patch.object(
            __import__("legaldesk.mcp_server", fromlist=["_mcp_repositories_from_environment"]),
            "_mcp_repositories_from_environment",
            return_value=(load_authorization_store(), self.repository),
        ):
            response = mcp_lambda_handler(
                {
                    "body": json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/list", "params": {}}),
                    "headers": {
                        "x-legaldesk-verified-subject": ALICE.subject,
                        "x-legaldesk-requested-matter-id": "mat_sundial",
                        "x-legaldesk-correlation-id": CORRELATION_ID,
                    },
                },
                object(),
            )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["MCP-Protocol-Version"], "2025-03-26")

    def test_function_url_lifecycle_allows_sync_without_business_scope(self) -> None:
        with patch.object(
            __import__("legaldesk.mcp_server", fromlist=["_mcp_repositories_from_environment"]),
            "_mcp_repositories_from_environment",
            return_value=(load_authorization_store(), self.repository),
        ):
            response = mcp_lambda_handler(
                {
                    "body": json.dumps(
                        {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}}
                    ),
                    "headers": {},
                },
                object(),
            )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["MCP-Protocol-Version"], "2025-03-26")

    def test_function_url_decodes_base64_json_body_and_rejects_invalid_base64(self) -> None:
        request_body = json.dumps(
            {"jsonrpc": "2.0", "id": "encoded", "method": "tools/list", "params": {}}
        ).encode("utf-8")
        with patch.object(
            __import__("legaldesk.mcp_server", fromlist=["_mcp_repositories_from_environment"]),
            "_mcp_repositories_from_environment",
            return_value=(load_authorization_store(), self.repository),
        ):
            response = mcp_lambda_handler(
                {
                    "body": base64.b64encode(request_body).decode("ascii"),
                    "isBase64Encoded": True,
                    "headers": {},
                },
                object(),
            )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["MCP-Protocol-Version"], "2025-03-26")
        invalid = mcp_lambda_handler(
            {"body": "not-base64!", "isBase64Encoded": True, "headers": {}}, object()
        )
        self.assertEqual(invalid["statusCode"], 400)
        self.assertEqual(json.loads(invalid["body"]), {"error": "invalid_request"})

    def test_function_url_reads_selector_headers_case_insensitively(self) -> None:
        grants = InMemoryGatewayGrantRepository()
        grant_id = "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
        grants.put(GatewayAuthorizationGrant(
            grant_id=grant_id,
            verified_subject=ALICE.subject,
            requested_matter_id="mat_sundial",
            correlation_id=CORRELATION_ID,
            tool_name=LIST_MATTER_DOCUMENTS,
            expires_at=4_000_000_000,
        ))
        with patch.object(
            __import__("legaldesk.mcp_server", fromlist=["_mcp_repositories_from_environment"]),
            "_mcp_repositories_from_environment",
            return_value=(load_authorization_store(), self.repository),
        ), patch.object(
            __import__("legaldesk.mcp_server", fromlist=["_mcp_grant_repository_from_environment"]),
            "_mcp_grant_repository_from_environment",
            return_value=grants,
        ):
            response = mcp_lambda_handler(
                {
                    "body": json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "mixed-case",
                            "method": "tools/call",
                            "params": {
                                "name": LIST_MATTER_DOCUMENTS,
                                "arguments": {},
                            },
                        }
                    ),
                    "headers": {
                        "x-legaldesk-grant-id": grant_id,
                        "X-LegalDesk-Verified-Subject": ALICE.subject,
                        "X-LEGALDESK-REQUESTED-MATTER-ID": "mat_sundial",
                        "x-LeGaLdEsK-cOrReLaTiOn-Id": CORRELATION_ID,
                    },
                },
                object(),
            )
        self.assertEqual(response["statusCode"], 200)

    def test_function_url_rejects_unknown_mcp_schema_version(self) -> None:
        with patch.dict(
            os.environ,
            {"MCP_SCHEMA_VERSION": "999", "DOCUMENT_METADATA_TABLE_NAME": "fictional-table"},
        ):
            # The environment guard is exercised directly; no provider call is
            # made when the configured protocol version is unsupported.
            from legaldesk.mcp_server import _mcp_repositories_from_environment

            self.assertIsNone(_mcp_repositories_from_environment())


if __name__ == "__main__":
    unittest.main()
