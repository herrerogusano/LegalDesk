"""Option-B IDP persistence and trusted queue-job lookup.

The Boto adapter uses the existing metadata table.  Queue messages contain an
opaque job ID only; the locator first reads the server-owned locator and then
the tenant/matter-scoped item.  No queue field is an authorization source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
import base64
import binascii
from decimal import Decimal
from threading import RLock
from typing import Any, Mapping, Protocol, Sequence

from .models import (
    IDPClaim,
    IDPConcurrencyError,
    IDPContractError,
    EvidenceAnchor,
    FieldAcceptance,
    FieldOrigin,
    FieldPresence,
    IDPExtractionRun,
    IDPFieldResult,
    IDPJob,
    IDPJobStatus,
    IDP_TERMINAL_JOB_STATUSES,
    IDPCheckpoint,
    IDPSkipReason,
    new_id,
    public_idp_status,
)
from .processing import PaidStage, PaidStageRecord, PaidStageState


MAX_IDP_RUN_ITEM_BYTES = 350_000


def _dynamo_safe(value: Any) -> Any:
    """Convert JSON numbers to Dynamo-safe finite values without floats."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise IDPContractError("non-finite IDP numeric value")
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _dynamo_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dynamo_safe(item) for item in value]
    return value


def matter_partition_key(tenant_id: str, matter_id: str) -> str:
    return f"TENANT#{tenant_id}#MATTER#{matter_id}"


def job_sort_key(job_id: str) -> str:
    return f"IDP#JOB#{job_id}"


def job_locator_partition_key(job_id: str) -> str:
    return f"IDP#JOB#{job_id}"


def job_locator_sort_key() -> str:
    return "LOCATOR"


def idempotency_sort_key(idempotency_key: str) -> str:
    return f"IDP#IDEMPOTENCY#{idempotency_key}"


def run_sort_key(document_id: str, run_id: str) -> str:
    return f"IDP#RUN#{document_id}#{run_id}"


def history_sort_key(document_id: str, created_at: datetime, run_id: str) -> str:
    """Chronological marker key; run_id only breaks equal-timestamp ties."""

    timestamp = created_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return f"IDP#HISTORY#{document_id}#{timestamp}#{run_id}"


@dataclass(frozen=True, slots=True)
class RunHistoryPage:
    runs: tuple[IDPExtractionRun, ...]
    next_cursor: str | None = None


def _history_cursor(*, tenant_id: str, matter_id: str, document_id: str, key: Mapping[str, Any]) -> str:
    payload = {"v": 1, "scope": [tenant_id, matter_id, document_id], "key": dict(key)}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_history_cursor(*, tenant_id: str, matter_id: str, document_id: str, cursor: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
        raise IDPContractError("history cursor is invalid")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise IDPContractError("history cursor is invalid") from exc
    if not isinstance(payload, Mapping) or payload.get("v") != 1 or payload.get("scope") != [tenant_id, matter_id, document_id] or not isinstance(payload.get("key"), Mapping):
        raise IDPContractError("history cursor scope is invalid")
    key = dict(payload["key"])
    if set(key) != {"pk", "sk"} or not all(isinstance(value, str) and value for value in key.values()) or not key["sk"].startswith(f"IDP#HISTORY#{document_id}#"):
        raise IDPContractError("history cursor key is invalid")
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IDPIdempotencyConflict(IDPContractError):
    pass


def stage_sort_key(run_id: str, stage: PaidStage | str) -> str:
    value = stage.value if isinstance(stage, PaidStage) else str(stage)
    return f"IDP#STAGE#{run_id}#{value}"


_CHECKPOINT_NEXT: dict[IDPCheckpoint, frozenset[IDPCheckpoint]] = {
    IDPCheckpoint.CREATED: frozenset({IDPCheckpoint.VALIDATED, IDPCheckpoint.PAID_CALL_READY}),
    # The production processor uses the durable per-stage ledger for paid
    # calls, so a validated job may persist a completed/WAITING projection
    # without passing through the legacy single-call checkpoint.
    IDPCheckpoint.VALIDATED: frozenset({IDPCheckpoint.PAID_CALL_READY, IDPCheckpoint.PERSISTED, IDPCheckpoint.AMBIGUOUS}),
    IDPCheckpoint.PAID_CALL_READY: frozenset({IDPCheckpoint.PAID_CALL_IN_FLIGHT}),
    IDPCheckpoint.PAID_CALL_IN_FLIGHT: frozenset({IDPCheckpoint.PAID_CALL_COMMITTED, IDPCheckpoint.AMBIGUOUS}),
    IDPCheckpoint.PAID_CALL_COMMITTED: frozenset({IDPCheckpoint.PERSISTED}),
    IDPCheckpoint.PERSISTED: frozenset({IDPCheckpoint.DONE}),
    IDPCheckpoint.DONE: frozenset(),
    IDPCheckpoint.AMBIGUOUS: frozenset(),
}


def _validate_checkpoint_transition(current: IDPCheckpoint, requested: IDPCheckpoint) -> None:
    if requested is current:
        if requested is IDPCheckpoint.PAID_CALL_IN_FLIGHT:
            raise IDPConcurrencyError("paid call is already in flight")
        return
    if requested not in _CHECKPOINT_NEXT[current]:
        raise IDPConcurrencyError("IDP checkpoint transition is invalid")


@dataclass(frozen=True, slots=True)
class DeliveryCandidatePage:
    jobs: tuple[IDPJob, ...]
    next_cursor: Mapping[str, Any] | str | None = None


class IDPRepository(Protocol):
    def create_job(self, job: IDPJob) -> IDPJob: ...
    def reserve_reprocess_job(self, *, job: IDPJob, expected_run_id: str | None, expected_source_key: str) -> IDPJob: ...
    def record_clean_intent(self, *, job: IDPJob) -> None: ...
    def list_clean_intents(self, *, tenant_id: str, matter_id: str, limit: int) -> tuple[IDPJob, ...]: ...
    def get_clean_recovery_cursor(self, *, tenant_id: str, matter_id: str) -> str | None: ...
    def set_clean_recovery_cursor(self, *, tenant_id: str, matter_id: str, cursor: str | None) -> None: ...
    def get_review_recovery_cursor(self, *, tenant_id: str, matter_id: str) -> str | None: ...
    def set_review_recovery_cursor(self, *, tenant_id: str, matter_id: str, cursor: str | None) -> None: ...
    def get_job(self, job_id: str) -> IDPJob | None: ...
    def list_review_jobs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int = 20) -> tuple[IDPJob, ...]: ...
    def claim_job(self, *, job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None, allow_waiting_for_ocr: bool = False) -> IDPClaim: ...
    def checkpoint_job(self, *, claim: IDPClaim, checkpoint: IDPCheckpoint, status: IDPJobStatus | None = None, skip_reason: IDPSkipReason | None = None, now: datetime | None = None) -> IDPJob: ...
    def mark_delivery(self, *, job_id: str, status: IDPJobStatus) -> IDPJob: ...
    def save_run(self, run: IDPExtractionRun) -> IDPExtractionRun: ...
    def project_document_status(self, *, run: IDPExtractionRun) -> None: ...
    def project_document_job_status(self, *, job: IDPJob, source_key: str | None = None) -> None: ...
    def record_review_delivery(self, *, job_id: str, state: str, invocation_id: str, review_task_id: str | None = None) -> IDPJob: ...
    def fail_exhausted(self, *, job_id: str) -> IDPJob: ...
    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str) -> IDPExtractionRun | None: ...
    def list_runs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int) -> tuple[IDPExtractionRun, ...]: ...
    def list_run_history(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int, cursor: str | None = None) -> RunHistoryPage: ...
    def list_delivery_candidates(self, *, tenant_id: str, matter_id: str, limit: int, cursor: Mapping[str, Any] | str | None = None) -> DeliveryCandidatePage: ...


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 100:
        raise IDPContractError("query limit is invalid")
    return limit


