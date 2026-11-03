"""Phase 15 (T4.1, T4.3, T4.4): chat job store state machine, on both stores."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from dynamodb_rules import as_boto3_item, enforce_expression_rules
from legaldesk.agent_jobs import (
    ATTEMPT_WINDOW_SECONDS,
    JOB_TOTAL_DEADLINE_SECONDS,
    JOB_TTL_SECONDS,
    ChatJob,
    ClaimOutcome,
    DynamoDBChatJobStore,
    FailureCode,
    InMemoryChatJobStore,
    JobDeletion,
    JobKey,
    JobStatus,
    new_job,
)

T0 = 1_800_000_000


class _Conditional(Exception):
    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeJobTable:
    """Evaluates the exact condition and update expressions the store sends."""

    def __init__(self, page_size: int = 1000) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}
        self.requests: list[dict[str, object]] = []
        self.page_size = page_size  # DynamoDB ends a page at 1 MB of evaluated data, not at the Limit
        self.queries = 0

    @staticmethod
    def _condition(expression: str, item: dict, names: dict, values: dict) -> bool:
        if not item:
            return False if "attribute_not_exists" not in expression else True
        text = expression.replace("attribute_not_exists(pk)", "False")
        text = re.sub(r"#(\w+)", lambda m: f"N('#{m[1]}')", text)
        text = re.sub(r":(\w+)", lambda m: f"V(':{m[1]}')", text)
        text = re.sub(r"(?<![<>!=])=(?!=)", "==", text).replace(" AND ", " and ").replace(" IN ", " in ")
        scope = {"N": lambda alias: item.get(names[alias]), "V": lambda alias: values[alias]}
        try:
            return bool(eval(text, {"__builtins__": {}}, scope))  # noqa: S307 - test-only evaluator over fixed expressions
        except TypeError:
            return False  # comparison with a missing attribute is false in DynamoDB

    @staticmethod
    def _apply(expression: str, item: dict, names: dict, values: dict) -> None:
        set_part, _, remove_part = expression.partition(" REMOVE ")
        if set_part.startswith("SET "):
            for assignment in set_part[4:].split(", "):
                target, _, source = assignment.partition(" = ")
                if " + " in source:
                    left, _, right = source.partition(" + ")
                    item[names[target]] = int(item[names[left]]) + int(values[right])
                else:
                    item[names[target]] = values[source]
        for alias in filter(None, (part.strip() for part in remove_part.split(","))):
            item.pop(names[alias], None)

    def put_item(self, *, Item, ConditionExpression=None):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression and key in self.items:
            raise _Conditional()
        self.items[key] = as_boto3_item(dict(Item))

    def get_item(self, *, Key, **_):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item is not None else {}

    def update_item(self, *, Key, **kwargs):
        enforce_expression_rules(kwargs)
        self.requests.append(kwargs)
        item = self.items.get((Key["pk"], Key["sk"]))
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        if item is None or not self._condition(kwargs["ConditionExpression"], item, names, values):
            raise _Conditional()
        self._apply(kwargs["UpdateExpression"], item, names, values)
        return {"Attributes": dict(item)}

    def delete_item(self, *, Key, **_):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def query(self, **kwargs):
        self.queries += 1
        pk = kwargs["KeyConditionExpression"].get_expression()["values"][0].get_expression()["values"][1]
        found = sorted(({"pk": k[0], "sk": k[1]} for k in self.items if k[0] == pk), key=lambda item: item["sk"])
        start = kwargs.get("ExclusiveStartKey")
        offset = next((i + 1 for i, item in enumerate(found) if start and item["sk"] == start["sk"]), 0)
        page = found[offset: offset + self.page_size]
        response = {"Items": page}
        if offset + self.page_size < len(found):
            response["LastEvaluatedKey"] = dict(page[-1])
        return response

    def scan(self, **_):
        raise AssertionError("job store must never scan")


def make_job(job_id="job-1", *, tenant="tenant-a", matter="matter-a", subject="alice", now=T0) -> ChatJob:
    return new_job(
        job_id=job_id, subject=subject, tenant_id=tenant, matter_id=matter, conversation_id="conv-1",
        session_selector="sel-1", correlation_id="corr-1", question="¿Qué documentos hay?", session_ref="a" * 64, now=now,
    )


class JobStoreContract:
    """Same assertions for every store implementation."""

    def make_store(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def setUp(self) -> None:
        self.store = self.make_store()
        self.job = make_job()
        self.key = self.job.key
        self.store.create(self.job)

    def test_new_job_has_ttl_deadline_and_scope(self) -> None:
        job = self.store.get(self.key)
        self.assertEqual((job.status, job.attempt), (JobStatus.QUEUED, 0))
        self.assertEqual(job.deadline, T0 + JOB_TOTAL_DEADLINE_SECONDS)
        self.assertEqual(job.expires_at, T0 + JOB_TTL_SECONDS)
        self.assertEqual((job.subject, job.tenant_id, job.matter_id, job.conversation_id), ("alice", "tenant-a", "matter-a", "conv-1"))

    def test_claim_starts_once_and_ignores_a_duplicate_during_a_live_attempt(self) -> None:
        outcome, job = self.store.claim(self.key, now=T0 + 1)
        self.assertEqual((outcome, job.status, job.attempt), (ClaimOutcome.STARTED, JobStatus.RUNNING, 1))
        outcome, _ = self.store.claim(self.key, now=T0 + 2)
        self.assertEqual(outcome, ClaimOutcome.DUPLICATE)
        self.assertEqual(self.store.get(self.key).attempt, 1)

    def test_one_retry_after_a_transient_error_and_never_two(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.assertFalse(self.store.retry_later(self.key, attempt=2))  # not the owning attempt
        self.assertTrue(self.store.retry_later(self.key, attempt=1))
        self.assertEqual(self.store.get(self.key).status, JobStatus.QUEUED)
        outcome, job = self.store.claim(self.key, now=T0 + 31)
        self.assertEqual((outcome, job.attempt), (ClaimOutcome.STARTED, 2))
        self.assertFalse(self.store.retry_later(self.key, attempt=2))  # the retry budget is spent

    def test_a_dead_attempt_is_taken_over_while_attempts_and_time_remain(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.assertEqual(self.store.claim(self.key, now=T0 + ATTEMPT_WINDOW_SECONDS)[0], ClaimOutcome.DUPLICATE)
        outcome, job = self.store.claim(self.key, now=T0 + 1 + ATTEMPT_WINDOW_SECONDS)
        self.assertEqual((outcome, job.attempt), (ClaimOutcome.STARTED, 2))
        self.assertEqual(self.store.claim(self.key, now=T0 + 200)[0], ClaimOutcome.DUPLICATE)  # attempt 2 is alive

    def test_a_second_dead_attempt_exhausts_the_job(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.store.retry_later(self.key, attempt=1)
        self.store.claim(self.key, now=T0 + 31)  # attempt 2, alive until T0 + 31 + window
        until = T0 + 31 + ATTEMPT_WINDOW_SECONDS
        self.assertEqual(self.store.claim(self.key, now=until - 1)[0], ClaimOutcome.DUPLICATE)
        outcome, job = self.store.claim(self.key, now=until)
        self.assertEqual((outcome, job.status, job.failure_code),
                         (ClaimOutcome.FAILED_NOW, JobStatus.FAILED, FailureCode.ATTEMPTS_EXHAUSTED))
        self.assertEqual(self.store.claim(self.key, now=until + 1)[0], ClaimOutcome.TERMINAL)

    def test_total_deadline_fails_the_job_exactly_once(self) -> None:
        outcome, job = self.store.claim(self.key, now=T0 + JOB_TOTAL_DEADLINE_SECONDS)
        self.assertEqual((outcome, job.failure_code), (ClaimOutcome.FAILED_NOW, FailureCode.DEADLINE_EXCEEDED))
        self.assertEqual(self.store.claim(self.key, now=T0 + JOB_TOTAL_DEADLINE_SECONDS + 1)[0], ClaimOutcome.TERMINAL)

    def test_finalize_and_complete_are_conditional_on_the_owning_attempt(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.assertFalse(self.store.complete(self.key, attempt=1, result={"x": 1}))  # not finalizing yet
        self.assertFalse(self.store.finalize(self.key, attempt=2, now=T0 + 2))
        self.assertTrue(self.store.finalize(self.key, attempt=1, now=T0 + 2))
        self.assertFalse(self.store.finalize(self.key, attempt=1, now=T0 + 2))  # side effects run once
        self.assertFalse(self.store.complete(self.key, attempt=2, result={"x": 1}))
        self.assertTrue(self.store.complete(self.key, attempt=1, result={"answer": "ok", "n": 2}))
        done = self.store.get(self.key)
        self.assertEqual((done.status, dict(done.result)), (JobStatus.SUCCEEDED, {"answer": "ok", "n": 2}))
        self.assertEqual((done.question, done.session_ref), (None, None))  # nothing sensitive is kept once done
        self.assertFalse(self.store.fail(self.key, FailureCode.INTERNAL_ERROR))
        self.assertFalse(self.store.retry_later(self.key, attempt=1))

    def test_finalize_refuses_after_the_deadline(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.assertFalse(self.store.finalize(self.key, attempt=1, now=T0 + JOB_TOTAL_DEADLINE_SECONDS))

    def test_fail_wins_once_and_clears_sensitive_fields(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.assertTrue(self.store.fail(self.key, FailureCode.SESSION_CLOSED))
        self.assertFalse(self.store.fail(self.key, FailureCode.AGENT_FAILED))  # the first writer owns the quota release
        failed = self.store.get(self.key)
        self.assertEqual((failed.status, failed.failure_code, failed.question, failed.session_ref),
                         (JobStatus.FAILED, FailureCode.SESSION_CLOSED, None, None))
        self.assertFalse(self.store.finalize(self.key, attempt=1, now=T0 + 2))
        self.assertFalse(self.store.complete(self.key, attempt=1, result={}))

    def test_a_late_worker_cannot_overwrite_an_expired_job(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        job, failed_now = self.store.expire_if_due(self.key, now=T0 + JOB_TOTAL_DEADLINE_SECONDS)
        self.assertTrue(failed_now)
        self.assertEqual(job.failure_code, FailureCode.DEADLINE_EXCEEDED)
        self.assertEqual(self.store.expire_if_due(self.key, now=T0 + 400)[1], False)
        self.assertFalse(self.store.finalize(self.key, attempt=1, now=T0 + 2))

    def test_expiry_check_leaves_healthy_jobs_alone(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        job, failed_now = self.store.expire_if_due(self.key, now=T0 + 60)
        self.assertEqual((job.status, failed_now), (JobStatus.RUNNING, False))

    def test_a_stuck_finalization_fails_instead_of_being_retried(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.store.finalize(self.key, attempt=1, now=T0 + 2)
        job, failed_now = self.store.expire_if_due(self.key, now=T0 + 2 + 60)
        self.assertEqual((job.status, job.failure_code, failed_now), (JobStatus.FAILED, FailureCode.INTERNAL_ERROR, True))

    def test_job_is_only_readable_under_its_own_tenant_and_matter(self) -> None:
        for tenant, matter in (("tenant-b", "matter-a"), ("tenant-a", "matter-b")):
            other = JobKey(tenant, matter, "job-1")
            self.assertIsNone(self.store.get(other))
            self.assertEqual(self.store.claim(other, now=T0 + 1)[0], ClaimOutcome.MISSING)
            self.assertFalse(self.store.fail(other, FailureCode.INTERNAL_ERROR))
        self.assertEqual(self.store.get(self.key).status, JobStatus.QUEUED)

    def test_ttl_hides_an_expired_record_even_before_dynamodb_deletes_it(self) -> None:
        self.assertIsNone(self.store.get(self.key, **self.after_ttl()))

    def test_matter_delete_removes_every_job_of_the_matter_only(self) -> None:
        self.store.create(make_job("job-2", subject="bob"))
        self.store.create(make_job("job-3", matter="matter-b"))
        self.store.create(make_job("job-4", tenant="tenant-b"))
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=10), JobDeletion(2, True))
        self.assertIsNone(self.store.get(self.key))
        self.assertIsNotNone(self.store.get(JobKey("tenant-a", "matter-b", "job-3")))
        self.assertIsNotNone(self.store.get(JobKey("tenant-b", "matter-a", "job-4")))

    def test_matter_with_more_jobs_than_the_limit_is_deleted_in_bounded_batches(self) -> None:
        for index in range(6):
            self.store.create(make_job(f"extra-{index}"))  # 7 jobs of matter-a
        self.store.create(make_job("keep", matter="matter-b"))
        batches = []
        while True:
            outcome = self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=3)
            batches.append(outcome)
            if outcome.complete:
                break
            self.assertEqual(outcome.removed, 3)  # a full batch, and it never claims to be finished
            self.assertLess(len(batches), 10)
        self.assertEqual([(b.removed, b.complete) for b in batches], [(3, False), (3, False), (1, True)])
        self.assertIsNone(self.store.get(self.key))
        self.assertIsNotNone(self.store.get(JobKey("tenant-a", "matter-b", "keep")))
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=3), JobDeletion(0, True))

    def test_exactly_the_limit_is_complete_and_one_more_is_not(self) -> None:
        self.store.create(make_job("job-b"))
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=3), JobDeletion(2, True))
        for index in range(4):
            self.store.create(make_job(f"again-{index}"))
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=3), JobDeletion(3, False))

    def test_matter_delete_rejects_an_invalid_limit(self) -> None:
        for bad in (0, -1, 101, True, "5"):
            with self.assertRaises(ValueError):
                self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=bad)  # type: ignore[arg-type]
        self.assertIsNotNone(self.store.get(self.key))

    def test_public_view_never_carries_scope_or_content(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        view = self.store.get(self.key).public_dict()
        self.assertEqual(set(view), {"jobId", "status", "conversationId", "correlationId"})
        self.store.fail(self.key, FailureCode.SESSION_CLOSED)
        self.assertEqual(self.store.get(self.key).public_dict()["error"], "session_closed")

    def test_duplicate_create_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.store.create(make_job())


class InMemoryJobStoreTests(JobStoreContract, unittest.TestCase):
    def make_store(self):
        self.now = T0
        return InMemoryChatJobStore(clock=lambda: self.now)

    def after_ttl(self):
        self.now = T0 + JOB_TTL_SECONDS
        return {}


class DynamoDBJobStoreTests(JobStoreContract, unittest.TestCase):
    def make_store(self):
        self.table = FakeJobTable()
        return DynamoDBChatJobStore("metadata", table=self.table)

    def after_ttl(self):
        return {"now": T0 + JOB_TTL_SECONDS}

    def test_every_transition_is_a_single_conditional_write(self) -> None:
        self.store.claim(self.key, now=T0 + 1)
        self.store.finalize(self.key, attempt=1, now=T0 + 2)
        self.store.complete(self.key, attempt=1, result={"a": 1})
        for request in self.table.requests:
            self.assertIn("ConditionExpression", request)
            self.assertIn("#entity = :entity", request["ConditionExpression"])

    def test_record_is_keyed_by_tenant_and_matter_and_expires_by_ttl(self) -> None:
        (pk, sk), item = next(iter(self.table.items.items()))
        self.assertIn("tenant-a#MATTER#matter-a", pk)
        self.assertEqual(sk, "JOB#job-1")
        self.assertEqual(int(item["ttl"]), T0 + JOB_TTL_SECONDS)

    def matter_a_keys(self):
        return [k for k in self.table.items if "matter-a" in k[0]]

    def test_matter_delete_follows_pages_to_the_end(self) -> None:
        self.table.page_size = 2  # a page can end long before the limit when jobs hold big results
        for index in range(4):
            self.store.create(make_job(f"extra-{index}"))  # 5 jobs of matter-a in total
        self.store.create(make_job("keep", matter="matter-b"))
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=10), JobDeletion(5, True))
        self.assertGreaterEqual(self.table.queries, 3)
        self.assertEqual(self.matter_a_keys(), [])
        self.assertIsNotNone(self.store.get(JobKey("tenant-a", "matter-b", "keep")))

    def test_over_the_limit_across_pages_deletes_a_batch_and_reports_incomplete(self) -> None:
        self.table.page_size = 2
        for index in range(4):
            self.store.create(make_job(f"extra-{index}"))
        first = self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=4)  # 5 > 4, seen on page 3
        self.assertEqual(first, JobDeletion(4, False))
        self.assertEqual(len(self.matter_a_keys()), 1)
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=4), JobDeletion(1, True))
        self.assertEqual(self.matter_a_keys(), [])

    def test_the_scan_stops_reading_once_it_knows_more_remain(self) -> None:
        self.table.page_size = 1
        for index in range(30):
            self.store.create(make_job(f"extra-{index}"))
        self.table.queries = 0
        self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=3)
        self.assertEqual(self.table.queries, 4)  # limit + 1 keys, not the whole partition

    def test_an_empty_last_page_still_ends_the_scan(self) -> None:
        self.table.page_size = 1
        self.assertEqual(self.store.delete_for_matter(tenant_id="tenant-a", matter_id="matter-a", limit=10), JobDeletion(1, True))

    def test_a_corrupt_record_is_not_served(self) -> None:
        (_pk, _sk), item = next(iter(self.table.items.items()))
        item["status"] = "bogus"
        self.assertIsNone(self.store.get(self.key))


if __name__ == "__main__":
    unittest.main()
