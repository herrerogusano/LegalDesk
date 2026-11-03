"""Cost model of the golden set's real mode: arithmetic, link to the T9 prices, link to the runner's budget, link to the document."""

from __future__ import annotations

import copy
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
for extra in (".", "backend/src", "agent/src", "tests"):
    if str(ROOT / extra) not in sys.path:
        sys.path.insert(0, str(ROOT / extra))

from evals import phase15_golden_eval as ev  # noqa: E402
from legaldesk import agent_chat_telemetry as telemetry  # noqa: E402
from scripts import phase15_golden_cost_model as model  # noqa: E402
from scripts import phase15_t9_cost_model as t9  # noqa: E402

DATASET = json.loads(ev.DATASET_PATH.read_text(encoding="utf-8"))
REPORT = model.report(DATASET)


class PricesAndBudgetTests(unittest.TestCase):
    def test_it_uses_the_t9_prices_which_are_the_ones_the_worker_reports_cost_with(self) -> None:
        self.assertEqual(REPORT["prices"], {"inputPerMillion": t9.INPUT_PER_M, "outputPerMillion": t9.OUTPUT_PER_M})
        self.assertAlmostEqual(t9.INPUT_PER_M, telemetry.PRICE_PER_MILLION_TOKENS["input"])
        self.assertAlmostEqual(t9.OUTPUT_PER_M, telemetry.PRICE_PER_MILLION_TOKENS["output"])

    def test_the_caps_are_the_runners_fixed_budget(self) -> None:
        plan = ev.real_plan(DATASET)
        caps = REPORT["caps"]
        self.assertEqual(caps["cases"], len(DATASET["cases"]))
        self.assertEqual((caps["input_guardrail_calls"], caps["probes"], caps["retries"]), (plan["inputChecks"], plan["probes"], plan["retries"]))
        self.assertEqual(caps["async_jobs"], plan["asyncJobs"])
        self.assertEqual(caps["monthly_quota_units"], plan["quotaUnitsConsumed"])
        # every case may invoke the agent once; the async subset and the duplicate-delivery scenario run the agent again
        self.assertEqual(caps["harness_invocations"], plan["harnessInvocations"] + len(plan["asyncCaseIds"]) + 1)
        self.assertEqual(caps["stop_if_harness_loop_usd_exceeds"], ev.HARNESS_LOOP_COST_STOP_USD)

    def test_the_real_run_fits_the_monthly_quota_next_to_the_t9_smoke(self) -> None:
        smoke_jobs = sum(t9.BLOCK["smoke"].values())
        self.assertLessEqual(REPORT["caps"]["monthly_quota_units"] + smoke_jobs, t9.QUOTA_HARNESS_PER_MONTH)

    def test_the_stop_rule_is_the_upper_bound_of_the_loops_plus_a_margin_and_never_trips_in_the_typical_case(self) -> None:
        self.assertGreaterEqual(REPORT["caps"]["stop_if_harness_loop_usd_exceeds"], REPORT["upper"]["harness_loop_only_usd"] * model.STOP_MARGIN)
        self.assertGreater(REPORT["caps"]["stop_if_harness_loop_usd_exceeds"], REPORT["typical"]["harness_loop_only_usd"] * 2)


