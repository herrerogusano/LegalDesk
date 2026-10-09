"""Provider-neutral domain models.

These records intentionally contain IDs and metadata, not document bodies,
tokens, prompts, or other secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Mapping
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MatterStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class DocumentStatus(StrEnum):
    """Lifecycle states owned by the document pipeline.

    Values are intentionally explicit because they are exposed to callers and
    persisted in metadata.  Indexing is implemented by a later phase.
    """

    PENDING_UPLOAD = "PENDING_UPLOAD"
    UPLOADED = "UPLOADED"
    PENDING_INGESTION = "PENDING_INGESTION"
    INDEXED = "INDEXED"
    FAILED = "FAILED"


class MalwareScanStatus(StrEnum):
    """Server-owned malware gate; only a clean result permits indexing."""

    PENDING = "PENDING"
    CLEAN = "NO_THREATS_FOUND"
    THREATS_FOUND = "THREATS_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    ACCESS_DENIED = "ACCESS_DENIED"
    FAILED = "FAILED"


# Kept as a compatibility name for callers that used the Phase 00 draft.
IngestionStatus = DocumentStatus


class ReviewTaskStatus(StrEnum):
    OPEN = "open"
    IN_REVIEW = "in_review"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class User:
    user_id: str
    verified_subject: str
    tenant_ids: frozenset[str]
    roles: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Matter:
    matter_id: str
    tenant_id: str
    name: str
    authorized_user_ids: frozenset[str]
    status: MatterStatus = MatterStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Document:
    document_id: str
    matter_id: str
    tenant_id: str
    name: str
    s3_key: str
    media_type: str
    jurisdiction: str
    document_date: str
    confidentiality: str
    status: DocumentStatus = DocumentStatus.PENDING_UPLOAD
    file_size_bytes: int = 0
    uploaded_at: datetime = field(default_factory=utc_now)
    quarantine_s3_key: str | None = None
    malware_scan_status: MalwareScanStatus = MalwareScanStatus.PENDING
    malware_scan_etag: str | None = None
    malware_scan_version_id: str | None = None
    # Additive IDP projection.  These values are a server-owned pointer to
    # the current IDP generation; they are not part of the canonical/RAG
    # lifecycle and remain absent for documents without an IDP run.
    idp_status: str | None = None
    idp_reason: str | None = None
    idp_attempt: int | None = None
    idp_job_id: str | None = None
    idp_run_id: str | None = None
    idp_document_sha256: str | None = None
    idp_generation_at: str | None = None
    idp_source_key: str | None = None

    @property
    def ingestion_status(self) -> DocumentStatus:
        """Compatibility view for the pre-Phase-02 field name."""

        return self.status


@dataclass(frozen=True, slots=True)
class Conversation:
    conversation_id: str
    user_id: str
    matter_id: str
    session_id: str
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class ReviewTask:
    review_task_id: str
    matter_id: str
    tenant_id: str
    created_by_user_id: str
    reason: str
    status: ReviewTaskStatus = ReviewTaskStatus.OPEN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    # Correlation metadata is safe to persist; workflow snapshots are bounded
    # answer/citation records and never contain full documents or storage URIs.
    correlation_id: str = ""
    # Workflow fields were added after the Phase 07 metadata-only contract.
    # Defaults keep old synthetic records readable while new workflow creates
    # always populate the durable snapshot and due date.
    snapshot: Mapping[str, object] | None = None
    note: str = ""
    due_at: date | None = None
    closed_at: datetime | None = None
    resolution_note: str = ""
    # Operator retention may strip workflow/user text while retaining a
    # bounded metadata record for auditability.
    archived_at: datetime | None = None
    # Optional additive source reference for IDP-created review tasks.  Human
    # chat tasks leave these unset and retain their existing contract.
    source: str | None = None
    idp_run_id: str | None = None
    idp_document_id: str | None = None
    idp_document_sha256: str | None = None
    idp_field_names: tuple[str, ...] = ()

    @property
    def reason_code(self) -> str:
        """Compatibility-friendly name for the closed review reason value."""

        return self.reason
