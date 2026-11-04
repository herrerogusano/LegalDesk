"""Diagnostic subsets cannot become gate evidence or exceed their paid-call budget."""
import json
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from evals import phase15_routing_eval as ev
from legaldesk_agent.client import classify_loop


class TargetedTests(unittest.TestCase):
    def setUp(self):
        self.dataset, self.digest = ev.load_dataset()

    def run_subset(self, observation=None, **kwargs):
        client = Mock()
        client.run.return_value = observation or ev.Observation()
        report = ev.run_real(self.dataset, self.digest, client, approval_ref="owner/F-01", release_commit="a" * 40,
                             artifact_sha256="b" * 64, prompt_identity=None,
                             case_ids=["text-02", "amb-01", "inj-03"], **kwargs)
        return report, client

    def test_budget_repeats_and_verdict_are_diagnostic_even_for_bad_results(self):
        report, client = self.run_subset(repeat=3)
        self.assertEqual(client.run.call_count, 9)
        self.assertEqual(report["budget"]["harnessInvocationsPlanned"], 9)
        self.assertEqual(report["caseIds"], ["text-02", "amb-01", "inj-03"])
        self.assertTrue(report["subset"])
        self.assertEqual(report["verdict"], {"passed": None, "failedGates": None, "gateRun": False, "reason": "not_a_gate_run"})
        self.assertEqual([r["repetition"] for r in report["cases"]], [1, 2, 3] * 3)

    def test_limits_and_unknown_or_duplicate_ids_fail_before_a_call(self):
        for ids, repeat in [([], 1), (["nope"], 1), (["text-02"] * 2, 1), (["text-02"], 13),
                            (["text-02"], 0), (["text-02"], True), (None, 2)]:
            with self.subTest(ids=ids, repeat=repeat), self.assertRaises(ValueError):
                ev.select_real_cases(self.dataset, ids, repeat)
        self.assertEqual(len(ev.select_real_cases(self.dataset, ["text-02"], 12)), 12)

    def test_subset_names_cannot_look_like_gate_names(self):
        root = ev.RESULTS_ROOT
        with self.assertRaises(ValueError):
            ev.assert_report_path(root / "phase15-routing-20261104-d.json", subset=True)
        with self.assertRaises(ValueError):
            ev.assert_report_path(root / "phase15-routing-subset-test.json")
        ev.assert_report_path(root / "phase15-routing-subset-test.json", subset=True)
        report, _ = self.run_subset()
        with self.assertRaises(ValueError):
            ev.write_report_create_only(root / "phase15-routing-pretend-gate.json", report)

    def test_perfect_subset_and_interruption_cannot_acquire_a_gate_verdict(self):
        from tests.test_phase15_routing_eval import expected_observation
        client = Mock()
        client.run.side_effect = [expected_observation(self.dataset["cases"][1]), KeyboardInterrupt()]
        sink = Mock()
        with self.assertRaises(KeyboardInterrupt):
            ev.run_real(self.dataset, self.digest, client, approval_ref="owner/F-01", release_commit="a" * 40,
                        artifact_sha256="b" * 64, prompt_identity=None, case_ids=["text-02"], repeat=3, sink=sink)
        report = sink.call_args.args[0]
        self.assertTrue(report["subset"])
        self.assertIsNone(report["verdict"]["passed"])
        self.assertEqual(report["interruptedBy"], "KeyboardInterrupt")
        self.assertEqual(sum(r["error"] == "not_run" for r in report["cases"]), 2)

    def test_cost_stop_and_errors_still_stop_subsets_without_a_gate_verdict(self):
        for obs, expected in [(ev.Observation(error="timeout"), 3),
                              (ev.Observation(usage={"estimatedLoopCostUsd": ev.LOOP_COST_STOP_USD + 1}), 1)]:
            report, client = self.run_subset(obs, repeat=4)
            self.assertEqual(client.run.call_count, expected)
            self.assertTrue(report["aborted"])
            self.assertIsNone(report["verdict"]["passed"])
            self.assertEqual(len(report["cases"]), 12)

    def test_stage_reason_reader_accepts_only_closed_codes_and_exact_correlation(self):
        correlation = str(uuid4())
        logs = Mock()
        logs.filter_log_events.return_value = {"events": [{"message": json.dumps(n)} for n in [
            {"correlation_id": correlation, "operation": "write_answer", "error_code": "writer_relationship_missing"},
            {"correlation_id": correlation, "operation": "write_answer", "error_code": "SECRET text"},
            {"correlation_id": str(uuid4()), "operation": "write_answer", "error_code": "model_failed"},
        ]], "nextToken": "opaque"}
        result = ev.read_stage_reasons(logs, "/aws/lambda/search", correlation, 10_000, 20_000)
        self.assertEqual(result, {"codes": [{"operation": "write_answer", "error_code": "writer_relationship_missing"}],
                                  "status": "log_page_incomplete"})
        self.assertEqual(logs.filter_log_events.call_count, 1)
        row = {"correlationId": correlation, "startedAtMs": 10_000, "endedAtMs": 20_000}
        command = ev.search_reason_command("/aws/lambda/search", row)
        for fragment in (correlation, "--start-time 5000", "--end-time 25000", "write_answer", "resolve_evidence", "--no-paginate"):
            self.assertIn(fragment, command)

    def test_preflight_stays_offline(self):
        with patch("boto3.client", side_effect=AssertionError("network")), patch("builtins.print") as output:
            self.assertEqual(ev.main(["real", "--case-ids", "text-02,amb-01,inj-03", "--repeat", "3"]), 0)
        plan = json.loads(output.call_args.args[0])["plan"]
        self.assertEqual((plan["harnessInvocations"], plan["maxIterationsPerInvocation"], plan["gateRun"]), (9, 4, False))

    def test_real_stop_reason_uses_existing_distinct_outcome(self):
        self.assertEqual(classify_loop(tool_calls=3, stop_reason="max_iterations_exceeded", error=None), "iterations_exhausted")
        self.assertEqual(classify_loop(tool_calls=3, stop_reason="other", error=None), "failed")

    def test_deadline_and_cost_dependencies_still_match_the_call_limit(self):
        from legaldesk_agent.client import AGENT_CHAT_TIMEOUT_SECONDS
        from legaldesk.agent_jobs import ATTEMPT_WINDOW_SECONDS, JOB_TOTAL_DEADLINE_SECONDS, RETRY_VISIBILITY_SECONDS
        from scripts.phase15_t9_cost_model import CLASSES
        from evals.phase15_golden_eval import load_dataset, real_plan
        self.assertEqual(sum(c[0] == "H" for c in CLASSES["E_mixed"].calls), 4)
        self.assertEqual(real_plan(load_dataset()[0])["maxIterationsPerInvocation"], 4)
        self.assertLess(AGENT_CHAT_TIMEOUT_SECONDS + 25, 150)
        self.assertLess(150, ATTEMPT_WINDOW_SECONDS)
        self.assertLess(AGENT_CHAT_TIMEOUT_SECONDS * 2 + RETRY_VISIBILITY_SECONDS, JOB_TOTAL_DEADLINE_SECONDS)
