"""Provider-neutral domain models.

These records intentionally contain IDs and metadata, not document bodies,
tokens, prompts, or other secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MatterStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class IngestionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


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
    ingestion_status: IngestionStatus = IngestionStatus.PENDING


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
