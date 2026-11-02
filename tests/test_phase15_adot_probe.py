"""Phase 15 T7: ADOT is prepared for the T9 probe but stays off; the marker probe is safe by construction."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from scripts import phase15_adot_marker_probe as probe

ROOT = Path(__file__).parents[1]
HARNESS_TEMPLATE = ROOT / "infra" / "cloudformation" / "phase-01-harness.yaml"
ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:harness/LegalDeskPhase01-AbCdEf"
APPROVAL = "owner-approval:2026-11-xx"


class HarnessTemplateAdotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = HARNESS_TEMPLATE.read_text(encoding="utf-8")

    def section(self, name: str) -> str:
        body = self.template.split(f"\n{name}:\n", 1)[1]
        return re.split(r"(?m)^[A-Z][A-Za-z]*:\s*$", body, maxsplit=1)[0]

    def test_adot_stays_disabled_unless_the_parameter_is_turned_on(self) -> None:
        parameter = self.section("Parameters").split("\n  EnableManagedAdot:\n", 1)[1]
        self.assertIn('Default: "false"', parameter.split("\nConditions", 1)[0])
        self.assertIn('ManagedAdotEnabled: !Equals [!Ref EnableManagedAdot, "true"]', self.section("Conditions"))
        # The disable switch is present for "false" (the default) and only removed for "true".
        self.assertIn(
            'DISABLE_ADOT_OBSERVABILITY: !If [ManagedAdotEnabled, !Ref AWS::NoValue, "true"]', self.template)

    def test_the_content_opt_out_is_unconditional(self) -> None:
        environment = self.template.split("EnvironmentVariables:\n", 1)[1].split("SystemPrompt:", 1)[0]
        self.assertRegex(environment, r'(?m)^        AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT: "true"$')  # no !If around it
        self.assertEqual(self.template.count("AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT"), 1)  # the variable only

    def test_nothing_else_in_the_harness_depends_on_the_parameter(self) -> None:
        self.assertEqual(self.template.count("ManagedAdotEnabled"), 2)  # condition + the one variable


class ProbeDryRunTests(unittest.TestCase):
    def test_dry_run_prints_a_bounded_plan_and_touches_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = probe.RESULTS_ROOT / "phase15-adot-probe-dry.json"
            result = probe.run_probe(harness_arn=ARN, runtime_id="rt-1", approval_ref="", output=output, execute=False,
                                     invoke=lambda m: self.fail("must not invoke"), logs_client=object())
            self.assertFalse(output.exists())
        self.assertEqual(result["mode"], "dry_run")
        plan = result["plan"]
        self.assertEqual((plan["harnessInvocations"], plan["retries"], plan["reportStoresMarker"]), (1, 0, False))
        self.assertEqual(plan["filterLogEventsCallsUpperBound"], 4 * 10 * 2 + 1)

    def test_execution_needs_approval_attestation_and_a_valid_target(self) -> None:
        output = probe.RESULTS_ROOT / "phase15-adot-probe-guard.json"
        base = dict(harness_arn=ARN, runtime_id="rt-1", output=output, execute=True,
                    invoke=lambda m: self.fail("must not invoke"), logs_client=object())
        for override in (
            {"approval_ref": "", "adot_enabled_attested": True},
            {"approval_ref": APPROVAL, "adot_enabled_attested": False},
            {"approval_ref": APPROVAL, "adot_enabled_attested": True, "harness_arn": "arn:aws:s3:::bucket"},
            {"approval_ref": APPROVAL, "adot_enabled_attested": True, "runtime_id": "../x"},
            {"approval_ref": APPROVAL, "adot_enabled_attested": True, "polls": 11},
            {"approval_ref": APPROVAL, "adot_enabled_attested": True, "output": ROOT / "outside.json"},
            {"approval_ref": APPROVAL, "adot_enabled_attested": True, "output": probe.RESULTS_ROOT / "other-name.json"},
        ):
            with self.subTest(override=override), self.assertRaises(ValueError):
                probe.run_probe(**{**base, **override})
        self.assertFalse(output.exists())


class FakeLogs:
    def __init__(self, groups, hits=None):
        self.groups, self.hits, self.queries = groups, hits or {}, []

    def get_paginator(self, name):
        assert name == "describe_log_groups"
        return self

    def paginate(self, **kwargs):
        assert kwargs["logGroupNamePrefix"].startswith(probe.RUNTIME_LOG_PREFIX)
        return [{"logGroups": [{"logGroupName": name} for name in self.groups]}]

    def filter_log_events(self, **kwargs):
        self.queries.append(kwargs)
        suffix = kwargs["filterPattern"].strip('"')[-2:]
        return {"events": [{"message": "SECRET EVENT BODY"}] * self.hits.get(suffix, 0)}


class ProbeExecutionTests(unittest.TestCase):
    def execute(self, name, logs, *, invoke=None, polls=2):
        output = probe.RESULTS_ROOT / f"phase15-adot-probe-test-{name}.json"
        self.addCleanup(lambda: output.unlink(missing_ok=True))
        calls, sleeps = [], []
        report = probe.run_probe(
            harness_arn=ARN, runtime_id="rt-1", approval_ref=APPROVAL, output=output, execute=True,
            adot_enabled_attested=True, polls=polls, wait_seconds=5, marker="LDADOTPROBEfixed",
            invoke=invoke or calls.append, logs_client=logs, sleep=sleeps.append,
        )
        return report, output, calls, sleeps

    def test_a_marker_in_provider_logs_is_reported_as_content_observed_and_never_stored(self) -> None:
        logs = FakeLogs(["/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"], hits={"-Q": 1, "-A": 2})
        report, output, calls, sleeps = self.execute("leak", logs)
        self.assertEqual((report["verdict"], report["questionMarkerEvents"], report["answerMarkerEvents"]), (probe.VERDICT_CONTENT, 1, 2))
        self.assertEqual(len(calls), 1)  # exactly one invocation, no retries
        self.assertEqual(sleeps, [5])    # stops polling as soon as it has the answer
        self.assertIn("LDADOTPROBEfixed-Q", calls[0])
        text = output.read_text(encoding="utf-8")
        for secret in ("LDADOTPROBEfixed", "SECRET EVENT BODY", "Synthetic probe"):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["markerSha256"], hashlib.sha256(b"LDADOTPROBEfixed").hexdigest())

    def test_no_marker_after_all_polls_is_bounded_evidence_not_proof(self) -> None:
        logs = FakeLogs(["/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT", "/aws/bedrock-agentcore/runtimes/rt-1-other"])
        report, _, _, sleeps = self.execute("clean", logs, polls=3)
        self.assertEqual((report["verdict"], report["pollsUsed"], sleeps), (probe.VERDICT_NO_CONTENT, 3, [5, 5, 5]))
        self.assertEqual(len(logs.queries), 3 * 2 * 2)  # polls x groups x (question, answer)
        self.assertIn("not proof", report["interpretation"])

    def test_missing_log_groups_and_failed_invocations_draw_no_conclusion(self) -> None:
        report, _, _, _ = self.execute("nogroups", FakeLogs([]))
        self.assertEqual(report["verdict"], probe.VERDICT_NO_LOG_GROUPS)

        def failing(_message):
            raise RuntimeError("throttled")

        logs = FakeLogs(["/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"], hits={"-Q": 1})
        report, _, _, sleeps = self.execute("invokefail", logs, invoke=failing)
        self.assertEqual((report["verdict"], report["retries"], sleeps, logs.queries), (probe.VERDICT_INVOKE_FAILED, 0, [], []))

    def test_reports_are_create_only(self) -> None:
        logs = FakeLogs(["/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"])
        _, output, _, _ = self.execute("immutable", logs)
        with self.assertRaises(FileExistsError):
            probe.run_probe(harness_arn=ARN, runtime_id="rt-1", approval_ref=APPROVAL, output=output, execute=True,
                            adot_enabled_attested=True, invoke=lambda m: self.fail("must not spend before the path check"),
                            logs_client=logs)


if __name__ == "__main__":
    unittest.main()
