from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.agent_integration import bind_harness_invocation
from legaldesk.authorization import build_request_context
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository
from legaldesk.memory import InMemoryConversationBindingStore
from legaldesk_agent import HarnessInvocationScope, HarnessInvoker
from legaldesk_agent.client import HarnessInvocationError, _structured_tool_results


class StreamClient:
    def __init__(self, events):
        self.events = events
        self.calls = []

    def invoke_harness(self, **kwargs):
        self.calls.append(kwargs)
        return {"stream": self.events}


def tool_stream(*, tool_id="tool-1", name="get_document_metadata", payload=None, status="success", result_message=True):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": tool_id, "name": name, "type": "mcp_tool_use"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
    ]
    if result_message:
        events.append({"messageStart": {"role": "user"}})
    events.append({"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolResult": {"toolUseId": tool_id, "status": status}}}})
    if payload is not None:
        events.append({"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"json": payload}]}}})
    events.append({"contentBlockStop": {"contentBlockIndex": 0}})
    return events


class Phase13HarnessStreamTests(unittest.TestCase):
    def test_fixtures_match_installed_provider_stream_schema(self):
        from botocore.session import Session
        from botocore.validate import validate_parameters

        shape = Session().get_service_model("bedrock-agentcore").operation_model("InvokeHarness").output_shape.members["stream"]
        for event in tool_stream(payload={"documents": []}):
            validate_parameters(event, shape)

    def test_mcp_error_cannot_be_promoted_to_success(self):
        payload = {"isError": True, "content": [{"text": '{"reviewTaskId":"invented"}'}]}
        with self.assertRaises(ValueError):
            _structured_tool_results(tool_stream(name="create_review_task", payload=payload))

    def test_provider_shaped_json_result_exposes_metadata_and_review_id(self):
        result = _structured_tool_results(tool_stream(payload={"content": [{"type": "text", "text": "{\"metadata\":{\"documentId\":\"doc-1\",\"status\":\"INDEXED\"},\"reviewTaskId\":\"review-1\"}"}]}))
        self.assertEqual(result[0].status, "SUCCESS")
        self.assertEqual(result[0].payload["reviewTaskId"], "review-1")
        self.assertEqual(result[0].payload["metadata"]["documentId"], "doc-1")

    def test_two_messages_can_reuse_content_block_index(self):
        events = tool_stream(tool_id="first", payload={"status": "INDEXED"}) + tool_stream(tool_id="second", payload={"status": "OPEN"})
        result = _structured_tool_results(events)
        self.assertEqual([item.tool_use_id for item in result], ["first", "second"])

    def test_missing_or_unbound_tool_result_fails_closed(self):
        missing = tool_stream(payload=None)[:-2]
        with self.assertRaises(ValueError):
            _structured_tool_results(missing)
        unbound = tool_stream(payload=None)
        unbound[-2]["contentBlockStart"]["start"]["toolResult"]["toolUseId"] = "other"
        with self.assertRaises(ValueError):
            _structured_tool_results(unbound)

    def test_stream_bounds_are_enforced(self):
        with self.assertRaises(HarnessInvocationError):
            HarnessInvoker(StreamClient([{"contentBlockDelta": {"delta": {"text": "x"}}}] * 257), "arn:test").invoke("hello")
        huge = [{"contentBlockDelta": {"delta": {"text": "x" * 1_100_000}}}]
        with self.assertRaises(HarnessInvocationError):
            HarnessInvoker(StreamClient(huge), "arn:test").invoke("hello")

    def test_application_overrides_are_fixed_and_outside_message(self):
        auth = load_authorization_store()
        identity = test_identity("idp|alice-fictional")
        conversations = InMemoryConversationBindingStore()
        context = build_request_context(identity, "mat_sundial", auth, correlation_id="11111111-1111-4111-8111-111111111111")
        conversations.bind(context, conversation_id="conversation-a", session_selector="session-a")

        class Verifier:
            def verify_authorization_header(self, authorization):
                return identity

        token = "signed-token"
        binding = bind_harness_invocation(
            bearer_token=token,
            gateway_url="https://gateway.example.test/mcp",
            identity_verifier=Verifier(),
            requested_matter_id="mat_sundial",
            conversation_id="conversation-a",
            session_selector="session-a",
            authorization_store=auth,
            conversation_store=conversations,
            invocation_repository=InMemoryGatewayGrantRepository(),
        )
        client = StreamClient([{"contentBlockDelta": {"delta": {"text": "done"}}}])
        HarnessInvoker(client, "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/legaldesk", system_prompt=({"text": "fixed"},), production_overrides=True).invoke("hello", invocation_scope=HarnessInvocationScope.from_derived(binding))
        call = client.calls[0]
        from botocore.session import Session
        from botocore.validate import validate_parameters
        validate_parameters(call, Session().get_service_model("bedrock-agentcore").operation_model("InvokeHarness").input_shape)
        self.assertEqual(call["maxIterations"], 3)
        self.assertEqual(call["maxTokens"], 512)
        self.assertEqual(call["timeoutSeconds"], 120)
        self.assertNotIn(token, call["messages"][0]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
