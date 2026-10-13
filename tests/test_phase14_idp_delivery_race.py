"""Publish-before-QUEUED race (live smoke): a fast worker can receive the SQS
message before the producer's post-publish QUEUED write lands.  The worker must
claim from the exact pre-publish state (DELIVERY_IN_FLIGHT) with the same CAS
fencing, without double-processing and without turning the producer ambiguous.
"""

from __future__ import annotations

import sys
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    Boto3DynamoIDPRepository,
    DocumentForIDP,
    IDPConcurrencyError,
    IDPConfig,
    IDPJob,
    IDPJobStatus,
    IDPWorker,
    InMemoryIDPRepository,
    create_verified_clean_job,
    enqueue_verified_clean_job,
    redeliver_ambiguous_job,
)
from legaldesk.idp.persistence import _job_item, job_sort_key, matter_partition_key  # noqa: E402
from tests.dynamodb_rules import as_boto3_item  # noqa: E402
from tests.test_phase14_idp_runtime_contract import _ConditionAwareTable  # noqa: E402

ARN = "arn:aws:sqs:eu-west-1:111122223333:legaldesk-idp"


def _document() -> DocumentForIDP:
    return DocumentForIDP(tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a", media_type="application/pdf", file_size_bytes=1024, malware_scan_clean=True, page_count=2, content_sha256="a" * 64)


class _FastWorkerQueue:
    """publish() hands the message to a worker immediately, before QUEUED."""

    def __init__(self, worker: IDPWorker | None = None) -> None:
        self.worker = worker
        self.seen_status: IDPJobStatus | None = None
        self.outcomes: list[object] = []
        self.repository: InMemoryIDPRepository | None = None

    def publish(self, *, job_id: str) -> None:
        assert self.repository is not None and self.worker is not None
        self.seen_status = self.repository.get_job(job_id).status  # type: ignore[union-attr]
        self.outcomes.append(self.worker.process_message({"jobId": job_id}))


def _boto_repository(job: IDPJob, status: IDPJobStatus) -> tuple[Boto3DynamoIDPRepository, _ConditionAwareTable]:
    table = _ConditionAwareTable()
    item = as_boto3_item(_job_item(replace(job, status=status)))
    table.items[(item["pk"], item["sk"])] = item
    # Locator row used by get_job(job_id).
    from legaldesk.idp.persistence import _locator_item, job_locator_partition_key, job_locator_sort_key
    table.items[(job_locator_partition_key(job.job_id), job_locator_sort_key())] = as_boto3_item(_locator_item(job))
    return Boto3DynamoIDPRepository("table", table=table, allow_non_atomic_test_adapter=True), table


class PublishBeforeQueuedTests(unittest.TestCase):
    def test_fast_worker_claims_from_delivery_in_flight_and_producer_is_not_ambiguous(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=_document())
        queue = _FastWorkerQueue()
        queue.repository = repository
        queue.worker = IDPWorker(repository=repository, document_lookup=lambda **_: _document(), config=IDPConfig(), expected_source_arn=ARN, worker_id="worker-a")
        returned = enqueue_verified_clean_job(repository=repository, queue=queue, job=job)  # type: ignore[arg-type]
        self.assertIs(queue.seen_status, IDPJobStatus.DELIVERY_IN_FLIGHT)
        stored = repository.get_job(job.job_id)
        self.assertEqual(stored.status, IDPJobStatus.PROCESSING)  # type: ignore[union-attr]
        self.assertEqual(stored.attempt, 1)  # type: ignore[union-attr]
        self.assertEqual(returned.status, IDPJobStatus.PROCESSING)
        self.assertEqual(len(queue.outcomes), 1)

    def test_duplicate_message_after_claim_does_not_claim_again(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=_document())
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.DELIVERY_IN_FLIGHT)
        worker = IDPWorker(repository=repository, document_lookup=lambda **_: _document(), config=IDPConfig(), expected_source_arn=ARN, worker_id="worker-a")
        worker.process_message({"jobId": job.job_id})
        with self.assertRaises(IDPConcurrencyError):
            worker.process_message({"jobId": job.job_id})
        self.assertEqual(repository.get_job(job.job_id).attempt, 1)  # type: ignore[union-attr]
        # The late producer finalisation cannot roll the claimed job back.
        with self.assertRaises(IDPConcurrencyError):
            repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.QUEUED)
        self.assertEqual(repository.get_job(job.job_id).status, IDPJobStatus.PROCESSING)  # type: ignore[union-attr]

    def test_unpublished_states_are_still_not_claimable(self) -> None:
        for status in (IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_AMBIGUOUS):
            with self.subTest(status=status):
                repository = InMemoryIDPRepository()
                job = create_verified_clean_job(repository=repository, document=_document())
                if status is IDPJobStatus.DELIVERY_AMBIGUOUS:
                    repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.DELIVERY_IN_FLIGHT)
                    repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.DELIVERY_AMBIGUOUS)
                with self.assertRaises(IDPConcurrencyError):
                    repository.claim_job(job_id=job.job_id, worker_id="w", lease_seconds=30)

    def test_boto_claim_from_delivery_in_flight_has_single_winner_and_fences_queued(self) -> None:
        job = IDPJob(job_id="job-1", tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a", document_sha256="a" * 64, idempotency_key="idem")
        repository, table = _boto_repository(job, IDPJobStatus.DELIVERY_IN_FLIGHT)
        outcomes: list[object] = []
        lock = threading.Lock()

        def claim(name: str) -> None:
            try:
                result = repository.claim_job(job_id=job.job_id, worker_id=name, lease_seconds=30, now=datetime.now(timezone.utc))
            except IDPConcurrencyError as exc:
                result = exc
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=claim, args=(f"w{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sum(not isinstance(outcome, IDPConcurrencyError) for outcome in outcomes), 1)
        stored = table.items[(matter_partition_key(job.tenant_id, job.matter_id), job_sort_key(job.job_id))]
        self.assertEqual((stored["status"], int(stored["attempt"])), (IDPJobStatus.CLAIMED.value, 1))
        with self.assertRaises(IDPConcurrencyError):
            repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.QUEUED)

    def test_boto_ambiguous_and_pending_are_not_claimable(self) -> None:
        job = IDPJob(job_id="job-2", tenant_id="tenant-a", matter_id="matter-a", document_id="doc-a", document_sha256="a" * 64, idempotency_key="idem")
        for status in (IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_AMBIGUOUS):
            with self.subTest(status=status):
                repository, _ = _boto_repository(job, status)
                with self.assertRaises(IDPConcurrencyError):
                    repository.claim_job(job_id=job.job_id, worker_id="w", lease_seconds=30)

    def test_reconciler_does_not_republish_a_job_already_claimed(self) -> None:
        repository = InMemoryIDPRepository()
        job = create_verified_clean_job(repository=repository, document=_document())
        repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.DELIVERY_IN_FLIGHT)
        repository.claim_job(job_id=job.job_id, worker_id="w", lease_seconds=30)

        class Queue:
            published = 0

            def publish(self, *, job_id: str) -> None:
                self.published += 1

        queue = Queue()
        with self.assertRaises(Exception):
            redeliver_ambiguous_job(repository=repository, queue=queue, job_id=job.job_id)  # type: ignore[arg-type]
        self.assertEqual(queue.published, 0)


if __name__ == "__main__":
    unittest.main()
