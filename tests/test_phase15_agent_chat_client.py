"""Phase 15 (T4.2): the Harness adapter's agent_chat mode."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.agent_integration import bind_harness_invocation
from legaldesk.authorization import build_request_context
from legaldesk.gateway_interceptor import InMemoryGatewayGrantRepository
from legaldesk.memory import InMemoryConversationBindingStore
from legaldesk_agent import HarnessInvocationError, HarnessInvocationScope, HarnessInvoker, invoke_agent_chat
from legaldesk_agent.client import classify_loop

HARNESS_ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/legaldesk"
SEARCH = "@legaldesk_gateway/search-documents___search_documents"
FIELDS = "@legaldesk_gateway/metadata-mcp___get_extracted_fields"
REVIEW = "@legaldesk_gateway/review-task-lambda___create_review_task"


class StreamClient:
    def __init__(self, events=(), *, error_after=None, raises=None):
        self.events, self.error_after, self.raises, self.calls = list(events), error_after, raises, []

    def invoke_harness(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises

        def stream():
            for index, event in enumerate(self.events):
                if self.error_after is not None and index == self.error_after[0]:
                    raise self.error_after[1]
                yield event
            if self.error_after is not None and self.error_after[0] >= len(self.events):
                raise self.error_after[1]

        return {"stream": stream()}


def tool_call(tool_id, name, payload, *, status="success"):
    events = [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": tool_id, "name": name, "type": "mcp_tool_use"}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {"messageStart": {"role": "user"}},
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolResult": {"toolUseId": tool_id, "status": status}}}},
    ]
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps({"content": [{"type": "text", "text": json.dumps(payload)}]})
        events.append({"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"text": text}]}}})
    events.append({"contentBlockStop": {"contentBlockIndex": 0}})
    return events


def final_message(text, stop="end_turn"):
    return [
        {"messageStart": {"role": "assistant"}},
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": stop}},
    ]


def make_scope(application_action="agent_chat"):
    auth = load_authorization_store()
    identity = test_identity("idp|alice-fictional")
    conversations = InMemoryConversationBindingStore()
    context = build_request_context(identity, "mat_sundial", auth, correlation_id="11111111-1111-4111-8111-111111111111")
    conversations.bind(context, conversation_id="conversation-a", session_selector="session-a")

    class Verifier:
        def verify_authorization_header(self, authorization):
            return identity

    binding = bind_harness_invocation(
        bearer_token="signed-token", gateway_url="https://gateway.example.test/mcp", identity_verifier=Verifier(),
        requested_matter_id="mat_sundial", conversation_id="conversation-a", session_selector="session-a",
        authorization_store=auth, conversation_store=conversations, invocation_repository=InMemoryGatewayGrantRepository(),
        application_action=application_action,
    )
    return HarnessInvocationScope.from_derived(binding)


PROMPT = ({"text": "agent prompt"},)
BIG_ANSWER = "Cláusula 4. " * 600  # ~7 KB: far above the 512-character filter of the button paths


class AgentChatInvocationTests(unittest.TestCase):
    def invoke(self, client, **kwargs):
        return invoke_agent_chat(HarnessInvoker(client, HARNESS_ARN), kwargs.pop("message", "¿Qué dice?"),
                                 invocation_scope=kwargs.pop("scope", None) or make_scope(), system_prompt=PROMPT, **kwargs)

    def test_request_is_fixed_by_the_server_and_carries_no_memory_actor(self) -> None:
        client = StreamClient(final_message("{}"))
        scope = make_scope()
        self.invoke(client, scope=scope, history=(("user", "antes"), ("assistant", "respuesta")))
        call = client.calls[0]
        from botocore.session import Session
        from botocore.validate import validate_parameters
        validate_parameters(call, Session().get_service_model("bedrock-agentcore").operation_model("InvokeHarness").input_shape)
        self.assertNotIn("actorId", call)  # Harness memory is not attached (ADR-021 D7)
        self.assertEqual((call["maxIterations"], call["timeoutSeconds"]), (3, 90))
        self.assertEqual(call["runtimeSessionId"], scope.memory_scope.session_id)
        self.assertEqual(call["systemPrompt"], list(PROMPT))
        self.assertEqual(set(call["allowedTools"]), {SEARCH, FIELDS, "@legaldesk_gateway/metadata-mcp___list_matter_documents"})
        self.assertNotIn(REVIEW, call["allowedTools"])
        self.assertEqual(call["tools"][0]["config"]["remoteMcp"]["headers"]["x-legaldesk-invocation-id"], scope.invocation_id)
        self.assertEqual([(m["role"], m["content"][0]["text"]) for m in call["messages"]],
                         [("user", "antes"), ("assistant", "respuesta"), ("user", "¿Qué dice?")])
        self.assertNotIn("signed-token", json.dumps(call["messages"]))
        for forbidden in ("model", "skills", "toolChoice"):
            self.assertNotIn(forbidden, call)

    def test_malformed_history_is_still_rejected(self) -> None:
        for history in ((("system", "x"),), (("user", " "),), (("user",),), [("user", "x")]):
            with self.assertRaises(ValueError):
                self.invoke(StreamClient(final_message("{}")), history=history)

    def test_a_long_conversation_is_trimmed_not_refused(self) -> None:
        # 12 messages of 5,000 characters: far above the budget, and it must not fail the question.
        history = tuple(("user" if index % 2 == 0 else "assistant", f"{index:02d}" + "x" * 4_998) for index in range(12))
        client = StreamClient(final_message("{}"))
        result = self.invoke(client, history=history)
        sent = client.calls[0]["messages"]
        texts = [m["content"][0]["text"] for m in sent[:-1]]
        self.assertTrue(all(len(t) <= 2_000 for t in texts))
        self.assertLessEqual(sum(map(len, texts)), 24_000)
        self.assertEqual(sent[-1]["content"][0]["text"], "¿Qué dice?")
        self.assertEqual(result.history_messages, len(texts))
        # Deterministic, newest kept, pairs intact: the same input gives the same output.
        self.assertTrue(texts[-1].startswith("11"))
        self.assertEqual([m["role"] for m in sent[:-1]][:2], ["user", "assistant"])
        again = StreamClient(final_message("{}"))
        self.invoke(again, history=history)
        self.assertEqual(again.calls[0]["messages"], sent)

    def test_more_than_twelve_messages_drop_the_oldest_pairs(self) -> None:
        history = tuple(("user" if i % 2 == 0 else "assistant", f"m{i}") for i in range(16))
        client = StreamClient(final_message("{}"))
        self.invoke(client, history=history)
        self.assertEqual([m["content"][0]["text"] for m in client.calls[0]["messages"][:-1]], [f"m{i}" for i in range(4, 16)])

    def test_when_nothing_fits_the_question_goes_without_history(self) -> None:
        from unittest.mock import patch

        client = StreamClient(final_message("{}"))
        with patch("legaldesk_agent.client.MAX_HISTORY_CHARS", 3):
            result = self.invoke(client, history=(("user", "antes"), ("assistant", "respuesta")))
        self.assertEqual((len(client.calls[0]["messages"]), result.history_messages), (1, 0))
        self.assertEqual(result.loop_outcome, "no_tool_called")

    def test_whole_tool_results_survive_and_only_the_final_message_is_the_plan(self) -> None:
        answer = {"answer": BIG_ANSWER, "evidenceStatus": "answerable", "operationStatus": "ok", "citations": []}
        events = [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Voy a buscar."}}},
            *tool_call("tu-1", SEARCH, answer),
            *final_message('{"version":1,"blocks":[]}'),
            {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 2, "totalTokens": 7}}},
        ]
        result = self.invoke(StreamClient(events))
        self.assertEqual(result.loop_outcome, "completed")
        self.assertEqual(result.text, '{"version":1,"blocks":[]}')
        (call,) = result.tool_calls
        self.assertEqual((call.tool_use_id, call.short_name, call.status), ("tu-1", "search_documents", "SUCCESS"))
        self.assertEqual(call.payload["answer"], BIG_ANSWER)  # the button-path filter would have rejected this
        self.assertEqual(result.usage.total_tokens, 7)
        self.assertEqual(result.iterations, 3)

    def test_no_tool_called(self) -> None:
        result = self.invoke(StreamClient(final_message("{}")))
        self.assertEqual((result.loop_outcome, result.tool_calls), ("no_tool_called", ()))

    def test_loop_cut_while_the_model_wanted_another_tool(self) -> None:
        events = [*tool_call("tu-1", SEARCH, {"answer": "a"}), {"messageStart": {"role": "assistant"}}, {"messageStop": {"stopReason": "tool_use"}}]
        result = self.invoke(StreamClient(events))
        self.assertEqual(result.loop_outcome, "iterations_exhausted")
        self.assertEqual(len(result.tool_calls), 1)

    def test_a_stream_cut_keeps_the_tool_results_already_completed(self) -> None:
        events = tool_call("tu-1", SEARCH, {"answer": "a"})
        for message, expected in (("Agent timeout exceeded", "timeout"), ("boom", "failed")):
            result = self.invoke(StreamClient(events, error_after=(len(events), RuntimeError(message))))
            self.assertEqual(result.loop_outcome, expected)
            self.assertEqual([call.tool_use_id for call in result.tool_calls], ["tu-1"])

    def test_a_tool_call_without_result_is_dropped_not_fatal(self) -> None:
        events = tool_call("tu-1", SEARCH, {"answer": "a"}) + [
            {"messageStart": {"role": "assistant"}},
            {"contentBlockStart": {"contentBlockIndex": 0, "start": {"toolUse": {"toolUseId": "tu-2", "name": FIELDS, "type": "mcp_tool_use"}}}},
        ]
        result = self.invoke(StreamClient(events, error_after=(len(events), RuntimeError("cut"))))
        self.assertEqual([call.tool_use_id for call in result.tool_calls], ["tu-1"])

    def test_one_unusable_tool_result_does_not_discard_the_others(self) -> None:
        events = tool_call("tu-1", SEARCH, {"answer": "a"}) + tool_call("tu-2", FIELDS, "not json") + tool_call("tu-3", FIELDS, json.dumps({"isError": True, "content": [{"type": "text", "text": "{}"}]}))
        result = self.invoke(StreamClient(events + final_message("{}")))
        self.assertEqual([(c.tool_use_id, c.status) for c in result.tool_calls],
                         [("tu-1", "SUCCESS"), ("tu-2", "ERROR"), ("tu-3", "ERROR")])

    def test_an_oversized_or_non_text_result_spoils_only_its_own_tool_call(self) -> None:
        huge = "x" * 300_000  # above the 256 KiB per-result bound, below the stream bound
        non_text = tool_call("tu-3", FIELDS, None)
        non_text.insert(-1, {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"image": {"format": "png"}}]}}})
        two_chunks = tool_call("tu-4", FIELDS, None)
        for _ in range(2):  # each chunk fits but together they exceed the bound
            two_chunks.insert(-1, {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"text": "y" * 200_000}]}}})
        events = (tool_call("tu-1", SEARCH, huge) + tool_call("tu-2", SEARCH, {"answer": "valida"})
                  + non_text + two_chunks + final_message("{}"))
        result = self.invoke(StreamClient(events))
        self.assertEqual([(c.tool_use_id, c.status) for c in result.tool_calls],
                         [("tu-1", "ERROR"), ("tu-2", "SUCCESS"), ("tu-3", "ERROR"), ("tu-4", "ERROR")])
        self.assertEqual(result.tool_calls[1].payload["answer"], "valida")
        self.assertEqual(result.tool_calls[0].payload, {})

    def test_the_non_agentic_parser_stays_strict(self) -> None:
        from legaldesk_agent.client import _structured_tool_results

        for events in (tool_call("tu-1", FIELDS, "x" * 70_000), tool_call("tu-1", FIELDS, "x" * 70_000)[:-2]):
            with self.assertRaises(ValueError):
                _structured_tool_results(events, allowed_tools=None)
        bad = tool_call("tu-1", FIELDS, None)
        bad.insert(-1, {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolResult": [{"image": {}}]}}})
        with self.assertRaises(ValueError):
            _structured_tool_results(bad)

    def test_a_tool_outside_the_allowlist_fails_closed(self) -> None:
        events = tool_call("tu-1", REVIEW, {"reviewTaskId": "r"}) + final_message("{}")
        with self.assertRaises(HarnessInvocationError):
            self.invoke(StreamClient(events))

    def test_errors_before_the_stream_propagate_for_the_worker_to_classify(self) -> None:
        class Throttled(Exception):
            response = {"Error": {"Code": "ThrottlingException"}}

        with self.assertRaises(Throttled):
            self.invoke(StreamClient(raises=Throttled()))
        with self.assertRaises(Throttled):  # throttled before any event: nothing is lost by retrying
            self.invoke(StreamClient([], error_after=(0, Throttled())))

    def test_only_an_agent_chat_scope_may_be_used(self) -> None:
        result = self.invoke(StreamClient(final_message("{}")), scope=make_scope("agent_chat"))
        self.assertEqual(result.loop_outcome, "no_tool_called")
        with self.assertRaises(TypeError):
            self.invoke(StreamClient(final_message("{}")), scope=object())

    def test_classify_loop_is_total_and_conservative(self) -> None:
        self.assertEqual(classify_loop(tool_calls=1, stop_reason="end_turn", error=None), "completed")
        self.assertEqual(classify_loop(tool_calls=0, stop_reason=None, error=None), "no_tool_called")
        self.assertEqual(classify_loop(tool_calls=1, stop_reason="max_tokens", error=None), "failed")
        self.assertEqual(classify_loop(tool_calls=1, stop_reason="tool_use", error=None), "iterations_exhausted")
        self.assertEqual(classify_loop(tool_calls=1, stop_reason="end_turn", error="request timed out"), "timeout")


if __name__ == "__main__":
    unittest.main()
