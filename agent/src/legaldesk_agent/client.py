"""Narrow, testable client for invoking an AgentCore Harness.

Only the user message and a server-managed session ID are variable. Model,
prompt, tools, skills, and other security-sensitive overrides are deliberately
not exposed through this adapter.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol
from uuid import UUID, uuid4


MIN_SESSION_ID_LENGTH = 33
_DERIVED_ACTOR = re.compile(r"^ldactor-[0-9a-f]{48}$")
_DERIVED_SESSION = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


@dataclass(frozen=True, slots=True)
class AgentTelemetryEvent:
    """Typed, redacted fallback event for environments without backend code."""

    event_type: str
    correlation_id: str
    outcome: str
    timestamp_ms: int
    operation: str
    latency_ms: int | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.event_type not in {"agent", "error"}:
            raise ValueError("event_type is invalid")
        if self.outcome not in {"started", "succeeded", "error"}:
            raise ValueError("outcome is invalid")
        if not isinstance(self.operation, str) or self.operation != "invoke_harness":
            raise ValueError("operation is invalid")
        try:
            UUID(self.correlation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("correlation_id is invalid") from exc
        if isinstance(self.timestamp_ms, bool) or self.timestamp_ms <= 0:
            raise ValueError("timestamp_ms is invalid")
        if self.latency_ms is not None and (
            isinstance(self.latency_ms, bool) or self.latency_ms < 0
        ):
            raise ValueError("latency_ms is invalid")
        if self.error_code is not None and self.error_code not in {
            "harness_invoke_failed",
            "harness_runtime_error",
            "harness_stream_error",
            "harness_malformed_response",
        }:
            raise ValueError("error_code is invalid")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_type": self.event_type,
            "correlation_id": self.correlation_id,
            "outcome": self.outcome,
            "timestamp_ms": self.timestamp_ms,
            "operation": self.operation,
        }
        if self.latency_ms is not None:
            result["latency_ms"] = self.latency_ms
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


class HarnessInvocationError(RuntimeError):
    """Raised when AgentCore returns a runtime client error."""


class HarnessDataPlane(Protocol):
    def invoke_harness(self, **kwargs: Any) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class InvokeResult:
    session_id: str
    text: str
    correlation_id: str | None = None
    # Provider/application metadata only; never raw event bodies or content.
    structured_metadata: tuple[Mapping[str, object], ...] = ()


@dataclass(frozen=True, slots=True, init=False)
class HarnessMemoryScope:
    """Server-derived opaque scope accepted by the Harness adapter.

    There is intentionally no CLI flag or free-form ``actor_id`` parameter on
    :meth:`HarnessInvoker.invoke`.  The trusted backend creates this value
    after authorization and passes it as one object.
    """

    actor_id: str
    session_id: str

    @classmethod
    def from_derived(cls, scope: object) -> "HarnessMemoryScope":
        """Adapt the backend's derived scope without accepting raw selectors."""

        if (
            type(scope).__module__ != "legaldesk.memory"
            or type(scope).__name__ != "MemoryScope"
        ):
            raise TypeError("scope must be produced by the backend memory adapter")
        is_sealed = getattr(scope, "_is_sealed", None)
        if not callable(is_sealed) or not is_sealed():
            raise TypeError("scope must be produced by the backend memory adapter")
        instance = object.__new__(cls)
        object.__setattr__(instance, "actor_id", getattr(scope, "actor_id", None))
        object.__setattr__(instance, "session_id", getattr(scope, "session_id", None))
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if (
            not isinstance(self.actor_id, str)
            or not self.actor_id
            or not isinstance(self.session_id, str)
            or not self.session_id
        ):
            raise ValueError("memory scope must contain opaque actor/session IDs")
        if not _DERIVED_ACTOR.fullmatch(self.actor_id) or not _DERIVED_SESSION.fullmatch(
            self.session_id
        ):
            raise ValueError("memory scope IDs must be server-derived opaque identifiers")


def new_session_id() -> str:
    """Return an opaque UUID suitable for AgentCore Runtime session isolation."""

    return str(uuid4())


