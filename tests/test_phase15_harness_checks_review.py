"""Review findings of PR #65: cut-loop classification, acknowledgement token, validation attribution, residual state."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts import phase15_harness_checks as checks

from tests.test_phase15_harness_checks import (
    ChecksTestCase, FakeError, Recorder, ack_reply, loop, tool_call, word_of,
)
from legaldesk_agent import HarnessInvocationError
from legaldesk_agent.client import ClientStreamParserError


class CutLoopClassificationTests(ChecksTestCase):
    """PB-001: ``lost`` only when the evidence says a result should be there and is not."""

    def cut(self, *calls, stop: str = "tool_use", outcome: str = "iterations_exhausted") -> str:
        return checks.classify_cut_loop(loop(*calls, stop=stop, outcome=outcome))[0]

    def test_a_template_tool_that_completed_is_retained_not_lost(self) -> None:
        for name in ("list_matter_documents", "get_document_metadata"):
            self.assertEqual(self.cut(tool_call(name)), "retained", name)

    def test_only_new_tools_count_as_exercising_allowed_tools(self) -> None:
        self.assertEqual(checks.classify_cut_loop(loop(tool_call("list_matter_documents"))), ("retained", 0))
        self.assertEqual(checks.classify_cut_loop(loop(tool_call("get_extracted_fields"))), ("retained", 1))
        self.assertEqual(checks.classify_cut_loop(loop(tool_call("list_matter_documents"), tool_call("search_documents"))), ("retained", 1))

    def test_an_error_result_for_another_reason_is_inconclusive_not_lost(self) -> None:
        self.assertEqual(self.cut(tool_call(status="ERROR", payload={"error": "denied"})), "inconclusive")
        self.assertEqual(self.cut(tool_call(status="ERROR")), "inconclusive")

    def test_an_error_next_to_a_good_result_does_not_hide_the_retention(self) -> None:
        self.assertEqual(self.cut(tool_call(status="ERROR"), tool_call("list_matter_documents")), "retained")

    def test_a_tool_requested_but_with_no_result_in_the_stream_is_lost(self) -> None:
        self.assertEqual(self.cut(), "lost")  # stop reason tool_use, no tool result at all

    def test_a_successful_result_without_content_is_lost(self) -> None:
        self.assertEqual(self.cut(tool_call(payload={})), "lost")

    def test_no_tool_or_a_loop_that_ended_for_another_reason_is_inconclusive(self) -> None:
        self.assertEqual(self.cut(stop="end_turn", outcome="no_tool_called"), "inconclusive")
        self.assertEqual(self.cut(stop="max_tokens", outcome="failed"), "inconclusive")
        self.assertEqual(checks.classify_cut_loop(SimpleNamespace(tool_calls=(), stop_reason=None, loop_outcome="timeout"))[0], "inconclusive")

    def test_verdict_of_the_check_follows_the_calls(self) -> None:
        ended = loop(outcome="no_tool_called", stop="end_turn")
        errored = loop(tool_call(status="ERROR"))
        cases = {
            "retained": [loop(tool_call("list_matter_documents")), loop(tool_call("list_matter_documents"))],
            "lost": [loop(), loop(tool_call())],
            "inconclusive": [ended, errored],
        }
        for expected, calls in cases.items():
            with self.subTest(expected):
                recorder = Recorder(agent=list(calls))
                record = checks.check_tool_results(recorder.backends(), checks._State(checks.CallBudget()), "q")
                self.assertEqual(record["verdict"], expected)

    def test_a_template_tool_only_run_is_retained_but_never_all_passed(self) -> None:
        both = loop(tool_call("list_matter_documents"))
        report = self.run_with(Recorder(agent=[loop(), both, both]))
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "retained")
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "request_accepted_tool_use_not_exercised")
        self.assertEqual(report["verdict"], "inconclusive")

    def test_the_call_record_names_tools_only_from_the_closed_list(self) -> None:
        weird = SimpleNamespace(short_name="Some Customer Document Name", status="SUCCESS", payload={"x": 1})
        report = self.run_with(Recorder(agent=[loop(), loop(weird), loop(weird)]))
        self.assertEqual(report["checks"]["tool_results"]["calls"][0]["toolNames"], ["other"])
        self.assertNotIn("Some Customer Document Name", self.output.read_text())


class AcknowledgementTests(ChecksTestCase):
    """PB-002: a unique random token, matched as a whole word."""

    def test_words_that_merely_contain_ack_do_not_acknowledge(self) -> None:
        for answer in ("ACK", "Thanks, I acknowledge that.", "back to you", "I will track it", "Sure"):
            with self.subTest(answer):
                report = self.run_with(Recorder(plain=[answer, "UNKNOWN"]))
                self.assertEqual(report["checks"]["session"]["verdict"], "inconclusive")
                self.output.unlink()

    def test_the_exact_token_acknowledges_with_or_without_punctuation(self) -> None:
        for wrap in ("{}", "{}.", "[{}]", "OK: {}!", "**{}**"):
            with self.subTest(wrap):
                report = self.run_with(Recorder(plain=[lambda m, w=wrap: w.format(ack_reply(m)), "UNKNOWN"]))
                self.assertEqual(report["checks"]["session"]["verdict"], "history_not_retained")
                self.output.unlink()

    def test_a_token_embedded_in_a_longer_run_does_not_count(self) -> None:
        self.assertTrue(checks._whole_token("LDSESSACKab12", "ok LDSESSACKab12."))
        self.assertFalse(checks._whole_token("LDSESSACKab12", "LDSESSACKab12ff"))
        self.assertFalse(checks._whole_token("LDSESSACKab12", "xLDSESSACKab12"))

    def test_the_token_is_random_per_run(self) -> None:
        first, second = Recorder(), Recorder()
        self.run_with(first)
        self.output.unlink()
        self.run_with(second)
        self.assertNotEqual(ack_reply(first.plain_calls[0][0]), ack_reply(second.plain_calls[0][0]))


class SessionConclusivenessTests(ChecksTestCase):
    """Live 2026-11-03: the session check was inconclusive twice (acknowledged=false, no error). It must now say why, without keeping any text,
    and it must not lose the guarantee that the acknowledgement is a whole word with the exact case."""

    def diagnosis(self, answer) -> dict:
        report = self.run_with(Recorder(plain=[answer, "UNKNOWN"]))
        self.output.unlink()
        return report["checks"]["session"]

    def test_each_reason_for_not_acknowledging_has_its_own_closed_label(self) -> None:
        cases = {
            "empty_answer": lambda m: "  ",
            "absent": lambda m: "I will remember that.",
            "different_case": lambda m: ack_reply(m).lower(),
            "inside_longer_token": lambda m: ack_reply(m) + "ff",
            "whole_word": lambda m: "Done: " + ack_reply(m),
        }
        for expected, answer in cases.items():
            with self.subTest(expected):
                session = self.diagnosis(answer)
                self.assertEqual(session["ackDiagnosis"], expected)
                self.assertIn(expected, checks.ACK_DIAGNOSES)
                self.assertEqual(session["acknowledged"], expected == "whole_word")
                self.assertEqual(session["verdict"], "history_not_retained" if expected == "whole_word" else "inconclusive")

    def test_the_diagnosis_adds_counts_and_labels_only_never_text(self) -> None:
        answer = "SECRET-MODEL-TEXT " + "x" * 40
        session = self.diagnosis(lambda m: answer)
        self.assertEqual((session["turn1Chars"], session["turn2Chars"]), (len(answer), len("UNKNOWN")))
        self.assertNotIn("SECRET-MODEL-TEXT", json.dumps(session))

    def test_the_markers_are_short_enough_to_copy_and_still_unguessable(self) -> None:
        recorder = Recorder()
        self.run_with(recorder)
        first = recorder.plain_calls[0][0]
        self.assertLessEqual(len(ack_reply(first)), 20)
        self.assertGreaterEqual(len(word_of(first)) - len("LDWORD"), 16)  # 64 random bits
        # the prompt says it is an automated check, forbids tools and asks for the token alone
        for needle in ("automated LegalDesk operations check", "Call no tool", "nothing else"):
            self.assertIn(needle, first)

    def test_the_word_of_turn_one_is_recognised_loosely_so_a_miscopy_of_case_cannot_hide_retained_history(self) -> None:
        for wrap in ("{}", "{} ", "the word was {}.", "**{}**", "ld{}x"):
            with self.subTest(wrap):
                recorder = Recorder()
                recorder.plain_script[:] = [ack_reply, lambda m, w=wrap: w.format(word_of(recorder.plain_calls[0][0]).lower())]
                report = self.run_with(recorder)
                self.assertEqual(report["checks"]["session"]["verdict"], "history_retained")
                self.output.unlink()

    def test_a_different_random_core_is_never_a_recall(self) -> None:
        self.assertFalse(checks._recalled("0123456789abcdef", "LDWORD0123456789abcde"))  # one character short
        self.assertTrue(checks._recalled("0123456789abcdef", "ldword0123456789ABCDEF"))
        report = self.run_with(Recorder(plain=[ack_reply, "LDWORD0000000000000000"]))
        self.assertEqual(report["checks"]["session"]["verdict"], "history_not_retained")


class ValidationAttributionTests(ChecksTestCase):
    """PB-003: a ValidationException is a tools rejection only when the message points at the tools."""

    def test_classification(self) -> None:
        cases = {
            "request_rejected": [
                FakeError("ValidationException", "Tool search_documents is not in the allowedTools list"),
                FakeError("ValidationException", "allowed tools must be a subset of the configured tools"),
                FakeError("ValidationException", "the tool x is not allowed"),
            ],
            "validation_unattributed": [
                FakeError("ValidationException", "maxTokens must be at most 1024"),
                FakeError("ValidationException", "Value at runtimeSessionId failed to satisfy constraint"),
                FakeError("ValidationException", ""),
            ],
            "client_parser_rejected": [RuntimeError("AgentCore returned an invalid event stream"), ClientStreamParserError("AgentCore returned an invalid event stream")],
            "harness_tool_rejected": [HarnessInvocationError("tool search_documents is not in the allowedTools list")],
            "other": [FakeError("ThrottlingException", "allowedTools"), RuntimeError("boom"), HarnessInvocationError("model timed out"), RuntimeError("not allowed tools")],
        }
        for expected, errors in cases.items():
            for error in errors:
                with self.subTest(expected=expected, error=str(error)):
                    self.assertEqual(checks.classify_agent_error(error), expected)

    def test_an_unattributed_validation_error_is_inconclusive_and_does_not_skip_the_cut_check(self) -> None:
        recorder = Recorder(agent=[FakeError("ValidationException", "maxTokens must be at most 1024"), loop(tool_call()), loop(tool_call())])
        report = self.run_with(recorder)
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "inconclusive")
        self.assertEqual(report["checks"]["allowed_tools"]["errorClass"], "validation_unattributed")
        self.assertNotEqual(report["checks"]["tool_results"]["verdict"], "skipped_tools_rejected")
        self.assertEqual(recorder.paid_calls, 5)

    def test_an_attributed_one_is_a_rejection_and_skips_the_cut_check(self) -> None:
        report = self.run_with(Recorder(agent=[FakeError("ValidationException", "tool not in allowedTools")]))
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "request_rejected")
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "skipped_tools_rejected")

    def test_the_provider_message_is_never_stored(self) -> None:
        message = "PROVIDER-MESSAGE-WITH-allowedTools-AND-A-CUSTOMER-NAME-Acme"
        self.run_with(Recorder(agent=[FakeError("ValidationException", message), loop(tool_call()), loop(tool_call())]))
        text = self.output.read_text()
        self.assertNotIn("Acme", text)
        self.assertNotIn("PROVIDER-MESSAGE", text)


class ResidualStateTests(ChecksTestCase):
    """The synthetic state the checks leave in the metadata table is reported, as ids only."""

    def test_every_agent_call_is_listed_with_its_ids_and_a_hash_of_the_key(self) -> None:
        report = self.run_with(Recorder())
        residual = report["residualState"]
        self.assertEqual(len(residual["conversationBindings"]), 3)  # pre-check + 2 cut-loop calls
        self.assertEqual(residual["invocationGrantsWritten"], 3)
        self.assertEqual(residual["grantTtlSeconds"], 300)
        for entry in residual["conversationBindings"]:
            self.assertEqual(set(entry), {"conversationId", "sessionSelector", "bindingPkSha256", "invocationId"})
            self.assertEqual(len(entry["bindingPkSha256"]), 64)
        self.assertNotIn("CONVERSATION#", self.output.read_text())  # the key (tenant, matter, user) is not stored

    def test_a_binding_is_listed_even_when_the_call_that_followed_it_failed(self) -> None:
        report = self.run_with(Recorder(agent=[FakeError("ThrottlingException"), FakeError("ThrottlingException")]))
        self.assertGreaterEqual(len(report["residualState"]["conversationBindings"]), 1)

    def test_the_bindings_are_listed_when_the_run_is_interrupted(self) -> None:
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(Recorder(agent=[loop(), KeyboardInterrupt()]))
        self.assertEqual(len(json.loads(self.output.read_text())["residualState"]["conversationBindings"]), 2)

    def test_a_rejected_precheck_leaves_only_its_own_binding(self) -> None:
        report = self.run_with(Recorder(agent=[FakeError("ValidationException", "tool not in allowedTools")]))
        self.assertEqual(len(report["residualState"]["conversationBindings"]), 1)

    def test_the_real_backend_records_the_binding_before_anything_that_can_fail_after_it(self) -> None:
        source = Path(checks.__file__).read_text(encoding="utf-8")
        body = source[source.index("def build_real_backends"):]
        self.assertLess(body.index("worker.conversation_store.bind("), body.index("conversation_bound("))
        self.assertLess(body.index("conversation_bound("), body.index("worker._bind("))
        self.assertLess(body.index("worker._bind("), body.index('entry["invocationId"]'))


if __name__ == "__main__":
    unittest.main()
