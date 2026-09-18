from __future__ import annotations

import sys
import unittest
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))

from fixture_loader import load_authorization_store, test_identity
from legaldesk.authorization import build_request_context
from legaldesk.guardrails import GuardrailConfig, GuardrailProcessor
from legaldesk.mcp_server import LIST_MATTER_DOCUMENTS, MCPServer
from legaldesk.observability import (
    InMemoryTelemetrySink,
    TelemetryEvent,
    TelemetryEventType,
    TelemetryOutcome,
    emit_telemetry,
)
from legaldesk.retrieval import search_legal_documents
from legaldesk.review_tasks import create_review_task
from legaldesk_agent import HarnessInvoker


CORRELATION_ID = "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"


class RetrievalClient:
    def retrieve(self, **kwargs: Any) -> Mapping[str, object]:
        return {
            "retrievalResults": [
                {
                    "content": {"text": "Synthetic evidence."},
                    "metadata": {
                        "tenantId": "tnt_aurora",
                        "matterId": "mat_sundial",
                        "documentId": "doc-synthetic",
                    },
                }
            ]
        }


class GuardrailClient:
    def apply_guardrail(self, **kwargs: object) -> Mapping[str, object]:
        return {"action": "NONE", "outputs": [], "assessments": []}


class HarnessClient:
    def invoke_harness(self, **kwargs: object) -> Mapping[str, object]:
        return {"stream": [{"contentBlockDelta": {"delta": {"text": "ok"}}}]}


