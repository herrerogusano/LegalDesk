"""Narrow, testable client for invoking an AgentCore Harness.

Only the user message and a server-managed session ID are variable. Model,
prompt, tools, skills, and other security-sensitive overrides are deliberately
not exposed through this adapter.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol
from uuid import UUID, uuid4


MIN_SESSION_ID_LENGTH = 33
_DERIVED_ACTOR = re.compile(r"^ldactor-[0-9a-f]{48}$")
_DERIVED_SESSION = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_HTTPS_URL = re.compile(r"^https://[^\s]+$")
_MCP_TOOL_NAMES = (
    "@legaldesk_gateway/metadata-mcp___list_matter_documents",
    "@legaldesk_gateway/metadata-mcp___get_document_metadata",
    "@legaldesk_gateway/review-task-lambda___create_review_task",
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
class HarnessUsage:
    """Validated numeric usage observed in one InvokeHarness stream.

    The installed SDK describes these as invocation token counts but does not
    document a repeated-block cadence. The adapter therefore accepts exactly
    one usage block per stream and rejects an ambiguous repeated block. Smoke
    aggregation happens across separate invocations, never by guessing how a
    provider stream should be summed.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cache_read_input_tokens",
            "cache_write_input_tokens",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0 or value > 10**12:
                raise ValueError(f"{name} is invalid")

    def to_dict(self) -> dict[str, int]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "cacheReadInputTokens": self.cache_read_input_tokens,
            "cacheWriteInputTokens": self.cache_write_input_tokens,
        }


@dataclass(frozen=True, slots=True)
class InvokeResult:
    session_id: str
    text: str
    correlation_id: str | None = None
    # Provider/application metadata only; never raw event bodies or content.
    structured_metadata: tuple[Mapping[str, object], ...] = ()
    # Structured provider tool results.  Text is never used to infer a tool
    # outcome or identifier.
    tool_results: tuple["HarnessToolResult", ...] = ()
    # Validated numeric usage only; raw provider metadata is never retained.
    usage: HarnessUsage | None = None
    # Preserve each validated provider block when cadence is repeated or
    # otherwise ambiguous; callers must not fabricate a cross-block total.
    usage_records: tuple[HarnessUsage, ...] = ()


@dataclass(frozen=True, slots=True)
class HarnessToolResult:
    """Bounded, validated result emitted by a Harness tool boundary."""

    tool_use_id: str
    name: str
    status: str
    payload: Mapping[str, object] = field(default_factory=dict)


# HarnessToolUseStatus is an SDK enum: ``success`` or ``error``.
_TOOL_STATUSES = {"SUCCESS", "ERROR"}
_TOOL_RESULT_KEYS = {
    "reviewTaskId", "status", "errorCode", "operationStatus", "documents",
    "documentId", "matterId", "name", "mediaType", "documentDate",
    "confidentiality", "metadata",
}
_MAX_TOOL_FRAGMENT = 64 * 1024
_MAX_TOOL_RESULTS = 16
_MAX_STREAM_EVENTS = 256
_MAX_STREAM_BYTES = 1_048_576


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


