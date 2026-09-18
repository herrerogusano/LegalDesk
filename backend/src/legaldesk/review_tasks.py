"""Authorized human-review tool and provider-neutral persistence boundary.

The tool accepts only a closed reason code and an optional idempotency key. Effective
tenant, matter, and creator scope always come from a server-built
``RequestContext``; none of those fields are part of the tool payload.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
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
REVIEW_TASK_SCHEMA_VERSION = "1"
LEGALDESK_GRANT_ARGUMENT = "_legaldeskGrantId"
_OPAQUE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_OPAQUE_SUBJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:|@-]{0,255}$")


class ReviewReasonCode(StrEnum):
    """Closed metadata vocabulary; free-form document/advice text is excluded."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    AMBIGUOUS_EVIDENCE = "ambiguous_evidence"
    MATERIAL_LEGAL_JUDGMENT = "material_legal_judgment"
    USER_REQUESTED_REVIEW = "user_requested_review"
    SAFETY_ESCALATION = "safety_escalation"

CREATE_REVIEW_TASK_TOOL_NAME = "create_review_task"
CREATE_REVIEW_TASK_TOOL_DESCRIPTION = (
    "Create an OPEN human-review task for the already authorized current matter. "
    "Provide one reasonCode, not document text or legal advice. The tool "
    "returns only reviewTaskId and status. Matter, tenant, user, status, and "
    "task ID are server-controlled and cannot be supplied or overridden."
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
    },
    "required": ["reasonCode"],
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
    """Strictly parse the tool payload; scope and lifecycle fields are rejected."""

    if not isinstance(payload, Mapping):
        raise ReviewTaskValidationError("tool input must be an object")
    if set(payload) - {"reasonCode", "idempotencyKey"} or "reasonCode" not in payload:
        raise ReviewTaskValidationError("tool input has unsupported or missing fields")
    raw_reason = payload["reasonCode"]
    try:
        reason_code = ReviewReasonCode(raw_reason)
    except (ValueError, TypeError) as exc:
        raise ReviewTaskValidationError("reasonCode is invalid") from exc
    raw_key = payload.get("idempotencyKey")
    key = None if raw_key is None else _validate_key(raw_key, field_name="idempotencyKey")
    return ReviewTaskInput(reason_code=reason_code, idempotency_key=key)


# Explicit alias makes the parser discoverable at the tool boundary.
parse_create_review_task_input = parse_review_task_input


class ReviewTaskRepository(Protocol):
    """Persistence contract for metadata-only review tasks."""

    def get(self, *, context: RequestContext, review_task_id: str) -> ReviewTask | None: ...

    def save(self, task: ReviewTask) -> None: ...


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


def _task_response(task: ReviewTask) -> dict[str, str]:
    # Keep the successful tool contract deliberately free of advice or task body.
    return {"reviewTaskId": task.review_task_id, "status": task.status.value}


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
        if set(raw_grant) != required or raw_grant.get("entityType") != "GatewayAuthorizationGrant":
            return {"error": "access_denied"}
        if raw_grant.get("pk") != f"GATEWAY#GRANT#{grant_id}" or raw_grant.get("sk") != "PROFILE":
            return {"error": "access_denied"}
        if raw_grant.get("toolName") != "create_review_task":
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
        # One interceptor grant represents one logical Gateway tool call.
        # Always overwrite a model-supplied retry key so replaying the same
        # grant cannot create multiple tasks under different keys.
        target_arguments["idempotencyKey"] = f"gateway-{grant_id}"
        repository_config = _repositories_from_environment()
        if repository_config is None:
            return {"error": "service_unavailable"}
        repository, authorization_store = repository_config
        return ReviewTaskLambdaHandler(repository, authorization_store).handle(
            {"arguments": target_arguments}, authorized_context=envelope
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
        return {
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
            expected_pk = review_task_partition_key(context.tenant_id, context.matter_id)
            expected_sk = review_task_sort_key(review_task_id)
            required = (
                "pk", "sk", "entityType", "tenantId", "matterId", "reviewTaskId",
                "createdByUserId", "reason", "status", "correlationId", "createdAt", "updatedAt",
            )
            if any(
                key not in item
                or not isinstance(item[key], str)
                or not item[key].strip()
                for key in required
            ):
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
            except (ValueError, TypeError) as exc:
                raise ReviewTaskPersistenceError("review task store unavailable") from exc
            if created_at.tzinfo is None or updated_at.tzinfo is None:
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
            )
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


__all__ = [
    "Boto3DynamoReviewTaskRepository",
    "AuthorizedToolEnvelope",
    "CREATE_REVIEW_TASK_TOOL_DESCRIPTION",
    "CREATE_REVIEW_TASK_TOOL_NAME",
    "CREATE_REVIEW_TASK_TOOL_SCHEMA",
    "InMemoryReviewTaskRepository",
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
    "gateway_lambda_handler",
    "lambda_handler",
    "parse_create_review_task_input",
    "parse_authorized_tool_envelope",
    "parse_review_task_input",
    "review_task_partition_key",
    "review_task_sort_key",
]
