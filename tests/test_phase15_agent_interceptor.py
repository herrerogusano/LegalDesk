"""Phase 15 T2: agent_chat allowlist and exact target/tool pairs (ADR-021 D6)."""

from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.agent_integration import _APPLICATION_TOOL_ALLOWLISTS, bind_harness_invocation
from legaldesk.authorization import AuthorizationDenied, build_request_context
from legaldesk.gateway_interceptor import (
    GatewayTarget,
    HarnessInvocationGrant,
    InMemoryGatewayGrantRepository,
    transform_gateway_request,
)
from legaldesk.memory import InMemoryConversationBindingStore

CORRELATION = "11111111-1111-4111-8111-111111111111"
SEARCH = "@legaldesk_gateway/search-documents___search_documents"
FIELDS = "@legaldesk_gateway/metadata-mcp___get_extracted_fields"
LIST = "@legaldesk_gateway/metadata-mcp___list_matter_documents"
TARGETS = {
    "search-documents": GatewayTarget.SEARCH_DOCUMENTS,
    "metadata-mcp": GatewayTarget.METADATA_MCP,
    "review-task-lambda": GatewayTarget.REVIEW_LAMBDA,
}


def gateway_token(subject: str) -> str:
    def segment(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'sub': subject})}.unsigned"


class Verifier:
    def __init__(self, identity, token):
        self.identity, self.token = identity, token

    def verify_authorization_header(self, authorization):
        if authorization != f"Bearer {self.token}":
            raise ValueError("bad token")
        return self.identity


class AgentChatInterceptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = load_authorization_store()
        self.identity = test_identity("idp|alice-fictional")
        self.token = gateway_token(self.identity.subject)
        self.bindings = InMemoryConversationBindingStore()
        self.bindings.bind(
            build_request_context(self.identity, "mat_sundial", self.auth, correlation_id=CORRELATION),
            conversation_id="conversation-a", session_selector="session-a",
        )
        self.grants = InMemoryGatewayGrantRepository()
        self.binding = self.bind("agent_chat")

    def bind(self, action: str):
        return bind_harness_invocation(
            bearer_token=self.token, gateway_url="https://gateway.example.test/mcp",
            identity_verifier=Verifier(self.identity, self.token), requested_matter_id="mat_sundial",
            conversation_id="conversation-a", session_selector="session-a",
            authorization_store=self.auth, conversation_store=self.bindings,
            invocation_repository=self.grants, correlation_id=CORRELATION, application_action=action,
        )

    def event(self, tool, arguments, *, headers=None, raw_arguments=None):
        raw = {"matterId": "mat_sundial", **(arguments if raw_arguments is None else raw_arguments)}
        return {"mcp": {
            "gatewayRequest": {
                "headers": self.binding.gateway_headers() if headers is None else headers,
                "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments}},
            },
            "rawGatewayRequest": {"body": json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": raw},
            })},
        }}

    def transform(self, tool, arguments, **kwargs):
        target = TARGETS.get(tool.split("/", 1)[1].split("___", 1)[0], GatewayTarget.METADATA_MCP)
        return transform_gateway_request(
            self.event(tool, arguments, **kwargs), target=target, authorization_store=self.auth,
            grant_repository=self.grants,
        )

    @staticmethod
    def arguments_of(response):
        return response["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]

    def test_agent_chat_allows_exactly_three_read_tools_and_no_review_tool(self) -> None:
        self.assertEqual(set(_APPLICATION_TOOL_ALLOWLISTS["agent_chat"]), {SEARCH, FIELDS, LIST})
        self.assertEqual(self.binding.allowed_tools, _APPLICATION_TOOL_ALLOWLISTS["agent_chat"])
        record = self.grants.get_invocation(self.binding.invocation_id)
        self.assertEqual(set(record["allowedTools"]), {SEARCH, FIELDS, LIST})
        # The existing UI actions keep their own, unchanged allowlists.
        self.assertEqual(len(_APPLICATION_TOOL_ALLOWLISTS["metadata"]), 2)
        self.assertEqual(len(_APPLICATION_TOOL_ALLOWLISTS["review"]), 4)

    def test_search_documents_gets_a_fresh_scoped_grant_and_ignores_a_forged_one(self) -> None:
        forged = "11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        first = self.arguments_of(self.transform(SEARCH, {"question": "¿Qué dice la cláusula 4?", "_legaldeskGrantId": forged}))
        second = self.arguments_of(self.transform(SEARCH, {"question": "otra"}))
        self.assertNotEqual(first["_legaldeskGrantId"], second["_legaldeskGrantId"])
        self.assertNotEqual(first["_legaldeskGrantId"], forged)
        self.assertEqual(first["matterId"], "mat_sundial")
        grant = self.grants.get(first["_legaldeskGrantId"])
        self.assertEqual(
            (grant["toolName"], grant["verifiedSubject"], grant["requestedMatterId"], grant["correlationId"]),
            ("search_documents", self.identity.subject, "mat_sundial", CORRELATION),
        )
        self.assertLessEqual(grant["expiresAt"], self.grants.get_invocation(self.binding.invocation_id)["expiresAt"])

    def test_get_extracted_fields_is_routed_to_the_metadata_target_with_a_grant(self) -> None:
        response = self.transform(FIELDS, {"documentIds": ["doc-1"]})["mcp"]["transformedGatewayRequest"]
        grant = self.grants.get(response["headers"]["x-legaldesk-grant-id"])
        self.assertEqual((grant["toolName"], grant["requestedMatterId"]), ("get_extracted_fields", "mat_sundial"))
        self.assertNotIn("matterId", response["body"]["params"]["arguments"])

    def test_agent_chat_cannot_call_review_tools_or_metadata_by_id(self) -> None:
        review = "@legaldesk_gateway/review-task-lambda___"
        for tool, arguments in (
            (review + "create_review_task", {"reasonCode": "user_requested_review"}),
            (review + "list_review_tasks", {}),
            (review + "get_review_task", {"reviewTaskId": "r1"}),
            (review + "update_review_task", {"reviewTaskId": "r1", "status": "closed"}),
            ("@legaldesk_gateway/metadata-mcp___get_document_metadata", {"documentId": "doc-1"}),
        ):
            with self.subTest(tool=tool), self.assertRaises(AuthorizationDenied):
                self.transform(tool, arguments)

    def test_other_actions_cannot_call_the_new_tools(self) -> None:
        for action in ("metadata", "review"):
            self.binding = self.bind(action)
            for tool, arguments in ((SEARCH, {"question": "q"}), (FIELDS, {"documentIds": ["doc-1"]})):
                with self.subTest(action=action, tool=tool), self.assertRaises(AuthorizationDenied):
                    self.transform(tool, arguments)

    def test_cross_matter_selectors_are_denied_for_the_new_tools(self) -> None:
        for tool, arguments in ((SEARCH, {"question": "q"}), (FIELDS, {"documentIds": ["doc-1"]})):
            with self.subTest(tool=tool, case="body differs from raw"), self.assertRaises(AuthorizationDenied):
                self.transform(tool, {**arguments, "matterId": "mat_other"})
            with self.subTest(tool=tool, case="raw names another matter"), self.assertRaises(AuthorizationDenied):
                self.transform(tool, arguments, raw_arguments={"matterId": "mat_other", **arguments})
        # The record binds the matter: it cannot be replayed against another authorized one.
        event = self.event(SEARCH, {"question": "q"})
        raw = json.loads(event["mcp"]["rawGatewayRequest"]["body"])
        raw["params"]["arguments"]["matterId"] = "mat_sundial-alt"
        event["mcp"]["rawGatewayRequest"]["body"] = json.dumps(raw)
        event["mcp"]["gatewayRequest"]["headers"] = {
            **event["mcp"]["gatewayRequest"]["headers"], "x-legaldesk-requested-matter-id": "mat_sundial-alt",
        }
        with self.assertRaises(AuthorizationDenied):
            transform_gateway_request(event, target=GatewayTarget.SEARCH_DOCUMENTS,
                                      authorization_store=self.auth, grant_repository=self.grants)

    def test_search_documents_requires_a_live_invocation_record(self) -> None:
        bare = {key: value for key, value in self.binding.gateway_headers().items()
                if key in {"Authorization", "x-legaldesk-requested-matter-id"}}
        with self.assertRaises(AuthorizationDenied):
            self.transform(SEARCH, {"question": "q"}, headers=bare)
        forged = {**self.binding.gateway_headers(), "x-legaldesk-invocation-id": "22222222-2222-4222-8222-222222222222"}
        with self.assertRaises(AuthorizationDenied):
            self.transform(SEARCH, {"question": "q"}, headers=forged)

    def test_expired_or_foreign_invocation_records_are_rejected(self) -> None:
        def record(invocation_id, *, subject, expires_at):
            return HarnessInvocationGrant(
                invocation_id=invocation_id, verified_subject=subject, requested_matter_id="mat_sundial",
                correlation_id=CORRELATION, memory_actor_id=self.binding.memory_scope.actor_id,
                memory_session_id=self.binding.memory_scope.session_id,
                allowed_tools=_APPLICATION_TOOL_ALLOWLISTS["agent_chat"], expires_at=expires_at,
            )

        for item in (
            record("33333333-3333-4333-8333-333333333333", subject=self.identity.subject, expires_at=int(time.time()) - 1),
            record("44444444-4444-4444-8444-444444444444", subject="idp|someone-else", expires_at=int(time.time()) + 300),
        ):
            self.grants.put_invocation(item)
            headers = {**self.binding.gateway_headers(), "x-legaldesk-invocation-id": item.invocation_id}
            with self.subTest(invocation=item.invocation_id), self.assertRaises(AuthorizationDenied):
                self.transform(SEARCH, {"question": "q"}, headers=headers)

    def test_only_exact_target_and_tool_pairs_are_accepted(self) -> None:
        for target, tool, arguments in (
            (GatewayTarget.METADATA_MCP, SEARCH, {"question": "q"}),
            (GatewayTarget.SEARCH_DOCUMENTS, FIELDS, {"documentIds": ["doc-1"]}),
            (GatewayTarget.SEARCH_DOCUMENTS, LIST, {}),
            (GatewayTarget.REVIEW_LAMBDA, SEARCH, {"question": "q"}),
        ):
            with self.subTest(target=target, tool=tool), self.assertRaises(AuthorizationDenied):
                transform_gateway_request(
                    self.event(tool, arguments), target=target, authorization_store=self.auth,
                    grant_repository=self.grants,
                )
        for tool in ("search-documents___get_document_metadata", "search_documents_v2", "metadata-mcp___search_documents"):
            with self.subTest(tool=tool), self.assertRaises(AuthorizationDenied):
                self.transform(f"@legaldesk_gateway/{tool}", {})


if __name__ == "__main__":
    unittest.main()
