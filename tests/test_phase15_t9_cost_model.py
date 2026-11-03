"""The T9 cost model: arithmetic, its link to the code's price table and its hard limits."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
for extra in ("backend/src", "agent/src"):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))

from legaldesk import agent_chat_telemetry as telemetry  # noqa: E402
from scripts import phase15_t9_cost_model as model  # noqa: E402


class CostModelTests(unittest.TestCase):
    def test_prices_are_the_ones_the_worker_reports_cost_with(self) -> None:
        self.assertAlmostEqual(model.INPUT_PER_M, telemetry.PRICE_PER_MILLION_TOKENS["input"])
        self.assertAlmostEqual(model.OUTPUT_PER_M, telemetry.PRICE_PER_MILLION_TOKENS["output"])

    def test_the_block_is_the_agreed_number_of_questions(self) -> None:
        self.assertEqual(sum(model.BLOCK["eval"].values()), 30)
        self.assertEqual(sum(model.BLOCK["smoke"].values()), 10)  # the forced fallback is one of them
        dataset = json.loads((ROOT / "evals/phase15_routing_dataset.json").read_text(encoding="utf-8"))
        self.assertEqual(len(dataset["cases"]), sum(model.BLOCK["eval"].values()))

    def test_invocations_stay_inside_the_hard_cap_and_the_reserve_is_explicit(self) -> None:
        for label in ("typical", "upper"):
            invocations = model.report()[label]["harness_invocations"]
            self.assertEqual(invocations, 30 + 10 + 1 + 1 + 2 + 2)
            self.assertLessEqual(invocations, model.HARD_CAP["harness_invocations"])
        self.assertEqual(model.HARD_CAP["invocation_retries"], 0)

    def test_the_checks_script_budget_is_inside_the_model(self) -> None:
        from scripts import phase15_harness_checks as checks

        planned = sum(model.BLOCK["eval"].values()) + sum(model.BLOCK["smoke"].values()) + len(model.ADOT_CALLS) + checks.MAX_INVOCATIONS
        self.assertEqual(model.report()["typical"]["harness_invocations"], planned)

    def test_upper_bound_stays_under_the_ceiling_to_approve_and_above_the_typical_case(self) -> None:
        report = model.report()
        typical, upper = report["typical"]["block_total_usd"], report["upper"]["block_total_usd"]
        self.assertLess(typical, upper)
        self.assertLess(upper, model.HARD_CAP["approved_ceiling_usd"])
        self.assertGreater(typical, 1.0, "PLAN_15 estimated 1-3 USD; a lower figure means an assumption was dropped")

    def test_eval_stop_rule_leaves_room_over_the_upper_loop_estimate_but_not_unbounded(self) -> None:
        upper_loop = model.report()["upper"]["harness_loop_only_usd"]["eval"]
        stop = model.HARD_CAP["stop_eval_if_loop_usd_exceeds"]
        self.assertGreater(stop, upper_loop)
        self.assertLessEqual(stop, upper_loop * 1.35)

    def test_token_arithmetic_of_one_call(self) -> None:
        tokens_in, tokens_out, cost = model._tokens_cost([("H", 1_000_000, 100_000)], upper=False)
        self.assertEqual((tokens_in, tokens_out), (1_000_000, 100_000))
        self.assertAlmostEqual(cost, 3.30 + 0.1 * 16.50, places=6)

    def test_upper_bound_respects_the_output_cap_of_each_call_kind(self) -> None:
        _, tokens_out, _ = model._tokens_cost([("R", 3000, 250), ("W", 3500, 400)], upper=True)
        self.assertEqual(tokens_out, model.OUTPUT_CAP["R"] + model.OUTPUT_CAP["W"])


class FixedAndQuotaCostTests(unittest.TestCase):
    """The fixed monthly cost follows the template; the usage cost follows the deployed quota."""

    template = (ROOT / "infra" / "cloudformation" / "phase-14-public-edge.yaml").read_text(encoding="utf-8")

    def test_fixed_monthly_cost_counts_what_the_template_really_creates(self) -> None:
        alarms = self.template.count("Type: AWS::CloudWatch::Alarm")
        filters = self.template.count("Type: AWS::Logs::MetricFilter")
        # one alarm metric per alarm, except the fallback-rate math alarm, which bills fallbacks and invocations
        self.assertEqual(alarms - 1 + 2, model.MONTHLY_FIXED["alarm_metrics"])
        self.assertEqual(filters, model.MONTHLY_FIXED["custom_metrics_typical"])
        self.assertEqual(filters, model.MONTHLY_FIXED["custom_metrics_upper"], "no dimensions: the upper bound adds no metric")
        fixed = model.report()["typical"]["monthly_while_enabled_usd"]
        self.assertEqual(fixed, round(7 * 0.10 + 2 * 0.30, 2))
        self.assertEqual(fixed, model.report()["upper"]["monthly_while_enabled_usd"])

    def test_the_fixed_cost_fell_compared_with_the_log_derived_metrics_it_replaces(self) -> None:
        old_typical = 6 * 0.10 + 8 * 0.30  # 6 alarm metrics + 8 custom metrics (4 filters + dimension values)
        self.assertLess(model.report()["typical"]["monthly_while_enabled_usd"], old_typical)

    def test_usage_at_the_deployed_quota(self) -> None:
        self.assertEqual(model.QUOTA_HARNESS_PER_MONTH, 50)
        usage = model.monthly_usage_at_quota()
        self.assertEqual(usage["quota"], 50)
        self.assertLess(usage["typicalUsd"], usage["upperUsd"])
        # a user question costs about what the model says per class, and the quota bounds the number of them
        typical_job = usage["typicalUsd"] / 50
        self.assertTrue(model.job_cost_usd("A_no_tool", upper=False) < typical_job < model.job_cost_usd("E_mixed", upper=False))
        self.assertAlmostEqual(usage["upperUsd"], 50 * model.job_cost_usd("E_mixed", upper=True), places=2)

    def test_a_larger_quota_costs_proportionally_more(self) -> None:
        small, large = model.monthly_usage_at_quota(50), model.monthly_usage_at_quota(300)
        self.assertAlmostEqual(large["typicalUsd"] / small["typicalUsd"], 6, places=1)

    def test_the_all_in_month_is_fixed_plus_usage(self) -> None:
        report = model.report()
        self.assertAlmostEqual(report["monthlyWithAllQuotaUsedUsd"]["typical"], report["typical"]["monthly_while_enabled_usd"] + report["monthlyUsageAtQuota"]["typicalUsd"], places=2)
        self.assertAlmostEqual(report["monthlyWithAllQuotaUsedUsd"]["upper"], report["upper"]["monthly_while_enabled_usd"] + report["monthlyUsageAtQuota"]["upperUsd"], places=2)

    def test_the_documents_quote_the_current_numbers(self) -> None:
        report = model.report()
        estimate = (ROOT / "docs" / "phase-15-t9-cost-estimate.md").read_text(encoding="utf-8")
        for needle in (
            f"{report['typical']['monthly_while_enabled_usd']:.2f}",
            f"{report['monthlyUsageAtQuota']['typicalUsd']:.2f}",
            f"{report['monthlyUsageAtQuota']['upperUsd']:.2f}",
            f"{report['monthlyWithAllQuotaUsedUsd']['typical']:.2f}",
            f"{report['monthlyWithAllQuotaUsedUsd']['upper']:.2f}",
            "QuotaHarnessPerMonth",
        ):
            self.assertIn(needle, estimate, needle)


if __name__ == "__main__":
    unittest.main()
