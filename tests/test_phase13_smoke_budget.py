from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.smoke_budget import (  # noqa: E402
    BudgetedSdkClient,
    SmokeBudget,
    SmokeBudgetExceeded,
    SmokeBudgetLimits,
    SmokeUsageInvalid,
)


class _Sdk:
    def __init__(self, *, fail: bool = False):
        self.calls = []
        self.fail = fail

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider failure")
        return {"ok": True}

    def unmetered_call(self):
        return {"ok": True}

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"usage": {"inputTokens": 4, "outputTokens": 2, "totalTokens": 6}}


class Phase13SmokeBudgetTests(unittest.TestCase):
    def test_failed_call_consumes_operation_slot(self):
        budget = SmokeBudget(SmokeBudgetLimits(retrieve=1))
        client = BudgetedSdkClient(_Sdk(fail=True), budget)
        with self.assertRaises(RuntimeError):
            client.retrieve(query="fictional")
        with self.assertRaises(SmokeBudgetExceeded):
            client.retrieve(query="fictional")
        self.assertEqual(budget.snapshot().counts["retrieve"], 1)

    def test_proxy_reserves_before_dispatch_and_usage_is_aggregated(self):
        sdk = _Sdk()
        budget = SmokeBudget(SmokeBudgetLimits(retrieve=1, input_tokens=10, output_tokens=5))
        client = BudgetedSdkClient(sdk, budget)
        client.retrieve(query="fictional")
        self.assertEqual(len(sdk.calls), 1)
        budget.consume_harness_usage({"inputTokens": 7, "outputTokens": 3})
        budget.consume_harness_usage({"inputTokens": 3, "outputTokens": 2})
        self.assertEqual(budget.snapshot().input_tokens, 10)
        self.assertEqual(budget.snapshot().output_tokens, 5)
        with self.assertRaises(SmokeBudgetExceeded):
            budget.consume_harness_usage({"inputTokens": 1, "outputTokens": 0})

    def test_usage_rejects_malformed_nonfinite_and_negative_values(self):
        budget = SmokeBudget()
        for value in (-1, float("nan"), float("inf"), 1.25, True):
            with self.subTest(value=value), self.assertRaises((SmokeUsageInvalid, SmokeBudgetExceeded)):
                budget.consume_harness_usage({"inputTokens": value, "outputTokens": 0})

    def test_missing_usage_halts_budget(self):
        budget = SmokeBudget()
        with self.assertRaises(SmokeUsageInvalid):
            budget.consume_harness_usage(None)
        with self.assertRaises(SmokeBudgetExceeded):
            budget.reserve("retrieve")

    def test_overbudget_usage_is_counted_and_latches(self):
        budget = SmokeBudget(SmokeBudgetLimits(input_tokens=10, output_tokens=5))
        with self.assertRaises(SmokeBudgetExceeded):
            budget.consume_harness_usage({"inputTokens": 11, "outputTokens": 1})
        snapshot = budget.snapshot()
        self.assertEqual(snapshot.input_tokens, 11)
        with self.assertRaises(SmokeBudgetExceeded):
            budget.consume_harness_usage({"inputTokens": 1, "outputTokens": 0})

    def test_failed_reserved_estimate_is_not_released_as_free(self):
        budget = SmokeBudget()
        with self.assertRaises(RuntimeError):
            with budget.reserve("retrieve", estimated_input_tokens=5) as reservation:
                raise RuntimeError("provider failure")
        self.assertEqual(budget.snapshot().input_tokens, 5)
        with self.assertRaises(SmokeBudgetExceeded):
            budget.reserve("retrieve")

    def test_unknown_sdk_method_is_not_forwarded(self):
        client = BudgetedSdkClient(_Sdk(), SmokeBudget())
        with self.assertRaises(SmokeBudgetExceeded):
            client.unmetered_call()

    def test_converse_usage_is_consumed_after_successful_call(self):
        budget = SmokeBudget()
        client = BudgetedSdkClient(_Sdk(), budget)
        client.converse(messages=[])
        snapshot = budget.snapshot()
        self.assertEqual(snapshot.counts["converse"], 1)
        self.assertEqual(snapshot.input_tokens, 4)
        self.assertEqual(snapshot.output_tokens, 2)

    def test_gateway_operation_is_counted_and_failed_call_halts(self):
        budget = SmokeBudget(SmokeBudgetLimits(gateway=2))
        budget.call("gateway", lambda: {"ok": True})
        budget.call("gateway", lambda: {"ok": True})
        self.assertEqual(budget.snapshot().counts["gateway"], 2)
        with self.assertRaises(SmokeBudgetExceeded):
            budget.call("gateway", lambda: {"ok": True})

    def test_failed_gateway_call_consumes_slot_and_halts(self):
        budget = SmokeBudget(SmokeBudgetLimits(gateway=1))
        with self.assertRaises(RuntimeError):
            budget.call("gateway", lambda: (_ for _ in ()).throw(RuntimeError("transport")))
        self.assertEqual(budget.snapshot().counts["gateway"], 1)
        with self.assertRaises(SmokeBudgetExceeded):
            budget.call("gateway", lambda: {"ok": True})


if __name__ == "__main__":
    unittest.main()
