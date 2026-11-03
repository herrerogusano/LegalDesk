"""The owner decisions of 2026-11-03 (final state A, quota 50, native metrics, ADOT off) hold in the runbook and the templates."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
RUNBOOK = (ROOT / "docs" / "phase-15-live-runbook.md").read_text(encoding="utf-8")
OBSERVABILITY = (ROOT / "docs" / "phase-15-observability.md").read_text(encoding="utf-8")
EDGE = (ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml").read_text(encoding="utf-8")


def step(text: str, number: int) -> str:
    start = text.index(f"## Step {number} ")
    end = text.find("\n## ", start + 5)
    return text[start:end if end != -1 else None]


class QuotaTests(unittest.TestCase):
    def test_the_deployment_sets_50_but_the_template_default_stays_300(self) -> None:
        self.assertIn('"QuotaHarnessPerMonth=50"', step(RUNBOOK, 8))
        self.assertNotIn("QuotaHarnessPerMonth=300", RUNBOOK)
        parameter = EDGE.split("\n  QuotaHarnessPerMonth:", 1)[1].split("\n  Quota", 1)[0].split("\n  EnableAgentChat", 1)[0]
        self.assertIn("Default: 300", parameter)  # ADR-021 D8: the default of the code and the template does not change

    def test_the_runbook_reads_the_quota_back(self) -> None:
        self.assertIn("LEGALDESK_QUOTA_HARNESS_PER_MONTH=50", step(RUNBOOK, 8))
        self.assertIn("LEGALDESK_QUOTA_HARNESS_PER_MONTH=50", step(RUNBOOK, 14))


class FinalStateTests(unittest.TestCase):
    def test_state_a_is_the_only_final_state_and_is_verified_by_read_back(self) -> None:
        final = step(RUNBOOK, 14)
        self.assertIn("Final state: A, the agentic chat stays on for demos", final)
        self.assertNotIn("the owner chooses one", final)
        self.assertNotIn("B · switch it off", final)
        for needle in ("`true` / `true`", "**`false`**", "empty (alarms in the console only, no SNS)", "Read-back"):
            self.assertIn(needle, final)

    def test_no_sns_topic_is_wired_by_default(self) -> None:
        parameter = EDGE.split("\n  AgentChatAlarmTopicArn:", 1)[1].split("\n\nConditions:", 1)[0]
        self.assertIn('Default: ""', parameter)
        self.assertIn("stays empty by decision", step(RUNBOOK, 8))

    def test_adot_is_always_switched_off_after_the_probe(self) -> None:
        probe, off = step(RUNBOOK, 5), step(RUNBOOK, 6)
        self.assertIn("ADOT goes **off**", probe)
        self.assertEqual(probe.count("ADOT goes **off**"), 2)  # whatever the verdict
        self.assertIn("always, after the probe", off)
        self.assertNotIn("unless the residual risk was accepted", RUNBOOK)

    def test_budget_ceiling_and_invocations_are_unchanged(self) -> None:
        self.assertIn("**50 Harness invocations**", RUNBOOK)
        self.assertIn("**5 USD**", RUNBOOK)

    def test_the_resource_counts_follow_the_template(self) -> None:
        resources = len(re.findall(r"(?m)^  AgentChat\w+:\n    Type: AWS::", EDGE))
        self.assertEqual(resources, 14)  # queue, DLQ, log group, role, function, mapping, 2 filters, 6 alarms
        self.assertIn("14 new AgentChat*", step(RUNBOOK, 2))
        self.assertIn("removes the 14 resources", step(RUNBOOK, 8))
        self.assertNotRegex(RUNBOOK, r"\b17 (?:new|resources)\b")


class ObservabilityDocTests(unittest.TestCase):
    def test_every_alarm_is_documented_with_its_metric_kind(self) -> None:
        alarms = re.findall(r"(?m)^  (AgentChat\w+Alarm):\s*$", EDGE)
        self.assertEqual(len(alarms), 6)
        for alarm in alarms:
            self.assertIn(f"`{alarm}`", OBSERVABILITY, alarm)
        for native in ("`ApproximateNumberOfMessagesVisible`", "`ApproximateAgeOfOldestMessage`", "Lambda `Duration`", "Lambda `Errors`", "Lambda `Invocations`"):
            self.assertIn(native, OBSERVABILITY, native)

    def test_each_remaining_custom_metric_is_justified_and_the_removed_ones_are_not_alarm_sources(self) -> None:
        self.assertIn("Why each custom metric stays", OBSERVABILITY)
        for name in ("`AgentChatFallbacks`", "`AgentChatJobFailures`"):
            self.assertIn(name, OBSERVABILITY)
        table = OBSERVABILITY.split("## Metrics and alarms", 1)[1].split("## Logs Insights", 1)[0]
        for removed in ("AgentChatJobDurationMs", "AgentChatJobsCompleted"):
            self.assertNotIn(f"| `{removed}`", table)
        self.assertIn("Failures by code", OBSERVABILITY)
        self.assertIn("Fallbacks by reason", OBSERVABILITY)


if __name__ == "__main__":
    unittest.main()
