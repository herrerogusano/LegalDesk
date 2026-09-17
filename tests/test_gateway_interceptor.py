from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
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
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "gatewayRequest": {
                "headers": {
                    "Authorization": f"Bearer {token(subject)}",
                    "authorization": "Bearer forged-client-token",
                    "x-legaldesk-verified-subject": "idp|bob-fictional",
                    "x-legaldesk-requested-matter-id": matter,
                },
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"documentId": "doc-one"}},
                },
            }
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
        self.assertIs(
            _target_for_gateway_tool("review-task-lambda___create_review_task"),
            GatewayTarget.REVIEW_LAMBDA,
        )
        for tool_name in (
            "metadata-mcp___list_matter_documents",
            "metadata-mcp___get_document_metadata",
        ):
            self.assertIs(_target_for_gateway_tool(tool_name), GatewayTarget.METADATA_MCP)

    def test_unknown_or_unprefixed_tools_are_rejected(self) -> None:
        for tool_name in (
            "create_review_task",
            "metadata-mcp___create_review_task",
            "review-task-lambda___get_document_metadata",
            "unknown-target___list_matter_documents",
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


if __name__ == "__main__":
    unittest.main()
