from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.quota import (
    CHATS,
    GATEWAY,
    HARNESS,
    INGESTION_STARTS,
    DynamoDBQuotaLedger,
    InMemoryQuotaLedger,
    QuotaExceededError,
    QuotaLimits,
    QuotaUnavailableError,
    scoped_idempotency_key,
    utc_month,
)
from legaldesk.http_app import LoopbackLegalDeskApp


class _ConditionalError(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class _Table:
    def __init__(self, *, fail_update: Exception | None = None) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.updates: list[dict[str, object]] = []
        self.fail_update = fail_update

    def put_item(self, *, Item: dict[str, object], **kwargs: object) -> None:
        key = (str(Item["pk"]), str(Item["sk"]))
        if kwargs.get("ConditionExpression") and key in self.items:
            raise _ConditionalError()
        self.items[key] = dict(Item)

    def delete_item(self, *, Key: dict[str, str], **_kwargs: object) -> None:
        self.items.pop((Key["pk"], Key["sk"]), None)

    def get_item(self, *, Key: dict[str, str], **_kwargs: object) -> dict[str, object]:
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, *, Key: dict[str, str], **kwargs: object) -> None:
        self.updates.append(kwargs)
        if self.fail_update is not None:
            raise self.fail_update
        key = (Key["pk"], Key["sk"])
        item = self.items.setdefault(key, {})
        values = kwargs["ExpressionAttributeValues"]
        names = kwargs["ExpressionAttributeNames"]
        counter_field = next((field for token, field in names.items() if token == "#counter"), None)
        if counter_field is not None and int(item.get(counter_field, 0)) > int(values[":remaining"]):
            raise _ConditionalError()
        if counter_field is None and (
            int(item.get("uploads", 0)) > int(values[":remaining_uploads"])
            or int(item.get("uploadBytes", 0)) > int(values[":remaining_bytes"])
        ):
            raise _ConditionalError()
        token = values.get(":token")
        if token is not None and token in item.get("idempotencyTokens", set()):
            raise _ConditionalError()
        for token, field in names.items():
            if token in {"#uploads", "#bytes", "#counter"}:
                item[field] = int(item.get(field, 0)) + int(values[":one"] if token == "#uploads" else values.get(":bytes", values.get(":amount", 0)))
        if ":token_set" in values:
            item.setdefault("idempotencyTokens", set()).update(values[":token_set"])


