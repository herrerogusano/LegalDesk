"""Authorized human-review tool and provider-neutral persistence boundary.

The workflow accepts a closed reason code, a bounded server-derived snapshot,
an optional note and a due date. Effective tenant, matter, and creator scope
always come from a server-built ``RequestContext``; none of those fields are
part of the tool payload.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Protocol
from uuid import UUID
from uuid import uuid4

from .authorization import (
    AuthorizationDenied,
    Boto3DynamoAuthorizationStore,
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    _gateway_identity_from_verified_subject,
    build_request_context,
    require_authorized_context,
)
from .domain.models import ReviewTask, ReviewTaskStatus
from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
)


MAX_IDEMPOTENCY_KEY_LENGTH = 128
REVIEW_TASK_SCHEMA_VERSION = "2"
LEGALDESK_GRANT_ARGUMENT = "_legaldeskGrantId"
MAX_REVIEW_NOTE_LENGTH = 2_000
MAX_RESOLUTION_NOTE_LENGTH = 2_000
MAX_REVIEW_SNAPSHOT_BYTES = 48_000
MAX_REVIEW_CITATIONS = 32
MAX_REVIEW_PASSAGE_LENGTH = 16_000
MAX_REVIEW_TASKS = 100
_OPAQUE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPAQUE_SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:|@-]{0,255}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ReviewReasonCode(StrEnum):
    """Closed metadata vocabulary; free-form document/advice text is excluded."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    AMBIGUOUS_EVIDENCE = "ambiguous_evidence"
    MATERIAL_LEGAL_JUDGMENT = "material_legal_judgment"
    USER_REQUESTED_REVIEW = "user_requested_review"
    SAFETY_ESCALATION = "safety_escalation"

CREATE_REVIEW_TASK_TOOL_NAME = "create_review_task"
LIST_REVIEW_TASKS_TOOL_NAME = "list_review_tasks"
GET_REVIEW_TASK_TOOL_NAME = "get_review_task"
UPDATE_REVIEW_TASK_TOOL_NAME = "update_review_task"
REVIEW_TOOL_NAMES = frozenset({
    CREATE_REVIEW_TASK_TOOL_NAME,
    LIST_REVIEW_TASKS_TOOL_NAME,
    GET_REVIEW_TASK_TOOL_NAME,
    UPDATE_REVIEW_TASK_TOOL_NAME,
})
CREATE_REVIEW_TASK_TOOL_DESCRIPTION = (
    "Create an OPEN human-review task for the already authorized current matter. "
    "Provide one reasonCode and a bounded server-derived accepted-answer snapshot, "
    "not a document body or legal advice. The snapshot is validated and durable; "
    "matter, tenant, user, status, and task ID remain server-controlled."
)
CREATE_REVIEW_TASK_TOOL_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reasonCode": {
            "type": "string",
            "enum": [code.value for code in ReviewReasonCode],
            "description": "Closed reason category; no document body or advice text is accepted.",
        },
        "idempotencyKey": {
            "type": "string",
            "pattern": _OPAQUE_KEY.pattern,
            "maxLength": MAX_IDEMPOTENCY_KEY_LENGTH,
            "description": "Optional caller retry key scoped to the authorized user and matter.",
        },
        "note": {
            "type": "string",
            "maxLength": MAX_REVIEW_NOTE_LENGTH,
            "description": "Optional bounded context note; no document body is required.",
        },
        "dueAt": {
            "type": "string",
            "pattern": _ISO_DATE.pattern,
            "description": "Target date in ISO 8601 date form.",
        },
        "snapshot": {
            "type": "object",
            "description": "Server-derived accepted answer snapshot; client values are not trusted.",
        },
    },
    # Legacy Phase 07 direct callers remain compatible; the HTTP workflow route
    # always supplies the server-derived snapshot and dueAt before Gateway.
    "required": ["reasonCode"],
}

LIST_REVIEW_TASKS_TOOL_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False,
    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_REVIEW_TASKS}},
}
GET_REVIEW_TASK_TOOL_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False,
    "properties": {"reviewTaskId": {"type": "string", "pattern": _OPAQUE_KEY.pattern, "maxLength": 128}},
    "required": ["reviewTaskId"],
}
UPDATE_REVIEW_TASK_TOOL_SCHEMA: dict[str, object] = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "reviewTaskId": {"type": "string", "pattern": _OPAQUE_KEY.pattern, "maxLength": 128},
        "status": {"type": "string", "enum": [ReviewTaskStatus.IN_REVIEW.value, ReviewTaskStatus.CLOSED.value]},
        "resolutionNote": {"type": "string", "maxLength": MAX_RESOLUTION_NOTE_LENGTH},
    },
    "required": ["reviewTaskId", "status"],
}


class ReviewTaskError(Exception):
    """Base class for expected, safe tool errors."""

    public_code = "review_task_error"


class ReviewTaskValidationError(ReviewTaskError, ValueError):
    public_code = "invalid_request"


class ReviewTaskIdempotencyConflict(ReviewTaskError):
    public_code = "idempotency_conflict"


class ReviewTaskPersistenceError(ReviewTaskError):
    public_code = "service_unavailable"


@dataclass(frozen=True, slots=True)
class ReviewTaskInput:
    reason_code: ReviewReasonCode
    idempotency_key: str | None = None
    note: str = ""
    due_at: date | None = None
    snapshot: Mapping[str, object] | None = None


def _validate_key(value: object, *, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_IDEMPOTENCY_KEY_LENGTH
        or _OPAQUE_KEY.fullmatch(value) is None
    ):
        raise ReviewTaskValidationError(f"{field_name} is invalid")
    return value


def parse_review_task_input(payload: Mapping[str, object]) -> ReviewTaskInput:
    """Strictly parse create input; scope and lifecycle fields are rejected."""

    if not isinstance(payload, Mapping):
        raise ReviewTaskValidationError("tool input must be an object")
    allowed = {"reasonCode", "idempotencyKey", "note", "dueAt", "snapshot"}
    if set(payload) - allowed or "reasonCode" not in payload:
        raise ReviewTaskValidationError("tool input has unsupported or missing fields")
    raw_reason = payload["reasonCode"]
    try:
        reason_code = ReviewReasonCode(raw_reason)
    except (ValueError, TypeError) as exc:
        raise ReviewTaskValidationError("reasonCode is invalid") from exc
    raw_key = payload.get("idempotencyKey")
    key = None if raw_key is None else _validate_key(raw_key, field_name="idempotencyKey")
    note = payload.get("note", "")
    if not isinstance(note, str) or len(note) > MAX_REVIEW_NOTE_LENGTH:
        raise ReviewTaskValidationError("note is invalid")
    due_at = _parse_due_at(payload.get("dueAt")) if "dueAt" in payload else None
    snapshot = _validate_snapshot(payload.get("snapshot")) if "snapshot" in payload else None
    return ReviewTaskInput(reason_code=reason_code, idempotency_key=key, note=note, due_at=due_at, snapshot=snapshot)