class InMemoryIDPRepository:
    """Thread-safe fake with the same conditional semantics as production."""

    def __init__(self, *, clock=_now) -> None:
        self._jobs: dict[str, IDPJob] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._runs: dict[tuple[str, str, str], IDPExtractionRun] = {}
        self._clean_intents: dict[tuple[str, str, str], IDPJob] = {}
        self._clean_recovery_cursors: dict[tuple[str, str, str], str | None] = {}
        self._lock = RLock()
        self.clock = clock

    def create_job(self, job: IDPJob) -> IDPJob:
        with self._lock:
            idempotency_scope = (job.tenant_id, job.matter_id, job.idempotency_key)
            existing_id = self._idempotency.get(idempotency_scope)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if (existing.tenant_id, existing.matter_id, existing.document_id, existing.document_sha256,
                        existing.schema_version, existing.model_id, existing.prompt_version) != (
                        job.tenant_id, job.matter_id, job.document_id, job.document_sha256,
                        job.schema_version, job.model_id, job.prompt_version):
                    raise IDPIdempotencyConflict("IDP idempotency key was already used")
                return existing
            if job.job_id in self._jobs:
                raise IDPIdempotencyConflict("IDP job ID already exists")
            self._jobs[job.job_id] = job
            self._idempotency[idempotency_scope] = job.job_id
            return job

    def reserve_reprocess_job(self, *, job: IDPJob, expected_run_id: str | None, expected_source_key: str) -> IDPJob:
        """Atomically reserve one reprocess intent under the repository lock."""

        with self._lock:
            existing_intent = self._clean_intents.get((job.tenant_id, job.matter_id, job.document_id))
            if existing_intent is not None and existing_intent.document_sha256 != job.document_sha256:
                raise IDPIdempotencyConflict("clean intent hash changed")
            if existing_intent is not None and existing_intent.idempotency_key != job.idempotency_key:
                existing_job = self._jobs.get(existing_intent.job_id)
                if existing_job is None:
                    raise IDPConcurrencyError("pending reprocess intent has no job")
                if existing_job.status not in IDP_TERMINAL_JOB_STATUSES:
                    raise IDPConcurrencyError("reprocess generation is already active")
                if expected_run_id != existing_job.job_id:
                    raise IDPConcurrencyError("reprocess generation pointer is stale")
            existing_id = self._idempotency.get((job.tenant_id, job.matter_id, job.idempotency_key))
            if existing_id is not None:
                return self._jobs[existing_id]
            if job.job_id in self._jobs:
                raise IDPIdempotencyConflict("IDP job ID already exists")
            self._jobs[job.job_id] = job
            self._idempotency[(job.tenant_id, job.matter_id, job.idempotency_key)] = job.job_id
            self._clean_intents[(job.tenant_id, job.matter_id, job.document_id)] = job
            return job

    def record_clean_intent(self, *, job: IDPJob) -> None:
        with self._lock:
            key = (job.tenant_id, job.matter_id, job.document_id)
            existing = self._clean_intents.get(key)
            if existing is not None and existing.document_sha256 != job.document_sha256:
                raise IDPIdempotencyConflict("clean intent hash changed")
            if existing is not None:
                if existing.idempotency_key != job.idempotency_key:
                    raise IDPConcurrencyError("clean intent belongs to another generation")
                # Re-registering the same intent is the crash-recovery path;
                # never rewrite its status or demote a terminal projection.
                return
            existing_by_key = self._idempotency.get((job.tenant_id, job.matter_id, job.idempotency_key))
            if existing_by_key is not None:
                return
            self._clean_intents[key] = job

    def list_clean_intents(self, *, tenant_id: str, matter_id: str, limit: int) -> tuple[IDPJob, ...]:
        _validate_limit(limit)
        with self._lock:
            return tuple(job for (tenant, matter, _), job in self._clean_intents.items() if tenant == tenant_id and matter == matter and job.status in {IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_IN_FLIGHT, IDPJobStatus.DELIVERY_AMBIGUOUS})[:limit]

    def get_clean_recovery_cursor(self, *, tenant_id: str, matter_id: str) -> str | None:
        with self._lock:
            return self._clean_recovery_cursors.get((tenant_id, matter_id, "clean"))

    def set_clean_recovery_cursor(self, *, tenant_id: str, matter_id: str, cursor: str | None) -> None:
        with self._lock:
            self._clean_recovery_cursors[(tenant_id, matter_id, "clean")] = cursor

    def get_review_recovery_cursor(self, *, tenant_id: str, matter_id: str) -> str | None:
        with self._lock:
            return self._clean_recovery_cursors.get((tenant_id, matter_id, "review"))

    def set_review_recovery_cursor(self, *, tenant_id: str, matter_id: str, cursor: str | None) -> None:
        with self._lock:
            self._clean_recovery_cursors[(tenant_id, matter_id, "review")] = cursor

    def get_job(self, job_id: str) -> IDPJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_review_jobs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int = 20) -> tuple[IDPJob, ...]:
        _validate_limit(limit)
        with self._lock:
            values = [
                job for job in self._jobs.values()
                if job.tenant_id == tenant_id and job.matter_id == matter_id
                and job.document_id == document_id and job.status is IDPJobStatus.REVIEW_REQUIRED
            ]
            values.sort(key=lambda item: (item.created_at, item.job_id), reverse=True)
            return tuple(values[:limit])

    def claim_job(self, *, job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None, allow_waiting_for_ocr: bool = False) -> IDPClaim:
        if not isinstance(worker_id, str) or not worker_id.strip() or isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 900:
            raise IDPContractError("claim parameters are invalid")
        now = self.clock() if now is None else now
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise IDPConcurrencyError("IDP job was not found")
            now = self.clock() if now is None else now
            safe_reclaim = job.status in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING, IDPJobStatus.WAITING_FOR_OCR) and job.claimed_until is not None and job.claimed_until <= now and job.checkpoint in (IDPCheckpoint.CREATED, IDPCheckpoint.VALIDATED, IDPCheckpoint.PAID_CALL_READY)
            continuation = allow_waiting_for_ocr and job.status is IDPJobStatus.WAITING_FOR_OCR and job.checkpoint is IDPCheckpoint.VALIDATED
            if not ((job.status is IDPJobStatus.QUEUED and job.claim_token is None) or safe_reclaim or continuation) or job.attempt >= job.max_attempts:
                raise IDPConcurrencyError("IDP job is not claimable")
            token = new_id()
            claimed = replace(
                job, status=IDPJobStatus.CLAIMED, attempt=job.attempt + 1,
                claim_token=token, claimed_until=now.replace(microsecond=0) + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            self._jobs[job_id] = claimed
            return IDPClaim(job_id, token, worker_id, claimed.claimed_until)  # type: ignore[arg-type]

    def checkpoint_job(self, *, claim: IDPClaim, checkpoint: IDPCheckpoint, status: IDPJobStatus | None = None, skip_reason: IDPSkipReason | None = None, now: datetime | None = None) -> IDPJob:
        with self._lock:
            job = self._jobs.get(claim.job_id)
            if job is None or job.claim_token != claim.claim_token or job.status not in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING, IDPJobStatus.WAITING_FOR_OCR):
                raise IDPConcurrencyError("IDP claim is stale")
            now = self.clock() if now is None else now
            if job.claimed_until is None or job.claimed_until <= now:
                raise IDPConcurrencyError("IDP claim lease has expired")
            _validate_checkpoint_transition(job.checkpoint, checkpoint)
            next_status = status or (IDPJobStatus.PROCESSING if checkpoint is not IDPCheckpoint.DONE else IDPJobStatus.COMPLETED)
            updated = replace(job, checkpoint=checkpoint, status=next_status, skip_reason=skip_reason, updated_at=now)
            self._jobs[job.job_id] = updated
            return updated

    def mark_delivery(self, *, job_id: str, status: IDPJobStatus) -> IDPJob:
        if status not in (IDPJobStatus.QUEUED, IDPJobStatus.DELIVERY_IN_FLIGHT, IDPJobStatus.DELIVERY_AMBIGUOUS):
            raise IDPContractError("invalid delivery transition")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise IDPConcurrencyError("IDP job was not found")
            if status is IDPJobStatus.QUEUED and job.status not in (IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_IN_FLIGHT, IDPJobStatus.DELIVERY_AMBIGUOUS):
                if job.status is IDPJobStatus.QUEUED:
                    return job
                raise IDPConcurrencyError("IDP delivery transition is stale")
            if status is IDPJobStatus.DELIVERY_IN_FLIGHT and job.status is not IDPJobStatus.ENQUEUE_PENDING:
                raise IDPConcurrencyError("IDP delivery transition is stale")
            if status is IDPJobStatus.DELIVERY_AMBIGUOUS and job.status is not IDPJobStatus.DELIVERY_IN_FLIGHT:
                raise IDPConcurrencyError("IDP delivery transition is stale")
            updated = replace(job, status=status, updated_at=self.clock())
            self._jobs[job_id] = updated
            return updated

    def save_run(self, run: IDPExtractionRun) -> IDPExtractionRun:
        key = (run.tenant_id, run.matter_id, run.run_id)
        with self._lock:
            existing = self._runs.get(key)
            if existing is not None:
                if existing != run:
                    raise IDPConcurrencyError("IDP run is immutable")
                return existing
            self._runs[key] = run
            return run

    def project_document_status(self, *, run: IDPExtractionRun) -> None:
        return None

    def project_document_job_status(self, *, job: IDPJob, source_key: str | None = None) -> None:
        return None

    def record_review_delivery(self, *, job_id: str, state: str, invocation_id: str, review_task_id: str | None = None) -> IDPJob:
        if state not in {"PENDING", "IN_FLIGHT", "AMBIGUOUS", "SENT"}:
            raise IDPContractError("review delivery state is invalid")
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not IDPJobStatus.REVIEW_REQUIRED:
                raise IDPConcurrencyError("review delivery job is not review-required")
            if job.review_delivery_state == "SENT" and state != "SENT":
                return job
            updated = replace(job, review_delivery_state=state, review_invocation_id=invocation_id, review_task_id=review_task_id or job.review_task_id, updated_at=self.clock())
            self._jobs[job_id] = updated
            return updated

    def fail_exhausted(self, *, job_id: str) -> IDPJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise IDPConcurrencyError("IDP job was not found")
            if job.status in {IDPJobStatus.COMPLETED, IDPJobStatus.REVIEW_REQUIRED, IDPJobStatus.FAILED, IDPJobStatus.SKIPPED}:
                return job
            if job.attempt < job.max_attempts:
                raise IDPConcurrencyError("IDP job has not exhausted attempts")
            failed = replace(job, status=IDPJobStatus.FAILED, checkpoint=IDPCheckpoint.AMBIGUOUS, error_code="MAX_ATTEMPTS_EXHAUSTED", claimed_until=None, claim_token=None, updated_at=self.clock())
            self._jobs[job_id] = failed
            return failed

    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str) -> IDPExtractionRun | None:
        with self._lock:
            run = self._runs.get((tenant_id, matter_id, run_id))
            return run if run is not None and run.document_id == document_id else None

    def list_runs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int) -> tuple[IDPExtractionRun, ...]:
        _validate_limit(limit)
        with self._lock:
            values = [run for (tenant, matter, _), run in self._runs.items() if tenant == tenant_id and matter == matter_id and run.document_id == document_id]
            return tuple(sorted(values, key=lambda item: item.created_at, reverse=True)[:limit])

    def list_run_history(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int, cursor: str | None = None) -> RunHistoryPage:
        _validate_limit(limit)
        with self._lock:
            values = sorted(
                (run for (tenant, matter, _), run in self._runs.items() if tenant == tenant_id and matter == matter_id and run.document_id == document_id),
                key=lambda item: (item.created_at, item.run_id), reverse=True,
            )
            if cursor is not None:
                key = _decode_history_cursor(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, cursor=cursor)["sk"]
                marker = key.rsplit("#", 1)[-1]
                timestamp = key.split("#", 3)[3].rsplit("#", 1)[0]
                values = [item for item in values if (item.created_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"), item.run_id) < (timestamp, marker)]
            page = tuple(values[:limit])
            next_cursor = None
            if len(values) > len(page) and page:
                last = page[-1]
                next_cursor = _history_cursor(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, key={"pk": matter_partition_key(tenant_id, matter_id), "sk": history_sort_key(document_id, last.created_at, last.run_id)})
            return RunHistoryPage(page, next_cursor)

    def list_delivery_candidates(self, *, tenant_id: str, matter_id: str, limit: int, cursor: Mapping[str, Any] | str | None = None) -> DeliveryCandidatePage:
        _validate_limit(limit)
        with self._lock:
            values = tuple(job for job in self._jobs.values() if job.tenant_id == tenant_id and job.matter_id == matter_id and job.status in (IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_IN_FLIGHT, IDPJobStatus.DELIVERY_AMBIGUOUS))
            if cursor is not None and (not isinstance(cursor, str) or not cursor.isdigit()):
                raise IDPContractError("delivery cursor is invalid")
            start = int(cursor) if cursor is not None else 0
            selected = values[start:start + limit]
            next_cursor = str(start + limit) if start + limit < len(values) else None
            return DeliveryCandidatePage(selected, next_cursor)


def _job_item(job: IDPJob) -> dict[str, Any]:
    item = {
        "entityType": "IDPJob",
        "pk": matter_partition_key(job.tenant_id, job.matter_id),
        "sk": job_sort_key(job.job_id),
        "jobId": job.job_id,
        "tenantId": job.tenant_id,
        "matterId": job.matter_id,
        "documentId": job.document_id,
        "documentSha256": job.document_sha256,
        "idempotencyKey": job.idempotency_key,
        "correlationId": job.correlation_id,
        "schemaVersion": job.schema_version,
        "modelId": job.model_id,
        "promptVersion": job.prompt_version,
        "status": job.status.value,
        "checkpoint": job.checkpoint.value,
        "attempt": job.attempt,
        "maxAttempts": job.max_attempts,
        "createdAt": job.created_at.isoformat(),
        "updatedAt": job.updated_at.isoformat(),
    }
    if job.claim_token is not None:
        item["claimToken"] = job.claim_token
    if job.claimed_until is not None:
        item["claimedUntil"] = job.claimed_until.isoformat()
    if job.skip_reason is not None:
        item["skipReason"] = job.skip_reason.value
    if job.error_code is not None:
        item["errorCode"] = job.error_code
    for key, value in (("reviewDeliveryState", job.review_delivery_state), ("reviewInvocationId", job.review_invocation_id), ("reviewTaskId", job.review_task_id)):
        if value is not None:
            item[key] = value
    return item


def _locator_item(job: IDPJob) -> dict[str, Any]:
    return {
        "entityType": "IDPJobLocator",
        "pk": job_locator_partition_key(job.job_id),
        "sk": job_locator_sort_key(),
        "jobId": job.job_id,
        "tenantId": job.tenant_id,
        "matterId": job.matter_id,
        "documentId": job.document_id,
        "jobSortKey": job_sort_key(job.job_id),
    }


def _idempotency_item(job: IDPJob) -> dict[str, Any]:
    return {
        "entityType": "IDPJobIdempotency",
        "pk": matter_partition_key(job.tenant_id, job.matter_id),
        "sk": idempotency_sort_key(job.idempotency_key),
        "jobId": job.job_id,
        "tenantId": job.tenant_id,
        "matterId": job.matter_id,
        "documentId": job.document_id,
    }


def _serialize_transaction_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Return native values for boto3's resource-injected Dynamo client.

    The resource client hook performs the one wire serialization.  Passing
    TypeSerializer output here would be serialized a second time as a Map.
    """

    return dict(item)


def _job_from_item(item: Mapping[str, Any]) -> IDPJob:
    from .models import IDPSkipReason
    return IDPJob(
        job_id=str(item["jobId"]), tenant_id=str(item["tenantId"]), matter_id=str(item["matterId"]),
        document_id=str(item["documentId"]), document_sha256=str(item["documentSha256"]),
        idempotency_key=str(item["idempotencyKey"]), correlation_id=str(item.get("correlationId", item["jobId"])), schema_version=str(item.get("schemaVersion", "1.0.0")),
        model_id=str(item.get("modelId", "")), prompt_version=str(item.get("promptVersion", "")),
        status=IDPJobStatus(item["status"]), checkpoint=IDPCheckpoint(item.get("checkpoint", IDPCheckpoint.CREATED.value)),
        attempt=int(item.get("attempt", 0)), max_attempts=int(item.get("maxAttempts", 3)),
        claim_token=item.get("claimToken") if isinstance(item.get("claimToken"), str) else None,
        claimed_until=datetime.fromisoformat(item["claimedUntil"]) if item.get("claimedUntil") else None,
        skip_reason=IDPSkipReason(item["skipReason"]) if item.get("skipReason") else None,
        error_code=item.get("errorCode") if isinstance(item.get("errorCode"), str) else None,
        review_delivery_state=item.get("reviewDeliveryState") if isinstance(item.get("reviewDeliveryState"), str) else None,
        review_invocation_id=item.get("reviewInvocationId") if isinstance(item.get("reviewInvocationId"), str) else None,
        review_task_id=item.get("reviewTaskId") if isinstance(item.get("reviewTaskId"), str) else None,
        created_at=datetime.fromisoformat(str(item["createdAt"])), updated_at=datetime.fromisoformat(str(item["updatedAt"])),
    )


class Boto3DynamoIDPRepository:
    """DynamoDB adapter for the existing single metadata table."""

    def __init__(self, table_name: str, *, table: Any | None = None, transaction_client: Any | None = None, allow_non_atomic_test_adapter: bool = False) -> None:
        self.table_name = table_name
        if table is None:
            import boto3
            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table
        # Use the resource's client by default: its before-call hook performs
        # exactly one native-value serialization.  Keep explicit injection
        # only as a narrow test seam; production does not need a second client
        # configuration path.
        self._transaction_client = transaction_client or getattr(getattr(table, "meta", None), "client", None)
        self._allow_non_atomic_test_adapter = allow_non_atomic_test_adapter

    def create_job(self, job: IDPJob) -> IDPJob:
        existing = self._get_by_locator(job.job_id)
        if existing is not None:
            return self._check_idempotency(existing, job)
        existing_by_key = self._query_idempotency(job)
        if existing_by_key is not None:
            return self._check_idempotency(existing_by_key, job)
        job_item, locator, idem = _job_item(job), _locator_item(job), _idempotency_item(job)
        transact = getattr(self._transaction_client, "transact_write_items", None)
        if callable(transact):
            try:
                transact(TransactItems=[
                    {"Put": {"TableName": self.table_name, "Item": _serialize_transaction_item(job_item), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
                    {"Put": {"TableName": self.table_name, "Item": _serialize_transaction_item(locator), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
                    {"Put": {"TableName": self.table_name, "Item": _serialize_transaction_item(idem), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
                ])
            except Exception as exc:
                # Conditional cancellation is expected under a concurrent
                # duplicate.  Re-read both authoritative indexes before
                # surfacing an error, making retries return the winner.
                existing = self._get_by_locator(job.job_id) or self._query_idempotency(job)
                if existing is not None:
                    return self._check_idempotency(existing, job)
                raise IDPConcurrencyError("IDP job transaction failed") from exc
        elif self._allow_non_atomic_test_adapter:
            # Explicitly test-only: production must expose the low-level
            # DynamoDB transaction client so job, locator and idempotency rows
            # cannot be partially committed.
            self.table.put_item(Item=job_item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
            self.table.put_item(Item=locator, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
            self.table.put_item(Item=idem, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        else:
            raise IDPContractError("atomic DynamoDB transaction client is required")
        return job

    def reserve_reprocess_job(self, *, job: IDPJob, expected_run_id: str | None, expected_source_key: str) -> IDPJob:
        """Atomically put a reprocess job and claim the document clean intent.

        The document row is the existing single-table lease seam.  Its
        conditional intent check prevents two operator processes from creating
        different generations during the same active/ambiguous dispatch.
        """

        existing = self._get_by_locator(job.job_id) or self._query_idempotency(job)
        if existing is not None:
            return self._check_idempotency(existing, job)
        if not isinstance(expected_source_key, str) or not expected_source_key.strip():
            raise IDPContractError("reprocess source key is required")
        job_item, locator, idem = _job_item(job), _locator_item(job), _idempotency_item(job)
        document_key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": f"DOCUMENT#{job.document_id}"}
        intent_statuses = tuple(status.value for status in IDP_TERMINAL_JOB_STATUSES)
        values: dict[str, object] = {
            ":intent": job_item, ":pending": IDPJobStatus.ENQUEUE_PENDING.value,
            ":hash": job.document_sha256, ":document": job.document_id,
            ":source": expected_source_key, ":expected_run": expected_run_id or "",
            ":terminal_statuses": intent_statuses,
        }
        condition = (
            "#document = :document AND #canonical = :source AND "
            "(attribute_not_exists(#hash) OR #hash = :hash) AND "
            "(attribute_not_exists(#run) OR #run = :expected_run) AND "
            "(attribute_not_exists(#intent) OR #intent.#status IN (:terminal_statuses) OR #intent.#job = :expected_run)"
        )
        # DynamoDB's IN operator requires individual placeholders, not a list.
        names = {"#document": "documentId", "#canonical": "s3Key", "#hash": "idpDocumentSha256", "#run": "idpRunId", "#intent": "idpCleanIntent", "#status": "status", "#idpstatus": "idpStatus", "#job": "jobId"}
        del values[":terminal_statuses"]
        status_values: dict[str, object] = {}
        for index, status in enumerate(intent_statuses):
            status_values[f":terminal{index}"] = status
        condition = condition.replace(":terminal_statuses", ", ".join(status_values))
        values.update(status_values)
        update = {
            "TableName": self.table_name,
            "Key": document_key,
            "UpdateExpression": "SET #intent = :intent, #idpstatus = :pending, #hash = :hash",
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ConditionExpression": condition,
        }
        transact = getattr(self._transaction_client, "transact_write_items", None)
        if not callable(transact):
            raise IDPContractError("atomic DynamoDB transaction client is required")
        try:
            transact(TransactItems=[
                {"Put": {"TableName": self.table_name, "Item": _serialize_transaction_item(job_item), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
                {"Put": {"TableName": self.table_name, "Item": _serialize_transaction_item(locator), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
                {"Put": {"TableName": self.table_name, "Item": _serialize_transaction_item(idem), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}},
                {"Update": {**update, "Key": _serialize_transaction_item(document_key), "ExpressionAttributeValues": _serialize_transaction_item(values)}},
            ])
        except Exception as exc:
            existing = self._get_by_locator(job.job_id) or self._query_idempotency(job)
            if existing is not None:
                return self._check_idempotency(existing, job)
            raise IDPConcurrencyError("reprocess reservation lost a conditional race") from exc
        return job

    def record_clean_intent(self, *, job: IDPJob) -> None:
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": f"DOCUMENT#{job.document_id}"}
        intent = _job_item(job)
        existing_job = self._query_idempotency(job)
        if existing_job is not None:
            # Existing job state is authoritative.  This includes terminal
            # runs whose clean-intent row is being rediscovered by recovery.
            return
        try:
            current = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        except Exception as exc:
            raise IDPConcurrencyError("clean IDP intent could not be read") from exc
        if not isinstance(current, Mapping) or current.get("documentId") != job.document_id or current.get("s3Key") is None:
            raise IDPConcurrencyError("clean IDP document is unavailable")
        current_hash = current.get("idpDocumentSha256")
        if current_hash is not None and current_hash != job.document_sha256:
            raise IDPIdempotencyConflict("clean intent hash changed")
        current_intent = current.get("idpCleanIntent")
        if isinstance(current_intent, Mapping):
            if current_intent.get("idempotencyKey") != job.idempotency_key:
                raise IDPConcurrencyError("clean intent belongs to another generation")
            # Same intent, but the job locator was not visible to the read:
            # retain the durable row and let create_job reconcile it.
            return
        if current.get("idpRunId") and current.get("idpStatus") in {"IDP_COMPLETED", "IDP_REVIEW_REQUIRED", "IDP_FAILED", "IDP_SKIPPED"}:
            raise IDPConcurrencyError("current IDP generation is terminal")
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET #intent = :intent, #status = :pending, #hash = :hash",
                ExpressionAttributeNames={"#intent": "idpCleanIntent", "#status": "idpStatus", "#hash": "idpIntentDocumentSha256", "#document": "documentId", "#existing_hash": "idpDocumentSha256", "#run": "idpRunId"},
                ExpressionAttributeValues={":intent": intent, ":pending": IDPJobStatus.ENQUEUE_PENDING.value, ":hash": job.document_sha256, ":document": job.document_id},
                # The read above handles same-intent crash recovery.  The
                # write itself must only create a previously absent intent;
                # permitting the same idempotency key here would let a
                # completion racing between read and write be demoted back
                # to ENQUEUE_PENDING.
                ConditionExpression="#document = :document AND (attribute_not_exists(#existing_hash) OR #existing_hash = :hash) AND attribute_not_exists(#intent) AND attribute_not_exists(#run)",
            )
        except Exception as exc:
            raise IDPConcurrencyError("clean IDP intent could not be recorded") from exc

    def list_clean_intents(self, *, tenant_id: str, matter_id: str, limit: int) -> tuple[IDPJob, ...]:
        _validate_limit(limit)
        from boto3.dynamodb.conditions import Attr, Key
        intents: list[IDPJob] = []
        cursor = None
        # DynamoDB applies FilterExpression after Limit.  Walk a bounded
        # number of pages so completed document rows cannot starve later clean
        # intents; never Scan and never exceed the caller's result budget.
        for _ in range(20):
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(matter_partition_key(tenant_id, matter_id)) & Key("sk").begins_with("DOCUMENT#"),
                "FilterExpression": Attr("idpCleanIntent").exists(),
                "ConsistentRead": True,
                "Limit": max(1, min(100, limit - len(intents) if intents else limit)),
            }
            if cursor:
                kwargs["ExclusiveStartKey"] = cursor
            response = self.table.query(**kwargs)
            for item in response.get("Items", ()):
                raw = item.get("idpCleanIntent") if isinstance(item, Mapping) else None
                if isinstance(raw, Mapping) and raw.get("status") in {IDPJobStatus.ENQUEUE_PENDING.value, IDPJobStatus.DELIVERY_IN_FLIGHT.value, IDPJobStatus.DELIVERY_AMBIGUOUS.value}:
                    intents.append(_job_from_item(raw))
                    if len(intents) >= limit:
                        return tuple(intents[:limit])
            cursor = response.get("LastEvaluatedKey")
            if not cursor:
                break
        return tuple(intents[:limit])

    def get_clean_recovery_cursor(self, *, tenant_id: str, matter_id: str) -> str | None:
        response = self.table.get_item(Key={"pk": matter_partition_key(tenant_id, matter_id), "sk": "IDP#RECOVERY#CLEAN"}, ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item.get("cursor") if isinstance(item, Mapping) and isinstance(item.get("cursor"), str) else None

    def set_clean_recovery_cursor(self, *, tenant_id: str, matter_id: str, cursor: str | None) -> None:
        key = {"pk": matter_partition_key(tenant_id, matter_id), "sk": "IDP#RECOVERY#CLEAN"}
        if cursor is None:
            try:
                self.table.delete_item(Key=key)
            except Exception as exc:
                raise IDPConcurrencyError("clean recovery cursor could not be cleared") from exc
            return
        try:
            self.table.put_item(Item={**key, "entityType": "IDPRecoveryCursor", "cursor": cursor})
        except Exception as exc:
            raise IDPConcurrencyError("clean recovery cursor could not be saved") from exc

    def get_review_recovery_cursor(self, *, tenant_id: str, matter_id: str) -> str | None:
        response = self.table.get_item(Key={"pk": matter_partition_key(tenant_id, matter_id), "sk": "IDP#RECOVERY#REVIEW"}, ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item.get("cursor") if isinstance(item, Mapping) and isinstance(item.get("cursor"), str) else None

    def set_review_recovery_cursor(self, *, tenant_id: str, matter_id: str, cursor: str | None) -> None:
        key = {"pk": matter_partition_key(tenant_id, matter_id), "sk": "IDP#RECOVERY#REVIEW"}
        if cursor is None:
            self.table.delete_item(Key=key)
        else:
            self.table.put_item(Item={**key, "entityType": "IDPRecoveryCursor", "cursor": cursor})

    def get_job(self, job_id: str) -> IDPJob | None:
        return self._get_by_locator(job_id)

    def _get_by_locator(self, job_id: str) -> IDPJob | None:
        locator_response = self.table.get_item(Key={"pk": job_locator_partition_key(job_id), "sk": job_locator_sort_key()}, ConsistentRead=True)
        locator = locator_response.get("Item")
        if not locator:
            return None
        if locator.get("jobId") != job_id or locator.get("jobSortKey") != job_sort_key(job_id) or not all(isinstance(locator.get(key), str) and locator.get(key) for key in ("tenantId", "matterId", "documentId")):
            raise IDPConcurrencyError("IDP locator is malformed")
        response = self.table.get_item(Key={"pk": matter_partition_key(locator["tenantId"], locator["matterId"]), "sk": locator["jobSortKey"]}, ConsistentRead=True)
        item = response.get("Item")
        if not item or item.get("jobId") != job_id or item.get("tenantId") != locator["tenantId"] or item.get("matterId") != locator["matterId"] or item.get("documentId") != locator["documentId"]:
            raise IDPConcurrencyError("IDP job locator target is invalid")
        return _job_from_item(item)

    def _query_idempotency(self, job: IDPJob) -> IDPJob | None:
        # Idempotency has its own point-readable record; it never needs a
        # table scan or an unbounded partition query.
        response = self.table.get_item(Key={"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": idempotency_sort_key(job.idempotency_key)}, ConsistentRead=True)
        item = response.get("Item")
        if not item:
            return None
        job_id = item.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            raise IDPConcurrencyError("IDP idempotency record is malformed")
        return self._get_by_locator(job_id)

    @staticmethod
    def _check_idempotency(existing: IDPJob, requested: IDPJob) -> IDPJob:
        if (existing.tenant_id, existing.matter_id, existing.document_id, existing.document_sha256,
                existing.schema_version, existing.model_id, existing.prompt_version) != (
                requested.tenant_id, requested.matter_id, requested.document_id, requested.document_sha256,
                requested.schema_version, requested.model_id, requested.prompt_version):
            raise IDPIdempotencyConflict("IDP idempotency key was already used")
        return existing

    def claim_job(self, *, job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None, allow_waiting_for_ocr: bool = False) -> IDPClaim:
        if not isinstance(worker_id, str) or not worker_id.strip() or isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 900:
            raise IDPContractError("claim parameters are invalid")
        job = self.get_job(job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        now = _now() if now is None else now
        safe_reclaim = job.status in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING, IDPJobStatus.WAITING_FOR_OCR) and job.claimed_until is not None and job.claimed_until <= now and job.checkpoint in (IDPCheckpoint.CREATED, IDPCheckpoint.VALIDATED, IDPCheckpoint.PAID_CALL_READY)
        continuation = allow_waiting_for_ocr and job.status is IDPJobStatus.WAITING_FOR_OCR and job.checkpoint is IDPCheckpoint.VALIDATED
        if not ((job.status is IDPJobStatus.QUEUED and job.claim_token is None) or safe_reclaim or continuation) or job.attempt >= job.max_attempts:
            raise IDPConcurrencyError("IDP job is not claimable")
        token = new_id()
        until = now + timedelta(seconds=lease_seconds)
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": job_sort_key(job.job_id)}
        try:
            condition = "((#status = :queued AND attribute_not_exists(#claim)) OR (#status IN (:claimed_status, :processing_status, :waiting_status) AND #until <= :now AND #checkpoint IN (:created, :validated, :ready)) OR (#status = :waiting_status AND #checkpoint = :validated AND #attempt < :max_attempts)) AND #attempt < :max_attempts" if allow_waiting_for_ocr else "((#status = :queued AND attribute_not_exists(#claim)) OR (#status IN (:claimed_status, :processing_status, :waiting_status) AND #until <= :now AND #checkpoint IN (:created, :validated, :ready))) AND #attempt < :max_attempts"
            self.table.update_item(Key=key, UpdateExpression="SET #status = :claimed, #attempt = #attempt + :one, #claim = :claim, #until = :until, #updated = :updated", ExpressionAttributeNames={"#status": "status", "#attempt": "attempt", "#claim": "claimToken", "#until": "claimedUntil", "#checkpoint": "checkpoint", "#updated": "updatedAt"}, ExpressionAttributeValues={":claimed": IDPJobStatus.CLAIMED.value, ":one": 1, ":claim": token, ":until": until.isoformat(), ":updated": now.isoformat(), ":queued": IDPJobStatus.QUEUED.value, ":claimed_status": IDPJobStatus.CLAIMED.value, ":processing_status": IDPJobStatus.PROCESSING.value, ":waiting_status": IDPJobStatus.WAITING_FOR_OCR.value, ":now": now.isoformat(), ":created": IDPCheckpoint.CREATED.value, ":validated": IDPCheckpoint.VALIDATED.value, ":ready": IDPCheckpoint.PAID_CALL_READY.value, ":max_attempts": job.max_attempts}, ConditionExpression=condition)
        except Exception as exc:
            raise IDPConcurrencyError("IDP job is not claimable") from exc
        return IDPClaim(job_id, token, worker_id, until)

    def checkpoint_job(self, *, claim: IDPClaim, checkpoint: IDPCheckpoint, status: IDPJobStatus | None = None, skip_reason: IDPSkipReason | None = None, now: datetime | None = None) -> IDPJob:
        job = self.get_job(claim.job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        now = _now() if now is None else now
        if job.claim_token != claim.claim_token or job.status not in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING, IDPJobStatus.WAITING_FOR_OCR) or job.claimed_until is None or job.claimed_until <= now:
            raise IDPConcurrencyError("IDP claim is stale")
        _validate_checkpoint_transition(job.checkpoint, checkpoint)
        next_status = status or (IDPJobStatus.PROCESSING if checkpoint is not IDPCheckpoint.DONE else IDPJobStatus.COMPLETED)
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": job_sort_key(job.job_id)}
        expression = "SET #checkpoint = :checkpoint, #status = :status, #updated = :updated"
        names = {"#checkpoint": "checkpoint", "#status": "status", "#updated": "updatedAt"}
        values: dict[str, object] = {":checkpoint": checkpoint.value, ":status": next_status.value, ":updated": now.isoformat(), ":claim": claim.claim_token, ":expected_status": job.status.value, ":expected_checkpoint": job.checkpoint.value}
        if skip_reason is not None:
            expression += ", #skip = :skip"
            names["#skip"] = "skipReason"
            values[":skip"] = skip_reason.value
        try:
            names = {**names, "#claim": "claimToken", "#until": "claimedUntil"}
            values[":now"] = now.isoformat()
            self.table.update_item(Key=key, UpdateExpression=expression, ExpressionAttributeNames=names, ExpressionAttributeValues=values, ConditionExpression="#claim = :claim AND #status = :expected_status AND #checkpoint = :expected_checkpoint AND #until > :now")
        except Exception as exc:
            raise IDPConcurrencyError("IDP claim is stale") from exc
        return self.get_job(claim.job_id)  # type: ignore[return-value]

    def mark_delivery(self, *, job_id: str, status: IDPJobStatus) -> IDPJob:
        job = self.get_job(job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        allowed = {
            IDPJobStatus.QUEUED: (IDPJobStatus.ENQUEUE_PENDING, IDPJobStatus.DELIVERY_IN_FLIGHT, IDPJobStatus.DELIVERY_AMBIGUOUS),
            IDPJobStatus.DELIVERY_IN_FLIGHT: (IDPJobStatus.ENQUEUE_PENDING,),
            IDPJobStatus.DELIVERY_AMBIGUOUS: (IDPJobStatus.DELIVERY_IN_FLIGHT,),
        }
        if job.status not in allowed.get(status, ()):
            raise IDPConcurrencyError("IDP delivery transition is stale")
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": job_sort_key(job.job_id)}
        self.table.update_item(Key=key, UpdateExpression="SET #status = :status, #updated = :updated", ExpressionAttributeNames={"#status": "status", "#updated": "updatedAt"}, ExpressionAttributeValues={":status": status.value, ":updated": _now().isoformat(), ":expected": job.status.value}, ConditionExpression="#status = :expected")
        return self.get_job(job_id)  # type: ignore[return-value]

    def save_run(self, run: IDPExtractionRun) -> IDPExtractionRun:
        key = {"pk": matter_partition_key(run.tenant_id, run.matter_id), "sk": run_sort_key(run.document_id, run.run_id)}
        fields = {
            name: {
                "field": result.field, "value": _dynamo_safe(result.value), "presence": result.presence.value,
                "origin": result.origin.value, "acceptance": result.acceptance.value,
                "evidence": [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in result.evidence],
                "validation": dict(result.validation), "schemaVersion": result.schema_version, "reason": result.reason, "provenance": _dynamo_safe(dict(result.provenance)),
            }
            for name, result in run.fields.items()
        }
        item = {"entityType": "IDPExtractionRun", "pk": key["pk"], "sk": key["sk"], "runId": run.run_id, "tenantId": run.tenant_id, "matterId": run.matter_id, "documentId": run.document_id, "documentSha256": run.document_sha256, "documentType": run.document_type.value, "schemaVersion": run.schema_version, "modelId": run.model_id, "promptVersion": run.prompt_version, "status": run.status.value, "fields": fields, "createdAt": run.created_at.isoformat(), "sourceKey": run.source_key, "pageTextArtifactKey": run.page_text_artifact_key}
        item = {key: value for key, value in item.items() if value is not None}
        if len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")) > MAX_IDP_RUN_ITEM_BYTES:
            raise IDPContractError("IDP run projection exceeds the bounded DynamoDB item budget; immutable S3 artifact remains authoritative")
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except Exception:
            response = self.table.get_item(Key=key, ConsistentRead=True)
            if response.get("Item") != item:
                raise IDPConcurrencyError("IDP run is immutable") from None
        marker = {
            "entityType": "IDPExtractionHistoryMarker", "pk": key["pk"],
            "sk": history_sort_key(run.document_id, run.created_at, run.run_id),
            "documentId": run.document_id, "runId": run.run_id,
            "createdAt": run.created_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z"),
        }
        try:
            self.table.put_item(Item=marker, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except Exception:
            existing_marker = self.table.get_item(Key={"pk": marker["pk"], "sk": marker["sk"]}, ConsistentRead=True).get("Item")
            if existing_marker != marker:
                raise IDPConcurrencyError("IDP history marker is immutable") from None
        return run

    def project_document_status(self, *, run: IDPExtractionRun) -> None:
        key = {"pk": matter_partition_key(run.tenant_id, run.matter_id), "sk": f"DOCUMENT#{run.document_id}"}
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET #status = :status, #run = :run, #hash = :hash, #generation = :generation, #source = :source",
                ExpressionAttributeNames={"#status": "idpStatus", "#run": "idpRunId", "#hash": "idpDocumentSha256", "#generation": "idpGenerationAt", "#source": "idpSourceKey", "#document": "documentId", "#canonical": "s3Key"},
                ExpressionAttributeValues={":status": public_idp_status(run.status), ":run": run.run_id, ":hash": run.document_sha256, ":generation": run.created_at.isoformat(), ":source": run.source_key or "", ":document": run.document_id},
                # The pointer may advance for a newer generation/content hash,
                # but only while the metadata row still names this exact
                # canonical object.  Queue/job fields never authorize it.
                ConditionExpression="#document = :document AND #canonical = :source AND (attribute_not_exists(#generation) OR #generation <= :generation)",
            )
        except Exception as exc:
            raise IDPConcurrencyError("IDP document projection is stale or unavailable") from exc

    def project_document_job_status(self, *, job: IDPJob, source_key: str | None = None) -> None:
        """Project bounded terminal/skip state without inventing a run.

        This is used for malformed, oversized, or ambiguous inputs.  It is
        conditional on the current document identity when a source key is
        available, and never changes the canonical document/RAG lifecycle.
        """

        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": f"DOCUMENT#{job.document_id}"}
        names = {"#status": "idpStatus", "#reason": "idpReason", "#attempt": "idpAttempt", "#job": "idpJobId", "#generation": "idpGenerationAt", "#document": "documentId"}
        values: dict[str, object] = {
            ":status": public_idp_status(job.status),
            ":reason": job.skip_reason.value if job.skip_reason is not None else (job.error_code or "PROCESSING_FAILED"),
            ":attempt": job.attempt,
            ":job": job.job_id,
            ":generation": job.created_at.isoformat(),
            ":document": job.document_id,
        }
        condition = "#document = :document AND (attribute_not_exists(#generation) OR #generation <= :generation)"
        if source_key:
            names["#canonical"] = "s3Key"
            values[":source"] = source_key
            condition = "#document = :document AND #canonical = :source AND (attribute_not_exists(#generation) OR #generation <= :generation)"
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET #status = :status, #reason = :reason, #attempt = :attempt, #job = :job, #generation = :generation",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
            )
        except Exception as exc:
            raise IDPConcurrencyError("IDP terminal projection is stale or unavailable") from exc

    def record_review_delivery(self, *, job_id: str, state: str, invocation_id: str, review_task_id: str | None = None) -> IDPJob:
        if state not in {"PENDING", "IN_FLIGHT", "AMBIGUOUS", "SENT"} or not isinstance(invocation_id, str) or not invocation_id.strip():
            raise IDPContractError("review delivery record is invalid")
        job = self.get_job(job_id)
        if job is None or job.status is not IDPJobStatus.REVIEW_REQUIRED:
            raise IDPConcurrencyError("review delivery job is not review-required")
        if job.review_delivery_state == "SENT" and state != "SENT":
            return job
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": job_sort_key(job.job_id)}
        names = {"#status": "status", "#state": "reviewDeliveryState", "#invocation": "reviewInvocationId", "#updated": "updatedAt"}
        values: dict[str, object] = {":review": IDPJobStatus.REVIEW_REQUIRED.value, ":state": state, ":invocation": invocation_id, ":updated": _now().isoformat()}
        update = "SET #state = :state, #invocation = :invocation, #updated = :updated"
        if review_task_id is not None:
            names["#task"] = "reviewTaskId"
            values[":task"] = review_task_id
            update += ", #task = :task"
        try:
            self.table.update_item(Key=key, UpdateExpression=update, ExpressionAttributeNames=names, ExpressionAttributeValues=values, ConditionExpression="#status = :review")
        except Exception as exc:
            raise IDPConcurrencyError("review delivery projection lost its race") from exc
        return self.get_job(job_id)  # type: ignore[return-value]

    def fail_exhausted(self, *, job_id: str) -> IDPJob:
        job = self.get_job(job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        if job.status in {IDPJobStatus.COMPLETED, IDPJobStatus.REVIEW_REQUIRED, IDPJobStatus.FAILED, IDPJobStatus.SKIPPED}:
            return job
        if job.attempt < job.max_attempts:
            raise IDPConcurrencyError("IDP job has not exhausted attempts")
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": job_sort_key(job.job_id)}
        now = _now()
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET #status = :failed, #checkpoint = :ambiguous, #error = :error, #updated = :updated REMOVE #claim, #until",
                ExpressionAttributeNames={"#status": "status", "#checkpoint": "checkpoint", "#error": "errorCode", "#updated": "updatedAt", "#claim": "claimToken", "#until": "claimedUntil", "#attempt": "attempt"},
                ExpressionAttributeValues={":failed": IDPJobStatus.FAILED.value, ":ambiguous": IDPCheckpoint.AMBIGUOUS.value, ":error": "MAX_ATTEMPTS_EXHAUSTED", ":updated": now.isoformat(), ":expected_attempt": job.attempt, ":max": job.max_attempts, ":completed": IDPJobStatus.COMPLETED.value, ":review": IDPJobStatus.REVIEW_REQUIRED.value, ":skipped": IDPJobStatus.SKIPPED.value},
                ConditionExpression="#attempt = :expected_attempt AND #attempt >= :max AND #status NOT IN (:completed, :review, :failed, :skipped)",
            )
        except Exception as exc:
            raise IDPConcurrencyError("IDP attempt exhaustion transition lost its race") from exc
        return self.get_job(job_id)  # type: ignore[return-value]

    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str) -> IDPExtractionRun | None:
        response = self.table.get_item(Key={"pk": matter_partition_key(tenant_id, matter_id), "sk": run_sort_key(document_id, run_id)}, ConsistentRead=True)
        item = response.get("Item")
        return _run_from_item(item) if item else None

    def list_runs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int) -> tuple[IDPExtractionRun, ...]:
        return self.list_run_history(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, limit=limit).runs

    def list_run_history(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int, cursor: str | None = None) -> RunHistoryPage:
        _validate_limit(limit)
        from boto3.dynamodb.conditions import Key
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("pk").eq(matter_partition_key(tenant_id, matter_id)) & Key("sk").begins_with(f"IDP#HISTORY#{document_id}#"),
            "ConsistentRead": True, "Limit": limit, "ScanIndexForward": False,
        }
        if cursor is not None:
            kwargs["ExclusiveStartKey"] = _decode_history_cursor(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, cursor=cursor)
        response = self.table.query(**kwargs)
        runs: list[IDPExtractionRun] = []
        for marker in response.get("Items", ()):
            if not isinstance(marker, Mapping) or marker.get("documentId") != document_id or not isinstance(marker.get("runId"), str):
                raise IDPContractError("IDP history marker is malformed")
            run = self.get_run(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, run_id=marker["runId"])
            if run is not None:
                runs.append(run)
        last = response.get("LastEvaluatedKey")
        next_cursor = _history_cursor(tenant_id=tenant_id, matter_id=matter_id, document_id=document_id, key=last) if isinstance(last, Mapping) else None
        return RunHistoryPage(tuple(runs), next_cursor)

    def list_delivery_candidates(self, *, tenant_id: str, matter_id: str, limit: int, cursor: Mapping[str, Any] | str | None = None) -> DeliveryCandidatePage:
        _validate_limit(limit)
        if cursor is not None and not isinstance(cursor, Mapping):
            raise IDPContractError("Dynamo delivery cursor is invalid")
        from boto3.dynamodb.conditions import Attr, Key
        jobs: list[IDPJob] = []
        next_cursor = cursor
        for _ in range(10):
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(matter_partition_key(tenant_id, matter_id)) & Key("sk").begins_with("IDP#JOB#"),
                "FilterExpression": Attr("status").is_in([IDPJobStatus.ENQUEUE_PENDING.value, IDPJobStatus.DELIVERY_IN_FLIGHT.value, IDPJobStatus.DELIVERY_AMBIGUOUS.value]),
                "ConsistentRead": True,
                "Limit": max(1, limit - len(jobs)),
            }
            if next_cursor:
                kwargs["ExclusiveStartKey"] = next_cursor
            response = self.table.query(**kwargs)
            jobs.extend(_job_from_item(item) for item in response.get("Items", ()))
            if len(jobs) >= limit:
                return DeliveryCandidatePage(tuple(jobs[:limit]), response.get("LastEvaluatedKey"))
            next_cursor = response.get("LastEvaluatedKey")
            if not next_cursor:
                break
        return DeliveryCandidatePage(tuple(jobs[:limit]), next_cursor)

    def list_review_jobs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int = 20) -> tuple[IDPJob, ...]:
        _validate_limit(limit)
        from boto3.dynamodb.conditions import Attr, Key
        jobs: list[IDPJob] = []
        cursor: Mapping[str, Any] | None = None
        for _ in range(20):
            kwargs: dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(matter_partition_key(tenant_id, matter_id)) & Key("sk").begins_with("IDP#JOB#"),
                "FilterExpression": Attr("documentId").eq(document_id) & Attr("status").eq(IDPJobStatus.REVIEW_REQUIRED.value),
                "ConsistentRead": True,
                "Limit": max(1, limit - len(jobs)),
            }
            if cursor:
                kwargs["ExclusiveStartKey"] = cursor
            response = self.table.query(**kwargs)
            for item in response.get("Items", ()):
                if isinstance(item, Mapping):
                    jobs.append(_job_from_item(item))
                    if len(jobs) >= limit:
                        return tuple(jobs)
            last = response.get("LastEvaluatedKey")
            if not isinstance(last, Mapping):
                break
            if cursor is not None and dict(last) == dict(cursor):
                raise IDPContractError("review job pagination did not advance")
            cursor = dict(last)
        return tuple(jobs)


class Boto3DynamoStageLedger:
    """Conditional durable ledger for classifier/extractor/OCR paid stages."""

    def __init__(self, table: Any, *, tenant_id: str, matter_id: str) -> None:
        if table is None or not all(isinstance(value, str) and value.strip() for value in (tenant_id, matter_id)):
            raise IDPContractError("stage ledger scope is invalid")
        self.table = table
        self.tenant_id = tenant_id
        self.matter_id = matter_id

    def _key(self, run_id: str, stage: PaidStage) -> dict[str, str]:
        if not isinstance(run_id, str) or not run_id.strip():
            raise IDPContractError("stage run id is invalid")
        return {"pk": matter_partition_key(self.tenant_id, self.matter_id), "sk": stage_sort_key(run_id, stage)}

    def _read(self, run_id: str, stage: PaidStage) -> PaidStageRecord | None:
        item = self.table.get_item(Key=self._key(run_id, stage), ConsistentRead=True).get("Item")
        if not item:
            return None
        try:
            return PaidStageRecord(
                run_id=str(item["runId"]), stage=PaidStage(item["stage"]), request_hash=str(item["requestHash"]),
                state=PaidStageState(item["state"]), lease_token=item.get("leaseToken"),
                lease_until=datetime.fromisoformat(str(item["leaseUntil"])) if item.get("leaseUntil") else None,
                artifact_ref=item.get("artifactRef") if isinstance(item.get("artifactRef"), str) else None,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise IDPContractError("durable stage record is malformed") from exc

    def get(self, *, run_id: str, stage: PaidStage) -> PaidStageRecord | None:
        return self._read(run_id, stage)

    def count(self, *, run_id: str) -> int:
        # The stage set is a closed allowlist.  Point reads avoid introducing
        # an unbounded query (and keep the fake/resource seam faithful).
        return sum(1 for stage in PaidStage if self._read(run_id, stage) is not None)

    def begin(self, *, run_id: str, stage: PaidStage, request_hash: str, lease_seconds: int = 300) -> PaidStageRecord:
        if not isinstance(request_hash, str) or not request_hash.strip() or not isinstance(lease_seconds, int) or isinstance(lease_seconds, bool) or not 1 <= lease_seconds <= 900:
            raise IDPContractError("stage request or lease is invalid")
        existing = self._read(run_id, stage)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IDPContractError("stage request configuration changed")
            if existing.state is PaidStageState.COMMITTED and existing.artifact_ref:
                return existing
            raise IDPConcurrencyError("stage is already in flight or ambiguous")
        now = _now()
        record = PaidStageRecord(run_id, stage, request_hash, PaidStageState.IN_FLIGHT, new_id(), now + timedelta(seconds=lease_seconds))
        item = {
            "entityType": "IDPPaidStage", "pk": self._key(run_id, stage)["pk"], "sk": self._key(run_id, stage)["sk"],
            "runId": run_id, "stage": stage.value, "requestHash": request_hash, "state": record.state.value,
            "leaseToken": record.lease_token, "leaseUntil": record.lease_until.isoformat(), "updatedAt": now.isoformat(),
        }
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except Exception:
            winner = self._read(run_id, stage)
            if winner is not None and winner.state is PaidStageState.COMMITTED and winner.request_hash == request_hash and winner.artifact_ref:
                return winner
            raise IDPConcurrencyError("stage begin lost a conditional race") from None
        return record

    def commit(self, record: PaidStageRecord, *, artifact_ref: str) -> PaidStageRecord:
        if not isinstance(artifact_ref, str) or not artifact_ref.strip() or record.lease_until is None:
            raise IDPContractError("stage commit requires artifact and lease")
        now = _now()
        try:
            self.table.update_item(
                Key=self._key(record.run_id, record.stage),
                UpdateExpression="SET #state = :committed, #artifact = :artifact, #updated = :updated REMOVE #leaseToken, #leaseUntil",
                ExpressionAttributeNames={"#state": "state", "#artifact": "artifactRef", "#updated": "updatedAt", "#leaseToken": "leaseToken", "#leaseUntil": "leaseUntil", "#requestHash": "requestHash"},
                ExpressionAttributeValues={":committed": PaidStageState.COMMITTED.value, ":artifact": artifact_ref, ":updated": now.isoformat(), ":inflight": PaidStageState.IN_FLIGHT.value, ":token": record.lease_token, ":hash": record.request_hash, ":now": now.isoformat()},
                ConditionExpression="#state = :inflight AND #leaseToken = :token AND #leaseUntil > :now AND #requestHash = :hash",
            )
        except Exception as exc:
            raise IDPConcurrencyError("stage commit lost its lease") from exc
        return PaidStageRecord(record.run_id, record.stage, record.request_hash, PaidStageState.COMMITTED, artifact_ref=artifact_ref)

    def mark_ambiguous(self, record: PaidStageRecord) -> PaidStageRecord:
        try:
            self.table.update_item(
                Key=self._key(record.run_id, record.stage),
                UpdateExpression="SET #state = :ambiguous, #updated = :updated REMOVE #leaseToken, #leaseUntil",
                ExpressionAttributeNames={"#state": "state", "#updated": "updatedAt", "#leaseToken": "leaseToken", "#leaseUntil": "leaseUntil"},
                ExpressionAttributeValues={":ambiguous": PaidStageState.AMBIGUOUS.value, ":updated": _now().isoformat(), ":inflight": PaidStageState.IN_FLIGHT.value, ":token": record.lease_token},
                ConditionExpression="#state = :inflight AND #leaseToken = :token",
            )
        except Exception as exc:
            raise IDPConcurrencyError("stage ambiguity transition lost its lease") from exc
        return PaidStageRecord(record.run_id, record.stage, record.request_hash, PaidStageState.AMBIGUOUS)


def _run_from_item(item: Mapping[str, Any]) -> IDPExtractionRun:
    from .models import DocumentType
    fields: dict[str, IDPFieldResult] = {}
    for field_name, raw in (item.get("fields") or {}).items():
        if not isinstance(raw, Mapping):
            raise IDPConcurrencyError("IDP run field record is malformed")
        evidence = tuple(
            EvidenceAnchor(
                page=int(anchor["page"]), quote=str(anchor["quote"]), content_sha256=str(anchor["contentSha256"]),
                start=int(anchor["start"]) if anchor.get("start") is not None else None,
                end=int(anchor["end"]) if anchor.get("end") is not None else None,
            )
            for anchor in raw.get("evidence", ())
        )
        fields[str(field_name)] = IDPFieldResult(
            field=str(raw.get("field", field_name)), value=raw.get("value"),
            presence=FieldPresence(raw["presence"]), origin=FieldOrigin(raw["origin"]),
            acceptance=FieldAcceptance(raw["acceptance"]), evidence=evidence,
            validation=dict(raw.get("validation", {})), schema_version=str(raw.get("schemaVersion", item["schemaVersion"])),
            reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None, provenance=dict(raw.get("provenance", {})) if isinstance(raw.get("provenance", {}), Mapping) else {},
        )
    return IDPExtractionRun(run_id=str(item["runId"]), tenant_id=str(item["tenantId"]), matter_id=str(item["matterId"]), document_id=str(item["documentId"]), document_sha256=str(item["documentSha256"]), document_type=DocumentType(item["documentType"]), schema_version=str(item["schemaVersion"]), model_id=str(item.get("modelId", "")), prompt_version=str(item.get("promptVersion", "")), status=IDPJobStatus(item["status"]), fields=fields, created_at=datetime.fromisoformat(str(item["createdAt"])), source_key=item.get("sourceKey") if isinstance(item.get("sourceKey"), str) else None, page_text_artifact_key=item.get("pageTextArtifactKey") if isinstance(item.get("pageTextArtifactKey"), str) else None)


class AuthoritativeJobLocator:
    """Resolve a queue message through persisted job state and scope."""

    def __init__(self, repository: IDPRepository, document_lookup) -> None:
        self.repository = repository
        self.document_lookup = document_lookup

    def locate(self, payload: Mapping[str, object]) -> tuple[IDPJob, object]:
        job_id = payload.get("jobId")
        if not isinstance(job_id, str) or not job_id.strip() or set(payload) - {"jobId", "eventId"}:
            raise IDPContractError("queue job locator is invalid")
        job = self.repository.get_job(job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        document = self.document_lookup(tenant_id=job.tenant_id, matter_id=job.matter_id, document_id=job.document_id)
        if document is None:
            raise IDPConcurrencyError("IDP document was not found")
        # A clean-promotion projection may not yet have a persisted content
        # hash.  Scope is still mandatory here; the trusted reader must bind
        # and verify the bytes against the job hash before any paid stage.
        if any(getattr(document, document_name, None) != getattr(job, job_name) for document_name, job_name in (("tenant_id", "tenant_id"), ("matter_id", "matter_id"), ("document_id", "document_id"))) or (getattr(document, "content_sha256", None) is not None and document.content_sha256 != job.document_sha256):
            raise IDPConcurrencyError("IDP job/document scope mismatch")
        return job, document


__all__ = [name for name in globals() if not name.startswith("_")]