class ArithmeticTests(unittest.TestCase):
    def test_typical_is_below_the_upper_bound_and_inside_the_owners_range(self) -> None:
        typical, upper = REPORT["typical"]["total_usd"], REPORT["upper"]["total_usd"]
        self.assertLess(typical, upper)
        self.assertTrue(5 <= typical <= upper <= 15, (typical, upper))      # D-060: roughly 5 to 15 USD

    def test_components_add_up_to_the_total(self) -> None:
        for label in ("typical", "upper"):
            part = REPORT[label]
            self.assertAlmostEqual(sum(part["usd"].values()), part["total_usd"], delta=0.01)
            self.assertAlmostEqual(part["total_usd"] + part["corpus_preparation_usd"], part["total_with_corpus_usd"], delta=0.011)

    def test_a_case_the_guardrail_blocks_costs_only_the_input_check_in_the_typical_case(self) -> None:
        blocked = [model.classify(c) for c in DATASET["cases"] if model.classify(c)["blockedFirst"]]
        self.assertGreaterEqual(len(blocked), 4)
        for entry in blocked:
            typical = model.case_usd(entry, upper=False)
            self.assertEqual((typical["harness_run"], typical["probe"]), (0.0, 0.0))
            upper = model.case_usd(entry, upper=True)
            self.assertGreater(upper["harness_run"], 0)            # if the Guardrail lets it through, it runs like any other question

    def test_invocation_counts_follow_from_the_dataset(self) -> None:
        classified = [model.classify(c) for c in DATASET["cases"]]
        not_blocked = sum(1 for e in classified if not e["blockedFirst"])
        subset = sum(1 for e in classified if e["async"])
        self.assertEqual(REPORT["typical"]["harness_invocations"], not_blocked + subset + 1)
        self.assertEqual(REPORT["upper"]["harness_invocations"], len(classified) + subset + 1)
        self.assertLessEqual(REPORT["upper"]["harness_invocations"], REPORT["caps"]["harness_invocations"])
        self.assertEqual(REPORT["typical"]["input_checks"], len(classified))

    def test_a_probe_is_cheaper_when_the_resolver_finds_nothing_and_a_text_question_costs_more_than_an_inventory(self) -> None:
        self.assertLess(model.probe_usd("insufficient_evidence", upper=False), model.probe_usd("answerable", upper=False))
        self.assertGreater(model.direct_run_usd("B_text", upper=False), model.direct_run_usd("C_inventory", upper=False))
        self.assertGreater(model.direct_run_usd("E_mixed", upper=False), model.direct_run_usd("D_data", upper=False))
        for name in t9.CLASSES:
            self.assertGreater(model.direct_run_usd(name, upper=True), model.direct_run_usd(name, upper=False), name)

    def test_the_direct_run_is_never_dearer_than_the_same_question_through_the_worker(self) -> None:
        for name in t9.CLASSES:
            self.assertLess(model.direct_run_usd(name, upper=False), t9.job_cost_usd(name, upper=False), name)
            self.assertLess(model.direct_run_usd(name, upper=True), t9.job_cost_usd(name, upper=True), name)

    def test_the_cost_follows_the_dataset(self) -> None:
        smaller = copy.deepcopy(DATASET)
        smaller["cases"] = smaller["cases"][:40]
        self.assertLess(model.report(smaller)["typical"]["total_usd"], REPORT["typical"]["total_usd"])
        no_async = copy.deepcopy(DATASET)
        for case in no_async["cases"]:
            case.pop("asyncReal", None)
        self.assertLess(model.report(no_async)["typical"]["total_usd"], REPORT["typical"]["total_usd"])
        self.assertEqual(model.report(no_async)["caps"]["monthly_quota_units"], 2)   # only the two real async scenarios remain

    def test_per_class_counts_cover_every_direct_run(self) -> None:
        counted = sum(v["cases"] for v in REPORT["typical"]["per_class"].values())
        classified = [model.classify(c) for c in DATASET["cases"]]
        self.assertEqual(counted, sum(1 for e in classified if not e["blockedFirst"]))

    def test_the_async_subset_is_reported_apart_and_is_small(self) -> None:
        subset = REPORT["async_subset"]
        self.assertEqual(subset["cases"], sum(1 for c in DATASET["cases"] if c.get("asyncReal")))
        self.assertLess(subset["typical_usd"], REPORT["typical"]["total_usd"] * 0.2)
        self.assertLess(subset["typical_usd"], subset["upper_usd"])

    def test_the_session_expired_scenario_makes_no_model_call(self) -> None:
        self.assertLess(model.SESSION_EXPIRED_JOB_USD, 0.0001)


class CostDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = (ROOT / "docs" / "phase-15-golden-set-cost.md").read_text(encoding="utf-8")

    def test_the_headline_numbers_are_the_models(self) -> None:
        typical, upper = REPORT["typical"], REPORT["upper"]
        for number in (typical["total_usd"], upper["total_usd"], typical["total_with_corpus_usd"], upper["total_with_corpus_usd"]):
            self.assertIn(f"{number:.1f}", self.doc, number)
        subset = REPORT["async_subset"]
        self.assertIn(f"{subset['typical_usd']:.2f}", self.doc)
        self.assertIn(f"{subset['upper_usd']:.2f}", self.doc)

    def test_the_caps_in_the_document_are_the_budget_of_the_runner(self) -> None:
        caps = REPORT["caps"]
        for needle in (f"**{caps['cases']}** input Guardrail", f"**{caps['harness_invocations']}**", f"**{caps['probes']}**", f"**{caps['async_jobs']}**",
                       f"{caps['stop_if_harness_loop_usd_exceeds']:.1f}", "**0**"):
            self.assertIn(needle, self.doc, needle)

    def test_the_document_says_it_is_an_estimate_and_what_it_does_not_include(self) -> None:
        self.assertRegex(self.doc, r"(?i)estimate to approve, not an approval")
        for topic in ("tax", "caching", "quota", "cleanup"):
            self.assertRegex(self.doc, rf"(?i){topic}")
        self.assertNotRegex(self.doc, re.compile(r"(?i)approved\b(?!\s*,? not)\s+by the owner"))


if __name__ == "__main__":
    unittest.main()
