from __future__ import annotations

import base64
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.agent_integration import bind_harness_invocation
from legaldesk.authorization import build_request_context
from legaldesk.domain.models import Matter, MatterStatus
from legaldesk.gateway_interceptor import (
    Boto3DynamoGatewayGrantRepository,
    GatewayTarget,
    InMemoryGatewayGrantRepository,
    transform_gateway_request,
)
from legaldesk.memory import InMemoryConversationBindingStore
from legaldesk_agent import HarnessInvocationScope, HarnessInvoker


class FakeHarnessClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_harness(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"stream": [{"contentBlockDelta": {"delta": {"text": "ok"}}}]}


class FakeTokenVerifier:
    def __init__(self, identities: dict[str, object]) -> None:
        self.identities = identities

    def verify_authorization_header(self, authorization: object) -> object:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise ValueError("missing bearer")
        return self.identities[authorization[7:]]


class FakeDynamoTable:
    """Provider-shaped table double returning Dynamo-deserialized Decimals."""

    def __init__(self, item: dict[str, object]) -> None:
        self.items = {(item["pk"], item["sk"]): dict(item)}

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        return {"Item": self.items.get((key["pk"], key["sk"]))}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        self.items[(item["pk"], item["sk"])] = dict(item)


def gateway_token(subject: str) -> str:
    def segment(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'sub': subject})}.unsigned"


