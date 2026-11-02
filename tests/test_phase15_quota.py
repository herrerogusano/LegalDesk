"""Phase 15 (T5): agentic questions charge ``chats`` and ``harness`` once, and give both back."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from dynamodb_rules import enforce_expression_rules
from legaldesk.quota import (
    CHATS,
    GATEWAY,
    HARNESS,
    DynamoDBQuotaLedger,
    InMemoryQuotaLedger,
    QuotaExceededError,
    QuotaLimits,
    QuotaUnavailableError,
)

JAN = 1_767_225_600.0  # 2026-01-01T00:00:00Z
FEB = 1_769_904_000.0  # 2026-02-01T00:00:00Z


class _Conditional(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _CounterTable:
    """Interprets exactly the reserve and release expressions the ledger sends."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def update_item(self, *, Key, **kwargs):
        enforce_expression_rules(kwargs)
        item = self.items.setdefault((Key["pk"], Key["sk"]), {})
        values, names = kwargs["ExpressionAttributeValues"], kwargs["ExpressionAttributeNames"]
        field = names["#counter"]
        tokens = set(item.get("idempotencyTokens", set()))
        if kwargs["UpdateExpression"].startswith("ADD #counter :decrement"):
            if values[":token"] not in tokens or int(item.get(field, 0)) < 1:
                raise _Conditional()
            item[field] = int(item[field]) + int(values[":decrement"])
            tokens -= values[":token_set"]
            if tokens:
                item["idempotencyTokens"] = tokens
            else:
                item.pop("idempotencyTokens", None)  # DynamoDB drops an emptied set
            return
        if int(item.get(field, 0)) > int(values[":remaining"]):
            raise _Conditional()
        if values.get(":token") in tokens:
            raise _Conditional()
        item[field] = int(item.get(field, 0)) + int(values[":amount"])
        if ":token_set" in values:
            item["idempotencyTokens"] = tokens | values[":token_set"]

    def get_item(self, *, Key, **_):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}


class AgenticQuotaTests(unittest.TestCase):
    def test_harness_default_matches_chats(self) -> None:
        limits = QuotaLimits()
        self.assertEqual(limits.harness_per_month, 300)
        self.assertEqual(limits.harness_per_month, limits.chats_per_month)

    def test_release_gives_capacity_back_once(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=1, harness_per_month=1))
        for operation in (CHATS, HARNESS):
            ledger.reserve("tenant-a", operation, idempotency_key="job-1", now=JAN)
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", CHATS, idempotency_key="job-2", now=JAN)
        for _ in range(2):  # a second release must not credit twice
            for operation in (CHATS, HARNESS):
                ledger.release("tenant-a", operation, idempotency_key="job-1", now=JAN)
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-2", now=JAN)
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", CHATS, idempotency_key="job-3", now=JAN)

    def test_release_credits_the_month_that_was_charged(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=1))
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-1", now=JAN)
        ledger.release("tenant-a", CHATS, idempotency_key="job-1", now=FEB)  # wrong month: nothing to give back
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", CHATS, idempotency_key="job-2", now=JAN)
        ledger.release("tenant-a", CHATS, idempotency_key="job-1", now=JAN)
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-2", now=JAN)

    def test_release_never_touches_other_tenants_or_operations(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(chats_per_month=1, harness_per_month=1))
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-1", now=JAN)
        ledger.reserve("tenant-a", HARNESS, idempotency_key="job-1", now=JAN)
        ledger.release("tenant-b", CHATS, idempotency_key="job-1", now=JAN)
        ledger.release("tenant-a", CHATS, idempotency_key="job-1", now=JAN)
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", HARNESS, idempotency_key="job-9", now=JAN)  # harness is still charged

    def test_gateway_counter_is_not_part_of_a_job(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(gateway_per_month=1))
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-1", now=JAN)
        ledger.reserve("tenant-a", HARNESS, idempotency_key="job-1", now=JAN)
        ledger.reserve("tenant-a", GATEWAY, now=JAN)  # the button budget is untouched by a job

    def test_release_rejects_invalid_arguments(self) -> None:
        ledger = InMemoryQuotaLedger()
        for bad in ({"operation": "uploads", "idempotency_key": "k"}, {"operation": CHATS, "idempotency_key": ""}):
            with self.assertRaises(ValueError):
                ledger.release("tenant-a", **bad)

    def test_dynamo_release_is_conditional_on_the_token_and_idempotent(self) -> None:
        table = _CounterTable()
        ledger = DynamoDBQuotaLedger("metadata", table=table, limits=QuotaLimits(chats_per_month=1), clock=lambda: JAN)
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-1")
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", CHATS, idempotency_key="job-2")
        ledger.release("tenant-a", CHATS, idempotency_key="job-1")
        ledger.release("tenant-a", CHATS, idempotency_key="job-1")  # second call: conditional failure, no-op
        counter = next(iter(table.items.values()))
        self.assertEqual(counter["chatCount"], 0)
        ledger.reserve("tenant-a", CHATS, idempotency_key="job-2")
        self.assertEqual(next(iter(table.items.values()))["chatCount"], 1)

    def test_dynamo_release_failure_is_reported_not_swallowed(self) -> None:
        class Broken:
            def update_item(self, **_):
                raise RuntimeError("network")

        ledger = DynamoDBQuotaLedger("metadata", table=Broken(), clock=lambda: JAN)
        with self.assertRaises(QuotaUnavailableError):
            ledger.release("tenant-a", CHATS, idempotency_key="job-1")


if __name__ == "__main__":
    unittest.main()
