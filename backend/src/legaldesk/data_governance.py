"""Operator-only privacy, export, deletion, and retention boundaries.

This module is deliberately not mounted by the public HTTP application.  A
future operator job or authenticated internal control plane must first create
an :class:`OperatorScope` from a verified identity and the authoritative
authorization store.  Browser-supplied tenant IDs, matter IDs, object keys,
and document bodies never enter these operations.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
    require_authorized_context,
)
from .documents import DocumentMetadataRepository, ObjectStorage
from .domain.models import Document, DocumentStatus, ReviewTask, ReviewTaskStatus
from .review_tasks import ReviewTaskRepository


MAX_GOVERNANCE_RECORDS = 100
MAX_EXPORT_BYTES = 256 * 1024
OPERATOR_ROLES = frozenset({"operator", "data_steward", "admin"})


class GovernanceError(Exception):
    """Expected, generic operator governance failure."""


class GovernanceAuthorizationError(GovernanceError, AuthorizationDenied):
    """The operator or exact matter scope is not authorized."""


class GovernanceLimitError(GovernanceError):
    """The bounded operation refused to guess past its safe limit."""


@dataclass(frozen=True, slots=True)
class OperatorScope:
    """Server-derived operator scope; never construct from browser fields."""

    subject: str
    context: RequestContext

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("operator subject is required")
        require_authorized_context(self.context)


def authorize_operator_scope(
    identity: VerifiedIdentity,
    requested_matter_id: str,
    authorization_store: AuthorizationStore,
    *,
    correlation_id: str | None = None,
) -> OperatorScope:
    """Reauthorize an operator against the canonical user/matter records."""

    try:
        context = build_request_context(
            identity,
            requested_matter_id,
            authorization_store,
            correlation_id=correlation_id,
        )
        if not (context.roles & OPERATOR_ROLES):
            raise GovernanceAuthorizationError("operator access denied")
        return OperatorScope(identity.subject, context)
    except (AuthorizationDenied, ValueError) as exc:
        raise GovernanceAuthorizationError("operator access denied") from exc


@dataclass(frozen=True, slots=True)
class GovernanceFailure:
    target: str
    retryable: bool = True


@dataclass(frozen=True, slots=True)
class GovernanceReport:
    operation: str
    tenant_id: str
    matter_id: str
    attempted: int
    completed: int
    failures: tuple[GovernanceFailure, ...] = ()
    state_records_removed: int = 0
    # Bedrock vectors are eventually removed by a subsequent bounded sync;
    # metadata deletion alone is not proof of physical index erasure.
    index_cleanup_pending: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, object]:
        """Safe report with no object keys, bodies, tokens, or provider errors."""

        return {
            "operation": self.operation,
            "tenantId": self.tenant_id,
            "matterId": self.matter_id,
            "attempted": self.attempted,
            "completed": self.completed,
            "stateRecordsRemoved": self.state_records_removed,
            "indexCleanupPending": self.index_cleanup_pending,
            "failures": [
                {"target": failure.target, "retryable": failure.retryable}
                for failure in self.failures
            ],
        }


class _ScopeStateStore(Protocol):
    def delete_scope_state(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> int: ...


def _document_export(document: Document) -> dict[str, object]:
    """Metadata-only export; storage keys and document content are omitted."""

    return {
        "documentId": document.document_id,
        "matterId": document.matter_id,
        "tenantId": document.tenant_id,
        "name": document.name,
        "mediaType": document.media_type,
        "jurisdiction": document.jurisdiction,
        "documentDate": document.document_date,
        "confidentiality": document.confidentiality,
        "status": document.status.value,
        "malwareScanStatus": document.malware_scan_status.value,
        "fileSizeBytes": document.file_size_bytes,
        "uploadedAt": document.uploaded_at.isoformat(),
    }


def _review_export(task: ReviewTask) -> dict[str, object]:
    """Review metadata only; snapshots, notes, and resolution text are excluded."""

    result: dict[str, object] = {
        "reviewTaskId": task.review_task_id,
        "matterId": task.matter_id,
        "tenantId": task.tenant_id,
        "createdByUserId": task.created_by_user_id,
        "reason": task.reason,
        "status": task.status.value,
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
        "correlationId": task.correlation_id,
    }
    if task.due_at is not None:
        result["dueAt"] = task.due_at.isoformat()
    if task.closed_at is not None:
        result["closedAt"] = task.closed_at.isoformat()
    if task.archived_at is not None:
        result["archivedAt"] = task.archived_at.isoformat()
    return result


class OperatorDataGovernanceService:
    """Bounded, idempotent operator data governance operations."""

    def __init__(
        self,
        *,
        metadata_repository: DocumentMetadataRepository,
        object_storage: ObjectStorage,
        review_repository: ReviewTaskRepository,
        state_store: _ScopeStateStore | None = None,
        max_records: int = MAX_GOVERNANCE_RECORDS,
        clock: Any = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not isinstance(max_records, int) or isinstance(max_records, bool) or not 1 <= max_records <= MAX_GOVERNANCE_RECORDS:
            raise ValueError("max_records is invalid")
        self.metadata_repository = metadata_repository
        self.object_storage = object_storage
        self.review_repository = review_repository
        self.state_store = state_store
        self.max_records = max_records
        self.clock = clock

    @staticmethod
    def _scope(scope: OperatorScope) -> RequestContext:
        if not isinstance(scope, OperatorScope):
            raise GovernanceAuthorizationError("operator scope is invalid")
        context = require_authorized_context(scope.context)
        if not (context.roles & OPERATOR_ROLES):
            raise GovernanceAuthorizationError("operator access denied")
        return context

    def export_matter_metadata(
        self,
        scope: OperatorScope,
        *,
        limit: int | None = None,
        include_approved_user_data: bool = False,
    ) -> dict[str, object]:
        """Export bounded metadata only; beta has no document-body export policy."""

        context = self._scope(scope)
        if include_approved_user_data:
            raise GovernanceError("approved user-data export is not enabled for beta")
        bounded = self.max_records if limit is None else limit
        if not isinstance(bounded, int) or isinstance(bounded, bool) or not 1 <= bounded <= self.max_records:
            raise GovernanceLimitError("export limit is invalid")
        documents = tuple(self.metadata_repository.list_for_scope(
            tenant_id=context.tenant_id, matter_id=context.matter_id, limit=bounded + 1
        ))
        if len(documents) > bounded:
            raise GovernanceLimitError("export limit exceeded")
        review_query_limit = min(bounded + 1, 100)
        reviews = tuple(self.review_repository.list(context=context, limit=review_query_limit))
        if len(reviews) > bounded or len(reviews) == bounded:
            # The existing review query contract has no overflow cursor. At
            # the provider limit, equality is ambiguous, so reject rather
            # than silently exporting a truncated matter.
            raise GovernanceLimitError("export review count is not proven complete")
        payload: dict[str, object] = {
            "schemaVersion": "p14-governance-metadata-1",
            "exportedAt": self.clock().isoformat(),
            "tenantId": context.tenant_id,
            "matterId": context.matter_id,
            "documents": [_document_export(document) for document in documents],
            "reviewTasks": [_review_export(task) for task in reviews],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_EXPORT_BYTES:
            raise GovernanceLimitError("export size exceeded")
        return payload

    @staticmethod
    def _object_keys(document: Document, context: RequestContext) -> tuple[str, ...]:
        canonical_prefix = f"tenants/{context.tenant_id}/matters/{context.matter_id}/documents/{document.document_id}/"
        extension = {"application/pdf": ".pdf", "text/plain": ".txt"}.get(document.media_type)
        expected_canonical = f"{canonical_prefix}original{extension}" if extension else None
        if expected_canonical is None or document.s3_key != expected_canonical:
            raise GovernanceError("document storage binding is invalid")
        keys = [document.s3_key, f"{document.s3_key}.metadata.json"]
        if document.quarantine_s3_key is not None:
            expected = f"quarantine/{document.s3_key}"
            if document.quarantine_s3_key != expected:
                raise GovernanceError("document quarantine binding is invalid")
            keys.extend((document.quarantine_s3_key, f"{document.quarantine_s3_key}.metadata.json"))
        return tuple(dict.fromkeys(keys))

    def delete_document(self, scope: OperatorScope, document_id: str) -> GovernanceReport:
        context = self._scope(scope)
        document = self.metadata_repository.get_for_scope(
            tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id
        )
        if document is None:
            return GovernanceReport("delete_document", context.tenant_id, context.matter_id, 1, 1)
        try:
            keys = self._object_keys(document, context)
        except GovernanceError:
            return GovernanceReport(
                "delete_document", context.tenant_id, context.matter_id, 1, 0,
                (GovernanceFailure("document_binding", False),),
            )
        # Tombstone before touching storage.  Retrieval revalidates this
        # status, so a stale Bedrock vector cannot reach the model while
        # object/metadata cleanup is retried.
        try:
            if document.status is not DocumentStatus.FAILED:
                document = self.metadata_repository.update_status(
                    tenant_id=context.tenant_id,
                    matter_id=context.matter_id,
                    document_id=document.document_id,
                    status=DocumentStatus.FAILED,
                )
        except Exception:
            return GovernanceReport(
                "delete_document", context.tenant_id, context.matter_id, 1, 0,
                (GovernanceFailure("metadata_tombstone"),),
                index_cleanup_pending=True,
            )
        failures: list[GovernanceFailure] = []
        for _key in keys:
            try:
                self.object_storage.delete_object(key=_key)
            except Exception:
                failures.append(GovernanceFailure("object_cleanup"))
        if failures:
            return GovernanceReport("delete_document", context.tenant_id, context.matter_id, 1, 0, tuple(failures), index_cleanup_pending=True)
        try:
            self.metadata_repository.delete_for_scope(
                tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document.document_id
            )
        except Exception:
            return GovernanceReport(
                "delete_document", context.tenant_id, context.matter_id, 1, 0,
                (GovernanceFailure("metadata_cleanup"),),
                index_cleanup_pending=True,
            )
        return GovernanceReport("delete_document", context.tenant_id, context.matter_id, 1, 1, index_cleanup_pending=True)

    def delete_matter(self, scope: OperatorScope) -> GovernanceReport:
        context = self._scope(scope)
        documents = tuple(self.metadata_repository.list_for_scope(
            tenant_id=context.tenant_id, matter_id=context.matter_id, limit=self.max_records + 1
        ))
        if len(documents) > self.max_records:
            raise GovernanceLimitError("matter document count exceeds bounded delete limit")
        reviews = tuple(self.review_repository.list(context=context, limit=min(self.max_records, 100)))
        if len(reviews) >= min(self.max_records, 100):
            raise GovernanceLimitError("matter review count reaches bounded delete limit")
        completed = 0
        failures: list[GovernanceFailure] = []
        for document in documents:
            report = self.delete_document(scope, document.document_id)
            completed += report.completed
            failures.extend(report.failures)
        for task in reviews:
            try:
                self.review_repository.delete(context=context, review_task_id=task.review_task_id)
                completed += 1
            except Exception:
                failures.append(GovernanceFailure("review_cleanup"))
        state_removed = 0
        if self.state_store is not None:
            try:
                state_removed = self.state_store.delete_scope_state(
                    subject=scope.subject,
                    tenant_id=context.tenant_id,
                    matter_id=context.matter_id,
                    limit=self.max_records,
                )
            except Exception:
                failures.append(GovernanceFailure("state_cleanup"))
        return GovernanceReport(
            "delete_matter", context.tenant_id, context.matter_id,
            len(documents) + len(reviews), completed, tuple(failures), state_removed,
            index_cleanup_pending=bool(documents),
        )

    def archive_closed_reviews(
        self, scope: OperatorScope, *, older_than: datetime, limit: int | None = None
    ) -> GovernanceReport:
        context = self._scope(scope)
        bounded = self.max_records if limit is None else limit
        if not isinstance(older_than, datetime) or older_than.tzinfo is None:
            raise GovernanceError("retention cutoff is invalid")
        if not isinstance(bounded, int) or isinstance(bounded, bool) or not 1 <= bounded <= self.max_records:
            raise GovernanceLimitError("retention limit is invalid")
        try:
            archived = self.review_repository.archive_closed(
                context=context, older_than=older_than, limit=bounded
            )
        except GovernanceError:
            raise
        except Exception as exc:
            raise GovernanceError("review archival failed") from exc
        return GovernanceReport(
            "archive_closed_reviews", context.tenant_id, context.matter_id,
            len(archived), len(archived),
        )


__all__ = [
    "GovernanceAuthorizationError",
    "GovernanceError",
    "GovernanceFailure",
    "GovernanceLimitError",
    "GovernanceReport",
    "MAX_EXPORT_BYTES",
    "MAX_GOVERNANCE_RECORDS",
    "OperatorDataGovernanceService",
    "OperatorScope",
    "authorize_operator_scope",
]