class Phase13IdentityTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = load_authorization_store()
        self.auth.matters_by_id["mat_sundial-alt"] = Matter(
            matter_id="mat_sundial-alt",
            tenant_id="tnt_aurora",
            name="Project Sundial Alternative",
            authorized_user_ids=frozenset({"usr_alice"}),
            status=MatterStatus.ACTIVE,
        )
        self.identity = test_identity("idp|alice-fictional")
        self.bindings = InMemoryConversationBindingStore()
        self.bindings.bind(
            build_request_context(
                self.identity,
                "mat_sundial",
                self.auth,
                correlation_id="11111111-1111-4111-8111-111111111111",
            ),
            conversation_id="conversation-a",
            session_selector="session-a",
        )
        self.invocation_repository = InMemoryGatewayGrantRepository()
        self.bearer_token = gateway_token(self.identity.subject)
        self.binding = bind_harness_invocation(
            bearer_token=self.bearer_token,
            gateway_url="https://gateway.example.test/mcp",
            identity_verifier=FakeTokenVerifier({self.bearer_token: self.identity}),
            requested_matter_id="mat_sundial",
            conversation_id="conversation-a",
            session_selector="session-a",
            authorization_store=self.auth,
            conversation_store=self.bindings,
            invocation_repository=self.invocation_repository,
            correlation_id="11111111-1111-4111-8111-111111111111",
        )
        self.context = self.binding.context
        self.memory_scope = self.binding.memory_scope

    def test_binding_requires_sealed_authorized_context_and_redacts_token(self) -> None:
        self.assertNotIn("signed-user-token", repr(self.binding))
        self.assertNotIn(self.bearer_token, repr(self.binding))
        self.assertNotIn(self.bearer_token, repr(HarnessInvocationScope.from_derived(self.binding)))
        self.assertEqual(self.binding.matter_id, "mat_sundial")
        self.assertEqual(self.binding.verified_subject, "idp|alice-fictional")
        with self.assertRaises(TypeError):
            HarnessInvocationScope.from_derived(object())

    def test_invoker_uses_fixed_remote_mcp_headers_and_exact_allowlist(self) -> None:
        client = FakeHarnessClient()
        result = HarnessInvoker(client, "arn:test").invoke(
            "list my documents",
            invocation_scope=HarnessInvocationScope.from_derived(self.binding),
        )
        call = client.calls[0]
        self.assertEqual(result.correlation_id, self.context.correlation_id)
        self.assertEqual(call["actorId"], self.memory_scope.actor_id)
        self.assertEqual(call["allowedTools"], list(self.binding_scope().allowed_tools))
        tool = call["tools"][0]
        remote = tool["config"]["remoteMcp"]
        self.assertEqual(remote["url"], "https://gateway.example.test/mcp")
        self.assertEqual(remote["headers"]["Authorization"], f"Bearer {self.bearer_token}")
        self.assertEqual(
            remote["headers"]["x-legaldesk-requested-matter-id"],
            "mat_sundial",
        )
        self.assertNotIn(self.bearer_token, call["messages"][0]["content"][0]["text"])

    def test_server_selected_review_scope_cannot_call_metadata(self) -> None:
        review_repo = InMemoryGatewayGrantRepository()
        review_binding = bind_harness_invocation(
            bearer_token=self.bearer_token,
            gateway_url="https://gateway.example.test/mcp",
            identity_verifier=FakeTokenVerifier({self.bearer_token: self.identity}),
            requested_matter_id="mat_sundial",
            conversation_id="conversation-a",
            session_selector="session-a",
            authorization_store=self.auth,
            conversation_store=self.bindings,
            invocation_repository=review_repo,
            correlation_id="22222222-2222-4222-8222-222222222222",
            application_action="review",
        )
        scope = HarnessInvocationScope.from_derived(review_binding)
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": scope.gateway_headers(),
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "get_document_metadata", "arguments": {}},
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"matterId": "mat_sundial"}},
                })},
            }
        }
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                request,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
                grant_repository=review_repo,
            )

    def test_factory_rejects_swapped_user_token_before_scope_derivation(self) -> None:
        bob = test_identity("idp|bob-fictional")
        with self.assertRaises(PermissionError):
            bind_harness_invocation(
                bearer_token="bob-token",
                gateway_url="https://gateway.example.test/mcp",
                identity_verifier=FakeTokenVerifier({"bob-token": bob}),
                requested_matter_id="mat_sundial",
                conversation_id="conversation-a",
                session_selector="session-a",
                authorization_store=self.auth,
                conversation_store=self.bindings,
                invocation_repository=InMemoryGatewayGrantRepository(),
            )

    def test_expired_invocation_record_is_denied(self) -> None:
        record = self.invocation_repository.invocations[self.binding.invocation_id]
        record["expiresAt"] = 0  # test-only mutation of the local table double
        scope = HarnessInvocationScope.from_derived(self.binding)
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": scope.gateway_headers(),
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "get_document_metadata", "arguments": {}},
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"matterId": "mat_sundial"}},
                })},
            }
        }
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                request,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
                grant_repository=self.invocation_repository,
            )

    def binding_scope(self) -> HarnessInvocationScope:
        return HarnessInvocationScope.from_derived(self.binding)

    def test_model_cannot_switch_to_another_matter_for_same_user_binding(self) -> None:
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": {
                        **HarnessInvocationScope.from_derived(self.binding).gateway_headers(),
                    },
                    "body": {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "get_document_metadata",
                            "arguments": {"documentId": "doc-one", "matterId": "mat_sundial-alt"},
                        },
                    },
                },
                "rawGatewayRequest": {
                    "body": json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "tools/call",
                            "params": {
                                "name": "get_document_metadata",
                                "arguments": {"documentId": "doc-one", "matterId": "mat_sundial-alt"},
                            },
                        }
                    )
                },
            }
        }
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                request,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
                grant_repository=self.invocation_repository,
            )

    def test_invocation_record_rejects_other_authorized_matter(self) -> None:
        headers = HarnessInvocationScope.from_derived(self.binding).gateway_headers()
        headers["x-legaldesk-requested-matter-id"] = "mat_sundial-alt"
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": headers,
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {
                            "name": "get_document_metadata",
                            "arguments": {"documentId": "doc-one", "matterId": "mat_sundial-alt"},
                        },
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"matterId": "mat_sundial-alt"}},
                })},
            }
        }
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                request,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
                grant_repository=self.invocation_repository,
            )

    def test_correlation_header_is_used_only_after_selector_and_subject_checks(self) -> None:
        self.invocation_repository.invocations[self.binding.invocation_id]["expiresAt"] = Decimal("4102444800")
        headers = HarnessInvocationScope.from_derived(self.binding).gateway_headers()
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": headers,
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "get_document_metadata", "arguments": {}},
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"matterId": "mat_sundial"}},
                })},
            }
        }
        response = transform_gateway_request(
            request,
            target=GatewayTarget.METADATA_MCP,
            authorization_store=self.auth,
            grant_repository=self.invocation_repository,
        )
        self.assertEqual(
            response["mcp"]["transformedGatewayRequest"]["headers"]["x-legaldesk-correlation-id"],
            self.context.correlation_id,
        )

    def test_unbound_correlation_header_is_not_authoritative(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.bearer_token}",
            "x-legaldesk-requested-matter-id": "mat_sundial",
            "x-legaldesk-correlation-id": self.context.correlation_id,
        }
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": headers,
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "get_document_metadata", "arguments": {}},
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"matterId": "mat_sundial"}},
                })},
            }
        }
        with self.assertRaises(PermissionError):
            transform_gateway_request(
                request,
                target=GatewayTarget.METADATA_MCP,
                authorization_store=self.auth,
                grant_repository=self.invocation_repository,
            )

    def test_boto_repository_accepts_actual_dynamo_decimal_invocation_ttl(self) -> None:
        item = dict(self.invocation_repository.invocations[self.binding.invocation_id])
        item["expiresAt"] = Decimal("4102444800")
        table = FakeDynamoTable(item)
        repository = Boto3DynamoGatewayGrantRepository("fictional-table", table=table)
        headers = HarnessInvocationScope.from_derived(self.binding).gateway_headers()
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": headers,
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "get_document_metadata", "arguments": {}},
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "get_document_metadata", "arguments": {"matterId": "mat_sundial"}},
                })},
            }
        }
        response = transform_gateway_request(
            request,
            target=GatewayTarget.METADATA_MCP,
            authorization_store=self.auth,
            grant_repository=repository,
        )
        self.assertEqual(
            response["mcp"]["transformedGatewayRequest"]["headers"]["x-legaldesk-correlation-id"],
            self.context.correlation_id,
        )

    def test_repeated_review_uses_one_idempotent_grant_after_time_tick(self) -> None:
        review_repository = InMemoryGatewayGrantRepository()
        review_binding = bind_harness_invocation(
            bearer_token=self.bearer_token,
            gateway_url="https://gateway.example.test/mcp",
            identity_verifier=FakeTokenVerifier({self.bearer_token: self.identity}),
            requested_matter_id="mat_sundial",
            conversation_id="conversation-a",
            session_selector="session-a",
            authorization_store=self.auth,
            conversation_store=self.bindings,
            invocation_repository=review_repository,
            correlation_id="33333333-3333-4333-8333-333333333333",
            application_action="review",
        )
        scope = HarnessInvocationScope.from_derived(review_binding)
        review_repository.invocations[review_binding.invocation_id]["expiresAt"] = Decimal("4102444800")
        request = {
            "mcp": {
                "gatewayRequest": {
                    "headers": scope.gateway_headers(),
                    "body": {
                        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "create_review_task", "arguments": {"matterId": "mat_sundial"}},
                    },
                },
                "rawGatewayRequest": {"body": json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "create_review_task", "arguments": {"matterId": "mat_sundial"}},
                })},
            }
        }
        clock = iter(range(4100000000, 4100000100))
        with patch("legaldesk.gateway_interceptor.time.time", side_effect=lambda: next(clock)):
            first = transform_gateway_request(
                request,
                target=GatewayTarget.REVIEW_LAMBDA,
                authorization_store=self.auth,
                grant_repository=review_repository,
            )
            second = transform_gateway_request(
                request,
                target=GatewayTarget.REVIEW_LAMBDA,
                authorization_store=self.auth,
                grant_repository=review_repository,
            )
        first_id = first["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]["_legaldeskGrantId"]
        second_id = second["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]["_legaldeskGrantId"]
        self.assertEqual(first_id, review_binding.invocation_id)
        self.assertEqual(second_id, first_id)
        self.assertEqual(len(review_repository.grants), 1)


if __name__ == "__main__":
    unittest.main()
