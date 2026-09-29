from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixture_loader import load_authorization_store, test_identity  # noqa: E402
from legaldesk.agent_integration import bind_harness_invocation  # noqa: E402
from legaldesk.authorization import build_request_context  # noqa: E402
from legaldesk.gateway_client import (  # noqa: E402
    DirectGatewayInvoker,
    GatewayHttpResponse,
    GatewayInvocationError,
    MCP_PROTOCOL_VERSION,
)
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository  # noqa: E402
from legaldesk.memory import InMemoryConversationBindingStore  # noqa: E402
from legaldesk.smoke_budget import SmokeBudget, SmokeBudgetExceeded, SmokeBudgetLimits  # noqa: E402


def _token(subject: str) -> str:
    def segment(value: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'none'})}.{segment({'sub': subject})}.unsigned"


class _Verifier:
    def __init__(self, token: str, identity: object) -> None:
        self.token = token
        self.identity = identity

    def verify_authorization_header(self, value: object) -> object:
        if value != f"Bearer {self.token}":
            raise ValueError("invalid token")
        return self.identity


class _Transport:
    def __init__(self, response_factory=None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response_factory = response_factory
        self.error = error

    def post(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": dict(headers), "timeout": timeout})
        if self.error is not None:
            raise self.error
        request = json.loads(body)
        if self.response_factory is not None:
            return self.response_factory(request)
        return GatewayHttpResponse(
            200,
            "application/json",
            json.dumps({
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": json.dumps({"items": []})}]},
            }).encode(),
        )


class GatewayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        auth = load_authorization_store()
        identity = test_identity("idp|alice-fictional")
        binding_store = InMemoryConversationBindingStore()
        binding_store.bind(
            build_request_context(identity, "mat_sundial", auth, correlation_id="11111111-1111-4111-8111-111111111111"),
            conversation_id="conversation-a",
            session_selector="session-a",
        )
        token = _token(identity.subject)
        self.binding = bind_harness_invocation(
            bearer_token=token,
            gateway_url="https://gateway.example.test/mcp",
            identity_verifier=_Verifier(token, identity),
            requested_matter_id="mat_sundial",
            conversation_id="conversation-a",
            session_selector="session-a",
            authorization_store=auth,
            conversation_store=binding_store,
            invocation_repository=InMemoryGatewayGrantRepository(),
            correlation_id="11111111-1111-4111-8111-111111111111",
        )
        self.token = token

    def test_request_uses_qualified_tool_sealed_headers_and_redacts_token(self) -> None:
        transport = _Transport()
        invoker = DirectGatewayInvoker(self.binding.gateway_url, transport=transport)
        result = invoker.invoke(self.binding, tool_name="list_matter_documents", arguments={"matterId": "forged"}, request_id="11111111-1111-4111-8111-111111111111")
        call = transport.calls[0]
        request = json.loads(call["body"])
        self.assertEqual(request["params"]["name"], "metadata-mcp___list_matter_documents")
        self.assertEqual(request["params"]["arguments"]["matterId"], "mat_sundial")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {self.token}")
        self.assertEqual(call["headers"]["MCP-Protocol-Version"], MCP_PROTOCOL_VERSION)
        self.assertEqual(result["correlationId"], self.binding.correlation_id)
        self.assertNotIn(self.token, repr(result))

    def test_allowlist_denies_review_tool_from_metadata_binding(self) -> None:
        invoker = DirectGatewayInvoker(self.binding.gateway_url, transport=_Transport())
        with self.assertRaises(GatewayInvocationError):
            invoker.invoke(self.binding, tool_name="create_review_task", arguments={})

    def test_deterministic_review_binding_does_not_depend_on_harness_tool_parser(self) -> None:
        auth = load_authorization_store()
        identity = test_identity("idp|alice-fictional")
        binding_store = InMemoryConversationBindingStore()
        binding_store.bind(
            build_request_context(identity, "mat_sundial", auth),
            conversation_id="conversation-review",
            session_selector="session-review",
        )
        token = _token(identity.subject)
        binding = bind_harness_invocation(
            bearer_token=token,
            gateway_url=self.binding.gateway_url,
            identity_verifier=_Verifier(token, identity),
            requested_matter_id="mat_sundial",
            conversation_id="conversation-review",
            session_selector="session-review",
            authorization_store=auth,
            conversation_store=binding_store,
            invocation_repository=InMemoryGatewayGrantRepository(),
            application_action="review",
        )
        transport = _Transport()
        result = DirectGatewayInvoker(binding.gateway_url, transport=transport).invoke(
            binding, tool_name="list_review_tasks", arguments={}
        )
        request = json.loads(transport.calls[0]["body"])
        self.assertEqual(request["params"]["name"], "review-task-lambda___list_review_tasks")
        self.assertEqual(request["params"]["arguments"]["matterId"], "mat_sundial")
        self.assertEqual(result["correlationId"], binding.correlation_id)

    def test_non_2xx_malformed_json_bad_sse_and_is_error_fail_closed(self) -> None:
        cases = [
            GatewayHttpResponse(403, "application/json", b"{}"),
            GatewayHttpResponse(200, "application/json", b"not-json"),
            GatewayHttpResponse(200, "text/event-stream", b"data: not-json\n\n"),
            GatewayHttpResponse(200, "application/json", b"\xff"),
        ]
        for response in cases:
            with self.subTest(response=response):
                transport = _Transport(response_factory=lambda _request, response=response: response)
                with self.assertRaises(GatewayInvocationError):
                    DirectGatewayInvoker(self.binding.gateway_url, transport=transport).invoke(
                        self.binding, tool_name="list_matter_documents", arguments={}
                    )

        def is_error(request):
            return GatewayHttpResponse(200, "application/json", json.dumps({
                "jsonrpc": "2.0", "id": request["id"], "result": {"isError": True}
            }).encode())

        with self.assertRaises(GatewayInvocationError):
            DirectGatewayInvoker(self.binding.gateway_url, transport=_Transport(response_factory=is_error)).invoke(
                self.binding, tool_name="list_matter_documents", arguments={}
            )

    def test_redirect_transport_does_not_retry_or_follow(self) -> None:
        transport = _Transport(error=GatewayInvocationError("gateway_redirect"))
        with self.assertRaises(GatewayInvocationError):
            DirectGatewayInvoker(self.binding.gateway_url, transport=transport).invoke(
                self.binding, tool_name="list_matter_documents", arguments={}
            )
        self.assertEqual(len(transport.calls), 1)

    def test_gateway_endpoint_rejects_reserved_placeholder_and_credentials(self) -> None:
        for endpoint in (
            "https://gateway.invalid/mcp",
            "https://user:password@gateway.example.test/mcp",
            "http://gateway.example.test/mcp",
            "https://gateway.example.test/mcp#fragment",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                DirectGatewayInvoker(endpoint)

    def test_gateway_budget_caps_and_failed_call_latches(self) -> None:
        budget = SmokeBudget(SmokeBudgetLimits(gateway=1))
        invoker = DirectGatewayInvoker(self.binding.gateway_url, transport=_Transport(), budget=budget)
        invoker.invoke(self.binding, tool_name="list_matter_documents", arguments={})
        with self.assertRaises(SmokeBudgetExceeded):
            invoker.invoke(self.binding, tool_name="list_matter_documents", arguments={})

        failed_budget = SmokeBudget(SmokeBudgetLimits(gateway=2))
        failed = DirectGatewayInvoker(
            self.binding.gateway_url,
            transport=_Transport(error=RuntimeError("PRIVATE_TOKEN")),
            budget=failed_budget,
        )
        with self.assertRaises(RuntimeError):
            failed.invoke(self.binding, tool_name="list_matter_documents", arguments={})
        self.assertNotIn("PRIVATE_TOKEN", repr(failed_budget.snapshot()))
        with self.assertRaises(SmokeBudgetExceeded):
            failed.invoke(self.binding, tool_name="list_matter_documents", arguments={})


if __name__ == "__main__":
    unittest.main()
