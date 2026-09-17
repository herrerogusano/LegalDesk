from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from fixture_loader import load_authorization_store
from legaldesk.gateway_interceptor import (
    GatewayTarget,
    InMemoryGatewayGrantRepository,
    _target_for_gateway_tool,
    gateway_request_interceptor,
    transform_gateway_request,
)


def token(subject: str) -> str:
    def segment(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'sub': subject})}.unsigned"


def event(*, subject: str = "idp|alice-fictional", matter: str = "mat_sundial") -> dict[str, object]:
    raw_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "get_document_metadata",
            "arguments": {"documentId": "doc-one", "matterId": matter},
        },
    }
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "gatewayRequest": {
                "headers": {
                    "Authorization": f"Bearer {token(subject)}",
                    "x-legaldesk-verified-subject": "idp|bob-fictional",
                    "x-legaldesk-requested-matter-id": matter,
                },
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"documentId": "doc-one"}},
                },
            },
            "rawGatewayRequest": {"body": json.dumps(raw_body)},
        },
    }


class GatewayInterceptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = load_authorization_store()

    def test_review_context_uses_jwt_sub_and_server_correlation(self) -> None:
        request = event()
        grants = InMemoryGatewayGrantRepository()
        response = transform_gateway_request(
            request,
            target=GatewayTarget.REVIEW_LAMBDA,
            authorization_store=self.auth,
            grant_repository=grants,
            correlation_id_factory=lambda: UUID("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
            grant_id_factory=lambda: UUID("9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
        )
        arguments = response["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        self.assertEqual(arguments["_legaldeskGrantId"], "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279")
        self.assertEqual(arguments["matterId"], "mat_sundial")
        grant = grants.get(arguments["_legaldeskGrantId"])
        self.assertEqual(grant["verifiedSubject"], "idp|alice-fictional")
        self.assertEqual(grant["requestedMatterId"], "mat_sundial")
        self.assertEqual(grant["correlationId"], "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279")

    def test_metadata_context_overwrites_client_headers(self) -> None:
        response = transform_gateway_request(
            event(),
            target=GatewayTarget.METADATA_MCP,
            authorization_store=self.auth,
            correlation_id_factory=lambda: UUID("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
        )
        headers = response["mcp"]["transformedGatewayRequest"]["headers"]
        self.assertEqual(headers["x-legaldesk-verified-subject"], "idp|alice-fictional")
        self.assertEqual(headers["x-legaldesk-requested-matter-id"], "mat_sundial")
        self.assertNotIn("Authorization", headers)

    def test_matter_argument_is_authorized_then_stripped_for_targets(self) -> None:
        request = event()
        headers = request["mcp"]["gatewayRequest"]["headers"]
        headers.pop("x-legaldesk-requested-matter-id")
        arguments = request["mcp"]["gatewayRequest"]["body"]["params"]["arguments"]
        arguments["matterId"] = "mat_sundial"
        response = transform_gateway_request(
            request,
            target=GatewayTarget.METADATA_MCP,
            authorization_store=self.auth,
            correlation_id_factory=lambda: UUID("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
        )
        transformed = response["mcp"]["transformedGatewayRequest"]
        self.assertNotIn("matterId", transformed["body"]["params"]["arguments"])
        self.assertNotIn("rawGatewayRequest", response["mcp"]["transformedGatewayRequest"])
        self.assertEqual(
            transformed["headers"]["x-legaldesk-requested-matter-id"], "mat_sundial"
        )

    def test_raw_gateway_selector_can_be_mapping_when_gateway_body_is_sanitized(self) -> None:
        request = event()
        request["mcp"]["rawGatewayRequest"]["body"] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "get_document_metadata",
                "arguments": {"documentId": "doc-one", "matterId": "mat_sundial"},
            },
        }
        response = transform_gateway_request(
            request,
            target=GatewayTarget.METADATA_MCP,
            authorization_store=self.auth,
            correlation_id_factory=lambda: UUID("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
        )
        self.assertEqual(
            response["mcp"]["transformedGatewayRequest"]["headers"][
                "x-legaldesk-requested-matter-id"
            ],
            "mat_sundial",
        )

    def test_raw_gateway_selector_conflicts_and_malformed_body_are_denied(self) -> None:
        conflict = event()
        conflict["mcp"]["gatewayRequest"]["body"]["params"]["arguments"]["matterId"] = "mat_glacier"
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                conflict,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )
        header_conflict = event()
        header_conflict["mcp"]["gatewayRequest"]["headers"]["x-legaldesk-requested-matter-id"] = "mat_glacier"
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                header_conflict,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )
        malformed = event()
        malformed["mcp"]["rawGatewayRequest"]["body"] = "{not-json"
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                malformed,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )

    def test_string_arguments_are_normalized_for_raw_and_gateway_requests(self) -> None:
        request = event()
        arguments = {"documentId": "doc-one", "matterId": "mat_sundial"}
        raw_body = json.loads(request["mcp"]["rawGatewayRequest"]["body"])
        raw_body["params"]["arguments"] = json.dumps(arguments)
        request["mcp"]["rawGatewayRequest"]["body"] = json.dumps(raw_body)
        request["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = json.dumps(
            {"documentId": "doc-one"}
        )
        response = transform_gateway_request(
            request,
            target=GatewayTarget.METADATA_MCP,
            authorization_store=self.auth,
            correlation_id_factory=lambda: UUID("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
        )
        transformed_arguments = response["mcp"]["transformedGatewayRequest"]["body"]["params"][
            "arguments"
        ]
        self.assertIsInstance(transformed_arguments, dict)
        self.assertNotIn("matterId", transformed_arguments)

    def test_string_arguments_malformed_or_conflicting_after_normalization_are_denied(self) -> None:
        malformed = event()
        raw_body = json.loads(malformed["mcp"]["rawGatewayRequest"]["body"])
        raw_body["params"]["arguments"] = "not-json"
        malformed["mcp"]["rawGatewayRequest"]["body"] = json.dumps(raw_body)
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                malformed,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )
        conflict = event()
        raw_body = json.loads(conflict["mcp"]["rawGatewayRequest"]["body"])
        raw_body["params"]["arguments"] = json.dumps(
            {"documentId": "doc-one", "matterId": "mat_sundial"}
        )
        conflict["mcp"]["rawGatewayRequest"]["body"] = json.dumps(raw_body)
        conflict["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = json.dumps(
            {"documentId": "doc-one", "matterId": "mat_glacier"}
        )
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                conflict,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )

    def test_audit_marks_selector_from_raw_gateway_body(self) -> None:
        request = event()
        raw_body = json.loads(request["mcp"]["rawGatewayRequest"]["body"])
        raw_body["params"]["arguments"] = json.dumps(
            {"documentId": "doc-one", "matterId": "mat_sundial"}
        )
        request["mcp"]["rawGatewayRequest"]["body"] = json.dumps(raw_body)
        request["mcp"]["gatewayRequest"]["body"]["params"]["arguments"] = json.dumps(
            {"documentId": "doc-one"}
        )
        with patch(
            "legaldesk.gateway_interceptor._authorization_store_from_environment",
            return_value=self.auth,
        ), patch(
            "legaldesk.gateway_interceptor._grant_repository_from_environment",
            return_value=InMemoryGatewayGrantRepository(),
        ), self.assertLogs("legaldesk.gateway_interceptor", level="INFO") as logs:
            response = gateway_request_interceptor(request, object())
        self.assertIn("transformedGatewayRequest", response["mcp"])
        self.assertIn('"matterSelectorPresent": true', logs.output[0])
        self.assertIn('"rawMatterPresent": true', logs.output[0])

    def test_review_interceptor_overwrites_model_grant_selector(self) -> None:
        request = event()
        request["mcp"]["gatewayRequest"]["body"]["params"]["arguments"]["_legaldeskGrantId"] = "00000000-0000-0000-0000-000000000000"
        grants = InMemoryGatewayGrantRepository()
        response = transform_gateway_request(
            request,
            target=GatewayTarget.REVIEW_LAMBDA,
            authorization_store=self.auth,
            grant_repository=grants,
            correlation_id_factory=lambda: UUID("8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
            grant_id_factory=lambda: UUID("9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"),
        )
        arguments = response["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        self.assertEqual(arguments["_legaldeskGrantId"], "9ec5d1c5-7b58-4bc2-a183-8fd48a3bd279")
        self.assertNotIn("00000000-0000-0000-0000-000000000000", grants.grants)

    def test_cross_matter_and_malformed_token_are_denied(self) -> None:
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                event(matter="mat_glacier"),
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )

        ambiguous = event()
        ambiguous["mcp"]["gatewayRequest"]["headers"]["authorization"] = "Bearer forged-client-token"
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                ambiguous,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )
        malformed = event()
        malformed["mcp"]["gatewayRequest"]["headers"]["Authorization"] = "Bearer not-a-jwt"
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                malformed,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
            )

    def test_lambda_interceptor_short_circuits_without_logging_or_body(self) -> None:
        response = gateway_request_interceptor({"mcp": {}}, object())
        self.assertEqual(response["mcp"]["transformedGatewayResponse"]["statusCode"], 403)
        self.assertNotIn("Authorization", json.dumps(response))

    def test_gateway_visible_tool_names_map_to_exact_targets(self) -> None:
        expected = {
            "review-task-lambda___create_review_task": GatewayTarget.REVIEW_LAMBDA,
            "metadata-mcp___list_matter_documents": GatewayTarget.METADATA_MCP,
            "metadata-mcp___get_document_metadata": GatewayTarget.METADATA_MCP,
        }
        for tool_name, target in expected.items():
            with self.subTest(tool_name=tool_name):
                self.assertIs(_target_for_gateway_tool(tool_name), target)

    def test_target_local_tool_names_map_to_exact_targets(self) -> None:
        expected = {
            "create_review_task": GatewayTarget.REVIEW_LAMBDA,
            "list_matter_documents": GatewayTarget.METADATA_MCP,
            "get_document_metadata": GatewayTarget.METADATA_MCP,
        }
        for tool_name, target in expected.items():
            with self.subTest(tool_name=tool_name):
                self.assertIs(_target_for_gateway_tool(tool_name), target)

    def test_unknown_or_unprefixed_tools_are_rejected(self) -> None:
        for tool_name in (
            "metadata-mcp___create_review_task",
            "review-task-lambda___get_document_metadata",
            "unknown-target___list_matter_documents",
            "create_review_task_extra",
            "get_document_metadata___suffix",
            None,
        ):
            with self.subTest(tool_name=tool_name), self.assertRaises(PermissionError):
                _target_for_gateway_tool(tool_name)

    def test_mcp_lifecycle_passes_without_business_context_for_dynamic_sync(self) -> None:
        response = gateway_request_interceptor(
            {
                "mcp": {
                    "gatewayRequest": {
                        "headers": {},
                        "body": {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/list",
                            "params": {},
                        },
                    }
                }
            },
            object(),
        )
        self.assertEqual(
            response["mcp"]["transformedGatewayRequest"]["body"]["method"],
            "tools/list",
        )

    def test_tools_list_null_cursor_is_normalized_but_next_cursor_is_preserved(self) -> None:
        base = {
            "mcp": {
                "gatewayRequest": {
                    "headers": {},
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/list",
                        "params": {"cursor": None},
                    },
                }
            }
        }
        initial = gateway_request_interceptor(base, object())
        self.assertEqual(
            initial["mcp"]["transformedGatewayRequest"]["body"]["params"], {}
        )
        with_meta = {
            "mcp": {
                "gatewayRequest": {
                    "headers": {},
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/list",
                        "params": {"cursor": None, "_meta": {"trace": "opaque"}},
                    },
                }
            }
        }
        meta_response = gateway_request_interceptor(with_meta, object())
        self.assertEqual(
            meta_response["mcp"]["transformedGatewayRequest"]["body"]["params"],
            {"_meta": {"trace": "opaque"}},
        )
        next_page = {
            "mcp": {
                "gatewayRequest": {
                    "headers": {},
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/list",
                        "params": {"cursor": "opaque-next-page"},
                    },
                }
            }
        }
        continued = gateway_request_interceptor(next_page, object())
        self.assertEqual(
            continued["mcp"]["transformedGatewayRequest"]["body"]["params"],
            {"cursor": "opaque-next-page"},
        )

    def test_tools_list_unexpected_params_are_rejected(self) -> None:
        response = gateway_request_interceptor(
            {
                "mcp": {
                    "gatewayRequest": {
                        "headers": {},
                        "body": {
                            "jsonrpc": "2.0",
                            "id": 3,
                            "method": "tools/list",
                            "params": {"cursor": None, "unexpected": True},
                        },
                    }
                }
            },
            object(),
        )
        self.assertEqual(
            response["mcp"]["transformedGatewayResponse"]["statusCode"], 403
        )
        malformed_meta = {
            "mcp": {
                "gatewayRequest": {
                    "headers": {},
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/list",
                        "params": {"_meta": "not-an-object"},
                    },
                }
            }
        }
        malformed_response = gateway_request_interceptor(malformed_meta, object())
        self.assertEqual(
            malformed_response["mcp"]["transformedGatewayResponse"]["statusCode"],
            403,
        )


if __name__ == "__main__":
    unittest.main()
