"""Button flows (metadata, review) through ``HarnessInvoker.invoke``: a fresh runtimeSessionId per call (T9 step 11, 2026-11-03).

Harness keeps the history of a ``runtimeSessionId`` even with memory disabled. A call made for a binding used to reuse the binding's memory
session id, so the tool results of every button click of a conversation (metadata, extracted fields, review tasks) piled up in a hidden
Harness session outside the backend. The binding's memory scope stays what feeds ``actorId``, the interceptor headers and the invocation record.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import test_phase15_agent_chat_client as chat  # noqa: E402
import test_phase15_agent_interceptor as icpt  # noqa: E402
from legaldesk import gateway_interceptor as gi  # noqa: E402
from legaldesk.authorization import AuthorizationDenied  # noqa: E402
from legaldesk_agent import HarnessInvocationScope, HarnessInvoker, HarnessMemoryScope  # noqa: E402
from legaldesk_agent.client import validate_session_id  # noqa: E402


class ButtonFixture(unittest.TestCase):
    setUp = icpt.AgentChatInterceptorTests.setUp
    bind = icpt.AgentChatInterceptorTests.bind
    event = icpt.AgentChatInterceptorTests.event

    def invoke(self, client, binding, **kwargs):
        scope = HarnessInvocationScope.from_derived(binding)
        return HarnessInvoker(client, chat.HARNESS_ARN).invoke("lista los documentos", invocation_scope=scope, **kwargs)


class EveryButtonCallGetsANewRuntimeSessionTests(ButtonFixture):
    def test_each_action_uses_a_new_runtime_session_per_call_and_never_the_binding_session(self) -> None:
        for action in ("metadata", "agent_chat"):  # (the review allowlist has tools the scope type does not know: not a scope this path can build)
            with self.subTest(action):
                binding = self.bind(action)
                client = chat.StreamClient([])
                for _ in range(3):  # three clicks / retries on one conversation
                    self.invoke(client, binding)
                sessions = [call["runtimeSessionId"] for call in client.calls]
                self.assertEqual(len(set(sessions)), 3)
                self.assertNotIn(binding.memory_scope.session_id, sessions)
                for session in sessions:
                    self.assertEqual(validate_session_id(session), session)

    def test_the_result_reports_the_runtime_session_that_was_used(self) -> None:
        client = chat.StreamClient([])
        result = self.invoke(client, self.bind("metadata"))
        self.assertEqual(result.session_id, client.calls[0]["runtimeSessionId"])

    def test_a_scoped_call_with_an_explicit_session_id_is_refused(self) -> None:
        binding = self.bind("metadata")
        scope = HarnessInvocationScope.from_derived(binding)
        with self.assertRaises(ValueError):
            HarnessInvoker(chat.StreamClient([]), chat.HARNESS_ARN).invoke("x", invocation_scope=scope, session_id=binding.memory_scope.session_id)
        with self.assertRaises(ValueError):
            HarnessInvoker(chat.StreamClient([]), chat.HARNESS_ARN).invoke("x", memory_scope=HarnessMemoryScope.from_derived(binding.memory_scope),
                                                                           session_id=binding.memory_scope.session_id)

    def test_a_caller_that_passes_only_a_session_id_keeps_it_on_purpose(self) -> None:
        """Smokes and probes (evals/real_smoke.py, the ADOT probe, the checks' plain turns) choose their own session."""

        client = chat.StreamClient([])
        mine = "22222222-3333-4444-8555-666666666666"
        HarnessInvoker(client, chat.HARNESS_ARN).invoke("x", session_id=mine)
        HarnessInvoker(client, chat.HARNESS_ARN).invoke("x", session_id=mine)
        self.assertEqual([call["runtimeSessionId"] for call in client.calls], [mine, mine])
        self.assertNotIn("actorId", client.calls[0])

    def test_without_any_argument_a_new_session_is_generated_as_before(self) -> None:
        client = chat.StreamClient([])
        HarnessInvoker(client, chat.HARNESS_ARN).invoke("x")
        HarnessInvoker(client, chat.HARNESS_ARN).invoke("x")
        self.assertNotEqual(client.calls[0]["runtimeSessionId"], client.calls[1]["runtimeSessionId"])


class WhatStillKeysOnTheBindingSessionTests(ButtonFixture):
    """The binding's memory scope is unchanged and everything that joins on it still joins."""

    def test_actor_headers_and_invocation_record_keep_the_binding_scope(self) -> None:
        binding = self.bind("metadata")
        record = self.grants.get_invocation(binding.invocation_id)
        client = chat.StreamClient([])
        self.invoke(client, binding)
        request = client.calls[0]
        self.assertEqual(request["actorId"], binding.memory_scope.actor_id)  # unchanged: inert while Harness memory is disabled
        sent = request["tools"][0]["config"]["remoteMcp"]["headers"]
        self.assertEqual(sent["x-legaldesk-memory-session-id"], binding.memory_scope.session_id)
        self.assertEqual(sent["x-legaldesk-memory-session-id"], record["memorySessionId"])
        self.assertEqual(sent["x-legaldesk-memory-actor-id"], record["memoryActorId"])
        self.assertNotEqual(request["runtimeSessionId"], record["memorySessionId"])

    def test_the_interceptor_still_accepts_the_headers_a_button_call_forwards(self) -> None:
        binding = self.bind("metadata")
        client = chat.StreamClient([])
        self.invoke(client, binding)
        headers = client.calls[0]["tools"][0]["config"]["remoteMcp"]["headers"]
        tool = "@legaldesk_gateway/metadata-mcp___list_matter_documents"
        response = gi.transform_gateway_request(
            self.event(tool, {"matterId": "mat_sundial"}, headers=headers), target=gi.GatewayTarget.METADATA_MCP,
            authorization_store=self.auth, grant_repository=self.grants,
        )
        self.assertIn("transformedGatewayRequest", response["mcp"])
        forged = {**headers, "x-legaldesk-memory-session-id": headers["x-legaldesk-memory-session-id"][:-1] + "0"}
        with self.assertRaises(AuthorizationDenied):
            gi.transform_gateway_request(
                self.event(tool, {"matterId": "mat_sundial"}, headers=forged), target=gi.GatewayTarget.METADATA_MCP,
                authorization_store=self.auth, grant_repository=self.grants,
            )

    def test_the_conversation_binding_still_derives_the_same_memory_session_for_the_same_conversation(self) -> None:
        self.assertEqual(self.bind("metadata").memory_scope.session_id, self.bind("agent_chat").memory_scope.session_id)

    def test_telemetry_without_a_correlation_id_still_joins_by_the_binding_session(self) -> None:
        events: list[dict] = []

        class Sink:
            def record(self, event) -> None:  # noqa: ANN001
                events.append(event.to_dict())

        binding = self.bind("metadata")
        scope = HarnessMemoryScope.from_derived(binding.memory_scope)
        invoker = HarnessInvoker(chat.StreamClient([]), chat.HARNESS_ARN, telemetry_sink=Sink())
        invoker.invoke("x", memory_scope=scope)
        self.assertTrue(events)
        self.assertIn(binding.memory_scope.session_id, json.dumps(events, default=str))  # the correlation id: unchanged join key
        self.assertNotIn(invoker.client.calls[0]["runtimeSessionId"], json.dumps(events, default=str))


class TheDocumentsSayWhatTheCodeDoesTests(unittest.TestCase):
    def test_adr_memory_note_and_runbook_agree_and_the_harness_template_default_keeps_memory_off(self) -> None:
        adr = " ".join((ROOT / "ARCHITECTURE_DECISIONS.md").read_text(encoding="utf-8").split())
        memory = " ".join((ROOT / "docs" / "memory.md").read_text(encoding="utf-8").split())
        for needle in ("`HarnessInvoker.invoke`", "las acciones de botón `metadata` y `review`", "decisión de ADR, no un parámetro de despliegue"):
            self.assertIn(needle, adr)
        for needle in ("no longer reuses the binding's session id", "EnablePhase09Memory=true", "ADR decision"):
            self.assertIn(needle, memory)
        template = (ROOT / "infra" / "cloudformation" / "phase-01-harness.yaml").read_text(encoding="utf-8")
        block = template[template.index("  EnablePhase09Memory:"):template.index("  Phase09MemoryArn:")]
        self.assertIn('Default: "false"', block)  # attaching it again is a decision, never a default


if __name__ == "__main__":
    unittest.main()