def default_due_at(today: date | None = None) -> date:
    """Return three business days after today, excluding weekends."""

    cursor = today or datetime.now(timezone.utc).date()
    remaining = 3
    while remaining:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            remaining -= 1
    return cursor


def _parse_due_at(value: object) -> date:
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise ReviewTaskValidationError("dueAt is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ReviewTaskValidationError("dueAt is invalid") from exc
    today = datetime.now(timezone.utc).date()
    if parsed < today or parsed > today + timedelta(days=366):
        raise ReviewTaskValidationError("dueAt is outside the allowed range")
    return parsed


def _validate_snapshot(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ReviewTaskValidationError("snapshot is invalid")
    allowed = {"question", "answer", "evidenceStatus", "promptVersion", "promptSha256", "promptHash", "citations"}
    if set(value) - allowed or not {"question", "answer", "evidenceStatus", "citations"}.issubset(value):
        raise ReviewTaskValidationError("snapshot is invalid")
    question, answer, evidence = value.get("question"), value.get("answer"), value.get("evidenceStatus")
    if not isinstance(question, str) or not question.strip() or len(question) > 1_000:
        raise ReviewTaskValidationError("snapshot is invalid")
    if not isinstance(answer, str) or not answer.strip() or len(answer) > 8_000:
        raise ReviewTaskValidationError("snapshot is invalid")
    if evidence not in {"answerable", "ambiguous", "insufficient_evidence"}:
        raise ReviewTaskValidationError("snapshot is invalid")
    for prompt_key in ("promptVersion", "promptSha256", "promptHash"):
        prompt_value = value.get(prompt_key)
        if prompt_value is not None and (not isinstance(prompt_value, str) or len(prompt_value) > 128):
            raise ReviewTaskValidationError("snapshot is invalid")
    citations = value.get("citations")
    if not isinstance(citations, (list, tuple)) or len(citations) > MAX_REVIEW_CITATIONS:
        raise ReviewTaskValidationError("snapshot is invalid")
    normalized: list[dict[str, object]] = []
    for citation in citations:
        if not isinstance(citation, Mapping) or set(citation) - {"citationId", "documentId", "documentName", "pageNumber", "section", "passage"}:
            raise ReviewTaskValidationError("snapshot is invalid")
        document_id, passage = citation.get("documentId"), citation.get("passage")
        if not isinstance(document_id, str) or not document_id or not isinstance(passage, str) or not passage or len(passage) > MAX_REVIEW_PASSAGE_LENGTH:
            raise ReviewTaskValidationError("snapshot is invalid")
        if not isinstance(citation.get("citationId", ""), str) or len(str(citation.get("citationId", ""))) > 128:
            raise ReviewTaskValidationError("snapshot is invalid")
        for key in ("documentName", "section"):
            if citation.get(key) is not None and (not isinstance(citation.get(key), str) or len(str(citation.get(key))) > 512):
                raise ReviewTaskValidationError("snapshot is invalid")
        page = citation.get("pageNumber")
        if page is not None and (isinstance(page, bool) or not isinstance(page, int) or page < 0 or page > 1_000_000):
            raise ReviewTaskValidationError("snapshot is invalid")
        normalized.append(dict(citation))
    normalized_snapshot: dict[str, object] = {
        "question": question,
        "answer": answer,
        "evidenceStatus": evidence,
        "citations": normalized,
    }
    for prompt_key in ("promptVersion", "promptSha256", "promptHash"):
        if value.get(prompt_key) is not None:
            normalized_snapshot[prompt_key] = value[prompt_key]
    try:
        encoded = json.dumps(normalized_snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewTaskValidationError("snapshot is invalid") from exc
    if len(encoded) > MAX_REVIEW_SNAPSHOT_BYTES:
        raise ReviewTaskValidationError("snapshot is too large")
    return normalized_snapshot


# Explicit alias makes the parser discoverable at the tool boundary.
parse_create_review_task_input = parse_review_task_input


class ReviewTaskRepository(Protocol):
    """Persistence contract for scoped review workflow records."""

    def get(self, *, context: RequestContext, review_task_id: str) -> ReviewTask | None: ...

    def save(self, task: ReviewTask) -> None: ...

    def list(self, *, context: RequestContext, limit: int = MAX_REVIEW_TASKS) -> tuple[ReviewTask, ...]: ...

    def update(
        self,
        *,
        context: RequestContext,
        review_task_id: str,
        status: ReviewTaskStatus,
        resolution_note: str = "",
    ) -> ReviewTask: ...

    def delete(self, *, context: RequestContext, review_task_id: str) -> None: ...

    def archive_closed(
        self, *, context: RequestContext, older_than: datetime, limit: int = MAX_REVIEW_TASKS
    ) -> tuple[ReviewTask, ...]: ...


@dataclass(frozen=True, slots=True)
class AuthorizedToolEnvelope:
    """Serializable Gateway envelope carrying only authorization selectors.

    The envelope is not trusted by Lambda: ``verifiedSubject`` selects a User
    record and ``requestedMatterId`` selects a Matter record. Effective user,
    tenant, and membership are loaded from DynamoDB and rechecked by
    ``build_request_context`` before any task write. The future Gateway must be
    the only non-public invoker allowed to construct this envelope.
    """

    verified_subject: str
    requested_matter_id: str
    correlation_id: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.verified_subject, str)
            or _OPAQUE_SUBJECT.fullmatch(self.verified_subject) is None
            or not isinstance(self.requested_matter_id, str)
            or _OPAQUE_KEY.fullmatch(self.requested_matter_id) is None
            or not isinstance(self.correlation_id, str)
        ):
            raise ValueError("authorized context envelope is invalid")
        try:
            UUID(self.correlation_id)
        except (ValueError, AttributeError) as exc:
            raise ValueError("authorized context envelope is invalid") from exc


def parse_authorized_tool_envelope(event: Mapping[str, object]) -> tuple[AuthorizedToolEnvelope, Mapping[str, object]]:
    """Parse a Lambda envelope; scope-bearing tool arguments are not accepted."""

    if not isinstance(event, Mapping) or set(event) != {"arguments", "authorizedContext"}:
        raise ReviewTaskValidationError("tool envelope is invalid")
    arguments = event.get("arguments")
    raw_context = event.get("authorizedContext")
    if not isinstance(arguments, Mapping) or not isinstance(raw_context, Mapping):
        raise ReviewTaskValidationError("tool envelope is invalid")
    envelope = _parse_context_mapping(raw_context)
    return envelope, arguments


def _parse_context_mapping(raw_context: Mapping[str, object]) -> AuthorizedToolEnvelope:
    if set(raw_context) != {"verifiedSubject", "requestedMatterId", "correlationId"}:
        raise ReviewTaskValidationError("tool context is invalid")
    correlation_id = raw_context["correlationId"]
    if not isinstance(correlation_id, str):
        raise ReviewTaskValidationError("tool context is invalid")
    try:
        UUID(correlation_id)
    except (ValueError, AttributeError) as exc:
        raise ReviewTaskValidationError("tool context is invalid") from exc
    try:
        return AuthorizedToolEnvelope(
            verified_subject=raw_context["verifiedSubject"],  # type: ignore[arg-type]
            requested_matter_id=raw_context["requestedMatterId"],  # type: ignore[arg-type]
            correlation_id=correlation_id,
        )
    except ValueError as exc:
        raise ReviewTaskValidationError("tool context is invalid") from exc


def review_task_partition_key(tenant_id: str, matter_id: str) -> str:
    return f"TENANT#{tenant_id}#MATTER#{matter_id}"


def review_task_sort_key(review_task_id: str) -> str:
    return f"REVIEW#{review_task_id}"


def _idempotent_task_id(context: RequestContext, key: str) -> str:
    context = require_authorized_context(context)
    scope = f"{context.tenant_id}\x00{context.matter_id}\x00{context.user_id}\x00{key}"
    return "review-" + hashlib.sha256(scope.encode("utf-8")).hexdigest()[:32]


class InMemoryReviewTaskRepository:
    """Deterministic fake used by local tests; no external side effects."""

    def __init__(self) -> None:
        self.tasks: dict[tuple[str, str, str], ReviewTask] = {}

    def get(self, *, context: RequestContext, review_task_id: str) -> ReviewTask | None:
        context = require_authorized_context(context)
        return self.tasks.get((context.tenant_id, context.matter_id, review_task_id))

    def save(self, task: ReviewTask) -> None:
        key = (task.tenant_id, task.matter_id, task.review_task_id)
        if key in self.tasks:
            raise RuntimeError("review task already exists")
        self.tasks[key] = task

    def list(self, *, context: RequestContext, limit: int = MAX_REVIEW_TASKS) -> tuple[ReviewTask, ...]:
        context = require_authorized_context(context)
        records = [
            task for (tenant_id, matter_id, _), task in self.tasks.items()
            if tenant_id == context.tenant_id and matter_id == context.matter_id
        ]
        records.sort(key=lambda item: (item.created_at, item.review_task_id), reverse=True)
        return tuple(records[:limit])

    def update(
        self,
        *,
        context: RequestContext,
        review_task_id: str,
        status: ReviewTaskStatus,
        resolution_note: str = "",
    ) -> ReviewTask:
        context = require_authorized_context(context)
        key = (context.tenant_id, context.matter_id, review_task_id)
        current = self.tasks.get(key)
        if current is None:
            raise ReviewTaskValidationError("review task not found")
        updated = _transition_task(current, status=status, resolution_note=resolution_note)
        self.tasks[key] = updated
        return updated

    def delete(self, *, context: RequestContext, review_task_id: str) -> None:
        context = require_authorized_context(context)
        self.tasks.pop((context.tenant_id, context.matter_id, review_task_id), None)

    def archive_closed(
        self, *, context: RequestContext, older_than: datetime, limit: int = MAX_REVIEW_TASKS
    ) -> tuple[ReviewTask, ...]:
        context = require_authorized_context(context)
        if not isinstance(older_than, datetime) or older_than.tzinfo is None:
            raise ReviewTaskValidationError("retention cutoff is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_REVIEW_TASKS:
            raise ReviewTaskValidationError("limit is invalid")
        candidates = [
            task for (tenant_id, matter_id, _), task in self.tasks.items()
            if tenant_id == context.tenant_id
            and matter_id == context.matter_id
            and task.status is ReviewTaskStatus.CLOSED
            and task.closed_at is not None
            and task.closed_at <= older_than
            and task.archived_at is None
        ][:limit]
        archived: list[ReviewTask] = []
        now = datetime.now(timezone.utc)
        for task in candidates:
            updated = replace(
                task,
                snapshot=None,
                note="",
                resolution_note="",
                archived_at=now,
                updated_at=now,
            )
            self.tasks[(task.tenant_id, task.matter_id, task.review_task_id)] = updated
            archived.append(updated)
        return tuple(archived)


def _transition_task(task: ReviewTask, *, status: ReviewTaskStatus, resolution_note: str = "") -> ReviewTask:
    if not isinstance(status, ReviewTaskStatus) or status is ReviewTaskStatus.OPEN:
        raise ReviewTaskValidationError("status transition is invalid")
    allowed = {
        ReviewTaskStatus.OPEN: {ReviewTaskStatus.IN_REVIEW, ReviewTaskStatus.CLOSED},
        ReviewTaskStatus.IN_REVIEW: {ReviewTaskStatus.CLOSED},
        ReviewTaskStatus.CLOSED: set(),
    }
    if status not in allowed[task.status]:
        raise ReviewTaskValidationError("status transition is invalid")
    if status is ReviewTaskStatus.CLOSED:
        if not isinstance(resolution_note, str) or not resolution_note.strip() or len(resolution_note) > MAX_RESOLUTION_NOTE_LENGTH:
            raise ReviewTaskValidationError("resolutionNote is required when closing")
        return replace(
            task,
            status=status,
            resolution_note=resolution_note.strip(),
            closed_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
    if resolution_note:
        raise ReviewTaskValidationError("resolutionNote is only valid when closing")
    return replace(task, status=status, updated_at=datetime.now(timezone.utc))


def _task_response(task: ReviewTask, *, include_workflow: bool = False) -> dict[str, object]:
    # The legacy Phase 07 create response remains intentionally narrow. New
    # workflow calls opt into the durable schedule fields.
    response: dict[str, object] = {"reviewTaskId": task.review_task_id, "status": task.status.value}
    if include_workflow or task.snapshot is not None or task.due_at is not None:
        response.update({
            "dueAt": task.due_at.isoformat() if task.due_at else None,
            "createdAt": task.created_at.isoformat(),
            "updatedAt": task.updated_at.isoformat(),
        })
    return response


def review_task_to_dict(task: ReviewTask, *, include_snapshot: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "reviewTaskId": task.review_task_id,
        "status": task.status.value,
        "reasonCode": task.reason,
        "note": task.note,
        "dueAt": task.due_at.isoformat() if task.due_at else None,
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
        "closedAt": task.closed_at.isoformat() if task.closed_at else None,
        "resolutionNote": task.resolution_note,
        "createdByUserId": task.created_by_user_id,
    }
    if include_snapshot and task.snapshot is not None:
        payload["snapshot"] = dict(task.snapshot)
    return payload


def review_task_summary(task: ReviewTask) -> dict[str, object]:
    """Return bounded list metadata; snapshots are never part of list output."""

    return {
        "reviewTaskId": task.review_task_id,
        "status": task.status.value,
        "reasonCode": task.reason,
        "dueAt": task.due_at.isoformat() if task.due_at else None,
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
        "closedAt": task.closed_at.isoformat() if task.closed_at else None,
    }


def _validate_limit(value: object) -> int:
    if value is None:
        return MAX_REVIEW_TASKS
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_REVIEW_TASKS:
        raise ReviewTaskValidationError("limit is invalid")
    return value


def parse_list_review_tasks_input(payload: Mapping[str, object]) -> int:
    if not isinstance(payload, Mapping) or set(payload) - {"limit"}:
        raise ReviewTaskValidationError("list input is invalid")
    return _validate_limit(payload.get("limit"))


def parse_get_review_task_input(payload: Mapping[str, object]) -> str:
    if not isinstance(payload, Mapping) or set(payload) != {"reviewTaskId"}:
        raise ReviewTaskValidationError("get input is invalid")
    return _validate_key(payload["reviewTaskId"], field_name="reviewTaskId")


def parse_update_review_task_input(payload: Mapping[str, object]) -> tuple[str, ReviewTaskStatus, str]:
    if not isinstance(payload, Mapping) or set(payload) - {"reviewTaskId", "status", "resolutionNote"} or not {"reviewTaskId", "status"}.issubset(payload):
        raise ReviewTaskValidationError("update input is invalid")
    review_task_id = _validate_key(payload.get("reviewTaskId"), field_name="reviewTaskId")
    try:
        status = ReviewTaskStatus(payload.get("status"))
    except (ValueError, TypeError) as exc:
        raise ReviewTaskValidationError("status is invalid") from exc
    resolution_note = payload.get("resolutionNote", "")
    if not isinstance(resolution_note, str) or len(resolution_note) > MAX_RESOLUTION_NOTE_LENGTH:
        raise ReviewTaskValidationError("resolutionNote is invalid")
    return review_task_id, status, resolution_note


def _validated_existing_task(
    existing: object,
    *,
    context: RequestContext,
    review_task_id: str,
    reason: str,
) -> ReviewTask:
    """Validate an idempotency read before exposing any stored fields."""

    context = require_authorized_context(context)
    if not isinstance(existing, ReviewTask):
        raise ReviewTaskPersistenceError("review task store unavailable")
    try:
        structural_values = (
            existing.review_task_id,
            existing.matter_id,
            existing.tenant_id,
            existing.created_by_user_id,
            existing.reason,
            existing.correlation_id,
        )
        if (
            any(
                not isinstance(value, str) or not value.strip()
                for value in structural_values
            )
            or not isinstance(existing.status, ReviewTaskStatus)
            or not isinstance(existing.created_at, datetime)
            or not isinstance(existing.updated_at, datetime)
            or existing.created_at.tzinfo is None
            or existing.updated_at.tzinfo is None
        ):
            raise ReviewTaskPersistenceError("review task store unavailable")
        ReviewReasonCode(existing.reason)
        UUID(existing.correlation_id)
    except ReviewTaskPersistenceError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReviewTaskPersistenceError("review task store unavailable") from exc

    if (
        existing.review_task_id != review_task_id
        or existing.tenant_id != context.tenant_id
        or existing.matter_id != context.matter_id
        or existing.created_by_user_id != context.user_id
        or existing.reason != reason
    ):
        raise ReviewTaskIdempotencyConflict("idempotency key was already used")
    return existing


def create_review_task(
    context: RequestContext,
    payload: Mapping[str, object],
    *,
    repository: ReviewTaskRepository,
    telemetry_sink: TelemetrySink | None = None,
) -> dict[str, str]:
    """Create a human-review task inside the server-authorized matter scope."""

    context = require_authorized_context(context)
    started_at = time.perf_counter()
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.TOOL,
        context.correlation_id,
        TelemetryOutcome.STARTED,
        operation="create_review_task",
    )
    try:
        parsed = parse_review_task_input(payload)
        task_id = (
            _idempotent_task_id(context, parsed.idempotency_key)
            if parsed.idempotency_key is not None
            else f"review-{uuid4().hex}"
        )
        try:
            existing = repository.get(context=context, review_task_id=task_id)
        except Exception as exc:
            raise ReviewTaskPersistenceError("review task store unavailable") from exc
        if existing is not None:
            response = _task_response(
                _validated_existing_task(
                    existing,
                    context=context,
                    review_task_id=task_id,
                    reason=parsed.reason_code.value,
                )
            )
            emit_telemetry(
                telemetry_sink,
                TelemetryEventType.TOOL,
                context.correlation_id,
                TelemetryOutcome.SUCCEEDED,
                started_at=started_at,
                operation="create_review_task",
            )
            return response

        task = ReviewTask(
            review_task_id=task_id,
            matter_id=context.matter_id,
            tenant_id=context.tenant_id,
            created_by_user_id=context.user_id,
            reason=parsed.reason_code.value,
            status=ReviewTaskStatus.OPEN,
            correlation_id=context.correlation_id,
            snapshot=parsed.snapshot,
            note=parsed.note,
            due_at=parsed.due_at if parsed.due_at is not None else (default_due_at() if parsed.snapshot is not None else None),
        )
        try:
            repository.save(task)
        except Exception as exc:
            # A concurrent idempotent retry may have won the conditional write.
            if parsed.idempotency_key is not None:
                try:
                    existing = repository.get(context=context, review_task_id=task_id)
                except Exception:
                    existing = None
                if existing is not None:
                    response = _task_response(
                        _validated_existing_task(
                            existing,
                            context=context,
                            review_task_id=task_id,
                            reason=parsed.reason_code.value,
                        )
                    )
                    emit_telemetry(
                        telemetry_sink,
                        TelemetryEventType.TOOL,
                        context.correlation_id,
                        TelemetryOutcome.SUCCEEDED,
                        started_at=started_at,
                        operation="create_review_task",
                    )
                    return response
            raise ReviewTaskPersistenceError("review task store unavailable") from exc
        response = _task_response(task)
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.SUCCEEDED,
            started_at=started_at,
            operation="create_review_task",
        )
        return response
    except ReviewTaskError as exc:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="create_review_task",
            error_code=exc.public_code,
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="create_review_task",
            error_code=exc.public_code,
        )
        raise
    except Exception as exc:
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="create_review_task",
            error_code="service_unavailable",
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="create_review_task",
            error_code="service_unavailable",
        )
        raise ReviewTaskPersistenceError("review task store unavailable") from exc


