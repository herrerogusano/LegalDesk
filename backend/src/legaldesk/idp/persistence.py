"""Option-B IDP persistence and trusted queue-job lookup.

The Boto adapter uses the existing metadata table.  Queue messages contain an
opaque job ID only; the locator first reads the server-owned locator and then
the tenant/matter-scoped item.  No queue field is an authorization source.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
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
    IDPCheckpoint,
    IDPSkipReason,
    new_id,
)


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


class IDPIdempotencyConflict(IDPContractError):
    pass


_CHECKPOINT_NEXT: dict[IDPCheckpoint, frozenset[IDPCheckpoint]] = {
    IDPCheckpoint.CREATED: frozenset({IDPCheckpoint.VALIDATED, IDPCheckpoint.PAID_CALL_READY}),
    IDPCheckpoint.VALIDATED: frozenset({IDPCheckpoint.PAID_CALL_READY}),
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
    def get_job(self, job_id: str) -> IDPJob | None: ...
    def claim_job(self, *, job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None) -> IDPClaim: ...
    def checkpoint_job(self, *, claim: IDPClaim, checkpoint: IDPCheckpoint, status: IDPJobStatus | None = None, skip_reason: IDPSkipReason | None = None, now: datetime | None = None) -> IDPJob: ...
    def mark_delivery(self, *, job_id: str, status: IDPJobStatus) -> IDPJob: ...
    def save_run(self, run: IDPExtractionRun) -> IDPExtractionRun: ...
    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str) -> IDPExtractionRun | None: ...
    def list_runs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int) -> tuple[IDPExtractionRun, ...]: ...
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

    def get_job(self, job_id: str) -> IDPJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def claim_job(self, *, job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None) -> IDPClaim:
        if not isinstance(worker_id, str) or not worker_id.strip() or isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 900:
            raise IDPContractError("claim parameters are invalid")
        now = self.clock() if now is None else now
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise IDPConcurrencyError("IDP job was not found")
            now = self.clock() if now is None else now
            safe_reclaim = job.status in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING) and job.claimed_until is not None and job.claimed_until <= now and job.checkpoint in (IDPCheckpoint.CREATED, IDPCheckpoint.VALIDATED, IDPCheckpoint.PAID_CALL_READY)
            if not ((job.status is IDPJobStatus.QUEUED and job.claim_token is None) or safe_reclaim) or job.attempt >= job.max_attempts:
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
            if job is None or job.claim_token != claim.claim_token or job.status not in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING):
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

    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str) -> IDPExtractionRun | None:
        with self._lock:
            run = self._runs.get((tenant_id, matter_id, run_id))
            return run if run is not None and run.document_id == document_id else None

    def list_runs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int) -> tuple[IDPExtractionRun, ...]:
        _validate_limit(limit)
        with self._lock:
            values = [run for (tenant, matter, _), run in self._runs.items() if tenant == tenant_id and matter == matter_id and run.document_id == document_id]
            return tuple(sorted(values, key=lambda item: item.created_at, reverse=True)[:limit])

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

    def claim_job(self, *, job_id: str, worker_id: str, lease_seconds: int, now: datetime | None = None) -> IDPClaim:
        if not isinstance(worker_id, str) or not worker_id.strip() or isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 900:
            raise IDPContractError("claim parameters are invalid")
        job = self.get_job(job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        now = _now() if now is None else now
        safe_reclaim = job.status in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING) and job.claimed_until is not None and job.claimed_until <= now and job.checkpoint in (IDPCheckpoint.CREATED, IDPCheckpoint.VALIDATED, IDPCheckpoint.PAID_CALL_READY)
        if not ((job.status is IDPJobStatus.QUEUED and job.claim_token is None) or safe_reclaim) or job.attempt >= job.max_attempts:
            raise IDPConcurrencyError("IDP job is not claimable")
        token = new_id()
        until = now + timedelta(seconds=lease_seconds)
        key = {"pk": matter_partition_key(job.tenant_id, job.matter_id), "sk": job_sort_key(job.job_id)}
        try:
            self.table.update_item(Key=key, UpdateExpression="SET #status = :claimed, #attempt = #attempt + :one, #claim = :claim, #until = :until, #updated = :updated", ExpressionAttributeNames={"#status": "status", "#attempt": "attempt", "#claim": "claimToken", "#until": "claimedUntil", "#checkpoint": "checkpoint", "#updated": "updatedAt"}, ExpressionAttributeValues={":claimed": IDPJobStatus.CLAIMED.value, ":one": 1, ":claim": token, ":until": until.isoformat(), ":updated": now.isoformat(), ":queued": IDPJobStatus.QUEUED.value, ":claimed_status": IDPJobStatus.CLAIMED.value, ":processing_status": IDPJobStatus.PROCESSING.value, ":now": now.isoformat(), ":created": IDPCheckpoint.CREATED.value, ":validated": IDPCheckpoint.VALIDATED.value, ":ready": IDPCheckpoint.PAID_CALL_READY.value, ":max_attempts": job.max_attempts}, ConditionExpression="((#status = :queued AND attribute_not_exists(#claim)) OR (#status IN (:claimed_status, :processing_status) AND #until <= :now AND #checkpoint IN (:created, :validated, :ready))) AND #attempt < :max_attempts")
        except Exception as exc:
            raise IDPConcurrencyError("IDP job is not claimable") from exc
        return IDPClaim(job_id, token, worker_id, until)

    def checkpoint_job(self, *, claim: IDPClaim, checkpoint: IDPCheckpoint, status: IDPJobStatus | None = None, skip_reason: IDPSkipReason | None = None, now: datetime | None = None) -> IDPJob:
        job = self.get_job(claim.job_id)
        if job is None:
            raise IDPConcurrencyError("IDP job was not found")
        now = _now() if now is None else now
        if job.claim_token != claim.claim_token or job.status not in (IDPJobStatus.CLAIMED, IDPJobStatus.PROCESSING) or job.claimed_until is None or job.claimed_until <= now:
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
                "field": result.field, "value": result.value, "presence": result.presence.value,
                "origin": result.origin.value, "acceptance": result.acceptance.value,
                "evidence": [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in result.evidence],
                "validation": dict(result.validation), "schemaVersion": result.schema_version, "reason": result.reason,
            }
            for name, result in run.fields.items()
        }
        item = {"entityType": "IDPExtractionRun", "pk": key["pk"], "sk": key["sk"], "runId": run.run_id, "tenantId": run.tenant_id, "matterId": run.matter_id, "documentId": run.document_id, "documentSha256": run.document_sha256, "documentType": run.document_type.value, "schemaVersion": run.schema_version, "modelId": run.model_id, "promptVersion": run.prompt_version, "status": run.status.value, "fields": fields, "createdAt": run.created_at.isoformat()}
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except Exception:
            response = self.table.get_item(Key=key, ConsistentRead=True)
            if response.get("Item") != item:
                raise IDPConcurrencyError("IDP run is immutable") from None
        return run

    def get_run(self, *, tenant_id: str, matter_id: str, document_id: str, run_id: str) -> IDPExtractionRun | None:
        response = self.table.get_item(Key={"pk": matter_partition_key(tenant_id, matter_id), "sk": run_sort_key(document_id, run_id)}, ConsistentRead=True)
        item = response.get("Item")
        return _run_from_item(item) if item else None

    def list_runs(self, *, tenant_id: str, matter_id: str, document_id: str, limit: int) -> tuple[IDPExtractionRun, ...]:
        _validate_limit(limit)
        from boto3.dynamodb.conditions import Key
        response = self.table.query(KeyConditionExpression=Key("pk").eq(matter_partition_key(tenant_id, matter_id)) & Key("sk").begins_with(f"IDP#RUN#{document_id}#"), ConsistentRead=True, Limit=limit)
        return tuple(_run_from_item(item) for item in response.get("Items", ()))

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
            reason=raw.get("reason") if isinstance(raw.get("reason"), str) else None,
        )
    return IDPExtractionRun(run_id=str(item["runId"]), tenant_id=str(item["tenantId"]), matter_id=str(item["matterId"]), document_id=str(item["documentId"]), document_sha256=str(item["documentSha256"]), document_type=DocumentType(item["documentType"]), schema_version=str(item["schemaVersion"]), model_id=str(item.get("modelId", "")), prompt_version=str(item.get("promptVersion", "")), status=IDPJobStatus(item["status"]), fields=fields, created_at=datetime.fromisoformat(str(item["createdAt"])))


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
        if any(getattr(document, document_name, None) != getattr(job, job_name) for document_name, job_name in (("tenant_id", "tenant_id"), ("matter_id", "matter_id"), ("document_id", "document_id"), ("content_sha256", "document_sha256"))):
            raise IDPConcurrencyError("IDP job/document scope mismatch")
        return job, document


__all__ = [name for name in globals() if not name.startswith("_")]