def validate_session_id(session_id: str) -> str:
    if len(session_id) < MIN_SESSION_ID_LENGTH:
        raise ValueError("session_id must contain at least 33 characters")
    try:
        UUID(session_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError("session_id must be a UUID") from exc
    return session_id


def _text_from_events(events: Iterable[Mapping[str, Any]]) -> str:
    chunks: list[str] = []
    for event in events:
        if "runtimeClientError" in event:
            error = event["runtimeClientError"]
            message = error.get("message", "AgentCore runtime client error")
            raise HarnessInvocationError(str(message))
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            chunks.append(str(delta["text"]))
    return "".join(chunks)


_SAFE_TRACE_KEYS = {
    "authorizationDecision",
    "authorizationCode",
    "errorCode",
    "policyCode",
    "denyCode",
    "targetInvoked",
    "tool",
    "toolName",
    "targetTool",
    "requestId",
    "traceId",
    "correlationId",
    "toolCallId",
    "toolResult",
    "guardrailDecision",
    "stopReason",
    "finalValidation",
}
_SAFE_METADATA_ENUMS = {
    "authorizationDecision": {"ALLOW", "DENY", "UNKNOWN"},
    "authorizationCode": {"CROSS_MATTER", "ACCESS_DENIED", "DENY", "UNAUTHORIZED", "ALLOW", "OK", "SUCCESS"},
    "errorCode": {"CROSS_MATTER", "ACCESS_DENIED", "DENY", "UNAUTHORIZED", "ALLOW", "OK", "SUCCESS"},
    "policyCode": {"CROSS_MATTER", "ACCESS_DENIED", "DENY", "UNAUTHORIZED", "ALLOW", "OK", "SUCCESS"},
    "denyCode": {"CROSS_MATTER", "ACCESS_DENIED", "DENY", "UNAUTHORIZED", "ALLOW", "OK", "SUCCESS"},
    "guardrailDecision": {"ALLOW", "BLOCK", "ANONYMIZE", "ERROR", "UNAVAILABLE"},
    "stopReason": {"END_TURN", "TOOL_USE", "MAX_TOKENS", "ERROR", "UNAVAILABLE"},
    "finalValidation": {"VALID", "INVALID", "UNAVAILABLE"},
    "toolResult": {"SUCCEEDED", "BLOCKED", "ERROR", "UNAVAILABLE"},
}
_SAFE_OPAQUE_METADATA_KEYS = {"tool", "toolName", "targetTool", "requestId", "traceId", "correlationId", "toolCallId"}
_SAFE_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _safe_structured_metadata(events: Iterable[Mapping[str, Any]]) -> tuple[Mapping[str, object], ...]:
    """Copy allowlisted trace fields without retaining event payloads."""

    records: list[Mapping[str, object]] = []

    def collect(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        record: dict[str, object] = {}
        for key in _SAFE_TRACE_KEYS:
            item = value.get(key)
            if key in _SAFE_METADATA_ENUMS and isinstance(item, str) and item.upper() in _SAFE_METADATA_ENUMS[key]:
                record[key] = item.upper()
            elif key in _SAFE_OPAQUE_METADATA_KEYS and isinstance(item, str) and _SAFE_OPAQUE.fullmatch(item):
                record[key] = item
            elif key == "targetInvoked" and type(item) is bool:
                record[key] = item
        if record:
            records.append(record)
        for item in value.values():
            if isinstance(item, Mapping):
                collect(item)
            elif isinstance(item, (list, tuple)):
                for nested in item:
                    collect(nested)

    for event in events:
        collect(event)
    return tuple(records)


@dataclass(slots=True)
class HarnessInvoker:
    client: HarnessDataPlane
    harness_arn: str
    telemetry_sink: object | None = None

    @classmethod
    def from_boto3(cls, harness_arn: str, region: str = "eu-west-1") -> "HarnessInvoker":
        import boto3

        return cls(
            client=boto3.client("bedrock-agentcore", region_name=region),
            harness_arn=harness_arn,
        )

    def invoke(
        self,
        message: str,
        *,
        session_id: str | None = None,
        memory_scope: HarnessMemoryScope | None = None,
        correlation_id: str | None = None,
    ) -> InvokeResult:
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        if memory_scope is not None and not isinstance(memory_scope, HarnessMemoryScope):
            raise TypeError("memory_scope must be a server-derived HarnessMemoryScope")
        if memory_scope is not None and session_id is not None:
            if validate_session_id(session_id) != memory_scope.session_id:
                raise ValueError("session_id conflicts with memory scope")
        effective_session_id = validate_session_id(
            memory_scope.session_id if memory_scope is not None else (session_id or new_session_id())
        )
        effective_correlation_id = correlation_id or effective_session_id
        validate_session_id(effective_correlation_id)
        started_at = time.perf_counter()
        _record_agent_event(
            self.telemetry_sink,
            "agent",
            effective_correlation_id,
            "started",
            operation="invoke_harness",
        )
        request: dict[str, Any] = {
            "harnessArn": self.harness_arn,
            "runtimeSessionId": effective_session_id,
            "messages": [{"role": "user", "content": [{"text": message.strip()}]}],
        }
        if memory_scope is not None:
            request["actorId"] = memory_scope.actor_id
        try:
            response = self.client.invoke_harness(**request)
        except Exception:
            _record_agent_event(
                self.telemetry_sink,
                "agent",
                effective_correlation_id,
                "error",
                operation="invoke_harness",
                started_at=started_at,
                error_code="harness_invoke_failed",
            )
            _record_agent_event(
                self.telemetry_sink,
                "error",
                effective_correlation_id,
                "error",
                operation="invoke_harness",
                started_at=started_at,
                error_code="harness_invoke_failed",
            )
            raise
        try:
            if not isinstance(response, Mapping):
                raise ValueError("response must be an object")
            stream = response.get("stream")
            if isinstance(stream, (str, bytes, Mapping)) or not isinstance(stream, Iterable):
                raise ValueError("stream must be iterable")
            events = list(stream)
            text = _text_from_events(events)
            structured_metadata = _safe_structured_metadata(events)
        except HarnessInvocationError:
            _record_agent_event(
                self.telemetry_sink,
                "agent",
                effective_correlation_id,
                "error",
                operation="invoke_harness",
                started_at=started_at,
                error_code="harness_runtime_error",
            )
            _record_agent_event(
                self.telemetry_sink,
                "error",
                effective_correlation_id,
                "error",
                operation="invoke_harness",
                started_at=started_at,
                error_code="harness_runtime_error",
            )
            raise
        except Exception as exc:
            _record_agent_event(
                self.telemetry_sink,
                "agent",
                effective_correlation_id,
                "error",
                operation="invoke_harness",
                started_at=started_at,
                error_code=(
                    "harness_stream_error"
                    if exc.__class__.__name__ == "EventStreamError"
                    else "harness_malformed_response"
                ),
            )
            code = (
                "harness_stream_error"
                if exc.__class__.__name__ == "EventStreamError"
                else "harness_malformed_response"
            )
            _record_agent_event(
                self.telemetry_sink,
                "error",
                effective_correlation_id,
                "error",
                operation="invoke_harness",
                started_at=started_at,
                error_code=code,
            )
            if exc.__class__.__name__ == "EventStreamError":
                raise HarnessInvocationError(str(exc)) from exc
            raise HarnessInvocationError("AgentCore returned an invalid event stream") from exc
        _record_agent_event(
            self.telemetry_sink,
            "agent",
            effective_correlation_id,
            "succeeded",
            operation="invoke_harness",
            started_at=started_at,
        )
        return InvokeResult(
            session_id=effective_session_id,
            text=text,
            correlation_id=effective_correlation_id,
            structured_metadata=structured_metadata,
        )


def _record_agent_event(
    sink: object | None,
    event_type: str,
    correlation_id: str,
    outcome: str,
    *,
    operation: str,
    started_at: float | None = None,
    error_code: str | None = None,
) -> None:
    latency_ms = (
        max(0, int((time.perf_counter() - started_at) * 1000))
        if started_at is not None
        else None
    )
    try:
        # Local integration uses the canonical backend event type, while the
        # standalone agent package uses the equivalent validated fallback.
        from legaldesk.observability import (
            TelemetryErrorCode,
            TelemetryEvent,
            TelemetryEventType,
            TelemetryOperation,
            TelemetryOutcome,
        )

        event = TelemetryEvent(
            event_type=(
                TelemetryEventType.ERROR
                if event_type == "error"
                else TelemetryEventType.AGENT
            ),
            correlation_id=correlation_id,
            outcome=TelemetryOutcome(outcome),
            timestamp_ms=int(time.time() * 1000),
            latency_ms=latency_ms,
            operation=TelemetryOperation(operation),
            error_code=TelemetryErrorCode(error_code) if error_code else None,
        )
    except ImportError:
        event = AgentTelemetryEvent(
            event_type=event_type,
            correlation_id=correlation_id,
            outcome=outcome,
            timestamp_ms=int(time.time() * 1000),
            operation=operation,
            latency_ms=latency_ms,
            error_code=error_code,
        )
    try:
        recorder = getattr(sink, "record", None)
        if callable(recorder):
            recorder(event)
        else:
            print(json.dumps(event.to_dict(), separators=(",", ":"), sort_keys=True), flush=True)
    except Exception:
        # Telemetry must never break the security-sensitive invocation path.
        pass