@dataclass(frozen=True, slots=True, init=False)
class HarnessInvocationScope:
    """Server-owned user/tool binding for one managed Harness invocation.

    The bearer token is deliberately excluded from ``repr`` and is accepted
    only from the backend's sealed binding.  It is never a message, prompt,
    model-produced argument, or public adapter parameter.
    """

    gateway_url: str
    bearer_token: str = field(repr=False)
    matter_id: str
    correlation_id: str
    invocation_id: str
    allowed_tool_names: tuple[str, ...]
    memory_scope: HarnessMemoryScope

    @classmethod
    def from_derived(cls, binding: object) -> "HarnessInvocationScope":
        if (
            type(binding).__module__ != "legaldesk.agent_integration"
            or type(binding).__name__ != "HarnessInvocationBinding"
        ):
            raise TypeError("scope must be produced by the backend integration boundary")
        is_sealed = getattr(binding, "_is_sealed", None)
        if not callable(is_sealed) or not is_sealed():
            raise TypeError("scope must be produced by the backend integration boundary")
        memory_scope = HarnessMemoryScope.from_derived(getattr(binding, "memory_scope", None))
        instance = object.__new__(cls)
        object.__setattr__(instance, "gateway_url", getattr(binding, "gateway_url", None))
        object.__setattr__(instance, "bearer_token", getattr(binding, "bearer_token", None))
        object.__setattr__(instance, "matter_id", getattr(binding, "matter_id", None))
        object.__setattr__(instance, "correlation_id", getattr(binding, "correlation_id", None))
        object.__setattr__(instance, "invocation_id", getattr(binding, "invocation_id", None))
        object.__setattr__(instance, "allowed_tool_names", getattr(binding, "allowed_tools", None))
        object.__setattr__(instance, "memory_scope", memory_scope)
        instance.__post_init__()
        return instance

    def __post_init__(self) -> None:
        if not isinstance(self.gateway_url, str) or _HTTPS_URL.fullmatch(self.gateway_url) is None:
            raise ValueError("gateway_url must be an HTTPS URL")
        if (
            not isinstance(self.bearer_token, str)
            or not self.bearer_token
            or len(self.bearer_token) > 16_384
            or any(char in self.bearer_token for char in "\r\n")
        ):
            raise ValueError("bearer_token is invalid")
        if not isinstance(self.matter_id, str) or not self.matter_id or len(self.matter_id) > 128:
            raise ValueError("matter_id is invalid")
        try:
            UUID(self.correlation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("correlation_id is invalid") from exc
        try:
            UUID(self.invocation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("invocation_id is invalid") from exc
        if (
            not isinstance(self.allowed_tool_names, tuple)
            or not self.allowed_tool_names
            or any(tool not in _MCP_TOOL_NAMES for tool in self.allowed_tool_names)
        ):
            raise ValueError("allowed_tool_names is invalid")
        if not isinstance(self.memory_scope, HarnessMemoryScope):
            raise TypeError("memory_scope must be server-derived")

    def gateway_headers(self) -> dict[str, str]:
        """Return fixed headers; scope selectors cannot be model-selected."""

        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "x-legaldesk-requested-matter-id": self.matter_id,
            "x-legaldesk-correlation-id": str(UUID(self.correlation_id)),
            "x-legaldesk-invocation-id": str(UUID(self.invocation_id)),
            "x-legaldesk-memory-actor-id": self.memory_scope.actor_id,
            "x-legaldesk-memory-session-id": self.memory_scope.session_id,
        }

    def remote_mcp_tool(self) -> dict[str, object]:
        """Build the only tool surface permitted for an application call."""

        return {
            "type": "remote_mcp",
            "name": "legaldesk_gateway",
            "config": {
                "remoteMcp": {
                    "url": self.gateway_url,
                    "headers": self.gateway_headers(),
                }
            },
        }

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        return self.allowed_tool_names


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


def _tool_name_allowed(name: str, allowed_tools: tuple[str, ...] | None) -> bool:
    if not isinstance(name, str) or not name or len(name) > 256:
        return False
    if allowed_tools is None:
        return name in _MCP_TOOL_NAMES or name in {
            item.rsplit("___", 1)[-1] for item in _MCP_TOOL_NAMES
        }
    return name in allowed_tools or any(
        name == item.rsplit("___", 1)[-1] for item in allowed_tools
    )


def _safe_tool_payload(value: object) -> Mapping[str, object]:
    """Keep bounded metadata needed by application code, never tool bodies."""

    if value in (None, ""):
        return {}
    if isinstance(value, str):
        if len(value) > _MAX_TOOL_FRAGMENT:
            raise ValueError("tool result is too large")
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("tool result is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError("tool result must be an object")
    if "isError" in value and value["isError"] is not False:
        raise ValueError("MCP tool returned an error")
    # MCP commonly wraps the JSON result in content blocks.  Unwrap only
    # text/json blocks and require text blocks to contain structured JSON;
    # prose is never promoted into an application result.
    if "content" in value:
        content = value.get("content")
        if not isinstance(content, (list, tuple)):
            raise ValueError("tool result content is malformed")
        unwrapped: dict[str, object] = {}
        for block in content:
            if not isinstance(block, Mapping):
                raise ValueError("tool result content is malformed")
            fragment = block.get("json")
            if fragment is None:
                fragment = block.get("text")
            if isinstance(fragment, str):
                try:
                    fragment = json.loads(fragment)
                except json.JSONDecodeError as exc:
                    raise ValueError("tool result content is not structured JSON") from exc
            if not isinstance(fragment, Mapping):
                raise ValueError("tool result content is not an object")
            unwrapped.update(fragment)
        value = unwrapped
    def clean(item: object, depth: int = 0) -> object:
        if depth > 3:
            raise ValueError("tool result is too deeply nested")
        if isinstance(item, str):
            if len(item) > 512:
                raise ValueError("tool result string is too large")
            return item
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        if isinstance(item, Mapping):
            output: dict[str, object] = {}
            for key, child in item.items():
                if not isinstance(key, str) or key not in _TOOL_RESULT_KEYS:
                    continue
                output[key] = clean(child, depth + 1)
            return output
        if isinstance(item, (list, tuple)):
            if len(item) > 32:
                raise ValueError("tool result list is too large")
            return [clean(child, depth + 1) for child in item]
        raise ValueError("tool result contains unsupported data")

    cleaned = clean(value)
    if not isinstance(cleaned, Mapping):
        raise ValueError("tool result must be an object")
    return cleaned


def _structured_tool_results(
    events: Iterable[Mapping[str, Any]],
    *,
    allowed_tools: tuple[str, ...] | None = None,
) -> tuple[HarnessToolResult, ...]:
    """Parse the actual SDK tool-use/result stream, failing closed on gaps.

    A tool result is associated by its SDK ``contentBlockIndex`` and
    ``toolUseId``.  The parser intentionally ignores model text and unknown
    provider fields, while rejecting an incomplete or out-of-scope boundary.
    """

    # The SDK reuses contentBlockIndex after each message.  Result blocks can
    # arrive in the following message, so match their toolUseId globally and
    # bind result deltas by the (message,index) block key.
    uses: dict[str, tuple[str, int, int]] = {}
    results: dict[str, dict[str, object]] = {}
    message_number = -1
    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("event must be an object")
        if "messageStart" in event:
            message_number += 1
            if message_number > 64:
                raise ValueError("too many Harness messages")
        start = event.get("contentBlockStart")
        if isinstance(start, Mapping):
            block_index = start.get("contentBlockIndex")
            if type(block_index) is not int or block_index < 0 or block_index > 1024:
                raise ValueError("Harness content block index is invalid")
            detail = start.get("start")
            if not isinstance(detail, Mapping):
                continue
            block_key = (message_number, block_index)
            tool_use = detail.get("toolUse")
            if isinstance(tool_use, Mapping):
                tool_id = tool_use.get("toolUseId")
                name = tool_use.get("name")
                if (
                    not isinstance(tool_id, str)
                    or not tool_id
                    or len(tool_id) > 256
                    or not isinstance(name, str)
                    or not _tool_name_allowed(name, allowed_tools)
                ):
                    raise ValueError("unknown or invalid Harness tool use")
                if tool_id in uses or any(record.get("block_key") == block_key for record in results.values()):
                    raise ValueError("duplicate Harness tool use")
                if len(uses) >= _MAX_TOOL_RESULTS:
                    raise ValueError("too many Harness tool results")
                uses[tool_id] = (name, message_number, block_index)
            tool_result = detail.get("toolResult")
            if isinstance(tool_result, Mapping):
                tool_id = tool_result.get("toolUseId")
                status = tool_result.get("status")
                if not isinstance(tool_id, str) or tool_id not in uses:
                    raise ValueError("Harness tool result has no matching use")
                if not isinstance(status, str) or status.upper() not in _TOOL_STATUSES:
                    raise ValueError("Harness tool result status is invalid")
                if tool_id in results:
                    raise ValueError("duplicate Harness tool result")
                # The SDK does not include content in this start block;
                # payload arrives only through contentBlockDelta.toolResult.
                results[tool_id] = {
                    "status": status.upper(),
                    "fragments": [],
                    "block_key": block_key,
                }
        delta = event.get("contentBlockDelta")
        if isinstance(delta, Mapping):
            body = delta.get("delta")
            if isinstance(body, Mapping) and "toolResult" in body:
                block_index = delta.get("contentBlockIndex")
                if type(block_index) is not int or block_index < 0 or block_index > 1024:
                    raise ValueError("Harness content block index is invalid")
                fragments_delta = body.get("toolResult")
                if not isinstance(fragments_delta, (list, tuple)) or not fragments_delta:
                    raise ValueError("Harness tool result fragment is invalid")
                # SDK deltas are bound to the content block.  Find only the
                # result whose index matches; a single active match is not
                # accepted for an ambiguous/missing index.
                block_key = (message_number, block_index)
                matches = [record for record in results.values() if record.get("block_key") == block_key]
                if len(matches) != 1:
                    raise ValueError("Harness tool result delta is unbound")
                result_record = matches[0]
                fragments = result_record["fragments"]
                assert isinstance(fragments, list)
                for fragment_block in fragments_delta:
                    if not isinstance(fragment_block, Mapping):
                        raise ValueError("Harness tool result fragment is invalid")
                    fragment = fragment_block.get("text")
                    if fragment is None and "json" in fragment_block:
                        fragment = json.dumps(fragment_block.get("json"), separators=(",", ":"))
                    if not isinstance(fragment, str) or len(fragment) > _MAX_TOOL_FRAGMENT:
                        raise ValueError("Harness tool result fragment is invalid")
                    if sum(len(item) for item in fragments) + len(fragment) > _MAX_TOOL_FRAGMENT:
                        raise ValueError("Harness tool result is too large")
                    fragments.append(fragment)
    if len(uses) > _MAX_TOOL_RESULTS:
        raise ValueError("too many Harness tool results")
    if set(uses) != set(results):
        raise ValueError("Harness tool result is missing")
    parsed: list[HarnessToolResult] = []
    for tool_id, (name, _message_number, _block_index) in uses.items():
        record = results[tool_id]
        fragments = record["fragments"]
        assert isinstance(fragments, list)
        payload = _safe_tool_payload("".join(fragments))
        if record["status"] == "SUCCESS" and not payload:
            raise ValueError("successful Harness tool result has no structured payload")
        if (
            record["status"] == "SUCCESS"
            and name.rsplit("___", 1)[-1] == "create_review_task"
            and not isinstance(payload.get("reviewTaskId"), str)
        ):
            raise ValueError("review tool result has no review task ID")
        parsed.append(
            HarnessToolResult(
                tool_use_id=tool_id,
                name=name,
                status=str(record["status"]),
                payload=payload,
            )
        )
    return tuple(parsed)


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


def _harness_usages(events: Iterable[Mapping[str, Any]]) -> tuple[HarnessUsage, ...]:
    """Validate SDK-shaped ``metadata.usage`` blocks without guessing cadence.

    Botocore exposes integer token fields but does not define a repeated
    cumulative versus per-message emission contract. Missing optional cache
    fields are zero; malformed blocks fail closed. Repeated validated blocks
    are retained individually so a caller can reject their ambiguous total.
    """

    observed: list[HarnessUsage] = []
    names = {
        "inputTokens": "input_tokens",
        "outputTokens": "output_tokens",
        "totalTokens": "total_tokens",
        "cacheReadInputTokens": "cache_read_input_tokens",
        "cacheWriteInputTokens": "cache_write_input_tokens",
    }
    for event in events:
        metadata = event.get("metadata") if isinstance(event, Mapping) else None
        if metadata is None:
            continue
        if not isinstance(metadata, Mapping):
            raise ValueError("Harness metadata is invalid")
        if "usage" not in metadata:
            continue
        usage = metadata.get("usage")
        if not isinstance(usage, Mapping) or not usage:
            raise ValueError("Harness usage is invalid")
        values = {name: 0 for name in names.values()}
        required = {"inputTokens", "outputTokens", "totalTokens"}
        if not required.issubset(usage):
            raise ValueError("Harness usage is missing core counts")
        recognized = False
        for provider_name, field_name in names.items():
            if provider_name not in usage:
                continue
            recognized = True
            value = usage[provider_name]
            if type(value) is not int or value < 0 or value > 10**12:
                raise ValueError("Harness usage count is invalid")
            values[field_name] = value
        if not recognized:
            raise ValueError("Harness usage has no known counts")
        observed.append(HarnessUsage(**values))
    return tuple(observed)


@dataclass(slots=True)
class HarnessInvoker:
    client: HarnessDataPlane
    harness_arn: str
    telemetry_sink: object | None = None
    # Application composition opts into these provider-supported controls.
    # The default preserves the narrow legacy adapter contract used by unit
    # callers that do not have a verified invocation scope.
    system_prompt: tuple[Mapping[str, object], ...] = ()
    production_overrides: bool = False

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
        invocation_scope: HarnessInvocationScope | None = None,
    ) -> InvokeResult:
        if not message or not message.strip():
            raise ValueError("message must not be empty")
        if memory_scope is not None and not isinstance(memory_scope, HarnessMemoryScope):
            raise TypeError("memory_scope must be a server-derived HarnessMemoryScope")
        if invocation_scope is not None and not isinstance(invocation_scope, HarnessInvocationScope):
            raise TypeError("invocation_scope must be a server-derived HarnessInvocationScope")
        if invocation_scope is not None:
            if memory_scope is not None and memory_scope != invocation_scope.memory_scope:
                raise ValueError("memory_scope conflicts with invocation scope")
            memory_scope = invocation_scope.memory_scope
            if correlation_id is not None and str(UUID(correlation_id)) != invocation_scope.correlation_id:
                raise ValueError("correlation_id conflicts with invocation scope")
            correlation_id = invocation_scope.correlation_id
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
        if invocation_scope is not None:
            request["tools"] = [invocation_scope.remote_mcp_tool()]
            request["allowedTools"] = list(invocation_scope.allowed_tools)
            request["baggage"] = f"legaldesk.correlation_id={invocation_scope.correlation_id}"
            if self.production_overrides:
                request["systemPrompt"] = list(self.system_prompt)
                request["maxIterations"] = 3
                request["maxTokens"] = 512
                request["timeoutSeconds"] = 120
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
            events: list[Mapping[str, Any]] = []
            stream_bytes = 0
            for event in stream:
                if not isinstance(event, Mapping):
                    raise ValueError("event must be an object")
                if len(events) >= _MAX_STREAM_EVENTS:
                    raise ValueError("Harness stream is too long")
                try:
                    event_bytes = len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise ValueError("event cannot be bounded") from exc
                stream_bytes += event_bytes
                if stream_bytes > _MAX_STREAM_BYTES:
                    raise ValueError("Harness stream is too large")
                events.append(event)
            text = _text_from_events(events)
            structured_metadata = _safe_structured_metadata(events)
            usage_records = _harness_usages(events)
            usage = usage_records[0] if len(usage_records) == 1 else None
            tool_results = _structured_tool_results(
                events,
                allowed_tools=(
                    invocation_scope.allowed_tools if invocation_scope is not None else None
                ),
            )
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
            tool_results=tool_results,
            usage=usage,
            usage_records=usage_records,
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
