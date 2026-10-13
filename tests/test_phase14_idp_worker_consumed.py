from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "backend" / "src"))

from legaldesk.idp import (  # noqa: E402
    DocumentForIDP,
    IDPCheckpoint,
    IDPConfig,
    IDPJobStatus,
    InMemoryIDPRepository,
    IDPSourceArnError,
    IDPWorker,
    create_verified_clean_job,
    enqueue_verified_clean_job,
)
from legaldesk.idp_lambda import Boto3IDPSQSQueue, IDPLambdaConfigurationError  # noqa: E402


SOURCE_ARN = "arn:aws:sqs:eu-west-1:123456789012:legaldesk-idp"


def _document() -> DocumentForIDP:
    return DocumentForIDP(
        tenant_id="tenant-a",
        matter_id="matter-a",
        document_id="doc-a",
        media_type="application/pdf",
        file_size_bytes=1024,
        malware_scan_clean=True,
        page_count=1,
        content_sha256="a" * 64,
    )


class _Queue:
    def publish(self, *, job_id: str) -> str:
        return "message-from-test"


class _PaidProcessor:
    def __init__(self) -> None:
        self.calls = 0

    def process(self, *, job, document, claim):
        self.calls += 1
        return job


def _worker(repository: InMemoryIDPRepository, processor=None) -> IDPWorker:
    return IDPWorker(
        repository=repository,
        document_lookup=lambda **kwargs: _document(),
        config=IDPConfig(),
        expected_source_arn=SOURCE_ARN,
        worker_id="worker-test",
        processor=processor,
    )


class WorkerConsumedTelemetryTests(unittest.TestCase):
    def _queued_job(self, repository: InMemoryIDPRepository):
        job = create_verified_clean_job(repository=repository, document=_document())
        enqueue_verified_clean_job(repository=repository, queue=_Queue(), job=job)
        return job

    def test_success_emits_only_bounded_ack_metadata_after_authoritative_processing(self) -> None:
        repository = InMemoryIDPRepository()
        job = self._queued_job(repository)
        output = io.StringIO()
        with redirect_stdout(output):
            result = _worker(repository).handle_sqs_event({"Records": [{
                "messageId": "message-1",
                "eventSourceARN": SOURCE_ARN,
                "body": json.dumps({"jobId": job.job_id}),
            }]})
        self.assertEqual(result, {"batchItemFailures": []})
        event = json.loads(output.getvalue().strip())
        self.assertEqual(set(event), {"event", "jobId", "messageId", "outcome", "status", "timestamp"})
        self.assertEqual(event["event"], "idp_worker_consumed")
        self.assertEqual(event["jobId"], job.job_id)
        self.assertEqual(event["messageId"], "message-1")
        self.assertEqual(event["outcome"], "acknowledged")
        self.assertEqual(event["status"], IDPJobStatus.PROCESSING.value)
        self.assertIsInstance(event["timestamp"], int)

    def test_failed_record_is_partial_batch_failure_without_ack_event(self) -> None:
        repository = InMemoryIDPRepository()
        job = self._queued_job(repository)
        output = io.StringIO()
        # The lookup is intentionally unauthorized/missing.  The worker must
        # not claim acknowledgement merely because the SQS record was read.
        worker = IDPWorker(
            repository=repository,
            document_lookup=lambda **kwargs: None,
            config=IDPConfig(),
            expected_source_arn=SOURCE_ARN,
            worker_id="worker-test",
        )
        with redirect_stdout(output):
            result = worker.handle_sqs_event({"Records": [{
                "messageId": "message-fail",
                "eventSourceARN": SOURCE_ARN,
                "body": json.dumps({"jobId": job.job_id}),
            }]})
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "message-fail"}]})
        self.assertEqual(output.getvalue(), "")

    def test_terminal_duplicate_is_acknowledged_without_paid_processor(self) -> None:
        repository = InMemoryIDPRepository()
        job = self._queued_job(repository)
        terminal = replace(job, status=IDPJobStatus.COMPLETED, checkpoint=IDPCheckpoint.DONE)
        repository._jobs[job.job_id] = terminal  # type: ignore[attr-defined]
        processor = _PaidProcessor()
        output = io.StringIO()
        with redirect_stdout(output):
            result = _worker(repository, processor).handle_sqs_event({"Records": [{
                "messageId": "message-terminal",
                "eventSourceARN": SOURCE_ARN,
                "body": json.dumps({"jobId": job.job_id}),
            }]})
        self.assertEqual(result, {"batchItemFailures": []})
        self.assertEqual(processor.calls, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], IDPJobStatus.COMPLETED.value)

    def test_wrong_source_fails_closed_without_ack_event(self) -> None:
        repository = InMemoryIDPRepository()
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(IDPSourceArnError):
                _worker(repository).handle_sqs_event({"Records": [{
                    "messageId": "message-wrong-source",
                    "eventSourceARN": "arn:aws:sqs:wrong",
                    "body": "{}",
                }]})
        self.assertEqual(output.getvalue(), "")


class QueueMessageIdTests(unittest.TestCase):
    def test_queue_adapter_returns_actual_send_message_id(self) -> None:
        class Client:
            def send_message(self, **kwargs):
                self.kwargs = kwargs
                return {"MessageId": "sqs-message-1"}

        client = Client()
        queue = Boto3IDPSQSQueue(client, queue_url="https://sqs.eu-west-1.amazonaws.com/123/idp")
        self.assertEqual(queue.publish(job_id="job-1"), "sqs-message-1")
        self.assertEqual(json.loads(client.kwargs["MessageBody"]), {"jobId": "job-1"})

    def test_queue_adapter_rejects_missing_send_message_id(self) -> None:
        class Client:
            def send_message(self, **kwargs):
                return {}

        with self.assertRaises(IDPLambdaConfigurationError):
            Boto3IDPSQSQueue(Client(), queue_url="https://sqs.eu-west-1.amazonaws.com/123/idp").publish(job_id="job-1")


if __name__ == "__main__":
    unittest.main()