class Phase14QuotaTests(unittest.TestCase):
    def test_limits_isolate_tenants_and_roll_over_utc(self) -> None:
        limits = QuotaLimits(chats_per_month=1)
        ledger = InMemoryQuotaLedger(limits=limits)
        december = datetime(2026, 1, 31, 23, 59, 59, tzinfo=timezone.utc).timestamp()
        january = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        ledger.reserve("tenant-a", CHATS, now=december)
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", CHATS, now=december)
        ledger.reserve("tenant-b", CHATS, now=december)
        ledger.reserve("tenant-a", CHATS, now=january)
        self.assertEqual(utc_month(december), "2026-01")
        self.assertEqual(utc_month(january), "2026-02")

    def test_upload_reserves_count_and_bytes_atomically(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(uploads_per_month=1, upload_bytes_per_month=10))
        ledger.reserve_upload("tenant-a", 10)
        with self.assertRaises(QuotaExceededError):
            ledger.reserve_upload("tenant-a", 1)
        with self.assertRaises(QuotaExceededError):
            ledger.reserve_upload("tenant-b", 11)

    def test_concurrent_reservations_only_one_can_cross_limit(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(gateway_per_month=1))
        outcomes: list[bool] = []

        def reserve() -> None:
            try:
                ledger.reserve("tenant-a", GATEWAY)
                outcomes.append(True)
            except QuotaExceededError:
                outcomes.append(False)

        threads = [threading.Thread(target=reserve) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count(True), 1)

    def test_ingestion_idempotency_does_not_double_charge(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(ingestion_starts_per_month=1))
        ledger.reserve("tenant-a", INGESTION_STARTS, idempotency_key="retry-a")
        ledger.reserve("tenant-a", INGESTION_STARTS, idempotency_key="retry-a")
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", INGESTION_STARTS, idempotency_key="retry-b")

    def test_dynamo_uses_conditional_update_and_fails_closed(self) -> None:
        table = _Table()
        ledger = DynamoDBQuotaLedger("metadata", table=table, limits=QuotaLimits(chats_per_month=1))
        ledger.reserve("tenant-a", CHATS, idempotency_key="chat-a")
        ledger.reserve("tenant-a", CHATS, idempotency_key="chat-a")
        self.assertEqual(len(table.updates), 2)
        self.assertIn("UpdateExpression", table.updates[0])
        self.assertEqual({key[1] for key in table.items}, {"COUNTER"})
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", CHATS, idempotency_key="chat-b")
        upload_table = _Table()
        upload_ledger = DynamoDBQuotaLedger("metadata", table=upload_table, limits=QuotaLimits(uploads_per_month=1, upload_bytes_per_month=10))
        upload_ledger.reserve_upload("tenant-a", 10, idempotency_key="upload-a")
        upload_ledger.reserve_upload("tenant-a", 10, idempotency_key="upload-a")
        with self.assertRaises(QuotaExceededError):
            upload_ledger.reserve_upload("tenant-a", 1, idempotency_key="upload-b")
        self.assertNotIn("+", upload_table.updates[0]["ConditionExpression"])
        unavailable = DynamoDBQuotaLedger("metadata", table=_Table(fail_update=RuntimeError("network")))
        with self.assertRaises(QuotaUnavailableError):
            unavailable.reserve("tenant-a", CHATS)

    def test_configuration_is_strict(self) -> None:
        with self.assertRaises(ValueError):
            QuotaLimits.from_environment({"LEGALDESK_QUOTA_CHATS_PER_MONTH": "0"})
        with self.assertRaises(ValueError):
            QuotaLimits.from_environment({"LEGALDESK_QUOTA_CHATS_PER_MONTH": "1.5"})
        with self.assertRaises(ValueError):
            QuotaLimits(chats_per_month=True)

    def test_scope_binds_same_client_key_to_matter(self) -> None:
        ledger = InMemoryQuotaLedger(limits=QuotaLimits(ingestion_starts_per_month=2))
        ledger.reserve("tenant-a", INGESTION_STARTS, idempotency_key=scoped_idempotency_key(subject="alice", tenant_id="tenant-a", matter_id="matter-a", key="retry"))
        ledger.reserve("tenant-a", INGESTION_STARTS, idempotency_key=scoped_idempotency_key(subject="alice", tenant_id="tenant-a", matter_id="matter-b", key="retry"))
        with self.assertRaises(QuotaExceededError):
            ledger.reserve("tenant-a", INGESTION_STARTS, idempotency_key=scoped_idempotency_key(subject="alice", tenant_id="tenant-a", matter_id="matter-c", key="retry"))

    def test_gateway_is_not_called_after_exhaustion(self) -> None:
        class Gateway:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, *_args: object, **_kwargs: object) -> dict[str, object]:
                self.calls += 1
                return {"status": "SUCCESS", "tool": "unexpected", "result": {}, "correlationId": "corr"}

        ledger = InMemoryQuotaLedger(limits=QuotaLimits(gateway_per_month=1))
        ledger.reserve("tenant-a", GATEWAY)
        gateway = Gateway()
        app = LoopbackLegalDeskApp(SimpleNamespace(state_store=object(), quota=ledger, gateway_invoker=gateway))
        binding = SimpleNamespace(context=SimpleNamespace(tenant_id="tenant-a"), correlation_id="corr", identity=object(), matter_id="matter-a")
        with self.assertRaises(QuotaExceededError):
            app._invoke_gateway_tool(binding, tool_name="get_review_task", arguments={})
        self.assertEqual(gateway.calls, 0)

    def test_harness_is_not_called_after_exhaustion(self) -> None:
        class Harness:
            def __init__(self) -> None:
                self.calls = 0

            def invoke(self, *_args: object, **_kwargs: object) -> object:
                self.calls += 1
                return object()

        ledger = InMemoryQuotaLedger(limits=QuotaLimits(harness_per_month=1))
        ledger.reserve("tenant-a", HARNESS)
        harness = Harness()
        app = LoopbackLegalDeskApp(
            SimpleNamespace(state_store=object(), quota=ledger, harness_invoker=harness)
        )
        binding = SimpleNamespace(
            context=SimpleNamespace(tenant_id="tenant-a"),
            correlation_id="corr",
            identity=object(),
            matter_id="matter-a",
        )
        with patch(
            "legaldesk_agent.HarnessInvocationScope.from_derived",
            return_value=SimpleNamespace(),
        ), self.assertRaises(QuotaExceededError):
            app._invoke_harness_tool(binding, {}, application_action="metadata")
        self.assertEqual(harness.calls, 0)


if __name__ == "__main__":
    unittest.main()
