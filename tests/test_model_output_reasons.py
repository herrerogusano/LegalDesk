"""A rejected model output says WHY on its stage event (closed reason, never content).

T9 step 12 (2026-11-03): the writer stage failed 3, then 5 times and every log line read ``model_failed``, so a rejected output
could not be told apart from a provider error. The stage event now carries a closed sub-code; the aggregate ``generate_answer``
event and the response keep ``model_failed``.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from test_chat import (  # noqa: E402
    ALICE,
    GUARDRAIL_CONFIG,
    FakeEvidenceResolver,
    FakeGenerator,
    FakeGroundingValidator,
    FakeGuardrailClient,
    FakeKnowledgeBaseClient,
    load_authorization_store,
    request,
    result,
)
from legaldesk import evidence  # noqa: E402
from legaldesk.authorization import build_request_context  # noqa: E402
from legaldesk.chat import TECHNICAL_ERROR_ANSWER, MAX_ANSWER_LENGTH, answer_question  # noqa: E402
from legaldesk.memory import InMemoryConversationBindingStore  # noqa: E402
from legaldesk.observability import TelemetryErrorCode, TelemetryEventType, TelemetryOutcome  # noqa: E402

CORRELATION = "8ec5d1c5-7b58-4bc2-a183-8fd48a3bd279"
COMPLETE = {"coverage": "complete", "conflict": False, "supportingCitationIds": ["citation-1"]}
GROUNDED = {"grounded": True, "score": 0.95, "matchedCitationIds": ["citation-1"]}
OPTIONAL_PASSAGE = "This Agreement is governed by the laws of California."
REQUIRED_PASSAGE = "The supplier must send written notice to the buyer."


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def record(self, event: Any) -> None:
        self.events.append(event)

    def codes(self) -> dict[str, str | None]:
        """operation -> closed error code of every errored model event."""

        return {
            event.operation.value: event.error_code.value if event.error_code else None
            for event in self.events
            if event.event_type is TelemetryEventType.MODEL and event.outcome is TelemetryOutcome.ERROR and event.operation
        }

    def dropped(self) -> str | None:
        """The closed reason on the write_answer stage event of an accepted answer whose relationship was dropped."""

        accepted = [
            event for event in self.events
            if event.event_type is TelemetryEventType.MODEL and event.outcome is TelemetryOutcome.SUCCEEDED
            and event.operation and event.operation.value == "write_answer"
        ]
        assert len(accepted) == 1, accepted
        return accepted[0].error_code.value if accepted[0].error_code else None


class Writer:
    def __init__(self, written: Any) -> None:
        self.written = written

    def write(self, request: object) -> Any:
        if isinstance(self.written, BaseException):
            raise self.written
        return self.written


def run(passage: str, *, resolver: Mapping[str, object] = COMPLETE, written: Any = None, writer: Any = None):
    auth = load_authorization_store()
    bindings = InMemoryConversationBindingStore()
    chat_request = request()
    bindings.bind(
        build_request_context(ALICE, chat_request.matter_id, auth),
        conversation_id=chat_request.conversation_id,
        session_selector=chat_request.session_id,
    )
    sink = Sink()
    response = answer_question(
        ALICE, chat_request,
        authorization_store=auth,
        retrieval_client=FakeKnowledgeBaseClient([result("tnt_aurora", "mat_sundial", "doc-one", passage)]),
        knowledge_base_id="kb-fictional", generator=FakeGenerator(),
        guardrail_client=FakeGuardrailClient(), guardrail_config=GUARDRAIL_CONFIG,
        correlation_id=CORRELATION, conversation_binding_store=bindings,
        evidence_resolver=FakeEvidenceResolver(resolver), answer_writer=writer or Writer(written),  # type: ignore[arg-type]
        grounding_validator=FakeGroundingValidator(GROUNDED), telemetry_sink=sink,
    )
    return response, sink


class StageEventNamesTheReason(unittest.TestCase):
    def assert_reason(self, response: Any, sink: Sink, stage: str, reason: str) -> None:
        self.assertEqual(response.operation_status, "error")
        self.assertEqual(response.answer, TECHNICAL_ERROR_ANSWER)
        self.assertEqual(response.error_code, "model_failed")  # the public/aggregate code does not change
        codes = sink.codes()
        self.assertEqual(codes[stage], reason)
        self.assertEqual(codes["generate_answer"], "model_failed")

    def test_optional_relationship_with_an_invalid_shape_keeps_the_prose(self) -> None:
        # F-01, text-02: "is governed by" ends in a preposition and the evidence requires no projection. The prose survives, unrendered.
        response, sink = run(OPTIONAL_PASSAGE, written={
            "answer": "California law governs.",
            "relationships": [{"actor": "This Agreement", "action": "is governed by", "recipient": "California law"}],
        })
        self.assertEqual(response.answer, "California law governs.")
        self.assertIsNone(response.error_code)
        self.assertEqual(sink.dropped(), "writer_relationship_action_preposition")
        self.assertEqual(sink.codes(), {}, "an accepted answer has no errored model event")

    def test_required_relationship_with_an_invalid_shape_still_fails_closed(self) -> None:
        response, sink = run(REQUIRED_PASSAGE, written={
            "answer": "The supplier must send written notice to the buyer.",
            "relationships": [{"actor": "The supplier", "action": "send written notice to", "recipient": "the buyer"}],
        })
        self.assert_reason(response, sink, "write_answer", "writer_relationship_action_preposition")

    def test_an_invalid_answer_text_is_never_rescued_by_dropping_the_relationship(self) -> None:
        response, sink = run(OPTIONAL_PASSAGE, written={
            "answer": "  ", "relationships": [{"actor": "This Agreement", "action": "is governed by", "recipient": "California law"}],
        })
        self.assertEqual(response.error_code, "model_failed")
        self.assertEqual(sink.codes()["generate_answer"], "model_failed")

    def test_a_dropped_unsupported_relationship_also_leaves_its_reason(self) -> None:
        response, sink = run(OPTIONAL_PASSAGE, written={
            "answer": "California law governs.",
            "relationships": [{"actor": "Somebody else", "action": "governs", "recipient": "California law"}],
        })
        self.assertEqual(response.answer, "California law governs.")
        self.assertEqual(sink.dropped(), "writer_relationship_unsupported")

    def test_each_shape_rule_has_its_own_closed_code(self) -> None:
        # F-01: one code hid five rules, so a real rejection could not say which one fired. No text from the model reaches the code.
        valid = {"actor": "The supplier", "action": "send written notice", "recipient": "the buyer"}
        cases = {
            "writer_relationship_list_invalid": [dict(valid, recipient=f"party {index}") for index in range(9)],
            "writer_relationship_shape_invalid": [{"actor": "The supplier", "action": "send written notice"}],
            "writer_relationship_value_invalid": [dict(valid, recipient="  ")],
            "writer_relationship_action_length": [dict(valid, action="a b c d e f g h i j k l m")],
            "writer_relationship_action_preposition": [dict(valid, action="send written notice to")],
            "writer_relationship_duplicate": [valid, dict(valid)],
        }
        for reason, relationships in cases.items():
            with self.subTest(reason=reason):
                response, sink = run(REQUIRED_PASSAGE, written={"answer": "The supplier must send written notice to the buyer.", "relationships": relationships})
                self.assert_reason(response, sink, "write_answer", reason)

    def test_required_relationship_omitted(self) -> None:
        response, sink = run(REQUIRED_PASSAGE, written={"answer": "The supplier must send notice."})
        self.assert_reason(response, sink, "write_answer", "writer_relationship_missing")

    def test_required_relationship_not_in_the_evidence(self) -> None:
        response, sink = run(REQUIRED_PASSAGE, written={
            "answer": "The supplier must send notice.",
            "relationships": [{"actor": "vendor", "action": "send written notice", "recipient": "client"}],
        })
        self.assert_reason(response, sink, "write_answer", "writer_relationship_unsupported")

    def test_answer_text_too_long(self) -> None:
        response, sink = run(OPTIONAL_PASSAGE, written={"answer": "x" * (MAX_ANSWER_LENGTH + 1)})
        self.assert_reason(response, sink, "write_answer", "writer_answer_invalid")

    def test_unsupported_writer_shape(self) -> None:
        response, sink = run(OPTIONAL_PASSAGE, written={"answer": "ok", "citations": ["citation-1"]})
        self.assert_reason(response, sink, "write_answer", "model_output_invalid")

    def test_resolver_breaking_its_contract(self) -> None:
        response, sink = run(OPTIONAL_PASSAGE, resolver={"coverage": "complete", "conflict": False, "supportingCitationIds": []})
        self.assert_reason(response, sink, "resolve_evidence", "resolution_invalid")

    def test_a_provider_exception_keeps_the_generic_code(self) -> None:
        """Not a contract violation: no sub-code is invented for it (and no reason attribute exists to read)."""

        response, sink = run(OPTIONAL_PASSAGE, written=RuntimeError("provider said something"))
        self.assertEqual(response.error_code, "model_failed")
        self.assertEqual(sink.codes()["write_answer"], "model_failed")

    def test_an_accepted_output_emits_no_error(self) -> None:
        response, sink = run(OPTIONAL_PASSAGE, written={"answer": "California law governs."})
        self.assertEqual(response.operation_status, "ok")
        self.assertEqual(sink.codes(), {})


class ConverseReplies(unittest.TestCase):
    """The adapters tell a reply cut off at the token limit from a malformed one."""

    class Client:
        def __init__(self, reply: Mapping[str, object]) -> None:
            self.reply = reply

        def converse(self, **_: object) -> Mapping[str, object]:
            return self.reply

    @staticmethod
    def reply(text: str | None, stop: str) -> dict[str, object]:
        content = [] if text is None else [{"text": text}]
        return {"output": {"message": {"content": content}}, "stopReason": stop}

    def writer_error(self, reply: Mapping[str, object]) -> evidence.EvidenceContractError:
        writer = evidence.ConverseAnswerWriter(self.Client(reply), model_id="model-fictional")  # type: ignore[arg-type]
        request_ = evidence.AnswerWriterRequest(
            question="q", evidence=[{"citation_id": "citation-1", "text": "t"}],
            resolution=evidence.EvidenceResolution(evidence.EvidenceCoverage.COMPLETE, False, ("citation-1",)),
        )
        with self.assertRaises(evidence.EvidenceContractError) as caught:
            writer.write(request_)
        return caught.exception

    def test_cut_off_reply_is_truncated(self) -> None:
        self.assertEqual(self.writer_error(self.reply('{"answer": "cut', "max_tokens")).reason, "model_output_truncated")

    def test_malformed_reply_is_invalid(self) -> None:
        self.assertEqual(self.writer_error(self.reply('{"answer": "cut', "end_turn")).reason, "model_output_invalid")

    def test_reply_without_a_text_block_is_invalid(self) -> None:
        self.assertEqual(self.writer_error(self.reply(None, "end_turn")).reason, "model_output_invalid")

    def test_resolver_reply_cut_off_is_truncated(self) -> None:
        resolver = evidence.ConverseEvidenceResolver(self.Client(self.reply('{"coverage": "co', "max_tokens")), model_id="model-fictional")  # type: ignore[arg-type]
        with self.assertRaises(evidence.EvidenceContractError) as caught:
            resolver.resolve(evidence.EvidenceResolutionRequest(question="q", evidence=[{"citation_id": "citation-1", "text": "t"}]))
        self.assertEqual(caught.exception.reason, "model_output_truncated")


class ThroughTheRealWriterAdapter(unittest.TestCase):
    """F-01: the fix first reached only fake writers, because the deployed adapter raised before the caller could decide."""

    PREPOSITION = [{"actor": "This Agreement", "action": "is governed by", "recipient": "California law"}]

    def adapter(self, written: Mapping[str, object]) -> evidence.ConverseAnswerWriter:
        reply = ConverseReplies.reply(json.dumps(written), "end_turn")
        return evidence.ConverseAnswerWriter(ConverseReplies.Client(reply), model_id="model-fictional")  # type: ignore[arg-type]

    def test_an_advisory_malformed_relationship_keeps_the_prose(self) -> None:
        written = {"answer": "California law governs.", "relationships": self.PREPOSITION}
        response, sink = run(OPTIONAL_PASSAGE, writer=self.adapter(written))
        self.assertEqual(response.operation_status, "ok")
        self.assertEqual(response.answer, "California law governs.")
        self.assertEqual(sink.dropped(), "writer_relationship_action_preposition")

    def test_a_required_malformed_relationship_still_fails_closed(self) -> None:
        written = {"answer": "The supplier must send written notice to the buyer.",
                   "relationships": [{"actor": "The supplier", "action": "send written notice to", "recipient": "the buyer"}]}
        response, sink = run(REQUIRED_PASSAGE, writer=self.adapter(written))
        self.assertEqual(response.error_code, "model_failed")
        self.assertEqual(sink.codes()["write_answer"], "writer_relationship_action_preposition")

    def test_other_contract_errors_are_still_rejected(self) -> None:
        for written in (
            {"answer": "  ", "relationships": self.PREPOSITION},
            {"answer": "ok", "extra": 1},
            # PullBeacon PB-001: a malformed relationship never hides another violation. Keys and answer are checked before it.
            {"answer": "ok", "extra": 1, "relationships": self.PREPOSITION},
            {"answer": 5, "relationships": self.PREPOSITION},
        ):
            with self.subTest(written=written):
                response, sink = run(OPTIONAL_PASSAGE, writer=self.adapter(written))
                self.assertEqual(response.error_code, "model_failed")
                self.assertEqual(sink.codes()["write_answer"], "model_output_invalid")


class ReasonsAreClosed(unittest.TestCase):
    def test_every_reason_is_an_allowlisted_telemetry_code(self) -> None:
        reasons = {getattr(evidence, name) for name in dir(evidence) if name.startswith("REASON_")}
        self.assertEqual(len(reasons), 13)
        self.assertLessEqual(reasons, {code.value for code in TelemetryErrorCode})

    def test_a_reason_never_carries_the_message(self) -> None:
        error = evidence.EvidenceContractError("text from the model or a document")
        self.assertEqual(error.reason, "model_output_invalid")


if __name__ == "__main__":
    unittest.main()
