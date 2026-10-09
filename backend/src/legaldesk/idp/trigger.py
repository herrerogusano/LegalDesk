"""Verified-clean IDP trigger and bounded delivery recovery."""

from __future__ import annotations

from typing import Any, Mapping, Protocol
import hashlib
import re
import time

from .models import DocumentForIDP, IDPConfig, IDPContractError, IDPJob, IDPJobStatus
from .persistence import IDPRepository
from .worker import IDPQueue, build_verified_clean_job, enqueue_verified_clean_job, redeliver_ambiguous_job


class IDPTriggerError(RuntimeError):
    pass


class IDPDocumentJobQueue(Protocol):
    def publish(self, *, job_id: str) -> None: ...


class VerifiedCleanIDPTrigger:
    """Optional callback used only after the malware gate is terminal-clean."""

    def __init__(self, *, repository: IDPRepository, queue: IDPQueue, config: IDPConfig, enabled: bool = False, model_id: str = "", prompt_version: str = "", review_recovery: Any | None = None) -> None:
        self.repository, self.queue, self.config = repository, queue, config
        self.enabled, self.model_id, self.prompt_version, self.review_recovery = bool(enabled), model_id, prompt_version, review_recovery

    def on_verified_clean(self, *, tenant_id: str, matter_id: str, document_id: str, media_type: str, file_size_bytes: int, content_sha256: str | None, page_count: int | None = None) -> IDPJob | None:
        if not self.enabled:
            return None
        document = DocumentForIDP(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, media_type=media_type, file_size_bytes=file_size_bytes, malware_scan_clean=True, page_count=page_count, content_sha256=content_sha256)
        if not document.content_sha256:
            raise IDPTriggerError("clean document lacks a server-verified content hash")
        job = build_verified_clean_job(document=document, model_id=self.model_id, prompt_version=self.prompt_version, max_attempts=self.config.max_attempts)
        record_intent = getattr(self.repository, "record_clean_intent", None)
        if callable(record_intent):
            record_intent(job=job)
        job = self.repository.create_job(job)
        # A repeated clean event must not reopen an in-progress/terminal job
        # or issue a second dispatch.  The scheduled reconciler owns bounded
        # recovery of ambiguous delivery outcomes.
        if job.status is not IDPJobStatus.ENQUEUE_PENDING:
            return job
        return enqueue_verified_clean_job(repository=self.repository, queue=self.queue, job=job)

    def recover_delivery(self, *, tenant_id: str, matter_id: str, limit: int = 25, metadata_repository: Any | None = None, object_storage: Any | None = None) -> tuple[IDPJob, ...]:
        if not self.enabled:
            return ()
        deadline = time.monotonic() + self.config.global_deadline_seconds
        remaining = limit
        candidates = self.repository.list_delivery_candidates(tenant_id=tenant_id, matter_id=matter_id, limit=limit)
        recovered: list[IDPJob] = []
        for job in candidates.jobs:
            if remaining <= 0 or time.monotonic() >= deadline:
                break
            recovered.append(redeliver_ambiguous_job(repository=self.repository, queue=self.queue, job_id=job.job_id))
            remaining -= 1
        list_intents = getattr(self.repository, "list_clean_intents", None)
        if callable(list_intents):
            # Keep the explicit partition query on every scheduled pass so a
            # cursor-backed adapter can advance past terminal rows; the shared
            # work budget still prevents processing when it is exhausted.
            for intent in list_intents(tenant_id=tenant_id, matter_id=matter_id, limit=max(1, remaining)):
                if remaining <= 0 or time.monotonic() >= deadline:
                    break
                job = self.repository.create_job(intent)
                if job.status in {IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_IN_FLIGHT, IDPJobStatus.DELIVERY_AMBIGUOUS}:
                    recovered.append(enqueue_verified_clean_job(repository=self.repository, queue=self.queue, job=job) if job.status is IDPJobStatus.ENQUEUE_PENDING else redeliver_ambiguous_job(repository=self.repository, queue=self.queue, job_id=job.job_id))
                    remaining -= 1
        if metadata_repository is not None and object_storage is not None and remaining > 0 and time.monotonic() < deadline:
            recovered.extend(self.recover_clean_documents(metadata_repository=metadata_repository, object_storage=object_storage, tenant_id=tenant_id, matter_id=matter_id, limit=remaining))
        if callable(self.review_recovery):
            # Review dispatch recovery is a separate bounded, server-scoped
            # pass.  It only reuses durable run artifacts; it never re-enters
            # classifier/extractor stages.
            review_results = self.review_recovery(tenant_id=tenant_id, matter_id=matter_id, limit=max(1, remaining))
            if isinstance(review_results, (tuple, list)):
                recovered.extend(item for item in review_results if isinstance(item, IDPJob))
        return tuple(recovered)

    def recover_clean_documents(self, *, metadata_repository: Any, object_storage: Any, tenant_id: str, matter_id: str, limit: int = 25) -> tuple[IDPJob, ...]:
        """Recover a clean document whose job intent was lost before write.

        Discovery is one explicit metadata partition query.  S3 metadata/body
        is then revalidated per candidate, with a bounded read fallback; no
        table or object-store scan is used.
        """

        if not self.enabled or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise IDPTriggerError("IDP recovery limit is invalid")
        get_cursor = getattr(self.repository, "get_clean_recovery_cursor", None)
        set_cursor = getattr(self.repository, "set_clean_recovery_cursor", None)
        cursor = get_cursor(tenant_id=tenant_id, matter_id=matter_id) if callable(get_cursor) else None
        page_reader = getattr(metadata_repository, "list_for_scope_page", None)
        if callable(page_reader):
            documents, next_cursor = page_reader(tenant_id=tenant_id, matter_id=matter_id, limit=limit, cursor=cursor)
        else:
            documents, next_cursor = metadata_repository.list_for_scope(tenant_id=tenant_id, matter_id=matter_id, limit=limit), None
        recovered: list[IDPJob] = []
        for document in documents:
            if len(recovered) >= limit or getattr(document, "tenant_id", None) != tenant_id or getattr(document, "matter_id", None) != matter_id:
                continue
            if getattr(getattr(document, "malware_scan_status", None), "value", None) != "NO_THREATS_FOUND":
                continue
            if getattr(getattr(document, "status", None), "value", None) not in {"UPLOADED", "PENDING_INGESTION", "INDEXED"}:
                continue
            source_key = getattr(document, "s3_key", None)
            size = getattr(document, "file_size_bytes", None)
            if not isinstance(source_key, str) or not source_key.strip() or isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= self.config.max_bytes:
                continue
            try:
                head = object_storage.head_object(key=source_key)
                length = head.get("ContentLength") if isinstance(head, Mapping) else None
                if length != size:
                    continue
                stored = head.get("Metadata", {}) if isinstance(head, Mapping) else {}
                digest = stored.get("legaldesk-sha256") if isinstance(stored, Mapping) else None
                if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
                    body = object_storage.read_object_bytes(key=source_key, max_bytes=self.config.max_bytes)
                    if not isinstance(body, bytes) or len(body) != size:
                        continue
                    digest = hashlib.sha256(body).hexdigest()
                document_for_idp = DocumentForIDP(
                    tenant_id=tenant_id,
                    matter_id=matter_id,
                    document_id=str(document.document_id),
                    media_type=str(document.media_type),
                    file_size_bytes=size,
                    malware_scan_clean=True,
                    content_sha256=digest,
                    source_key=source_key,
                )
                # Use the producer's initial generation so recovery converges
                # on an already-created job even if its clean intent row was
                # the part lost in the crash window.
                job = build_verified_clean_job(document=document_for_idp, model_id=self.model_id, prompt_version=self.prompt_version, max_attempts=self.config.max_attempts, generation="initial")
                record_intent = getattr(self.repository, "record_clean_intent", None)
                if callable(record_intent):
                    record_intent(job=job)
                durable = self.repository.create_job(job)
                if durable.status is IDPJobStatus.ENQUEUE_PENDING:
                    recovered.append(enqueue_verified_clean_job(repository=self.repository, queue=self.queue, job=durable))
            except Exception:
                # One malformed/stale candidate must not abort the bounded
                # scope pass; the next scheduled pass can retry it.
                continue
        if callable(set_cursor):
            set_cursor(tenant_id=tenant_id, matter_id=matter_id, cursor=next_cursor)
        return tuple(recovered)


__all__ = ["IDPTriggerError", "VerifiedCleanIDPTrigger"]
