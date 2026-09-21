"""Redacted operational telemetry and a minimal correlation-scoped audit view.

Telemetry is deliberately an allowlist, not a serialization of arbitrary
request objects.  This keeps CloudWatch useful for debugging while excluding
questions, answers, passages, prompts, tokens, secrets, and document bodies.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID


_SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|[0-9A-Za-z-]*[0-9A-Za-z-]))?"
    r"(?:\+[0-9A-Za-z-]+)?$"
)


class TelemetryEventType(StrEnum):
    AGENT = "agent"
    MODEL = "model"
    RETRIEVAL = "retrieval"
    TOOL = "tool"
    GUARDRAIL = "guardrail"
    FINAL = "final"
    ERROR = "error"


class TelemetryOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    BLOCKED = "blocked"
    NOT_FOUND = "not_found"
    ERROR = "error"


class TelemetryOperation(StrEnum):
    INVOKE_HARNESS = "invoke_harness"
    ANSWER_QUESTION = "answer_question"
    GENERATE_ANSWER = "generate_answer"
    RESOLVE_EVIDENCE = "resolve_evidence"
    WRITE_ANSWER = "write_answer"
    GROUNDING_VALIDATE = "grounding_validate"
    MCP_TOOL_CALL = "mcp_tool_call"
    MCP_REQUEST = "mcp_request"
    KNOWLEDGE_BASE_RETRIEVE = "knowledge_base_retrieve"
    CREATE_REVIEW_TASK = "create_review_task"
    GATEWAY_REQUEST = "gateway_request"
    LIST_MATTER_DOCUMENTS = "list_matter_documents"
    GET_DOCUMENT_METADATA = "get_document_metadata"
    INPUT = "input"
    OUTPUT = "output"


class TelemetryErrorCode(StrEnum):
    ACCESS_DENIED = "access_denied"
    REVIEW_TASK_ERROR = "review_task_error"
    INVALID_PARAMETERS = "invalid_parameters"
    MCP_SERVICE_UNAVAILABLE = "mcp_service_unavailable"
    RETRIEVAL_FAILED = "retrieval_failed"
    RETRIEVAL_INVALID_RESPONSE = "retrieval_invalid_response"
    MODEL_FAILED = "model_failed"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INVALID_REQUEST = "invalid_request"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    GUARDRAIL_ERROR = "guardrail_error"
    HARNESS_INVOKE_FAILED = "harness_invoke_failed"
    HARNESS_RUNTIME_ERROR = "harness_runtime_error"
    HARNESS_STREAM_ERROR = "harness_stream_error"
    HARNESS_MALFORMED_RESPONSE = "harness_malformed_response"


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """Safe event fields suitable for a structured log or audit pointer."""

    event_type: TelemetryEventType
    correlation_id: str
    outcome: TelemetryOutcome
    timestamp_ms: int
    latency_ms: int | None = None
    operation: TelemetryOperation | None = None
    count: int | None = None
    error_code: TelemetryErrorCode | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    resolver_prompt_version: str | None = None
    resolver_prompt_sha256: str | None = None
    writer_prompt_version: str | None = None
    writer_prompt_sha256: str | None = None

    def __post_init__(self) -> None:
        if type(self.event_type) is not TelemetryEventType:
            raise TypeError("event_type is invalid")
        if type(self.outcome) is not TelemetryOutcome:
            raise TypeError("outcome is invalid")
        if isinstance(self.timestamp_ms, bool) or not isinstance(self.timestamp_ms, int):
            raise ValueError("timestamp_ms is invalid")
        try:
            UUID(self.correlation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("correlation_id must be a UUID") from exc
        if isinstance(self.timestamp_ms, bool) or self.timestamp_ms <= 0:
            raise ValueError("timestamp_ms is invalid")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool) or self.latency_ms < 0
        ):
            raise ValueError("latency_ms is invalid")
        if self.count is not None and (
            isinstance(self.count, bool) or self.count < 0
        ):
            raise ValueError("count is invalid")
        if self.operation is not None and type(self.operation) is not TelemetryOperation:
            raise ValueError("operation is not allowlisted")
        if self.error_code is not None and type(self.error_code) is not TelemetryErrorCode:
            raise ValueError("error_code is not allowlisted")
        for name in (
            "prompt_version", "resolver_prompt_version", "writer_prompt_version"
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 64
                or _SEMVER.fullmatch(value) is None
            ):
                raise ValueError(f"{name} is invalid")
        for name in (
            "prompt_sha256", "resolver_prompt_sha256", "writer_prompt_sha256"
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{name} is invalid")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_type": self.event_type.value,
            "correlation_id": self.correlation_id,
            "outcome": self.outcome.value,
            "timestamp_ms": self.timestamp_ms,
        }
        for name in (
            "latency_ms", "operation", "count", "error_code", "prompt_version",
            "prompt_sha256", "resolver_prompt_version", "resolver_prompt_sha256",
            "writer_prompt_version", "writer_prompt_sha256",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value.value if isinstance(value, StrEnum) else value
        return result


class TelemetrySink(Protocol):
    def record(self, event: TelemetryEvent) -> None: ...


class LoggingTelemetrySink:
    """Emit one JSON object containing only event fields.

    ``print`` is intentional for Lambda: Python's default logging formatter
    may prefix a message with timestamp/level/request metadata, which would
    prevent CloudWatch JSON metric filters from matching the allowlisted
    fields.
    """

    def record(self, event: TelemetryEvent) -> None:
        print(json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True), flush=True)


@dataclass(slots=True)
class InMemoryTelemetrySink:
    """Bounded local audit view used by tests and operational inspection."""

    max_events: int = 1_000
    events: list[TelemetryEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if isinstance(self.max_events, bool) or not 1 <= self.max_events <= 100_000:
            raise ValueError("max_events is invalid")

    def record(self, event: TelemetryEvent) -> None:
        if not isinstance(event, TelemetryEvent):
            raise TypeError("telemetry sink accepts TelemetryEvent only")
        self.events.append(event)
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]

    def by_correlation_id(self, correlation_id: str) -> tuple[TelemetryEvent, ...]:
        try:
            UUID(correlation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("correlation_id must be a UUID") from exc
        return tuple(event for event in self.events if event.correlation_id == correlation_id)


class NullTelemetrySink:
    def record(self, event: TelemetryEvent) -> None:
        del event


DEFAULT_TELEMETRY_SINK: TelemetrySink = LoggingTelemetrySink()


def emit_telemetry(
    sink: TelemetrySink | None,
    event_type: TelemetryEventType,
    correlation_id: str,
    outcome: TelemetryOutcome,
    *,
    started_at: float | None = None,
    operation: str | None = None,
    count: int | None = None,
    error_code: TelemetryErrorCode | str | None = None,
    prompt_version: str | None = None,
    prompt_sha256: str | None = None,
    resolver_prompt_version: str | None = None,
    resolver_prompt_sha256: str | None = None,
    writer_prompt_version: str | None = None,
    writer_prompt_sha256: str | None = None,
) -> None:
    """Record safe metadata, defaulting to the structured application logger."""

    try:
        normalized_operation = (
            None
            if operation is None
            else TelemetryOperation(operation)
        )
        normalized_error_code = (
            None
            if error_code is None
            else TelemetryErrorCode(error_code)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("telemetry field is not allowlisted") from exc
    event = TelemetryEvent(
        event_type=event_type,
        correlation_id=correlation_id,
        outcome=outcome,
        timestamp_ms=int(time.time() * 1000),
        latency_ms=(
            max(0, int((time.perf_counter() - started_at) * 1000))
            if started_at is not None
            else None
        ),
        operation=normalized_operation,
        count=count,
        error_code=normalized_error_code,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        resolver_prompt_version=resolver_prompt_version,
        resolver_prompt_sha256=resolver_prompt_sha256,
        writer_prompt_version=writer_prompt_version,
        writer_prompt_sha256=writer_prompt_sha256,
    )
    (sink or DEFAULT_TELEMETRY_SINK).record(event)

__all__ = [
    "DEFAULT_TELEMETRY_SINK",
    "InMemoryTelemetrySink",
    "LoggingTelemetrySink",
    "NullTelemetrySink",
    "TelemetryEvent",
    "TelemetryEventType",
    "TelemetryErrorCode",
    "TelemetryOperation",
    "TelemetryOutcome",
    "TelemetrySink",
    "emit_telemetry",
]
