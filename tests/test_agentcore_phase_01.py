from __future__ import annotations

import sys
import unittest
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "agent" / "src"))

from legaldesk_agent import HarnessInvocationError, HarnessInvoker, LocalAgent, new_session_id


class FakeHarnessClient:
    def __init__(self, events=None):
        self.events = events or [
            {"contentBlockDelta": {"delta": {"text": "LegalDesk "}}},
            {"contentBlockDelta": {"delta": {"text": "ready"}}},
        ]
        self.calls = []

    def invoke_harness(self, **kwargs):
        self.calls.append(kwargs)
        return {"stream": self.events}


class EventStreamError(Exception):
    pass


class FailingEventStream:
    def __iter__(self):
        raise EventStreamError("stream rejected")


class AgentCorePhase01Tests(unittest.TestCase):
    def test_local_health_and_invoke(self) -> None:
        agent = LocalAgent()
        self.assertEqual(agent.health()["status"], "ok")
        result = agent.invoke("hello")
        self.assertIn("healthy", result.text)
        UUID(result.session_id)

    def test_two_new_sessions_use_distinct_uuid_ids(self) -> None:
        first = new_session_id()
        second = new_session_id()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 33)
        UUID(first)
        UUID(second)

    def test_invoker_forwards_only_fixed_shape_request(self) -> None:
        client = FakeHarnessClient()
        invoker = HarnessInvoker(client, "arn:aws:bedrock-agentcore:eu-west-1:123:harness/test")
        result = invoker.invoke("  hello  ")

        self.assertEqual(result.text, "LegalDesk ready")
        self.assertEqual(
            set(client.calls[0]),
            {"harnessArn", "runtimeSessionId", "messages"},
        )
        self.assertEqual(
            client.calls[0]["messages"],
            [{"role": "user", "content": [{"text": "hello"}]}],
        )

    def test_runtime_client_error_is_not_silently_ignored(self) -> None:
        client = FakeHarnessClient(
            events=[{"runtimeClientError": {"message": "rejected"}}]
        )
        with self.assertRaisesRegex(HarnessInvocationError, "rejected"):
            HarnessInvoker(client, "arn:test").invoke("hello")

    def test_botocore_event_stream_error_is_wrapped(self) -> None:
        client = FakeHarnessClient()
        client.events = FailingEventStream()
        with self.assertRaisesRegex(HarnessInvocationError, "stream rejected"):
            HarnessInvoker(client, "arn:test").invoke("hello")

    def test_template_excludes_future_phase_services_and_wildcard_actions(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-01-harness.yaml").read_text(
            encoding="utf-8"
        )
        for forbidden in ("s3:", "KnowledgeBase", "InvokeGateway", "CreateEvent"):
            self.assertNotIn(forbidden, template)
        self.assertNotIn('Action: "*"', template)
        self.assertNotIn("Action: '*'", template)
        self.assertIn("phase01_no_tools", template)
        self.assertIn("Temperature:", template)
        self.assertNotIn("TopP:", template)


if __name__ == "__main__":
    unittest.main()