class ObservabilityTests(unittest.TestCase):
    def test_event_allowlist_and_redacted_pointer(self) -> None:
        sink = InMemoryTelemetrySink(max_events=3)
        emit_telemetry(
            sink,
            TelemetryEventType.FINAL,
            CORRELATION_ID,
            TelemetryOutcome.SUCCEEDED,
            operation="answer_question",
            count=1,
        )
        event = sink.by_correlation_id(CORRELATION_ID)[0]
        serialized = str(event.to_dict())
        self.assertEqual(event.correlation_id, CORRELATION_ID)
        self.assertNotIn("Synthetic evidence", serialized)
        self.assertNotIn("token", serialized.lower())
        with self.assertRaises(ValueError):
            TelemetryEvent(
                TelemetryEventType.FINAL,
                CORRELATION_ID,
                TelemetryOutcome.SUCCEEDED,
                1,
                operation="prompt",
            )
        with self.assertRaises(ValueError):
            emit_telemetry(
                sink,
                TelemetryEventType.FINAL,
                CORRELATION_ID,
                TelemetryOutcome.SUCCEEDED,
                operation="answer prompt with secret token",
            )
        with self.assertRaises(ValueError):
            emit_telemetry(
                sink,
                TelemetryEventType.ERROR,
                CORRELATION_ID,
                TelemetryOutcome.ERROR,
                operation="answer_question",
                error_code="document body leaked",
            )
        output = StringIO()
        with redirect_stdout(output):
            emit_telemetry(
                None,
                TelemetryEventType.AGENT,
                CORRELATION_ID,
                TelemetryOutcome.STARTED,
                operation="gateway_request",
            )
        self.assertEqual(json.loads(output.getvalue())["event_type"], "agent")
        self.assertTrue(output.getvalue().lstrip().startswith("{"))

    def test_retrieval_and_guardrail_share_correlation_without_content(self) -> None:
        sink = InMemoryTelemetrySink()
        search_legal_documents(
            test_identity("idp|alice-fictional"),
            "mat_sundial",
            "synthetic question",
            authorization_store=load_authorization_store(),
            bedrock_client=RetrievalClient(),
            knowledge_base_id="kb-fictional",
            correlation_id=CORRELATION_ID,
            telemetry_sink=sink,
        )
        GuardrailProcessor(
            GuardrailClient(),
            GuardrailConfig("guardrail-fictional", "1"),
            telemetry_sink=sink,
        ).check_input("synthetic question", correlation_id=CORRELATION_ID)
        events = sink.by_correlation_id(CORRELATION_ID)
        self.assertIn((TelemetryEventType.RETRIEVAL, TelemetryOutcome.SUCCEEDED), {(e.event_type, e.outcome) for e in events})
        self.assertIn((TelemetryEventType.GUARDRAIL, TelemetryOutcome.SUCCEEDED), {(e.event_type, e.outcome) for e in events})
        self.assertTrue(all("question" not in event.to_dict() for event in events))

    def test_harness_agent_event_preserves_correlation_and_does_not_log_message(self) -> None:
        sink = InMemoryTelemetrySink()
        result = HarnessInvoker(
            HarnessClient(),
            "arn:aws:bedrock-agentcore:eu-west-1:123:harness/test",
            telemetry_sink=sink,
        ).invoke("synthetic secret question", correlation_id=CORRELATION_ID)
        self.assertEqual(result.correlation_id, CORRELATION_ID)
        self.assertTrue(sink.events)
        self.assertTrue(all(event.correlation_id == CORRELATION_ID for event in sink.events))
        self.assertNotIn("synthetic", str(sink.events).lower())
        self.assertTrue(all(event.timestamp_ms > 0 for event in sink.events))

    def test_harness_malformed_stream_closes_started_event(self) -> None:
        class MalformedHarnessClient:
            def invoke_harness(self, **kwargs: object) -> Mapping[str, object]:
                return {"unexpected": True}

        sink = InMemoryTelemetrySink()
        with self.assertRaises(Exception):
            HarnessInvoker(
                MalformedHarnessClient(),
                "arn:aws:bedrock-agentcore:eu-west-1:123:harness/test",
                telemetry_sink=sink,
            ).invoke("synthetic question", correlation_id=CORRELATION_ID)
        self.assertEqual(
            [(event.event_type, event.outcome) for event in sink.events],
            [
                (TelemetryEventType.AGENT, TelemetryOutcome.STARTED),
                (TelemetryEventType.AGENT, TelemetryOutcome.ERROR),
                (TelemetryEventType.ERROR, TelemetryOutcome.ERROR),
            ],
        )

    def test_retrieval_exception_closes_retrieval_agent_and_final_events(self) -> None:
        class FailingRetrievalClient:
            def retrieve(self, **kwargs: object) -> Mapping[str, object]:
                raise RuntimeError("synthetic provider failure")

        from legaldesk.chat import ChatRequest, answer_question
        from legaldesk.memory import InMemoryConversationBindingStore

        auth = load_authorization_store()
        bindings = InMemoryConversationBindingStore()
        request = ChatRequest("conv-A8df2", "sess-93ba2", "mat_sundial", "synthetic question")
        context = build_request_context(
            test_identity("idp|alice-fictional"), "mat_sundial", auth, correlation_id=CORRELATION_ID
        )
        bindings.bind(context, conversation_id=request.conversation_id, session_selector=request.session_id)
        sink = InMemoryTelemetrySink()
        with self.assertRaises(RuntimeError):
            answer_question(
                test_identity("idp|alice-fictional"),
                request,
                authorization_store=auth,
                retrieval_client=FailingRetrievalClient(),
                knowledge_base_id="kb-fictional",
                generator=HarnessClient(),  # never reached
                guardrail_client=GuardrailClient(),
                guardrail_config=GuardrailConfig("guardrail-fictional", "1"),
                conversation_binding_store=bindings,
                correlation_id=CORRELATION_ID,
                telemetry_sink=sink,
            )
        observed = [(event.event_type, event.outcome) for event in sink.events]
        self.assertIn((TelemetryEventType.RETRIEVAL, TelemetryOutcome.STARTED), observed)
        self.assertIn((TelemetryEventType.RETRIEVAL, TelemetryOutcome.ERROR), observed)
        self.assertIn((TelemetryEventType.AGENT, TelemetryOutcome.ERROR), observed)
        self.assertIn((TelemetryEventType.FINAL, TelemetryOutcome.ERROR), observed)

    def test_malformed_retrieval_normalization_closes_retrieval_and_error_events(self) -> None:
        for malformed_field, malformed_value in (
            ("score", 10**1000),
            ("x-amz-bedrock-kb-document-page-number", "9" * 5000),
        ):
            with self.subTest(malformed_field=malformed_field):
                result = {
                    "content": {"text": "Synthetic evidence."},
                    "metadata": {
                        "tenantId": "tnt_aurora",
                        "matterId": "mat_sundial",
                        "documentId": "doc-synthetic",
                    },
                }
                if malformed_field == "score":
                    result[malformed_field] = malformed_value
                else:
                    result["metadata"][malformed_field] = malformed_value

                class MalformedRetrievalClient:
                    def retrieve(self, **kwargs: object) -> Mapping[str, object]:
                        return {"retrievalResults": [result]}

                sink = InMemoryTelemetrySink()
                passages = search_legal_documents(
                    test_identity("idp|alice-fictional"),
                    "mat_sundial",
                    "synthetic question",
                    authorization_store=load_authorization_store(),
                    bedrock_client=MalformedRetrievalClient(),
                    knowledge_base_id="kb-fictional",
                    correlation_id=CORRELATION_ID,
                    telemetry_sink=sink,
                )
                self.assertEqual(passages, ())
                self.assertEqual(
                    [
                        (event.event_type, event.outcome, event.operation)
                        for event in sink.events
                    ],
                    [
                        (
                            TelemetryEventType.RETRIEVAL,
                            TelemetryOutcome.STARTED,
                            "knowledge_base_retrieve",
                        ),
                        (
                            TelemetryEventType.RETRIEVAL,
                            TelemetryOutcome.ERROR,
                            "knowledge_base_retrieve",
                        ),
                        (
                            TelemetryEventType.ERROR,
                            TelemetryOutcome.ERROR,
                            "knowledge_base_retrieve",
                        ),
                    ],
                )

    def test_phase11_iac_is_small_and_does_not_enable_raw_agentcore_logs(self) -> None:
        template = (ROOT / "infra" / "cloudformation" / "phase-11-observability.yaml").read_text()
        commands = (ROOT / "infra" / "phase-11-commands.md").read_text()
        self.assertEqual(template.count("AWS::Logs::MetricFilter"), 7)
        self.assertNotIn("AWS::Logs::LogGroup", template)
        self.assertNotIn("AWS::CloudWatch::Dashboard", template)
        self.assertNotIn("APPLICATION_LOGS", template + commands)
        self.assertNotIn("request_payload", template + commands)
        self.assertNotIn("response_payload", template + commands)
        self.assertIn("--stack-name LegalDeskPhase11Observability", commands)

    def test_tool_error_events_match_metric_filters_and_close_started_spans(self) -> None:
        class FailingMetadataRepository:
            def list_for_scope(self, **_: object) -> tuple[object, ...]:
                raise RuntimeError("synthetic provider failure")

        sink = InMemoryTelemetrySink()
        request_context = build_request_context(
            test_identity("idp|alice-fictional"),
            "mat_sundial",
            load_authorization_store(),
            correlation_id=CORRELATION_ID,
        )
        response = MCPServer(
            FailingMetadataRepository(), telemetry_sink=sink
        ).handle_jsonrpc(
            {
                "jsonrpc": "2.0",
                "id": "provider-error",
                "method": "tools/call",
                "params": {"name": LIST_MATTER_DOCUMENTS, "arguments": {}},
            },
            request_context=request_context,
        )
        self.assertEqual(response["error"]["code"], -32000)
        mcp_events = sink.by_correlation_id(CORRELATION_ID)
        self.assertEqual(
            [(event.event_type, event.outcome) for event in mcp_events],
            [
                (TelemetryEventType.TOOL, TelemetryOutcome.STARTED),
                (TelemetryEventType.TOOL, TelemetryOutcome.ERROR),
                (TelemetryEventType.ERROR, TelemetryOutcome.ERROR),
            ],
        )
        template = (ROOT / "infra" / "cloudformation" / "phase-11-observability.yaml").read_text()
        self.assertIn('{ $.event_type = "tool" && $.outcome = "error" }', template)
        self.assertIn('{ $.event_type = "error" }', template)

        class FailingReviewRepository:
            def get(self, **_: object) -> None:
                return None

            def save(self, _: object) -> None:
                raise RuntimeError("synthetic persistence failure")

        review_sink = InMemoryTelemetrySink()
        with self.assertRaises(Exception):
            create_review_task(
                request_context,
                {"reasonCode": "user_requested_review"},
                repository=FailingReviewRepository(),
                telemetry_sink=review_sink,
            )
        review_events = review_sink.by_correlation_id(CORRELATION_ID)
        self.assertEqual(
            [(event.event_type, event.outcome) for event in review_events],
            [
                (TelemetryEventType.TOOL, TelemetryOutcome.STARTED),
                (TelemetryEventType.TOOL, TelemetryOutcome.ERROR),
                (TelemetryEventType.ERROR, TelemetryOutcome.ERROR),
            ],
        )

    def test_started_events_close_by_same_type_and_operation(self) -> None:
        sink = InMemoryTelemetrySink()
        emit_telemetry(
            sink,
            TelemetryEventType.RETRIEVAL,
            CORRELATION_ID,
            TelemetryOutcome.STARTED,
            operation="knowledge_base_retrieve",
        )
        emit_telemetry(
            sink,
            TelemetryEventType.RETRIEVAL,
            CORRELATION_ID,
            TelemetryOutcome.ERROR,
            operation="knowledge_base_retrieve",
            error_code="retrieval_failed",
        )
        started = {
            (event.event_type, event.operation)
            for event in sink.events
            if event.outcome is TelemetryOutcome.STARTED
        }
        terminal = {
            (event.event_type, event.operation)
            for event in sink.events
            if event.outcome in {TelemetryOutcome.SUCCEEDED, TelemetryOutcome.ERROR}
        }
        self.assertTrue(started <= terminal)


if __name__ == "__main__":
    unittest.main()
