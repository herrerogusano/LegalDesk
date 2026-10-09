"""Focused clean-intent recovery and conditional-write contracts."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from legaldesk.idp.models import IDPConfig, IDPConcurrencyError, IDPJob, IDPJobStatus  # noqa: E402
from legaldesk.idp.persistence import Boto3DynamoIDPRepository, InMemoryIDPRepository  # noqa: E402
from legaldesk.idp.trigger import VerifiedCleanIDPTrigger  # noqa: E402
from legaldesk.idp.worker import build_verified_clean_job  # noqa: E402
from legaldesk.domain.models import DocumentStatus, MalwareScanStatus  # noqa: E402


TENANT = "tenant-clean"
MATTER = "matter-clean"
DOCUMENT = "document-clean"
HASH = "a" * 64


def _job(*, job_id: str = "job-initial", idem: str = "idem-initial", status: IDPJobStatus = IDPJobStatus.ENQUEUE_PENDING) -> IDPJob:
    return IDPJob(
        job_id=job_id,
        tenant_id=TENANT,
        matter_id=MATTER,
        document_id=DOCUMENT,
        document_sha256=HASH,
        idempotency_key=idem,
        model_id="model",
        prompt_version="prompt",
        status=status,
    )


class _Queue:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, *, job_id: str) -> None:
        self.published.append(job_id)


class _Metadata:
    def __init__(self, document) -> None:
        self.document = document

    def list_for_scope_page(self, *, tenant_id: str, matter_id: str, limit: int, cursor=None):
        return ((self.document,), None)


class _Storage:
    def head_object(self, *, key: str):
        return {"ContentLength": 1, "Metadata": {"legaldesk-sha256": HASH}}


class CleanIntentTests(unittest.TestCase):
    def test_inmemory_same_initial_intent_is_idempotent_after_terminal_completion(self) -> None:
        repository = InMemoryIDPRepository()
        original = _job()
        repository.record_clean_intent(job=original)
        repository.create_job(original)
        completed = _job(status=IDPJobStatus.COMPLETED)
        repository._jobs[original.job_id] = completed  # type: ignore[attr-defined]

        repository.record_clean_intent(job=original)

        self.assertEqual(repository._clean_intents[(TENANT, MATTER, DOCUMENT)].idempotency_key, original.idempotency_key)  # type: ignore[attr-defined]
        self.assertEqual(repository.get_job(original.job_id).status, IDPJobStatus.COMPLETED)

    def test_inmemory_old_initial_event_cannot_replace_active_reprocess_intent(self) -> None:
        repository = InMemoryIDPRepository()
        active = _job(job_id="operator-job", idem="operator-generation")
        stale = _job(job_id="old-job", idem="initial-generation")
        repository.record_clean_intent(job=active)

        with self.assertRaises(IDPConcurrencyError):
            repository.record_clean_intent(job=stale)
        self.assertEqual(repository._clean_intents[(TENANT, MATTER, DOCUMENT)].job_id, active.job_id)  # type: ignore[attr-defined]

    def test_boto_terminal_document_is_not_demoted_by_recovery_event(self) -> None:
        import boto3

        resource = boto3.resource("dynamodb", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9", aws_access_key_id="offline", aws_secret_access_key="offline")
        table = resource.Table("table")
        table.get_item = Mock(side_effect=[{}, {"Item": {
            "documentId": DOCUMENT,
            "s3Key": "tenants/t/document.pdf",
            "idpDocumentSha256": HASH,
            "idpRunId": "completed-run",
            "idpStatus": "IDP_COMPLETED",
        }}])
        table.update_item = Mock()
        repository = Boto3DynamoIDPRepository("table", table=table)

        with self.assertRaises(IDPConcurrencyError):
            repository.record_clean_intent(job=_job())
        table.update_item.assert_not_called()

    def test_boto_initial_intent_condition_fences_another_generation(self) -> None:
        import boto3

        resource = boto3.resource("dynamodb", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9", aws_access_key_id="offline", aws_secret_access_key="offline")
        table = resource.Table("table")
        table.get_item = Mock(side_effect=[{}, {"Item": {
            "documentId": DOCUMENT,
            "s3Key": "tenants/t/document.pdf",
            "idpDocumentSha256": HASH,
            "idpCleanIntent": {"idempotencyKey": "operator-generation"},
        }}])
        repository = Boto3DynamoIDPRepository("table", table=table)

        with self.assertRaises(IDPConcurrencyError):
            repository.record_clean_intent(job=_job())

    def test_boto_initial_write_has_idempotency_condition_under_real_resource_serialization(self) -> None:
        import boto3

        resource = boto3.resource("dynamodb", region_name="eu-west-1", endpoint_url="http://127.0.0.1:9", aws_access_key_id="offline", aws_secret_access_key="offline")
        table = resource.Table("table")
        table.get_item = Mock(side_effect=[{}, {"Item": {
            "documentId": DOCUMENT,
            "s3Key": "tenants/t/document.pdf",
            "idpDocumentSha256": HASH,
        }}])
        captured: dict[str, object] = {}

        class AbortBeforeNetwork(Exception):
            pass

        def before_call(model, params, **kwargs):
            captured.update(params)
            raise AbortBeforeNetwork()

        table.meta.client.meta.events.register("before-call.dynamodb.UpdateItem", before_call)
        repository = Boto3DynamoIDPRepository("table", table=table)
        with self.assertRaises(IDPConcurrencyError):
            repository.record_clean_intent(job=_job())

        request = json.loads(captured["body"])
        condition = request["ConditionExpression"]
        self.assertIn("#intent.#idempotencyKey = :idempotency", condition)
        values = request["ExpressionAttributeValues"]
        self.assertEqual(values[":pending"], {"S": "ENQUEUE_PENDING"})

    def test_trigger_does_not_create_or_publish_when_old_clean_event_loses_race(self) -> None:
        class Repository:
            def record_clean_intent(self, *, job):
                raise IDPConcurrencyError("another generation is authoritative")

            def create_job(self, job):
                raise AssertionError("must not create a second generation")

        queue = _Queue()
        trigger = VerifiedCleanIDPTrigger(repository=Repository(), queue=queue, config=IDPConfig(), enabled=True, model_id="model", prompt_version="prompt")
        result = trigger.on_verified_clean(tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT, media_type="application/pdf", file_size_bytes=1, content_sha256=HASH)

        self.assertIsNone(result)
        self.assertEqual(queue.published, [])

    def test_recovery_reuses_completed_initial_job_without_reset_or_publish(self) -> None:
        document = SimpleNamespace(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            media_type="application/pdf", file_size_bytes=1,
            s3_key="tenants/t/document.pdf", status=DocumentStatus.INDEXED,
            malware_scan_status=MalwareScanStatus.CLEAN,
        )
        document_for_idp = SimpleNamespace(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            media_type="application/pdf", file_size_bytes=1,
            malware_scan_clean=True, content_sha256=HASH,
            source_key=document.s3_key,
        )
        repository = InMemoryIDPRepository()
        initial = build_verified_clean_job(document=document_for_idp, model_id="model", prompt_version="prompt")
        repository.record_clean_intent(job=initial)
        repository.create_job(initial)
        repository._jobs[initial.job_id] = _job(job_id=initial.job_id, idem=initial.idempotency_key, status=IDPJobStatus.COMPLETED)  # type: ignore[attr-defined]
        queue = _Queue()
        trigger = VerifiedCleanIDPTrigger(repository=repository, queue=queue, config=IDPConfig(), enabled=True, model_id="model", prompt_version="prompt")

        recovered = trigger.recover_clean_documents(metadata_repository=_Metadata(document), object_storage=_Storage(), tenant_id=TENANT, matter_id=MATTER, limit=1)

        self.assertEqual(recovered, ())
        self.assertEqual(repository.get_job(initial.job_id).status, IDPJobStatus.COMPLETED)
        self.assertEqual(queue.published, [])

    def test_recovery_does_not_replace_active_operator_generation(self) -> None:
        document = SimpleNamespace(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            media_type="application/pdf", file_size_bytes=1,
            s3_key="tenants/t/document.pdf", status=DocumentStatus.INDEXED,
            malware_scan_status=MalwareScanStatus.CLEAN,
        )
        document_for_idp = SimpleNamespace(
            tenant_id=TENANT, matter_id=MATTER, document_id=DOCUMENT,
            media_type="application/pdf", file_size_bytes=1,
            malware_scan_clean=True, content_sha256=HASH,
            source_key=document.s3_key,
        )
        repository = InMemoryIDPRepository()
        operator = build_verified_clean_job(document=document_for_idp, model_id="model", prompt_version="prompt", generation="operator")
        repository.record_clean_intent(job=operator)
        repository.create_job(operator)
        queue = _Queue()
        trigger = VerifiedCleanIDPTrigger(repository=repository, queue=queue, config=IDPConfig(), enabled=True, model_id="model", prompt_version="prompt")

        recovered = trigger.recover_clean_documents(metadata_repository=_Metadata(document), object_storage=_Storage(), tenant_id=TENANT, matter_id=MATTER, limit=1)

        self.assertEqual(recovered, ())
        self.assertEqual(repository._clean_intents[(TENANT, MATTER, DOCUMENT)].job_id, operator.job_id)  # type: ignore[attr-defined]
        self.assertEqual(queue.published, [])


if __name__ == "__main__":
    unittest.main()
