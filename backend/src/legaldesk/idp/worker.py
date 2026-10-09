"""Trusted IDP delivery and state-only SQS worker foundation.

This block intentionally performs no OCR or Bedrock calls.  It establishes the
security boundary, skip behavior and paid-call checkpoint contract used by
later processing blocks.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from hashlib import sha256
from typing import Any, Mapping, Protocol

from .models import (
    DocumentForIDP,
    IDPCheckpoint,
    IDPClaim,
    IDPConfig,
    IDPContractError,
    IDPDeliveryAmbiguous,
    IDPJob,
    IDPJobStatus,
    IDPSkipReason,
    IDP_SCHEMA_VERSION,
    new_id,
)
from .persistence import AuthoritativeJobLocator, IDPRepository


class IDPQueue(Protocol):
    def publish(self, *, job_id: str) -> str: ...


class IDPDocumentLookup(Protocol):
    def __call__(self, *, tenant_id: str, matter_id: str, document_id: str) -> DocumentForIDP | None: ...


class IDPProcessor(Protocol):
    def process(self, *, job: IDPJob, document: DocumentForIDP, claim: IDPClaim) -> IDPJob: ...


class IDPWorkerError(RuntimeError):
    pass


class IDPSourceArnError(IDPWorkerError):
    pass


class IDPTransientError(IDPWorkerError):
    pass


def idempotency_key(*, document: DocumentForIDP, schema_version: str = IDP_SCHEMA_VERSION, model_id: str = "", prompt_version: str = "", generation: str = "initial") -> str:
    """Stable, scoped key that changes for content/config/reprocess changes."""

    if not document.content_sha256:
        raise IDPContractError("server-verified content SHA-256 is required")
    raw = "|".join((document.tenant_id, document.matter_id, document.document_id, document.content_sha256, schema_version, model_id, prompt_version, generation))
    return sha256(raw.encode("utf-8")).hexdigest()


def create_verified_clean_job(*, repository: IDPRepository, document: DocumentForIDP, schema_version: str = IDP_SCHEMA_VERSION, model_id: str = "", prompt_version: str = "", generation: str = "initial", max_attempts: int = 3) -> IDPJob:
    """Create an IDP job only after the caller's clean-promotion trust check."""

    if not document.malware_scan_clean:
        raise IDPContractError("IDP job requires a verified clean document")
    job = build_verified_clean_job(document=document, schema_version=schema_version, model_id=model_id, prompt_version=prompt_version, generation=generation, max_attempts=max_attempts)
    record_intent = getattr(repository, "record_clean_intent", None)
    if callable(record_intent):
        record_intent(job=job)
    return repository.create_job(job)


def build_verified_clean_job(*, document: DocumentForIDP, schema_version: str = IDP_SCHEMA_VERSION, model_id: str = "", prompt_version: str = "", generation: str = "initial", max_attempts: int = 3) -> IDPJob:
    if not document.malware_scan_clean:
        raise IDPContractError("IDP job requires a verified clean document")
    return IDPJob(
        job_id=new_id(), tenant_id=document.tenant_id, matter_id=document.matter_id,
        document_id=document.document_id, document_sha256=document.content_sha256 or "",
        idempotency_key=idempotency_key(document=document, schema_version=schema_version, model_id=model_id, prompt_version=prompt_version, generation=generation),
        schema_version=schema_version, model_id=model_id, prompt_version=prompt_version,
        max_attempts=max_attempts,
    )


def _publish_and_finalize(*, repository: IDPRepository, queue: IDPQueue, current: IDPJob) -> IDPJob:
    try:
        queue.publish(job_id=current.job_id)
    except Exception as exc:
        try:
            repository.mark_delivery(job_id=current.job_id, status=IDPJobStatus.DELIVERY_AMBIGUOUS)
        except Exception:
            pass
        raise IDPDeliveryAmbiguous("IDP queue publish outcome is unknown") from exc
    try:
        return repository.mark_delivery(job_id=current.job_id, status=IDPJobStatus.QUEUED)
    except Exception as exc:
        # SQS may already contain the message.  Marking ambiguous makes a
        # bounded reconciler eligible to resend the same idempotent job.
        try:
            repository.mark_delivery(job_id=current.job_id, status=IDPJobStatus.DELIVERY_AMBIGUOUS)
        except Exception:
            pass
        raise IDPDeliveryAmbiguous("IDP queue commit outcome is unknown") from exc


def enqueue_verified_clean_job(*, repository: IDPRepository, queue: IDPQueue, job: IDPJob) -> IDPJob:
    """Deliver one durable job intent; unknown outcomes become recoverable."""

    current = repository.mark_delivery(job_id=job.job_id, status=IDPJobStatus.DELIVERY_IN_FLIGHT)
    return _publish_and_finalize(repository=repository, queue=queue, current=current)


def redeliver_ambiguous_job(*, repository: IDPRepository, queue: IDPQueue, job_id: str) -> IDPJob:
    """Explicit bounded reconciliation action; never an automatic retry loop."""

    current = repository.get_job(job_id)
    if current is None or current.status not in (IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_AMBIGUOUS, IDPJobStatus.DELIVERY_IN_FLIGHT):
        raise IDPContractError("IDP delivery is not eligible for reconciliation")
    if current.status is IDPJobStatus.ENQUEUE_PENDING:
        return enqueue_verified_clean_job(repository=repository, queue=queue, job=current)
    return _publish_and_finalize(repository=repository, queue=queue, current=current)


