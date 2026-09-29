"""Bounded, fail-closed reconciliation for public-beta lifecycle state.

Reconciliation is deliberately candidate-driven.  Callers supply explicit
tenant/matter scopes or bounded operational candidate IDs; this module never
discovers work with a table or repository scan.  TTL remains a storage
cleanup hint, not an authorization or lifecycle decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import math
from time import time
from typing import Callable, Mapping, Sequence

from .documents import DocumentMetadataRepository, ObjectStorage
from .domain.models import DocumentStatus
from .gateway_interceptor import GatewayGrantRepository
from .ingestion import AsyncKnowledgeBaseIngestionService
from .state import EphemeralStateStore


MAX_RECONCILIATION_BATCH = 100
_INGESTION_STALE_STATUSES = frozenset({"STARTING", "RECOVERY_PENDING", "PENDING"})


@dataclass(frozen=True, slots=True)
class ReconciliationScope:
    """One explicitly authorized tenant/matter query scope."""

    tenant_id: str
    matter_id: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (self.tenant_id, self.matter_id)):
            raise ValueError("reconciliation scope is invalid")


@dataclass(frozen=True, slots=True)
class IngestionReconciliationCandidate:
    """An operation ID plus the server-owned scope expected for that ID."""

    operation_id: str
    subject: str
    tenant_id: str
    matter_id: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.operation_id, self.subject, self.tenant_id, self.matter_id)
        ):
            raise ValueError("ingestion reconciliation candidate is invalid")


@dataclass(frozen=True, slots=True)
class IngestionReconciliationScope:
    """An explicit subject/tenant/matter partition for operation queries."""

    subject: str
    tenant_id: str
    matter_id: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.subject, self.tenant_id, self.matter_id)
        ):
            raise ValueError("ingestion reconciliation scope is invalid")


@dataclass(frozen=True, slots=True)
class GatewayReconciliationCandidate:
    """One point-read Gateway grant or invocation candidate.

    Gateway records currently bind subject and matter, but not tenant.  The
    expected subject/matter are therefore mandatory and are checked before a
    cleanup delete.
    """

    record_id: str
    kind: str
    verified_subject: str
    matter_id: str

    def __post_init__(self) -> None:
        if self.kind not in {"grant", "invocation"}:
            raise ValueError("gateway reconciliation kind is invalid")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.record_id, self.verified_subject, self.matter_id)
        ):
            raise ValueError("gateway reconciliation candidate is invalid")


@dataclass(frozen=True, slots=True)
class ReconciliationItem:
    identifier: str
    outcome: str


@dataclass(slots=True)
class ReconciliationReport:
    examined: int = 0
    changed: int = 0
    skipped: int = 0
    ambiguous: int = 0
    failed: int = 0
    items: list[ReconciliationItem] = field(default_factory=list)

    def record(self, identifier: str, outcome: str) -> None:
        self.items.append(ReconciliationItem(identifier, outcome))


class ReconciliationService:
    """Run bounded lifecycle repair against already-authorized candidates."""

    def __init__(
        self,
        *,
        metadata_repository: DocumentMetadataRepository,
        object_storage: ObjectStorage,
        state_store: EphemeralStateStore,
        ingestion_service: AsyncKnowledgeBaseIngestionService | None = None,
        gateway_repository: GatewayGrantRepository | None = None,
        clock: Callable[[], float] = time,
        max_batch: int = MAX_RECONCILIATION_BATCH,
    ) -> None:
        if not isinstance(max_batch, int) or isinstance(max_batch, bool) or not 0 < max_batch <= MAX_RECONCILIATION_BATCH:
            raise ValueError("max_batch is invalid")
        self.metadata_repository = metadata_repository
        self.object_storage = object_storage
        self.state_store = state_store
        self.ingestion_service = ingestion_service
        self.gateway_repository = gateway_repository
        self.clock = clock
        self.max_batch = max_batch

    def reconcile_pending_uploads(
        self,
        *,
        scopes: Sequence[ReconciliationScope],
        stale_after_seconds: float,
        limit_per_scope: int | None = None,
    ) -> ReconciliationReport:
        """Fail abandoned pending uploads only after object cleanup succeeds."""

        scopes = self._bounded(scopes, "scopes")
        limit = self._limit(limit_per_scope or self.max_batch)
        stale_after = self._positive_duration(stale_after_seconds)
        now = self.clock()
        report = ReconciliationReport()
        for scope in scopes:
            try:
                documents = self.metadata_repository.list_for_scope(
                    tenant_id=scope.tenant_id, matter_id=scope.matter_id, limit=limit
                )
            except Exception:
                report.failed += 1
                report.record(f"{scope.tenant_id}/{scope.matter_id}", "query_failed")
                continue
            for document in documents:
                report.examined += 1
                identifier = f"{scope.tenant_id}/{scope.matter_id}/{getattr(document, 'document_id', '')}"
                if (
                    document.tenant_id != scope.tenant_id
                    or document.matter_id != scope.matter_id
                ):
                    report.skipped += 1
                    report.record(identifier, "scope_mismatch")
                    continue
                if document.status is not DocumentStatus.PENDING_UPLOAD:
                    report.skipped += 1
                    report.record(identifier, "not_pending_upload")
                    continue
                quarantine_prefix = (
                    f"quarantine/tenants/{scope.tenant_id}/matters/{scope.matter_id}/documents/"
                )
                upload_key = document.quarantine_s3_key
                if not isinstance(upload_key, str) or not upload_key.startswith(quarantine_prefix):
                    # The public reconciler may delete only quarantined beta
                    # uploads. Canonical source keys are never cleanup targets.
                    report.ambiguous += 1
                    report.record(identifier, "non_quarantine_key")
                    continue
                uploaded_at = document.uploaded_at
                if not isinstance(uploaded_at, datetime):
                    report.ambiguous += 1
                    report.record(identifier, "invalid_timestamp")
                    continue
                try:
                    age = now - uploaded_at.timestamp()
                except (OverflowError, OSError, ValueError):
                    report.ambiguous += 1
                    report.record(identifier, "invalid_timestamp")
                    continue
                if not math.isfinite(age) or age < stale_after:
                    report.skipped += 1
                    report.record(identifier, "not_stale")
                    continue
                try:
                    self.object_storage.delete_object(key=upload_key)
                    self.object_storage.delete_object(key=f"{upload_key}.metadata.json")
                    self.metadata_repository.update_status(
                        tenant_id=scope.tenant_id,
                        matter_id=scope.matter_id,
                        document_id=document.document_id,
                        status=DocumentStatus.FAILED,
                    )
                except Exception:
                    report.failed += 1
                    report.record(identifier, "cleanup_failed")
                    continue
                report.changed += 1
                report.record(identifier, "marked_failed")
        return report

    def reconcile_ingestion(
        self,
        *,
        candidates: Sequence[IngestionReconciliationCandidate],
        stale_after_seconds: float,
    ) -> ReconciliationReport:
        """Retry only stale operation candidates in their exact stored scope."""

        if self.ingestion_service is None:
            raise ValueError("ingestion reconciliation is not configured")
        candidates = self._bounded(candidates, "ingestion candidates")
        stale_after = self._positive_duration(stale_after_seconds)
        now = self.clock()
        report = ReconciliationReport()
        for candidate in candidates:
            report.examined += 1
            operation = self.state_store.get_ingestion_operation(candidate.operation_id)
            if operation is None:
                report.skipped += 1
                report.record(candidate.operation_id, "unavailable")
                continue
            if (
                operation.subject != candidate.subject
                or operation.tenant_id != candidate.tenant_id
                or operation.matter_id != candidate.matter_id
            ):
                report.skipped += 1
                report.record(candidate.operation_id, "scope_mismatch")
                continue
            if not self._valid_future_expiry(operation.expires_at, now):
                report.ambiguous += 1
                report.record(candidate.operation_id, "invalid_or_expired")
                continue
            if operation.status not in _INGESTION_STALE_STATUSES:
                report.skipped += 1
                report.record(candidate.operation_id, "not_reconcilable")
                continue
            if not self._stale(operation.updated_at, now, stale_after):
                report.skipped += 1
                report.record(candidate.operation_id, "not_stale")
                continue
            if operation.status == "STARTING" and not operation.ingestion_job_id:
                # A provider timeout may have created a job.  Releasing this
                # binding would allow a second job, so keep it quarantined.
                report.ambiguous += 1
                report.record(candidate.operation_id, "ambiguous_provider_start")
                continue
            try:
                repaired = self.ingestion_service.status(
                    operation_id=candidate.operation_id,
                    subject=candidate.subject,
                    tenant_id=candidate.tenant_id,
                    matter_id=candidate.matter_id,
                )
            except Exception:
                report.failed += 1
                report.record(candidate.operation_id, "status_failed")
                continue
            if repaired != operation:
                report.changed += 1
                report.record(candidate.operation_id, "status_reconciled")
            else:
                report.skipped += 1
                report.record(candidate.operation_id, "status_unchanged")
        return report

    def reconcile_ingestion_scopes(
        self,
        *,
        scopes: Sequence[IngestionReconciliationScope],
        stale_after_seconds: float,
        limit_per_scope: int | None = None,
    ) -> ReconciliationReport:
        """Discover stale operations only through explicit bounded partitions."""

        scopes = self._bounded(scopes, "ingestion scopes")
        limit = self._limit(limit_per_scope or self.max_batch)
        report = ReconciliationReport()
        for scope in scopes:
            try:
                operations = self.state_store.list_ingestion_operations_for_scope(
                    subject=scope.subject,
                    tenant_id=scope.tenant_id,
                    matter_id=scope.matter_id,
                    limit=limit,
                )
            except Exception:
                report.failed += 1
                report.record(f"{scope.subject}/{scope.tenant_id}/{scope.matter_id}", "query_failed")
                continue
            candidates = tuple(
                IngestionReconciliationCandidate(
                    operation_id=operation.operation_id,
                    subject=scope.subject,
                    tenant_id=scope.tenant_id,
                    matter_id=scope.matter_id,
                )
                for operation in operations
            )
            child = self.reconcile_ingestion(
                candidates=candidates,
                stale_after_seconds=stale_after_seconds,
            ) if candidates else ReconciliationReport()
            self._merge(report, child)
        return report

    def reconcile_gateway(
        self,
        *,
        candidates: Sequence[GatewayReconciliationCandidate],
    ) -> ReconciliationReport:
        """Delete only valid, expired point-read Gateway records."""

        if self.gateway_repository is None:
            raise ValueError("Gateway reconciliation is not configured")
        candidates = self._bounded(candidates, "Gateway candidates")
        now = self.clock()
        report = ReconciliationReport()
        for candidate in candidates:
            report.examined += 1
            try:
                item = (
                    self.gateway_repository.get(candidate.record_id)
                    if candidate.kind == "grant"
                    else self.gateway_repository.get_invocation(candidate.record_id)
                )
            except Exception:
                report.failed += 1
                report.record(candidate.record_id, "query_failed")
                continue
            if item is None:
                report.skipped += 1
                report.record(candidate.record_id, "unavailable")
                continue
            expected_entity = (
                "GatewayAuthorizationGrant" if candidate.kind == "grant" else "HarnessInvocationBinding"
            )
            expected_prefix = (
                f"GATEWAY#GRANT#{candidate.record_id}"
                if candidate.kind == "grant"
                else f"GATEWAY#INVOCATION#{candidate.record_id}"
            )
            if (
                item.get("entityType") != expected_entity
                or item.get("pk") != expected_prefix
                or item.get("sk") != "PROFILE"
                or item.get("verifiedSubject") != candidate.verified_subject
                or item.get("requestedMatterId") != candidate.matter_id
            ):
                report.ambiguous += 1
                report.record(candidate.record_id, "scope_or_shape_mismatch")
                continue
            expires_at = item.get("expiresAt")
            try:
                expires_at = float(expires_at)
            except (TypeError, ValueError):
                expires_at = math.nan
            if not math.isfinite(expires_at):
                report.ambiguous += 1
                report.record(candidate.record_id, "invalid_expiry")
                continue
            if expires_at > now:
                report.skipped += 1
                report.record(candidate.record_id, "not_expired")
                continue
            try:
                if candidate.kind == "grant":
                    self.gateway_repository.delete(candidate.record_id)
                else:
                    self.gateway_repository.delete_invocation(candidate.record_id)
            except Exception:
                report.failed += 1
                report.record(candidate.record_id, "delete_failed")
                continue
            report.changed += 1
            report.record(candidate.record_id, "deleted_expired")
        return report

    def reconcile_gateway_indexed(
        self,
        *,
        now: float | None = None,
        limit: int | None = None,
        allowed_matter_ids: Sequence[str],
    ) -> ReconciliationReport:
        """Enumerate expiry-index candidates, then revalidate by point-read.

        The repository-owned expiry index is an operational hint, not an
        authorization source. Every candidate is converted to the same
        server-bound point-read path as an explicit candidate, so malformed or
        stale index rows fail closed and cannot cause a cross-scope delete.
        """

        if self.gateway_repository is None:
            raise ValueError("Gateway reconciliation is not configured")
        allowed_matter_ids = self._bounded(allowed_matter_ids, "Gateway matter allowlist")
        if any(not isinstance(matter_id, str) or not matter_id.strip() for matter_id in allowed_matter_ids):
            raise ValueError("Gateway matter allowlist is invalid")
        allowed_matters = frozenset(allowed_matter_ids)
        enumerate_candidates = getattr(self.gateway_repository, "list_expired_candidates", None)
        if not callable(enumerate_candidates):
            raise ValueError("Gateway expiry index is not configured")
        bounded_limit = self._limit(limit or self.max_batch)
        candidates: list[GatewayReconciliationCandidate] = []
        report = ReconciliationReport()
        try:
            indexed = enumerate_candidates(now=self.clock() if now is None else now, limit=bounded_limit)
        except Exception:
            report = ReconciliationReport(failed=1)
            report.record("gateway-expiry-index", "query_failed")
            return report
        for item in indexed:
            if not isinstance(item, Mapping):
                report.examined += 1
                report.ambiguous += 1
                continue
            record_id = item.get("recordId")
            kind = item.get("kind")
            subject = item.get("verifiedSubject")
            matter_id = item.get("requestedMatterId")
            if not isinstance(matter_id, str) or matter_id not in allowed_matters:
                report.examined += 1
                report.skipped += 1
                report.record(str(record_id) if isinstance(record_id, str) else "gateway-expiry-index", "scope_mismatch")
                continue
            if all(isinstance(value, str) and value.strip() for value in (record_id, kind, subject, matter_id)):
                try:
                    candidates.append(
                        GatewayReconciliationCandidate(
                            record_id=record_id,
                            kind=kind,
                            verified_subject=subject,
                            matter_id=matter_id,
                        )
                    )
                except ValueError:
                    continue
        if candidates:
            child = self.reconcile_gateway(candidates=tuple(candidates))
            self._merge(report, child)
        return report

    def _bounded(self, values: Sequence[object], label: str) -> tuple[object, ...]:
        if isinstance(values, (str, bytes)):
            raise ValueError(f"{label} must be a bounded sequence")
        values = tuple(values)
        if not values or len(values) > self.max_batch:
            raise ValueError(f"{label} exceeds the reconciliation batch limit")
        return values

    @staticmethod
    def _merge(target: ReconciliationReport, source: ReconciliationReport) -> None:
        target.examined += source.examined
        target.changed += source.changed
        target.skipped += source.skipped
        target.ambiguous += source.ambiguous
        target.failed += source.failed
        target.items.extend(source.items)

    def _limit(self, value: int) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= self.max_batch:
            raise ValueError("reconciliation query limit is invalid")
        return value

    @staticmethod
    def _positive_duration(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or value <= 0:
            raise ValueError("reconciliation stale duration is invalid")
        return float(value)

    @staticmethod
    def _valid_future_expiry(value: object, now: float) -> bool:
        try:
            expiry = float(value)
        except (TypeError, ValueError):
            return False
        return math.isfinite(expiry) and expiry > now

    @staticmethod
    def _stale(updated_at: object, now: float, stale_after: float) -> bool:
        try:
            updated = float(updated_at)
        except (TypeError, ValueError):
            return False
        return math.isfinite(updated) and now - updated >= stale_after


__all__ = [
    "GatewayReconciliationCandidate",
    "IngestionReconciliationCandidate",
    "IngestionReconciliationScope",
    "MAX_RECONCILIATION_BATCH",
    "ReconciliationItem",
    "ReconciliationReport",
    "ReconciliationScope",
    "ReconciliationService",
]