def create_review_task_for_identity(
    identity: VerifiedIdentity,
    requested_matter_id: str,
    payload: Mapping[str, object],
    *,
    authorization_store: AuthorizationStore,
    repository: ReviewTaskRepository,
    correlation_id: str | None = None,
    telemetry_sink: TelemetrySink | None = None,
) -> dict[str, str]:
    """Adapter for a verified edge: derive scope, then enter the tool boundary."""

    context = build_request_context(
        identity,
        requested_matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    return create_review_task(
        context, payload, repository=repository, telemetry_sink=telemetry_sink
    )


def list_review_tasks(
    context: RequestContext,
    payload: Mapping[str, object],
    *,
    repository: ReviewTaskRepository,
    telemetry_sink: TelemetrySink | None = None,
) -> dict[str, object]:
    context = require_authorized_context(context)
    limit = parse_list_review_tasks_input(payload)
    try:
        tasks = repository.list(context=context, limit=limit)
    except ReviewTaskError:
        raise
    except Exception as exc:
        raise ReviewTaskPersistenceError("review task store unavailable") from exc
    # Listing is intentionally summary-only: the bounded answer/citation
    # snapshot is fetched only by get_review_task when a user opens an item.
    return {"tasks": [review_task_summary(task) for task in tasks]}


def get_review_task(
    context: RequestContext,
    payload: Mapping[str, object],
    *,
    repository: ReviewTaskRepository,
    telemetry_sink: TelemetrySink | None = None,
) -> dict[str, object]:
    context = require_authorized_context(context)
    review_task_id = parse_get_review_task_input(payload)
    try:
        task = repository.get(context=context, review_task_id=review_task_id)
    except Exception as exc:
        raise ReviewTaskPersistenceError("review task store unavailable") from exc
    if task is None:
        raise ReviewTaskValidationError("review task not found")
    return review_task_to_dict(task)


def update_review_task(
    context: RequestContext,
    payload: Mapping[str, object],
    *,
    repository: ReviewTaskRepository,
    telemetry_sink: TelemetrySink | None = None,
) -> dict[str, object]:
    context = require_authorized_context(context)
    review_task_id, status, resolution_note = parse_update_review_task_input(payload)
    try:
        task = repository.update(
            context=context,
            review_task_id=review_task_id,
            status=status,
            resolution_note=resolution_note,
        )
    except ReviewTaskError:
        raise
    except Exception as exc:
        raise ReviewTaskPersistenceError("review task store unavailable") from exc
    return review_task_to_dict(task)


class ReviewTaskLambdaHandler:
    """Injectable Lambda/service adapter; event claims are never trusted.

    The Gateway Phase 08 adapter supplies a serializable envelope. This
    handler treats its selectors as untrusted and reauthorizes against the
    injected store before entering the tool boundary.
    """

    def __init__(
        self,
        repository: ReviewTaskRepository,
        authorization_store: AuthorizationStore,
        telemetry_sink: TelemetrySink | None = None,
    ) -> None:
        self.repository = repository
        self.authorization_store = authorization_store
        self.telemetry_sink = telemetry_sink

    def handle(
        self,
        event: Mapping[str, object],
        *,
        authorized_context: AuthorizedToolEnvelope | None = None,
    ) -> dict[str, object]:
        if not isinstance(event, Mapping):
            return {"error": "invalid_request"}
        if not isinstance(authorized_context, AuthorizedToolEnvelope):
            return {"error": "access_denied"}
        try:
            payload = event.get("arguments")
            if not isinstance(payload, Mapping):
                raise ReviewTaskValidationError("tool input must be an object")
            request_context = build_request_context(
                _gateway_identity_from_verified_subject(
                    authorized_context.verified_subject
                ),
                authorized_context.requested_matter_id,
                self.authorization_store,
                correlation_id=authorized_context.correlation_id,
            )
            return create_review_task(
                request_context,
                payload,
                repository=self.repository,
                telemetry_sink=self.telemetry_sink,
            )
        except AuthorizationDenied:
            emit_telemetry(
                self.telemetry_sink,
                TelemetryEventType.TOOL,
                authorized_context.correlation_id,
                TelemetryOutcome.ERROR,
                operation="create_review_task",
                error_code="access_denied",
            )
            emit_telemetry(
                self.telemetry_sink,
                TelemetryEventType.ERROR,
                authorized_context.correlation_id,
                TelemetryOutcome.ERROR,
                operation="create_review_task",
                error_code="access_denied",
            )
            return {"error": "access_denied"}
        except ReviewTaskError as exc:
            # create_review_task closes its own STARTED span. Validation
            # errors raised before entering it still get a redacted pointer.
            if not isinstance(event.get("arguments"), Mapping):
                emit_telemetry(
                    self.telemetry_sink,
                    TelemetryEventType.TOOL,
                    authorized_context.correlation_id,
                    TelemetryOutcome.ERROR,
                    operation="create_review_task",
                    error_code=exc.public_code,
                )
                emit_telemetry(
                    self.telemetry_sink,
                    TelemetryEventType.ERROR,
                    authorized_context.correlation_id,
                    TelemetryOutcome.ERROR,
                    operation="create_review_task",
                    error_code=exc.public_code,
                )
            return {"error": exc.public_code}
        except Exception:
            emit_telemetry(
                self.telemetry_sink,
                TelemetryEventType.ERROR,
                authorized_context.correlation_id,
                TelemetryOutcome.ERROR,
                operation="create_review_task",
                error_code="service_unavailable",
            )
            emit_telemetry(
                self.telemetry_sink,
                TelemetryEventType.ERROR,
                authorized_context.correlation_id,
                TelemetryOutcome.ERROR,
                operation="create_review_task",
                error_code="service_unavailable",
            )
            return {"error": "service_unavailable"}

    def handle_operation(
        self,
        operation: str,
        event: Mapping[str, object],
        *,
        authorized_context: AuthorizedToolEnvelope | None = None,
    ) -> dict[str, object]:
        """Dispatch one exact Gateway tool after the same reauthorization."""

        if operation == CREATE_REVIEW_TASK_TOOL_NAME:
            return self.handle(event, authorized_context=authorized_context)
        if not isinstance(event, Mapping) or not isinstance(authorized_context, AuthorizedToolEnvelope):
            return {"error": "access_denied"}
        payload = event.get("arguments")
        if not isinstance(payload, Mapping):
            return {"error": "invalid_request"}
        try:
            context = build_request_context(
                _gateway_identity_from_verified_subject(authorized_context.verified_subject),
                authorized_context.requested_matter_id,
                self.authorization_store,
                correlation_id=authorized_context.correlation_id,
            )
            if operation == LIST_REVIEW_TASKS_TOOL_NAME:
                return list_review_tasks(context, payload, repository=self.repository, telemetry_sink=self.telemetry_sink)
            if operation == GET_REVIEW_TASK_TOOL_NAME:
                return get_review_task(context, payload, repository=self.repository, telemetry_sink=self.telemetry_sink)
            if operation == UPDATE_REVIEW_TASK_TOOL_NAME:
                return update_review_task(context, payload, repository=self.repository, telemetry_sink=self.telemetry_sink)
            return {"error": "invalid_request"}
        except AuthorizationDenied:
            return {"error": "access_denied"}
        except ReviewTaskError as exc:
            return {"error": exc.public_code}
        except Exception:
            return {"error": "service_unavailable"}

    __call__ = handle


def _repositories_from_environment() -> tuple[ReviewTaskRepository, AuthorizationStore] | None:
    table_name = os.environ.get("REVIEW_TASK_TABLE_NAME")
    if (
        not table_name
        or os.environ.get("REVIEW_TASK_SCHEMA_VERSION", REVIEW_TASK_SCHEMA_VERSION)
        != REVIEW_TASK_SCHEMA_VERSION
    ):
        return None
    import boto3

    table = boto3.resource("dynamodb").Table(table_name)
    return (
        Boto3DynamoReviewTaskRepository(table_name, table=table),
        Boto3DynamoAuthorizationStore(table_name, table=table),
    )


def _gateway_grant_repository_from_environment() -> Any | None:
    table_name = os.environ.get("REVIEW_TASK_TABLE_NAME")
    if (
        not table_name
        or os.environ.get("REVIEW_TASK_SCHEMA_VERSION", REVIEW_TASK_SCHEMA_VERSION)
        != REVIEW_TASK_SCHEMA_VERSION
    ):
        return None
    from .gateway_interceptor import Boto3DynamoGatewayGrantRepository

    return Boto3DynamoGatewayGrantRepository(table_name)


def lambda_handler(event: Mapping[str, object], _lambda_context: object) -> dict[str, object]:
    """AWS-compatible entrypoint with Dynamo authorization before PutItem.

    The function is intended for a private IAM/Gateway invoker. The legacy
    service envelope contains ``authorizedContext`` outside ``arguments``;
    AgentCore flat events dispatch through the opaque grant path below. In
    both cases, subject and matter are looked up in DynamoDB and reauthorized
    bilaterally. No tenant or user value from model input is authoritative.
    """

    if not isinstance(event, Mapping):
        return {"error": "invalid_request"}
    # Phase 08 AgentCore targets deliver flat tool properties. Keep this
    # compatibility dispatch in the existing Phase 07 AWS handler so the
    # deployed function can be upgraded in place without changing its ARN.
    # AgentCore supplies a flat event. The reserved grant selector is the
    # only Phase 08 marker; client_context.custom is provider metadata and is
    # never treated as caller identity or authorization.
    if LEGALDESK_GRANT_ARGUMENT in event:
        return gateway_lambda_handler(event, _lambda_context)
    try:
        envelope, arguments = parse_authorized_tool_envelope(event)
        repository_config = _repositories_from_environment()
    except ReviewTaskValidationError:
        return {"error": "invalid_request"}
    except Exception:
        return {"error": "service_unavailable"}
    if repository_config is None:
        return {"error": "service_unavailable"}
    repository, authorization_store = repository_config
    return ReviewTaskLambdaHandler(repository, authorization_store).handle(
        {"arguments": arguments}, authorized_context=envelope
    )


def gateway_lambda_handler(event: Mapping[str, object], lambda_context: object) -> dict[str, object]:
    """AgentCore Lambda-target adapter for flat tool arguments.

    AgentCore supplies flat tool properties. The trusted REQUEST interceptor
    writes a short-lived grant to the existing DynamoDB table and injects only
    its opaque ID into the transformed tool arguments. Provider metadata in
    ``context.client_context.custom`` is intentionally ignored.
    """

    if not isinstance(event, Mapping):
        return {"error": "invalid_request"}
    try:
        grant_id = event.get(LEGALDESK_GRANT_ARGUMENT)
        if not isinstance(grant_id, str):
            return {"error": "access_denied"}
        if "authorizedContext" in event:
            return {"error": "access_denied"}
        try:
            UUID(grant_id)
        except (ValueError, AttributeError) as exc:
            raise ReviewTaskValidationError("grant ID is invalid") from exc
        grants = _gateway_grant_repository_from_environment()
        if grants is None:
            return {"error": "service_unavailable"}
        raw_grant = grants.get(grant_id)
        if not isinstance(raw_grant, Mapping):
            return {"error": "access_denied"}
        required = {
            "pk", "sk", "entityType", "verifiedSubject", "requestedMatterId",
            "correlationId", "toolName", "expiresAt",
        }
        if set(raw_grant) not in (required, required | {"ttl"}) or raw_grant.get("entityType") != "GatewayAuthorizationGrant":
            return {"error": "access_denied"}
        if raw_grant.get("pk") != f"GATEWAY#GRANT#{grant_id}" or raw_grant.get("sk") != "PROFILE":
            return {"error": "access_denied"}
        operation = raw_grant.get("toolName")
        if operation not in REVIEW_TOOL_NAMES:
            return {"error": "access_denied"}
        expires_at = raw_grant.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float, Decimal)):
            return {"error": "access_denied"}
        import time

        try:
            expired = not math.isfinite(float(expires_at)) or expires_at <= time.time()
        except (TypeError, ValueError, OverflowError):
            return {"error": "access_denied"}
        if expired:
            return {"error": "access_denied"}
        ttl = raw_grant.get("ttl", expires_at)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float, Decimal)) or not math.isfinite(float(ttl)):
            return {"error": "access_denied"}
        try:
            envelope = AuthorizedToolEnvelope(
                verified_subject=raw_grant["verifiedSubject"],  # type: ignore[arg-type]
                requested_matter_id=raw_grant["requestedMatterId"],  # type: ignore[arg-type]
                correlation_id=raw_grant["correlationId"],  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise ReviewTaskValidationError("grant context is invalid") from exc
        target_arguments = dict(event)
        target_arguments.pop(LEGALDESK_GRANT_ARGUMENT, None)
        # Gateway keeps matterId to satisfy the target schema; it is only a
        # selector and must never reach the parser or influence authorization.
        target_arguments.pop("matterId", None)
        # The accepted answer correlation is the stable server-owned retry
        # identity. Grant IDs are intentionally short-lived and change on a
        # retried HTTP call, so they must not become the idempotency key.
        if operation == CREATE_REVIEW_TASK_TOOL_NAME:
            target_arguments["idempotencyKey"] = f"gateway-{envelope.correlation_id}"
        repository_config = _repositories_from_environment()
        if repository_config is None:
            return {"error": "service_unavailable"}
        repository, authorization_store = repository_config
        return ReviewTaskLambdaHandler(repository, authorization_store).handle_operation(
            str(operation), {"arguments": target_arguments}, authorized_context=envelope
        )
    except ReviewTaskValidationError:
        return {"error": "invalid_request"}
    except (TypeError, ValueError):
        return {"error": "invalid_request"}
    except AuthorizationDenied:
        return {"error": "access_denied"}
    except Exception:
        return {"error": "service_unavailable"}


class Boto3DynamoReviewTaskRepository:
    """DynamoDB adapter using the existing Phase 02 metadata table keys."""

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    @staticmethod
    def _item(task: ReviewTask) -> dict[str, object]:
        item: dict[str, object] = {
            "pk": review_task_partition_key(task.tenant_id, task.matter_id),
            "sk": review_task_sort_key(task.review_task_id),
            "entityType": "ReviewTask",
            "tenantId": task.tenant_id,
            "matterId": task.matter_id,
            "reviewTaskId": task.review_task_id,
            "createdByUserId": task.created_by_user_id,
            "reason": task.reason,
            "status": task.status.value,
            "correlationId": task.correlation_id,
            "createdAt": task.created_at.isoformat(),
            "updatedAt": task.updated_at.isoformat(),
        }
        if task.snapshot is not None:
            item["snapshot"] = dict(task.snapshot)
        if task.note:
            item["note"] = task.note
        if task.due_at is not None:
            item["dueAt"] = task.due_at.isoformat()
        if task.closed_at is not None:
            item["closedAt"] = task.closed_at.isoformat()
        if task.resolution_note:
            item["resolutionNote"] = task.resolution_note
        if task.archived_at is not None:
            item["archivedAt"] = task.archived_at.isoformat()
        return item

    @staticmethod
    def _task_from_item(*, item: Mapping[str, object], context: RequestContext, review_task_id: str) -> ReviewTask:
        """Parse one already scope-bound Dynamo record without another read."""

        expected_pk = review_task_partition_key(context.tenant_id, context.matter_id)
        expected_sk = review_task_sort_key(review_task_id)
        required = (
            "pk", "sk", "entityType", "tenantId", "matterId", "reviewTaskId",
            "createdByUserId", "reason", "status", "correlationId", "createdAt", "updatedAt",
        )
        if any(key not in item or not isinstance(item[key], str) or not item[key].strip() for key in required):
            raise ReviewTaskPersistenceError("review task store unavailable")
        if (
            item["pk"] != expected_pk
            or item["sk"] != expected_sk
            or item["entityType"] != "ReviewTask"
            or item["tenantId"] != context.tenant_id
            or item["matterId"] != context.matter_id
            or item["reviewTaskId"] != review_task_id
        ):
            raise ReviewTaskPersistenceError("review task store unavailable")
        try:
            status = ReviewTaskStatus(item["status"])
            ReviewReasonCode(item["reason"])
            UUID(item["correlationId"])
            created_at = datetime.fromisoformat(item["createdAt"])
            updated_at = datetime.fromisoformat(item["updatedAt"])
            due_at = date.fromisoformat(item["dueAt"]) if item.get("dueAt") else None
            closed_at = datetime.fromisoformat(item["closedAt"]) if item.get("closedAt") else None
            archived_at = datetime.fromisoformat(item["archivedAt"]) if item.get("archivedAt") else None
        except (ValueError, TypeError) as exc:
            raise ReviewTaskPersistenceError("review task store unavailable") from exc
        if created_at.tzinfo is None or updated_at.tzinfo is None:
            raise ReviewTaskPersistenceError("review task store unavailable")
        snapshot = _validate_snapshot(item["snapshot"]) if item.get("snapshot") is not None else None
        note = item.get("note", "")
        resolution_note = item.get("resolutionNote", "")
        if not isinstance(note, str) or len(note) > MAX_REVIEW_NOTE_LENGTH or not isinstance(resolution_note, str) or len(resolution_note) > MAX_RESOLUTION_NOTE_LENGTH:
            raise ReviewTaskPersistenceError("review task store unavailable")
        if closed_at is not None and closed_at.tzinfo is None:
            raise ReviewTaskPersistenceError("review task store unavailable")
        if archived_at is not None and archived_at.tzinfo is None:
            raise ReviewTaskPersistenceError("review task store unavailable")
        return ReviewTask(
            review_task_id=item["reviewTaskId"],
            matter_id=item["matterId"],
            tenant_id=item["tenantId"],
            created_by_user_id=item["createdByUserId"],
            reason=item["reason"],
            status=status,
            created_at=created_at,
            updated_at=updated_at,
            correlation_id=item["correlationId"],
            snapshot=snapshot,
            note=note,
            due_at=due_at,
            closed_at=closed_at,
            resolution_note=resolution_note,
            archived_at=archived_at,
        )

    def get(self, *, context: RequestContext, review_task_id: str) -> ReviewTask | None:
        context = require_authorized_context(context)
        try:
            response = self.table.get_item(
                Key={
                    "pk": review_task_partition_key(context.tenant_id, context.matter_id),
                    "sk": review_task_sort_key(review_task_id),
                },
                ConsistentRead=True,
            )
            item = response.get("Item")
            if not item:
                return None
            if not isinstance(item, Mapping):
                raise ReviewTaskPersistenceError("review task store unavailable")
            return self._task_from_item(item=item, context=context, review_task_id=review_task_id)
        except ReviewTaskPersistenceError:
            raise
        except Exception as exc:
            raise ReviewTaskPersistenceError("review task store unavailable") from exc

    def save(self, task: ReviewTask) -> None:
        try:
            self.table.put_item(
                Item=self._item(task),
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
        except Exception as exc:
            raise ReviewTaskPersistenceError("review task store unavailable") from exc

    def list(self, *, context: RequestContext, limit: int = MAX_REVIEW_TASKS) -> tuple[ReviewTask, ...]:
        context = require_authorized_context(context)
        if not isinstance(limit, int) or not 1 <= limit <= MAX_REVIEW_TASKS:
            raise ReviewTaskValidationError("limit is invalid")
        try:
            from boto3.dynamodb.conditions import Key
            projection = "#pk,#sk,#entityType,#tenantId,#matterId,#reviewTaskId,#createdByUserId,#reason,#status,#correlationId,#createdAt,#updatedAt,#note,#dueAt,#closedAt,#resolutionNote,#archivedAt"
            response = self.table.query(
                KeyConditionExpression=Key("pk").eq(review_task_partition_key(context.tenant_id, context.matter_id)) & Key("sk").begins_with("REVIEW#"),
                Limit=limit,
                ConsistentRead=True,
                ProjectionExpression=projection,
                ExpressionAttributeNames={
                    "#pk": "pk", "#sk": "sk", "#entityType": "entityType", "#tenantId": "tenantId", "#matterId": "matterId",
                    "#reviewTaskId": "reviewTaskId", "#createdByUserId": "createdByUserId", "#reason": "reason", "#status": "status",
                    "#correlationId": "correlationId", "#createdAt": "createdAt", "#updatedAt": "updatedAt", "#note": "note", "#archivedAt": "archivedAt",
                    "#dueAt": "dueAt", "#closedAt": "closedAt", "#resolutionNote": "resolutionNote",
                },
            )
            records: list[ReviewTask] = []
            for item in response.get("Items", ()):
                if not isinstance(item, Mapping):
                    raise ReviewTaskPersistenceError("review task store unavailable")
                task_id = item.get("reviewTaskId")
                if not isinstance(task_id, str):
                    raise ReviewTaskPersistenceError("review task store unavailable")
                records.append(self._task_from_item(item=item, context=context, review_task_id=task_id))
            return tuple(records)
        except ReviewTaskPersistenceError:
            raise
        except Exception as exc:
            raise ReviewTaskPersistenceError("review task store unavailable") from exc

    def update(
        self,
        *,
        context: RequestContext,
        review_task_id: str,
        status: ReviewTaskStatus,
        resolution_note: str = "",
    ) -> ReviewTask:
        context = require_authorized_context(context)
        current = self.get(context=context, review_task_id=review_task_id)
        if current is None:
            raise ReviewTaskValidationError("review task not found")
        updated = _transition_task(current, status=status, resolution_note=resolution_note)
        try:
            values: dict[str, object] = {
                ":status": updated.status.value,
                ":expectedStatus": current.status.value,
                ":updatedAt": updated.updated_at.isoformat(),
                ":matterId": context.matter_id,
            }
            names = {"#status": "status", "#updatedAt": "updatedAt", "#matterId": "matterId"}
            expression = "SET #status = :status, #updatedAt = :updatedAt"
            if updated.status is ReviewTaskStatus.CLOSED:
                values[":closedAt"] = updated.closed_at.isoformat() if updated.closed_at else ""
                values[":resolutionNote"] = updated.resolution_note
                names.update({"#closedAt": "closedAt", "#resolutionNote": "resolutionNote"})
                expression += ", #closedAt = :closedAt, #resolutionNote = :resolutionNote"
            self.table.update_item(
                Key={"pk": review_task_partition_key(context.tenant_id, context.matter_id), "sk": review_task_sort_key(review_task_id)},
                UpdateExpression=expression,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression="attribute_exists(pk) AND attribute_exists(sk) AND #matterId = :matterId AND #status = :expectedStatus",
                ReturnValues="NONE",
            )
        except Exception as exc:
            raise ReviewTaskPersistenceError("review task store unavailable") from exc
        return updated

    def delete(self, *, context: RequestContext, review_task_id: str) -> None:
        context = require_authorized_context(context)
        try:
            self.table.delete_item(
                Key={
                    "pk": review_task_partition_key(context.tenant_id, context.matter_id),
                    "sk": review_task_sort_key(review_task_id),
                },
                ConditionExpression="#entity = :entity AND #tenant = :tenant AND #matter = :matter",
                ExpressionAttributeNames={"#entity": "entityType", "#tenant": "tenantId", "#matter": "matterId"},
                ExpressionAttributeValues={":entity": "ReviewTask", ":tenant": context.tenant_id, ":matter": context.matter_id},
            )
        except Exception as exc:
            if self.get(context=context, review_task_id=review_task_id) is not None:
                raise ReviewTaskPersistenceError("review task deletion failed") from exc

    def archive_closed(
        self, *, context: RequestContext, older_than: datetime, limit: int = MAX_REVIEW_TASKS
    ) -> tuple[ReviewTask, ...]:
        context = require_authorized_context(context)
        if not isinstance(older_than, datetime) or older_than.tzinfo is None:
            raise ReviewTaskValidationError("retention cutoff is invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_REVIEW_TASKS:
            raise ReviewTaskValidationError("limit is invalid")
        candidates = self.list(context=context, limit=limit)
        archived: list[ReviewTask] = []
        now = datetime.now(timezone.utc)
        for task in candidates:
            if (
                task.status is not ReviewTaskStatus.CLOSED
                or task.closed_at is None
                or task.closed_at > older_than
                or task.archived_at is not None
            ):
                continue
            updated = replace(task, snapshot=None, note="", resolution_note="", archived_at=now, updated_at=now)
            try:
                self.table.update_item(
                    Key={"pk": review_task_partition_key(context.tenant_id, context.matter_id), "sk": review_task_sort_key(task.review_task_id)},
                    UpdateExpression="SET #archivedAt = :archivedAt, #updatedAt = :updatedAt REMOVE #snapshot, #note, #resolutionNote",
                    ExpressionAttributeNames={"#archivedAt": "archivedAt", "#updatedAt": "updatedAt", "#snapshot": "snapshot", "#note": "note", "#resolutionNote": "resolutionNote", "#status": "status", "#matterId": "matterId"},
                    ExpressionAttributeValues={":archivedAt": now.isoformat(), ":updatedAt": now.isoformat(), ":expectedStatus": ReviewTaskStatus.CLOSED.value, ":expectedMatter": context.matter_id},
                    ConditionExpression="#status = :expectedStatus AND #matterId = :expectedMatter AND attribute_not_exists(#archivedAt)",
                    ReturnValues="NONE",
                )
            except Exception as exc:
                raise ReviewTaskPersistenceError("review task archival failed") from exc
            archived.append(updated)
        return tuple(archived)


__all__ = [
    "Boto3DynamoReviewTaskRepository",
    "AuthorizedToolEnvelope",
    "CREATE_REVIEW_TASK_TOOL_DESCRIPTION",
    "CREATE_REVIEW_TASK_TOOL_NAME",
    "CREATE_REVIEW_TASK_TOOL_SCHEMA",
    "GET_REVIEW_TASK_TOOL_NAME",
    "GET_REVIEW_TASK_TOOL_SCHEMA",
    "InMemoryReviewTaskRepository",
    "LIST_REVIEW_TASKS_TOOL_NAME",
    "LIST_REVIEW_TASKS_TOOL_SCHEMA",
    "ReviewTaskIdempotencyConflict",
    "ReviewTaskInput",
    "ReviewTaskLambdaHandler",
    "ReviewTaskPersistenceError",
    "ReviewTaskRepository",
    "REVIEW_TASK_SCHEMA_VERSION",
    "LEGALDESK_GRANT_ARGUMENT",
    "ReviewTaskValidationError",
    "ReviewReasonCode",
    "create_review_task",
    "create_review_task_for_identity",
    "get_review_task",
    "gateway_lambda_handler",
    "list_review_tasks",
    "lambda_handler",
    "parse_create_review_task_input",
    "parse_authorized_tool_envelope",
    "parse_get_review_task_input",
    "parse_list_review_tasks_input",
    "parse_review_task_input",
    "parse_update_review_task_input",
    "review_task_partition_key",
    "review_task_sort_key",
    "UPDATE_REVIEW_TASK_TOOL_NAME",
    "UPDATE_REVIEW_TASK_TOOL_SCHEMA",
    "update_review_task",
]
