"""Usage and cost accounting of an agent loop (T9 step 12, run c).

Before: ``usage = records[0] if len(records) == 1 else None``. A loop with more than one model call, which is every loop that uses a tool, reported
no usage at all; 7 of 30 cases had tokens, ``estimatedLoopCostUsd`` said 0.089 USD for a run that cost about 1.4 USD (CloudWatch: 809 019 input and
29 303 output tokens that day, about 3.15 USD in all), and the eval's cost stop rules could not trip.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "agent" / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT))

import test_phase15_agent_chat_client as chat  # noqa: E402
import test_phase15_routing_eval as routing_tests  # noqa: E402
from evals import phase15_routing_eval as routing  # noqa: E402
from legaldesk.agent_chat_telemetry import (  # noqa: E402
    PRICE_PER_MILLION_TOKENS,
    SAFE_META_KEYS,
    UNKNOWN_CALL_INPUT_TOKENS,
    UNKNOWN_CALL_OUTPUT_TOKENS,
    estimate_loop_cost_usd,
    loop_usage_metadata,
    sanitize_job_metadata,
    unknown_call_cost_usd,
    usage_metadata,
)
from legaldesk_agent import HarnessInvoker, HarnessUsage, invoke_agent_chat  # noqa: E402
from legaldesk_agent.client import USAGE_CADENCE, sum_usage  # noqa: E402

SEARCH = "legaldesk_gateway_search-documents___search_documents"
LIST = "legaldesk_gateway_metadata-mcp___list_matter_documents"


def usage_block(input_tokens, output_tokens, *, cache_read=0, cache_write=0):
    usage = {"inputTokens": input_tokens, "outputTokens": output_tokens, "totalTokens": input_tokens + output_tokens}
    if cache_read:
        usage["cacheReadInputTokens"] = cache_read
    if cache_write:
        usage["cacheWriteInputTokens"] = cache_write
    return {"metadata": {"usage": usage, "metrics": {"latencyMs": 900}}}


def model_call(*, tool=None, usage=None, text=""):
    """One assistant message (a model call) of the loop, followed by its usage block when ``usage`` is given."""

    events = [{"messageStart": {"role": "assistant"}}]
    if tool:
        tool_id, name, payload = tool
        events += chat.tool_call(tool_id, name, payload)[1:4]  # toolUse start, stop, messageStop(tool_use)
    else:
        events += [{"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": text or "{}"}}}, {"contentBlockStop": {"contentBlockIndex": 0}},
                   {"messageStop": {"stopReason": "end_turn"}}]
    if usage:
        events.append(usage_block(*usage))
    return events


def tool_result(tool_id, payload):
    return chat.tool_call(tool_id, "unused", payload)[4:]  # the user message that carries the result


def three_call_loop(*, usages=((2700, 120), (3400, 130), (4700, 600))):
    """list -> search -> final plan: three model calls, one usage block each."""

    listing = {"documents": []}
    search = {"answer": "x", "evidenceStatus": "insufficient_evidence", "operationStatus": "ok", "citations": []}
    return (
        model_call(tool=("tu-1", LIST, listing), usage=usages[0]) + tool_result("tu-1", listing)
        + model_call(tool=("tu-2", SEARCH, search), usage=usages[1]) + tool_result("tu-2", search)
        + model_call(text='{"version":1,"blocks":[{"type":"text","source":{"tool":"search_documents","call":1}}]}', usage=usages[2])
    )


def run(events):
    return invoke_agent_chat(HarnessInvoker(chat.StreamClient(events), chat.HARNESS_ARN), "q", invocation_scope=chat.make_scope(), system_prompt=chat.PROMPT)


def cost(input_tokens, output_tokens, cache_read=0, cache_write=0):
    price = PRICE_PER_MILLION_TOKENS
    return round((input_tokens * price["input"] + output_tokens * price["output"] + cache_read * price["cache_read"] + cache_write * price["cache_write"]) / 1_000_000, 6)


class TheTotalOfALoopIsTheSumOfItsModelCallsTests(unittest.TestCase):
    def test_sum_usage_adds_every_field_and_knows_when_there_is_nothing_to_add(self) -> None:
        self.assertIsNone(sum_usage([]))
        one = HarnessUsage(10, 2, 12, 3, 4)
        self.assertEqual(sum_usage([one]), one)
        total = sum_usage([HarnessUsage(10, 2, 12, 3, 0), HarnessUsage(7, 5, 12, 0, 4), HarnessUsage(1, 1, 2, 1, 1)])
        self.assertEqual(total.to_dict(), {"inputTokens": 18, "outputTokens": 8, "totalTokens": 26, "cacheReadInputTokens": 4, "cacheWriteInputTokens": 5})

    def test_a_loop_of_three_model_calls_reports_the_total_not_nothing(self) -> None:
        result = run(three_call_loop())
        self.assertEqual((result.model_calls, result.usage_records_count), (3, 3))
        self.assertEqual(result.usage.to_dict(), {"inputTokens": 10800, "outputTokens": 850, "totalTokens": 11650, "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0})
        meta = loop_usage_metadata(result)
        self.assertEqual((meta["usageAvailable"], meta["usageComplete"], meta["costBasis"], meta["modelCalls"]), (True, True, "reported", 3))
        self.assertEqual(meta["estimatedLoopCostUsd"], cost(10800, 850))
        self.assertEqual(meta["tokens"], 11650)

    def test_cache_tokens_are_priced_apart_and_summed(self) -> None:
        result = run(three_call_loop(usages=((2700, 120), (3400, 130), (4700, 600))) + [usage_block(100, 10, cache_read=900, cache_write=50)])
        # a fourth block without a fourth message is still a block of the stream: it adds, it is not dropped
        self.assertEqual(result.usage.cache_read_input_tokens, 900)
        self.assertEqual(loop_usage_metadata(result)["cacheReadTokens"], 900)

    def test_a_single_call_loop_is_unchanged(self) -> None:
        result = run(model_call(text="{}", usage=(2700, 150)))
        meta = loop_usage_metadata(result)
        self.assertEqual((result.model_calls, meta["estimatedLoopCostUsd"], meta["costBasis"]), (1, cost(2700, 150), "reported"))

    def test_the_sum_never_hides_cost_whatever_the_cadence_turns_out_to_be(self) -> None:
        """If the blocks were cumulative the sum would over-count (conservative for a stop rule); it can never be below the last block."""

        self.assertEqual(USAGE_CADENCE, "per_call")
        cumulative_looking = [HarnessUsage(2700, 120, 2820), HarnessUsage(6100, 250, 6350), HarnessUsage(10800, 850, 11650)]
        self.assertGreaterEqual(sum_usage(cumulative_looking).total_tokens, cumulative_looking[-1].total_tokens)

    def test_the_button_flow_adapter_sums_too_and_the_smoke_budget_accepts_it(self) -> None:
        from legaldesk.smoke_budget import SmokeBudget, SmokeBudgetLimits

        client = chat.StreamClient([usage_block(10, 1), usage_block(7, 2)])
        result = HarnessInvoker(client, "arn:test").invoke("hello")
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens, len(result.usage_records)), (17, 3, 2))
        budget = SmokeBudget(SmokeBudgetLimits(input_tokens=100, output_tokens=100))
        budget.consume_harness_usage(result.usage)  # was refused (usage None halted the budget) for any stream with two blocks
        self.assertEqual((budget.snapshot().input_tokens, budget.snapshot().output_tokens), (17, 3))


class AMissingUsageIsUnknownAndPricedAtAnUpperBoundTests(unittest.TestCase):
    def test_model_calls_without_any_usage_are_priced_not_zeroed(self) -> None:
        result = run(three_call_loop(usages=(None, None, None)))
        self.assertEqual((result.usage, result.model_calls, result.usage_records_count), (None, 3, 0))
        meta = loop_usage_metadata(result)
        self.assertEqual((meta["usageAvailable"], meta["usageComplete"], meta["costBasis"]), (False, False, "upper_bound"))
        self.assertEqual(meta["estimatedLoopCostUsd"], round(3 * unknown_call_cost_usd(), 6))
        self.assertNotIn("tokens", meta)  # no invented token counts

    def test_a_cut_stream_adds_the_upper_bound_of_the_missing_blocks_to_what_was_reported(self) -> None:
        result = run(three_call_loop(usages=((2700, 120), (3400, 130), None)))
        self.assertEqual((result.model_calls, result.usage_records_count), (3, 2))
        meta = loop_usage_metadata(result)
        self.assertEqual((meta["usageComplete"], meta["costBasis"]), (False, "upper_bound"))
        self.assertEqual(meta["estimatedLoopCostUsd"], round(cost(6100, 250) + unknown_call_cost_usd(), 6))
        self.assertEqual(meta["tokens"], 6100 + 250)

    def test_the_upper_bound_of_a_call_is_a_full_context_and_the_output_cap(self) -> None:
        price = PRICE_PER_MILLION_TOKENS
        self.assertAlmostEqual(unknown_call_cost_usd(), (UNKNOWN_CALL_INPUT_TOKENS * price["input"] + UNKNOWN_CALL_OUTPUT_TOKENS * price["output"]) / 1e6)
        self.assertEqual(UNKNOWN_CALL_OUTPUT_TOKENS, 2048)  # AGENT_CHAT_MAX_TOKENS and the Harness cap
        self.assertGreater(unknown_call_cost_usd(), cost(4700, 600))  # more than the heaviest call of the model in the cost doc

    def test_without_the_loop_facts_the_old_contract_holds(self) -> None:
        self.assertEqual(usage_metadata(None), {"usageAvailable": False})
        self.assertEqual(usage_metadata(HarnessUsage(100, 10, 110))["estimatedLoopCostUsd"], cost(100, 10))
        self.assertEqual(loop_usage_metadata(object()), {"usageAvailable": False})  # a loop object without the fields (a simulated one)

    def test_the_job_telemetry_carries_the_new_fields_and_only_closed_values(self) -> None:
        meta = usage_metadata(None, model_calls=3)
        safe = sanitize_job_metadata({**meta, "costBasis": "SECRET text with spaces"})
        self.assertTrue({"usageComplete", "modelCalls", "costBasis"} <= SAFE_META_KEYS)
        self.assertEqual((safe["usageComplete"], safe["modelCalls"], safe["estimatedLoopCostUsd"]), (False, 3, meta["estimatedLoopCostUsd"]))
        self.assertNotIn("costBasis", safe)  # free text is dropped; the closed one passes
        self.assertEqual(sanitize_job_metadata(meta)["costBasis"], "upper_bound")

    def test_the_worker_metadata_of_a_real_loop_counts_every_model_call(self) -> None:
        from legaldesk.agent_chat import AgentChatWorker  # noqa: F401 - the module imports loop_usage_metadata at import time

        source = (ROOT / "backend" / "src" / "legaldesk" / "agent_chat.py").read_text(encoding="utf-8")
        self.assertIn("**loop_usage_metadata(loop),", source)
        self.assertNotIn("usage_metadata(loop.usage)", source)


class TheCostStopRulesTripTests(unittest.TestCase):
    """Uses the routing eval's scripted client; only the new rules are run here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset, cls.digest = routing.load_dataset()

    def run_real(self, make):
        client = routing_tests.ScriptedClient(make)
        report = routing.run_real(self.dataset, self.digest, client, approval_ref="owner-approval:test",
                                  release_commit="a" * 40, artifact_sha256="b" * 64, prompt_identity=("1.4.0", "c" * 64))
        return report, client

    def priced(self, case, n, usd, **extra):
        usage = {"usageAvailable": True, "usageComplete": True, "costBasis": "reported", "modelCalls": 3, "tokens": 12000, "estimatedLoopCostUsd": usd, **extra}
        return routing_tests.expected_observation(case, usage=usage)

    def test_the_routing_eval_stops_when_the_summed_estimate_passes_the_rule(self) -> None:
        report, client = self.run_real(lambda case, n: self.priced(case, n, 0.30))
        self.assertEqual(report["abortedBy"], "cost_stop")
        self.assertEqual(len(client.calls), 8)  # 8 x 0.30 = 2.40 > 2.20: the eighth call is the last
        self.assertTrue(report["aborted"])
        self.assertFalse(report["verdict"]["passed"])  # the cases not run count as failed
        self.assertEqual(report["budget"]["harnessInvocationsUsed"], 8)
        self.assertEqual(report["loopCostStopUsd"], 2.2)

    def test_a_run_under_the_rule_is_not_stopped(self) -> None:
        report, client = self.run_real(lambda case, n: self.priced(case, n, 0.07))  # 2.10 in all
        self.assertEqual((report["abortedBy"], len(client.calls), report["aborted"]), (None, 30, False))
        self.assertEqual(report["summary"]["loopCostBasis"], "reported")

    def test_unknown_usage_is_an_upper_bound_so_it_trips_the_rule_instead_of_passing_as_free(self) -> None:
        unknown = usage_metadata(None, model_calls=3)
        report, client = self.run_real(lambda case, n: routing_tests.expected_observation(case, usage=unknown))
        self.assertEqual(report["abortedBy"], "cost_stop")
        self.assertLess(len(client.calls), 30)
        self.assertEqual(report["summary"]["loopCostBasis"], "upper_bound")
        self.assertEqual(report["summary"]["usageIncompleteCases"], len(client.calls))
        row = report["cases"][0]
        self.assertEqual((row["usageComplete"], row["costBasis"], row["modelCalls"]), (False, "upper_bound", 3))

    def test_the_rule_is_the_one_of_the_cost_model(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import phase15_t9_cost_model as model

        self.assertEqual(routing.LOOP_COST_STOP_USD, model.HARD_CAP["stop_eval_if_loop_usd_exceeds"])

    def test_the_golden_set_stop_rule_trips_on_an_unknown_cost_too(self) -> None:
        import test_phase15_golden_eval as golden_tests

        ev = golden_tests.ev

        def unknown(case, n):
            obs = golden_tests.healthy(case, n)
            obs.usage = usage_metadata(None, model_calls=3)
            return obs

        client = golden_tests.FakeClient(unknown)
        report = ev.run_real(golden_tests.DATASET, golden_tests.DIGEST, client, **golden_tests.kwargs())
        self.assertEqual(report["abortedBy"], "cost_stop")
        self.assertLess(len(client.calls), len(golden_tests.DATASET["cases"]))
        self.assertEqual(report["summary"]["all"]["loopCostBasis"], "upper_bound")
        self.assertGreater(report["summary"]["all"]["usageIncompleteCases"], 0)


class TheDocumentsSayItTests(unittest.TestCase):
    def test_the_cost_estimate_and_the_runbook_correct_the_earlier_numbers_and_give_the_cross_check(self) -> None:
        cost_doc = " ".join((ROOT / "docs" / "phase-15-t9-cost-estimate.md").read_text(encoding="utf-8").split())
        runbook = " ".join((ROOT / "docs" / "phase-15-live-runbook.md").read_text(encoding="utf-8").split())
        for text in (cost_doc, runbook):
            for needle in ("undercounted", "0.089", "AWS/Bedrock", "InputTokenCount", "OutputTokenCount", "ModelId"):
                self.assertIn(needle, text)
        for needle in ("get-metric-statistics", "resolver and the writer", "difference", "cumulative"):
            self.assertIn(needle, cost_doc)
        observability = (ROOT / "docs" / "phase-15-observability.md").read_text(encoding="utf-8")
        self.assertIn("costBasis", observability)


if __name__ == "__main__":
    unittest.main()
