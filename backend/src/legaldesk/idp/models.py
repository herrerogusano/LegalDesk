"""Provider-neutral contracts for the Phase 14 IDP foundation.

This module deliberately contains no AWS SDK calls and no document bodies.  It
is the small, versioned boundary shared by the future extractor, persistence
adapter and queue worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping
from uuid import uuid4
import re


DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PAGES = 100
IDP_SCHEMA_VERSION = "1.0.0"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DocumentType(StrEnum):
    CONTRACT = "CONTRACT"
    DEMAND = "DEMAND"
    JUDGMENT = "JUDGMENT"
    UNKNOWN = "UNKNOWN"


class FieldPresence(StrEnum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class FieldOrigin(StrEnum):
    LITERAL = "LITERAL"
    DERIVED = "DERIVED"
    INTERPRETIVE = "INTERPRETIVE"


class FieldAcceptance(StrEnum):
    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    PROVISIONAL = "PROVISIONAL"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    HUMAN_CONFIRMED = "HUMAN_CONFIRMED"
    REJECTED = "REJECTED"
    UNAVAILABLE = "UNAVAILABLE"


class IDPJobStatus(StrEnum):
    ENQUEUE_PENDING = "ENQUEUE_PENDING"
    DELIVERY_IN_FLIGHT = "DELIVERY_IN_FLIGHT"
    DELIVERY_AMBIGUOUS = "DELIVERY_AMBIGUOUS"
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    PROCESSING = "PROCESSING"
    WAITING_FOR_OCR = "WAITING_FOR_OCR"
    SKIPPED = "IDP_SKIPPED"
    REVIEW_REQUIRED = "IDP_REVIEW_REQUIRED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IDPCheckpoint(StrEnum):
    CREATED = "CREATED"
    VALIDATED = "VALIDATED"
    PAID_CALL_READY = "PAID_CALL_READY"
    PAID_CALL_IN_FLIGHT = "PAID_CALL_IN_FLIGHT"
    PAID_CALL_COMMITTED = "PAID_CALL_COMMITTED"
    PERSISTED = "PERSISTED"
    DONE = "DONE"
    AMBIGUOUS = "AMBIGUOUS"


class IDPSkipReason(StrEnum):
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    SIZE_LIMIT = "SIZE_LIMIT"
    PAGE_LIMIT = "PAGE_LIMIT"
    MALWARE_NOT_CLEAN = "MALWARE_NOT_CLEAN"
    DOCUMENT_NOT_FOUND = "DOCUMENT_NOT_FOUND"


PUBLIC_IDP_STATUS = MappingProxyType({
    IDPJobStatus.ENQUEUE_PENDING: "PENDING_IDP",
    IDPJobStatus.DELIVERY_IN_FLIGHT: "PENDING_IDP",
    IDPJobStatus.DELIVERY_AMBIGUOUS: "PENDING_IDP",
    IDPJobStatus.QUEUED: "PENDING_IDP",
    IDPJobStatus.CLAIMED: "PROCESSING_IDP",
    IDPJobStatus.PROCESSING: "PROCESSING_IDP",
    IDPJobStatus.WAITING_FOR_OCR: "PROCESSING_IDP",
    IDPJobStatus.SKIPPED: "IDP_SKIPPED",
    IDPJobStatus.REVIEW_REQUIRED: "IDP_REVIEW_REQUIRED",
    IDPJobStatus.COMPLETED: "IDP_COMPLETED",
    IDPJobStatus.FAILED: "IDP_FAILED",
})


def public_idp_status(status: IDPJobStatus) -> str:
    if not isinstance(status, IDPJobStatus):
        raise IDPContractError("IDP status is invalid")
    return PUBLIC_IDP_STATUS[status]


class IDPContractError(ValueError):
    """A malformed or unsafe IDP contract value."""


class IDPConcurrencyError(RuntimeError):
    """A conditional IDP transition lost a race or found stale state."""


class IDPDeliveryAmbiguous(RuntimeError):
    """Queue delivery outcome is unknown; automatic retry is prohibited."""


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IDPContractError(f"{field_name} is required")
    return value.strip()


def _sha256(value: object, field_name: str) -> str:
    value = _nonempty(value, field_name)
    if _SHA256.fullmatch(value) is None:
        raise IDPContractError(f"{field_name} is invalid")
    return value.lower()


def _aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise IDPContractError(f"{field_name} must be timezone-aware")
    return value


def new_id() -> str:
    return str(uuid4())


@dataclass(frozen=True, slots=True)
class IDPConfig:
    """Bounded IDP limits; these do not widen public upload limits."""

    max_bytes: int = DEFAULT_MAX_BYTES
    max_pages: int = DEFAULT_MAX_PAGES
    visibility_timeout_seconds: int = 300
    max_attempts: int = 3
    max_calls_per_run: int = 2
    global_deadline_seconds: int = 330

    def __post_init__(self) -> None:
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int) or not 1 <= self.max_bytes <= 100 * 1024 * 1024:
            raise IDPContractError("max_bytes is invalid")
        if isinstance(self.max_pages, bool) or not isinstance(self.max_pages, int) or not 1 <= self.max_pages <= 1000:
            raise IDPContractError("max_pages is invalid")
        if isinstance(self.visibility_timeout_seconds, bool) or not isinstance(self.visibility_timeout_seconds, int) or not 1 <= self.visibility_timeout_seconds <= 900:
            raise IDPContractError("visibility_timeout_seconds is invalid")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise IDPContractError("max_attempts is invalid")
        if isinstance(self.max_calls_per_run, bool) or not isinstance(self.max_calls_per_run, int) or not 1 <= self.max_calls_per_run <= 8:
            raise IDPContractError("max_calls_per_run is invalid")
        if isinstance(self.global_deadline_seconds, bool) or not isinstance(self.global_deadline_seconds, int) or not 60 <= self.global_deadline_seconds <= 330:
            raise IDPContractError("global_deadline_seconds is invalid")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> "IDPConfig":
        def integer(name: str, default: int) -> int:
            value = values.get(name, default)
            if isinstance(value, bool):
                raise IDPContractError(f"{name} is invalid")
            if isinstance(value, int):
                return value
            if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
                return int(value.strip())
            raise IDPContractError(f"{name} is invalid")

        return cls(
            max_bytes=integer("max_bytes", DEFAULT_MAX_BYTES),
            max_pages=integer("max_pages", DEFAULT_MAX_PAGES),
            visibility_timeout_seconds=integer("visibility_timeout_seconds", 300),
            max_attempts=integer("max_attempts", 3),
            max_calls_per_run=integer("max_calls_per_run", 2),
            global_deadline_seconds=integer("global_deadline_seconds", 330),
        )


@dataclass(frozen=True, slots=True)
class EvidenceAnchor:
    page: int
    quote: str
    content_sha256: str
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 1:
            raise IDPContractError("evidence page is invalid")
        _nonempty(self.quote, "evidence quote")
        _sha256(self.content_sha256, "evidence content_sha256")
        if self.start is not None and (isinstance(self.start, bool) or not isinstance(self.start, int) or self.start < 0):
            raise IDPContractError("evidence start is invalid")
        if self.end is not None and (isinstance(self.end, bool) or not isinstance(self.end, int) or self.end < 0):
            raise IDPContractError("evidence end is invalid")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise IDPContractError("evidence span is invalid")


@dataclass(frozen=True, slots=True)
class IDPFieldResult:
    field: str
    value: Any = None
    presence: FieldPresence = FieldPresence.UNKNOWN
    origin: FieldOrigin = FieldOrigin.LITERAL
    acceptance: FieldAcceptance = FieldAcceptance.UNAVAILABLE
    evidence: tuple[EvidenceAnchor, ...] = ()
    validation: Mapping[str, bool] = field(default_factory=dict)
    schema_version: str = IDP_SCHEMA_VERSION
    reason: str | None = None
    provenance: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _nonempty(self.field, "field")
        _nonempty(self.schema_version, "schema_version")
        if not isinstance(self.presence, FieldPresence):
            raise IDPContractError("presence is invalid")
        if not isinstance(self.origin, FieldOrigin):
            raise IDPContractError("origin is invalid")
        if not isinstance(self.acceptance, FieldAcceptance):
            raise IDPContractError("acceptance is invalid")
        if self.presence is FieldPresence.PRESENT and self.value is None:
            raise IDPContractError("present fields require a value")
        if self.presence is not FieldPresence.PRESENT and self.value is not None:
            raise IDPContractError("non-present fields cannot contain a value")
        if not isinstance(self.evidence, tuple) or any(not isinstance(item, EvidenceAnchor) for item in self.evidence):
            raise IDPContractError("evidence is invalid")
        if any(not isinstance(key, str) or not isinstance(value, bool) for key, value in self.validation.items()):
            raise IDPContractError("validation is invalid")
        object.__setattr__(self, "validation", MappingProxyType(dict(self.validation)))
        if any(not isinstance(key, str) for key in self.provenance):
            raise IDPContractError("provenance is invalid")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))


@dataclass(frozen=True, slots=True)
class IDPJob:
    job_id: str
    tenant_id: str
    matter_id: str
    document_id: str
    document_sha256: str
    idempotency_key: str
    correlation_id: str = field(default_factory=new_id)
    schema_version: str = IDP_SCHEMA_VERSION
    model_id: str = ""
    prompt_version: str = ""
    status: IDPJobStatus = IDPJobStatus.ENQUEUE_PENDING
    checkpoint: IDPCheckpoint = IDPCheckpoint.CREATED
    attempt: int = 0
    max_attempts: int = 3
    claim_token: str | None = None
    claimed_until: datetime | None = None
    skip_reason: IDPSkipReason | None = None
    error_code: str | None = None
    # Durable review-dispatch projection.  These fields are references only;
    # proposed values remain in the immutable run artifact.
    review_delivery_state: str | None = None
    review_invocation_id: str | None = None
    review_task_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for name in ("job_id", "tenant_id", "matter_id", "document_id", "idempotency_key", "correlation_id"):
            _nonempty(getattr(self, name), name)
        _sha256(self.document_sha256, "document_sha256")
        if not isinstance(self.status, IDPJobStatus) or not isinstance(self.checkpoint, IDPCheckpoint):
            raise IDPContractError("job state is invalid")
        if self.skip_reason is not None and not isinstance(self.skip_reason, IDPSkipReason):
            raise IDPContractError("skip_reason is invalid")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise IDPContractError("attempt is invalid")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 10:
            raise IDPContractError("max_attempts is invalid")
        if self.claim_token is not None:
            _nonempty(self.claim_token, "claim_token")
        _aware_datetime(self.created_at, "created_at")
        _aware_datetime(self.updated_at, "updated_at")
        if self.claimed_until is not None:
            _aware_datetime(self.claimed_until, "claimed_until")
        if self.review_delivery_state is not None and self.review_delivery_state not in {"PENDING", "IN_FLIGHT", "AMBIGUOUS", "SENT"}:
            raise IDPContractError("review_delivery_state is invalid")
        for name in ("review_invocation_id", "review_task_id"):
            if getattr(self, name) is not None:
                _nonempty(getattr(self, name), name)
        if self.status is IDPJobStatus.SKIPPED and self.skip_reason is None:
            raise IDPContractError("skipped jobs require a reason")


@dataclass(frozen=True, slots=True)
class IDPExtractionRun:
    run_id: str
    tenant_id: str
    matter_id: str
    document_id: str
    document_sha256: str
    document_type: DocumentType
    schema_version: str
    model_id: str
    prompt_version: str
    status: IDPJobStatus
    fields: Mapping[str, IDPFieldResult] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    # Canonical source identity used to fence the document pointer.  It is
    # optional for old durable runs; new production runs populate it after
    # the final authoritative re-read.
    source_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("run_id", "tenant_id", "matter_id", "document_id", "schema_version", "model_id", "prompt_version"):
            _nonempty(getattr(self, name), name)
        _sha256(self.document_sha256, "document_sha256")
        if not isinstance(self.document_type, DocumentType) or not isinstance(self.status, IDPJobStatus):
            raise IDPContractError("run state is invalid")
        if any(not isinstance(result, IDPFieldResult) or name != result.field for name, result in self.fields.items()):
            raise IDPContractError("run fields are invalid")
        _aware_datetime(self.created_at, "created_at")
        if self.source_key is not None:
            _nonempty(self.source_key, "source_key")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True, slots=True)
class IDPClaim:
    job_id: str
    claim_token: str
    worker_id: str
    claimed_until: datetime

    def __post_init__(self) -> None:
        _nonempty(self.job_id, "job_id")
        _nonempty(self.claim_token, "claim_token")
        _nonempty(self.worker_id, "worker_id")
        _aware_datetime(self.claimed_until, "claimed_until")


@dataclass(frozen=True, slots=True)
class DocumentForIDP:
    """Server-resolved document metadata used by the trusted worker."""

    tenant_id: str
    matter_id: str
    document_id: str
    media_type: str
    file_size_bytes: int
    malware_scan_clean: bool
    page_count: int | None = None
    content_sha256: str | None = None
    source_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "matter_id", "document_id", "media_type"):
            _nonempty(getattr(self, name), name)
        if isinstance(self.file_size_bytes, bool) or not isinstance(self.file_size_bytes, int) or self.file_size_bytes < 0:
            raise IDPContractError("file_size_bytes is invalid")
        if not isinstance(self.malware_scan_clean, bool):
            raise IDPContractError("malware_scan_clean is invalid")
        if self.page_count is not None and (isinstance(self.page_count, bool) or not isinstance(self.page_count, int) or self.page_count < 1):
            raise IDPContractError("page_count is invalid")
        if self.content_sha256 is not None:
            _sha256(self.content_sha256, "content_sha256")
        if self.source_key is not None:
            _nonempty(self.source_key, "source_key")


__all__ = [name for name in globals() if not name.startswith("_")]