def preflight_document(*, document: DocumentForIDP, config: IDPConfig) -> IDPSkipReason | None:
    """Return an explicit skip reason without changing upload/RAG state."""

    if document.media_type.lower() != "application/pdf":
        return IDPSkipReason.UNSUPPORTED_MEDIA_TYPE
    if document.file_size_bytes > config.max_bytes:
        return IDPSkipReason.SIZE_LIMIT
    if document.page_count is not None and document.page_count > config.max_pages:
        return IDPSkipReason.PAGE_LIMIT
    if not document.malware_scan_clean:
        return IDPSkipReason.MALWARE_NOT_CLEAN
    return None


class IDPPaidCallGate:
    """Explicit checkpoint boundary preventing automatic duplicate paid calls."""

    def __init__(self, repository: IDPRepository, claim: IDPClaim) -> None:
        self.repository = repository
        self.claim = claim

    def ready(self) -> None:
        self.repository.checkpoint_job(claim=self.claim, checkpoint=IDPCheckpoint.PAID_CALL_READY)

    def begin(self) -> None:
        current = self.repository.get_job(self.claim.job_id)
        if current is not None and current.checkpoint is IDPCheckpoint.PAID_CALL_IN_FLIGHT:
            raise IDPContractError("paid call is already in flight")
        self.repository.checkpoint_job(claim=self.claim, checkpoint=IDPCheckpoint.PAID_CALL_IN_FLIGHT)

    def ambiguous(self) -> None:
        self.repository.checkpoint_job(claim=self.claim, checkpoint=IDPCheckpoint.AMBIGUOUS, status=IDPJobStatus.FAILED)

    def committed(self) -> None:
        self.repository.checkpoint_job(claim=self.claim, checkpoint=IDPCheckpoint.PAID_CALL_COMMITTED)


class IDPWorker:
    def __init__(self, *, repository: IDPRepository, document_lookup: IDPDocumentLookup, config: IDPConfig, expected_source_arn: str, worker_id: str, processor: IDPProcessor | None = None) -> None:
        if not expected_source_arn.strip() or not worker_id.strip():
            raise IDPContractError("worker configuration is invalid")
        self.repository = repository
        self.locator = AuthoritativeJobLocator(repository, document_lookup)
        self.config = config
        self.expected_source_arn = expected_source_arn
        self.worker_id = worker_id
        self.processor = processor

    def handle_sqs_event(self, event: Mapping[str, object]) -> dict[str, list[dict[str, str]]]:
        records = event.get("Records") if isinstance(event, Mapping) else None
        if not isinstance(records, list) or not records:
            raise IDPContractError("SQS event is invalid")
        failures: list[dict[str, str]] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise IDPContractError("SQS record is invalid")
            message_id = record.get("messageId")
            source_arn = record.get("eventSourceARN")
            if not isinstance(message_id, str) or not message_id.strip() or source_arn != self.expected_source_arn:
                # A wrong queue source is a security/configuration error, not a
                # normal message failure that may be retried indefinitely.
                raise IDPSourceArnError("SQS source ARN is not authorized")
            try:
                body = json.loads(record.get("body", ""))
                if not isinstance(body, Mapping):
                    raise IDPContractError("SQS body is invalid")
                processed_job = self.process_message(body)
                # This is deliberately emitted only after the authoritative
                # repository path returns.  It contains enough bounded
                # metadata for duplicate-delivery observation, never payload,
                # document text, tenant/matter, or model output.
                self._emit_consumed(message_id=message_id, job=processed_job)
            except IDPSourceArnError:
                raise
            except Exception:
                failures.append({"itemIdentifier": message_id})
        return {"batchItemFailures": failures}

    @staticmethod
    def _emit_consumed(*, message_id: str, job: IDPJob) -> None:
        print(
            json.dumps(
                {
                    "event": "idp_worker_consumed",
                    "jobId": job.job_id,
                    "messageId": message_id,
                    "outcome": "acknowledged",
                    "status": job.status.value,
                    "timestamp": int(time.time() * 1000),
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
            flush=True,
        )

    def process_message(self, payload: Mapping[str, object]) -> IDPJob:
        job, document = self.locator.locate(payload)
        # At-least-once delivery is expected.  Terminal jobs are acknowledged
        # without touching the document or issuing another paid call.
        if job.status in (IDPJobStatus.SKIPPED, IDPJobStatus.REVIEW_REQUIRED, IDPJobStatus.COMPLETED, IDPJobStatus.FAILED, IDPJobStatus.DELIVERY_AMBIGUOUS):
            return job
        if job.attempt >= job.max_attempts:
            exhaust = getattr(self.repository, "fail_exhausted", None)
            if callable(exhaust):
                failed = exhaust(job_id=job.job_id)
                project = getattr(self.repository, "project_document_job_status", None)
                if callable(project):
                    project(job=failed, source_key=getattr(document, "source_key", None))
                return failed
        claim = self.repository.claim_job(job_id=job.job_id, worker_id=self.worker_id, lease_seconds=self.config.visibility_timeout_seconds)
        reason = preflight_document(document=document, config=self.config)
        if reason is not None:
            skipped = self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.VALIDATED, status=IDPJobStatus.SKIPPED, skip_reason=reason)
            project = getattr(self.repository, "project_document_job_status", None)
            if callable(project):
                project(job=skipped, source_key=getattr(document, "source_key", None))
            return skipped
        if self.processor is not None:
            validated = self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.VALIDATED, status=IDPJobStatus.PROCESSING)
            return self.processor.process(job=validated, document=document, claim=claim)
        # Injectable foundation mode intentionally stops before paid work.
        return self.repository.checkpoint_job(claim=claim, checkpoint=IDPCheckpoint.PAID_CALL_READY, status=IDPJobStatus.PROCESSING)


__all__ = [name for name in globals() if not name.startswith("_")]
