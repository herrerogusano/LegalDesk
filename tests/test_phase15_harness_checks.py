"""The T9 Harness checks: verdicts, fixed budget, no retries and an always-written report (no AWS)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import phase15_harness_checks as checks

ROOT = Path(__file__).parents[1]
ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/LegalDeskPhase01-abc"
ANSWER_SECRET = "FAKE-ANSWER-TEXT-THAT-MUST-NEVER-BE-STORED"
PAYLOAD_SECRET = "FAKE-TOOL-PAYLOAD-THAT-MUST-NEVER-BE-STORED"


def tool_call(name: str = "search_documents", status: str = "SUCCESS", payload: object = None):
    return SimpleNamespace(short_name=name, status=status, payload={"answer": PAYLOAD_SECRET} if payload is None else payload)


def loop(*calls, outcome: str = "iterations_exhausted", stop: str = "tool_use"):
    return SimpleNamespace(tool_calls=tuple(calls), loop_outcome=outcome, stop_reason=stop, iterations=1, text=ANSWER_SECRET)


class FakeError(Exception):
    def __init__(self, code: str, message: str = "boom") -> None:
        super().__init__(message)
        self.response = {"Error": {"Code": code}}


class Recorder:
    """Scripted backends. Every call is recorded; a script entry that is an Exception is raised."""

    def __init__(self, *, agent=None, plain=None) -> None:
        self.agent_script = list(agent if agent is not None else [loop(), loop(tool_call()), loop(tool_call())])
        self.plain_script = list(plain if plain is not None else ["ACK", "UNKNOWN"])
        self.agent_calls: list[str] = []
        self.plain_calls: list[tuple[str, str]] = []
        self.resolved: list[object] = []

    def _next(self, script: list):
        value = script.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def backends(self) -> checks.Backends:
        def agent(question: str):
            self.agent_calls.append(question)
            return self._next(self.agent_script)

        def plain(message: str, session_id: str) -> str:
            self.plain_calls.append((message, session_id))
            return self._next(self.plain_script)

        def resolve(item):
            self.resolved.append(item)
            return "tool_result"

        return checks.Backends(plain=plain, agent=agent, resolve=resolve, new_session_id=lambda: "session-" + "a" * 40)

    @property
    def paid_calls(self) -> int:
        return len(self.agent_calls) + len(self.plain_calls)


class ChecksTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        patcher = mock.patch.object(checks, "RESULTS_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.output = self.root / "phase15-harness-checks-test-a.json"

    def run_with(self, recorder: Recorder, **kwargs):
        return checks.execute_checks(
            recorder.backends(), output=self.output, approval_ref="ticket-1", release_commit="a" * 40,
            artifact_sha256="b" * 64, harness_arn=ARN, matter_id="mat_synthetic", **kwargs,
        )


class VerdictTests(ChecksTestCase):
    def test_everything_good_is_all_passed_in_exactly_five_calls(self) -> None:
        recorder = Recorder()
        report = self.run_with(recorder)
        self.assertEqual(report["verdict"], "all_passed")
        self.assertEqual({name: value["verdict"] for name, value in report["checks"].items()},
                         {"allowed_tools": "accepted", "session": "history_not_retained", "tool_results": "retained"})
        self.assertEqual((recorder.paid_calls, report["budget"]["harnessInvocationsUsed"], report["budget"]["retries"]), (5, 5, 0))
        self.assertEqual(json.loads(self.output.read_text())["verdict"], "all_passed")
        self.assertEqual(report["checks"]["tool_results"]["calls"][0]["outputPath"], "tool_result")

    def test_session_that_keeps_history_is_a_finding(self) -> None:
        recorder = Recorder(plain=["ACK", "I remember"])
        recorder.plain_script[1] = None  # placeholder replaced below once the word is known

        def plain(message: str, session_id: str) -> str:
            recorder.plain_calls.append((message, session_id))
            if len(recorder.plain_calls) == 1:
                recorder.word = message.split("later: ")[1].split(".")[0]
                return "ACK"
            return "The word was " + recorder.word

        backends = recorder.backends()
        report = checks.execute_checks(
            checks.Backends(plain=plain, agent=backends.agent, resolve=backends.resolve, new_session_id=backends.new_session_id),
            output=self.output, approval_ref="ticket-1", release_commit="a" * 40, artifact_sha256="b" * 64, harness_arn=ARN, matter_id="m",
        )
        self.assertEqual(report["checks"]["session"]["verdict"], "history_retained")
        self.assertEqual(report["verdict"], "findings")

    def test_unacknowledged_first_turn_is_inconclusive_not_a_pass(self) -> None:
        report = self.run_with(Recorder(plain=["hmm", "UNKNOWN"]))
        self.assertEqual(report["checks"]["session"]["verdict"], "inconclusive")
        self.assertEqual(report["verdict"], "inconclusive")

    def test_session_markers_are_distinct_and_only_turn_one_carries_the_word(self) -> None:
        recorder = Recorder()
        self.run_with(recorder)
        (first, session_1), (second, session_2) = recorder.plain_calls
        self.assertEqual(session_1, session_2)
        word = first.split("later: ")[1].split(".")[0]
        token_1, token_2 = first.split("check ")[1].split(".")[0], second.split("check ")[1].split(".")[0]
        self.assertEqual(len({word, token_1, token_2}), 3)
        self.assertNotIn(word, second)
        self.assertNotIn(token_1, second)

    def test_lost_tool_result_is_a_finding(self) -> None:
        report = self.run_with(Recorder(agent=[loop(), loop(tool_call(payload={})), loop(tool_call(status="ERROR"))]))
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "lost")
        self.assertEqual(report["verdict"], "findings")

    def test_a_model_that_calls_no_tool_is_inconclusive(self) -> None:
        report = self.run_with(Recorder(agent=[loop(), loop(), loop()]))
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "inconclusive")
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "request_accepted_tool_use_not_exercised")
        self.assertEqual(report["verdict"], "inconclusive")

    def test_only_a_tool_outside_the_template_counts_as_exercising_allowed_tools(self) -> None:
        report = self.run_with(Recorder(agent=[loop(), loop(tool_call("list_matter_documents")), loop(tool_call("list_matter_documents"))]))
        self.assertNotEqual(report["checks"]["allowed_tools"]["verdict"], "accepted")

    def test_one_conclusive_and_one_missing_call_is_inconclusive(self) -> None:
        report = self.run_with(Recorder(agent=[loop(), loop(tool_call()), loop()]))
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "inconclusive")


class AllowedToolsTests(ChecksTestCase):
    def test_request_rejected_is_reported_and_the_tool_check_is_skipped_but_not_the_session(self) -> None:
        recorder = Recorder(agent=[FakeError("ValidationException", "tool not in allowedTools")], plain=["ACK", "UNKNOWN"])
        report = self.run_with(recorder)
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "request_rejected")
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "skipped_tools_rejected")
        self.assertEqual(report["checks"]["session"]["verdict"], "history_not_retained")
        self.assertEqual((recorder.paid_calls, report["verdict"]), (3, "findings"))

    def test_a_refused_tool_use_in_the_stream_is_reported(self) -> None:
        stream_error = RuntimeError("AgentCore returned an invalid event stream")
        report = self.run_with(Recorder(agent=[loop(), stream_error, stream_error]))
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "tool_use_rejected")
        self.assertEqual(report["verdict"], "findings")

    def test_a_stream_refusal_already_in_the_precheck_skips_the_tool_check(self) -> None:
        report = self.run_with(Recorder(agent=[RuntimeError("AgentCore returned an invalid event stream")]))
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "tool_use_rejected")
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "skipped_tools_rejected")

    def test_other_errors_are_inconclusive_with_a_closed_code(self) -> None:
        report = self.run_with(Recorder(agent=[FakeError("ThrottlingException"), loop(tool_call()), loop(tool_call())]))
        self.assertEqual(report["checks"]["allowed_tools"]["verdict"], "inconclusive")
        self.assertEqual(report["checks"]["allowed_tools"]["errorCode"], "ThrottlingException")


class BudgetAndFailureTests(ChecksTestCase):
    def test_no_retry_after_a_failed_call(self) -> None:
        recorder = Recorder(agent=[FakeError("ThrottlingException"), loop(tool_call()), loop(tool_call())])
        self.run_with(recorder)
        self.assertEqual(len(recorder.agent_calls), 3)  # the failed one was not repeated

    def test_two_failures_in_a_row_stop_the_run(self) -> None:
        recorder = Recorder(agent=[FakeError("ThrottlingException")], plain=[FakeError("ThrottlingException")])
        report = self.run_with(recorder)
        self.assertEqual(report["verdict"], "aborted_after_errors")
        self.assertEqual(recorder.paid_calls, 2)
        self.assertEqual(report["checks"]["tool_results"]["verdict"], "not_run")

    def test_the_budget_is_spent_before_the_call_and_never_exceeded(self) -> None:
        budget = checks.CallBudget(limit=2)
        recorder = Recorder()
        with self.assertRaises(checks.BudgetExceeded):
            checks.execute_checks(recorder.backends(), output=self.output, approval_ref="ticket-1", release_commit="a" * 40,
                                  artifact_sha256="b" * 64, harness_arn=ARN, matter_id="m", budget=budget)
        self.assertEqual((recorder.paid_calls, budget.used), (2, 2))
        report = json.loads(self.output.read_text())  # the spent calls left evidence
        self.assertEqual(report["budget"]["harnessInvocationsUsed"], 2)
        self.assertEqual(report["internalErrorCode"], "BudgetExceeded")
        self.assertNotEqual(report["verdict"], "all_passed")

    def test_the_default_budget_equals_the_plan(self) -> None:
        self.assertEqual(checks.MAX_INVOCATIONS, 5)
        self.assertEqual(checks.build_plan()["harnessInvocations"], 5)

    def test_report_is_written_when_something_unexpected_raises_after_a_paid_call(self) -> None:
        recorder = Recorder(agent=[loop(), KeyboardInterrupt()])
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(recorder)
        report = json.loads(self.output.read_text())
        self.assertGreaterEqual(report["budget"]["harnessInvocationsUsed"], 1)
        self.assertNotEqual(report["verdict"], "all_passed")

    def test_a_report_that_cannot_be_written_is_printed_and_raised(self) -> None:
        recorder = Recorder()
        with mock.patch.object(checks, "write_create_only", side_effect=OSError("disk full")), mock.patch("sys.stderr") as stderr:
            with self.assertRaises(OSError):
                self.run_with(recorder)
        self.assertTrue(stderr.write.called)

    def test_nothing_is_spent_when_the_report_path_is_refused(self) -> None:
        recorder = Recorder()
        for bad in (Path(tempfile.gettempdir()) / "phase15-harness-checks-x.json", self.root / "other-name.json"):
            with self.assertRaises(ValueError):
                checks.execute_checks(recorder.backends(), output=bad, approval_ref="t", release_commit="a" * 40, artifact_sha256="b" * 64, harness_arn=ARN, matter_id="m")
        self.output.write_text("{}")
        with self.assertRaises(FileExistsError):
            self.run_with(recorder)
        self.assertEqual(recorder.paid_calls, 0)


class ReportContentTests(ChecksTestCase):
    def test_the_report_stores_no_marker_question_answer_or_tool_payload(self) -> None:
        recorder = Recorder(plain=["ACK " + ANSWER_SECRET, "UNKNOWN " + ANSWER_SECRET])
        self.run_with(recorder, cut_question="SYNTHETIC-CUT-QUESTION-TEXT")
        text = self.output.read_text()
        forbidden = [ANSWER_SECRET, PAYLOAD_SECRET, "SYNTHETIC-CUT-QUESTION-TEXT", checks.ALLOWED_TOOLS_QUESTION, "mat_synthetic", ARN]
        forbidden += [message.split("later: ")[1].split(".")[0] for message, _ in recorder.plain_calls[:1]]
        for secret in forbidden:
            self.assertNotIn(secret, text)
        data = json.loads(text)
        self.assertEqual(set(data["checks"]["session"]["markerSha256"]), {"word", "turn1", "turn2"})
        self.assertEqual(len(data["harnessArnSha256"]), 64)

    def test_unexpected_labels_from_harness_are_not_copied_verbatim(self) -> None:
        odd = SimpleNamespace(tool_calls=(), loop_outcome="Some Free Text, with user content!", stop_reason="x" * 200, iterations=1, text="")
        report = self.run_with(Recorder(agent=[loop(), odd, odd]))
        call = report["checks"]["tool_results"]["calls"][0]
        self.assertEqual((call["loopOutcome"], call["stopReason"]), ("other", "other"))

    def test_every_verdict_has_an_interpretation_in_the_closed_vocabulary(self) -> None:
        for name, verdicts in (("allowed_tools", checks.ALLOWED_TOOLS), ("session", checks.SESSION), ("tool_results", checks.TOOL_RESULTS)):
            self.assertEqual(set(checks.INTERPRETATION[name]), set(verdicts), name)
        report = self.run_with(Recorder())
        self.assertTrue(all(report["interpretation"].values()))
        self.assertIn(report["verdict"], checks.OVERALL)


class DryRunAndCommandLineTests(unittest.TestCase):
    def test_dry_run_prints_the_plan_and_imports_no_cloud_or_application_package(self) -> None:
        code = (
            "import sys, importlib.util;"
            "spec = importlib.util.spec_from_file_location('c', 'scripts/phase15_harness_checks.py');"
            "c = importlib.util.module_from_spec(spec); sys.modules['c'] = c; spec.loader.exec_module(c);"
            f"rc = c.main(['--harness-arn', '{ARN}', '--matter-id', 'm', '--output', 'phase15-harness-checks-x.json']);"
            "bad = [m for m in ('boto3', 'botocore', 'legaldesk', 'legaldesk_agent') if m in sys.modules];"
            "print('IMPORTED', bad); raise SystemExit(rc)"
        )
        done = subprocess.run([sys.executable, "-I", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("dry_run", done.stdout)
        self.assertIn("IMPORTED []", done.stdout)

    def test_execute_needs_every_acknowledgement_before_anything_is_built(self) -> None:
        base = ["--harness-arn", ARN, "--matter-id", "m", "--output", "phase15-harness-checks-x.json", "--execute"]
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(checks, "build_real_backends", side_effect=AssertionError("built")):
            os.environ.pop("LEGALDESK_EVAL_ACCESS_TOKEN", None)
            with self.assertRaises(SystemExit):
                checks.main(base)  # no approval, no release identity, no token
            os.environ["LEGALDESK_EVAL_ACCESS_TOKEN"] = "t"
            with self.assertRaises(SystemExit):
                checks.main([*base, "--approval-ref", "ticket-1", "--release-commit", "a" * 40])  # no artifact hash

    def test_a_non_harness_arn_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            checks.main(["--harness-arn", "arn:aws:s3:::bucket", "--matter-id", "m", "--output", "x"])

    def test_the_forced_iteration_limit_is_restored_even_if_the_call_fails(self) -> None:
        module = SimpleNamespace(AGENT_CHAT_MAX_ITERATIONS=3)
        with self.assertRaises(RuntimeError), checks._max_iterations_one(module):
            self.assertEqual(module.AGENT_CHAT_MAX_ITERATIONS, 1)
            raise RuntimeError("stream cut")
        self.assertEqual(module.AGENT_CHAT_MAX_ITERATIONS, 3)

    def test_the_production_iteration_limit_is_still_three(self) -> None:
        sys.path[:0] = [str(ROOT / "agent" / "src")]
        try:
            import legaldesk_agent.client as client
        finally:
            sys.path.pop(0)
        self.assertEqual(client.AGENT_CHAT_MAX_ITERATIONS, 3)


if __name__ == "__main__":
    unittest.main()
