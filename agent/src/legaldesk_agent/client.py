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
    "@legaldesk_gateway/metadata-mcp___get_extracted_fields",
    "@legaldesk_gateway/search-documents___search_documents",
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


class StreamLimitExceeded(ValueError):
    """Local event or byte budget exhausted; never a tool argument error."""


class ClientStreamParserError(HarnessInvocationError):
    """OUR stream parser refused what Harness sent (an unknown tool name, a result without its use...).

    It is not Harness refusing anything: the checks and the evals must not report it as a Harness rejection. Found live on
    2026-11-03 (T9 step 11), when the parser did not know the name format that Harness uses for Gateway tools.
    """


class HarnessDataPlane(Protocol):
    def invoke_harness(self, **kwargs: Any) -> Mapping[str, Any]: ...


# How the usage blocks of ONE stream add up. The SDK does not document it. The stream is the model's own (Converse/Strands): one
# ``metadata.usage`` block after every model call of the loop, holding the tokens of THAT call, so the total of a loop is their sum.
# A loop of three model calls used to report no usage at all (only a stream with exactly one block did), which made the eval's cost
# stop rules unable to trip (T9 step 12, run c: 0.089 USD reported for about 1.4 USD spent). If the first live comparison with
# CloudWatch (AWS/Bedrock InputTokenCount/OutputTokenCount, docs/phase-15-t9-cost-estimate.md) shows the blocks are CUMULATIVE
# instead, the total is the last block and this constant and ``sum_usage`` change in one place; until then the sum is the
# conservative reading (it can only over-count, never hide cost).
USAGE_CADENCE = "per_call"


@dataclass(frozen=True, slots=True)
class HarnessUsage:
    """Validated numeric usage: one block of an InvokeHarness stream, or the total of a loop (see ``sum_usage``).

    A stream carries one block per model call (``USAGE_CADENCE``). ``total_tokens`` is the provider's own figure
    (input + output), kept as reported rather than recomputed.
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


def sum_usage(records: Iterable[HarnessUsage]) -> HarnessUsage | None:
    """The total of the usage blocks of one stream, or ``None`` when there is none (usage unknown, never zero)."""

    total = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cache_read_input_tokens": 0, "cache_write_input_tokens": 0}
    seen = 0
    for record in records:
        seen += 1
        for name in total:
            total[name] += getattr(record, name)
    return HarnessUsage(**total) if seen else None


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
    # Validated numeric usage only (the total of the stream's blocks); raw provider metadata is never retained. ``None`` = unknown.
    usage: HarnessUsage | None = None
    # Each validated block, so a caller can see how many model calls reported.
    usage_records: tuple[HarnessUsage, ...] = ()


@dataclass(frozen=True, slots=True)
class HarnessToolResult:
    """Bounded, validated result emitted by a Harness tool boundary."""

    tool_use_id: str
    name: str
    status: str
    payload: Mapping[str, object] = field(default_factory=dict)
    # Closed label (see ERROR_HINTS) of WHY an unusable result failed, computed from its text and never keeping it.
    error_hint: str | None = None


# Closed vocabulary for the reason of a failed tool result. The text of an error is never stored or logged: it can echo
# arguments or document content, so only a label chosen by fixed keywords survives (T9 step 11, 2026-11-03: a failed
# search_documents call whose reason could not be told apart).
ERROR_HINTS = (
    "missing_required_argument", "invalid_arguments", "access_denied", "tool_not_allowed", "timeout", "throttled", "no_text",
    "tool_reported_error", "unclassified", "stream_limit_exceeded",
)


def _reports_its_own_failure(payload: Mapping[str, object]) -> bool:
    """A tool that FAILED but answered like a success: ``{"error": "<code>"}`` or an ``operationStatus`` of ``error``.

    Gateway reports a returned payload as a successful call, whatever it says. The search tool did exactly that eighteen times in the
    T9 step 12 run; it now raises instead, but the call log must not depend on every target doing so: such a result is an ERROR here.
    """

    return isinstance(payload.get("error"), str) or payload.get("operationStatus") == "error"


def _tool_error_hint(text: str) -> str:
    lowered = text.lower()
    if not lowered.strip():
        return "no_text"
    # Most specific first: an access error whose text also says "required" is still an access error (PullBeacon PB-001).
    if (
        "access denied" in lowered or "forbidden" in lowered or "unauthorized" in lowered or "-32001" in lowered
        or re.search(r"\b(?:http|status|statuscode|code)\W{0,3}403\b", lowered)
    ):
        return "access_denied"
    if "required" in lowered or "missing" in lowered:
        return "missing_required_argument"
    # "not found" alone would also match a missing document; only a tool that is unknown or not found counts here.
    if (
        "not allowed" in lowered or "not permitted" in lowered or "allowedtools" in lowered or "unknown tool" in lowered
        or re.search(r"\btool\b[^.\n]{0,80}\bnot found\b", lowered)
    ):
        return "tool_not_allowed"
    if "validation" in lowered or "schema" in lowered or "invalid" in lowered:
        return "invalid_arguments"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "throttl" in lowered or "rate exceeded" in lowered or "too many requests" in lowered:
        return "throttled"
    return "unclassified"


def _record_stream_error(exc: Exception, correlation_id: str, events_received: int) -> None:
    """Closed diagnostics only; exception messages/headers may contain request data.

    For EventStreamError, botocore synthesizes status 400 for exception frames;
    sdk_status is not necessarily the HTTP status of the accepted invocation.
    """
    classes = {"EventStreamError", "HarnessInvocationError", "ValueError", "ReadTimeoutError",
               "TimeoutError", "ConnectionError", "ConnectionClosedError", "StreamLimitExceeded"}
    codes = {"runtimeClientError", "validationException", "internalServerException",
             "RuntimeClientError", "ValidationException", "InternalServerException",
             "ThrottlingException", "ServiceUnavailableException", "ModelTimeoutException"}
    try:
        response = getattr(exc, "response", None)
        response = response if isinstance(response, Mapping) else {}
        error, metadata = response.get("Error"), response.get("ResponseMetadata")
        code = error.get("Code") if isinstance(error, Mapping) else None
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
        kind = type(exc).__name__
        print(json.dumps({
            "event_type": "harness_stream_diagnostic", "correlation_id": correlation_id,
            "exception_class": kind if kind in classes else "other",
            "aws_error_code": code if isinstance(code, str) and code in codes else "other",
            "sdk_status": status if type(status) is int and 100 <= status <= 599 else None,
            "events_received": events_received,
        }, separators=(",", ":"), sort_keys=True), flush=True)
    except Exception:
        pass  # Diagnostics must never change stream recovery or retry behavior.


# HarnessToolUseStatus is an SDK enum: ``success`` or ``error``.
_TOOL_STATUSES = {"SUCCESS", "ERROR"}
_TOOL_RESULT_KEYS = {
    "reviewTaskId", "status", "errorCode", "operationStatus", "documents",
    "documentId", "matterId", "name", "mediaType", "documentDate",
    "confidentiality", "metadata",
}
_MAX_TOOL_FRAGMENT = 64 * 1024
# Agentic chat keeps whole tool results (a grounded answer plus its passages).
_MAX_AGENT_TOOL_RESULT = 256 * 1024
_MAX_TOOL_RESULTS = 16
_MAX_STREAM_BYTES = 1_048_576
# Every mapping serializes to at least two bytes ({}). At this bound even an
# empty-event flood exhausts the byte budget; ordinary text/tool deltas hit
# the byte cap first. This is not a limit on model calls or output tokens.
_MAX_STREAM_EVENTS = _MAX_STREAM_BYTES // 2


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


def _harness_stream_form(entry: str) -> str | None:
    """The name Harness gives a Gateway tool in its stream: ``@<group>/<rest>`` -> ``<group>_<rest>``.

    Observed live on 2026-11-03 (T9 step 11): ``allowedTools`` holds ``@legaldesk_gateway/search-documents___search_documents``
    and the stream names the same call ``legaldesk_gateway_search-documents___search_documents``. The mapping is derived
    from each permitted entry, one to one: a name that is not the image of an entry (another group, another target, another
    tool, a prefix or a suffix of one) is not recognised.
    """

    if not entry.startswith("@") or entry.count("/") != 1:
        return None
    group, _, rest = entry[1:].partition("/")
    if not group or not rest:
        return None
    return f"{group}_{rest}"


def _canonical_tool_name(name: object, allowed_tools: tuple[str, ...] | None) -> str | None:
    """The allowed name a stream tool name stands for, or ``None``.

    The exact entry and the short name (the segment after ``___``, the form of the local test doubles) are kept as they are;
    the Harness stream form is normalised to its entry so that everything downstream (call log, output tree, telemetry,
    allowlist checks) sees one name.
    """

    if not isinstance(name, str) or not name or len(name) > 256:
        return None
    entries = _MCP_TOOL_NAMES if allowed_tools is None else allowed_tools
    if name in entries or any(name == entry.rsplit("___", 1)[-1] for entry in entries):
        return name
    matches = [entry for entry in entries if _harness_stream_form(entry) == name]
    return matches[0] if len(matches) == 1 else None


def _tool_name_allowed(name: str, allowed_tools: tuple[str, ...] | None) -> bool:
    return _canonical_tool_name(name, allowed_tools) is not None


def _safe_tool_payload(value: object, *, raw: bool = False) -> Mapping[str, object]:
    """Keep bounded metadata needed by application code, never tool bodies.

    ``raw=True`` is for the agentic chat only: the result is unwrapped and
    returned whole (the backend validates it against a strict contract and
    never logs it) instead of being filtered to the review/metadata keys.
    """

    if value in (None, ""):
        return {}
    if isinstance(value, str):
        if len(value) > (_MAX_AGENT_TOOL_RESULT if raw else _MAX_TOOL_FRAGMENT):
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
    if raw:
        return value

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
    raw: bool = False,
    allow_incomplete: bool = False,
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
                    or (canonical := _canonical_tool_name(name, allowed_tools)) is None
                ):
                    raise ValueError("unknown or invalid Harness tool use")
                name = canonical
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
                try:
                    if not isinstance(fragments_delta, (list, tuple)) or not fragments_delta:
                        raise ValueError("Harness tool result fragment is invalid")
                    for fragment_block in fragments_delta:
                        if result_record.get("bad"):
                            break
                        if not isinstance(fragment_block, Mapping):
                            raise ValueError("Harness tool result fragment is invalid")
                        fragment = fragment_block.get("text")
                        if fragment is None and "json" in fragment_block:
                            fragment = json.dumps(fragment_block.get("json"), separators=(",", ":"))
                        limit = _MAX_AGENT_TOOL_RESULT if raw else _MAX_TOOL_FRAGMENT
                        if not isinstance(fragment, str) or len(fragment) > limit:
                            raise ValueError("Harness tool result fragment is invalid")
                        if sum(len(item) for item in fragments) + len(fragment) > limit:
                            raise ValueError("Harness tool result is too large")
                        fragments.append(fragment)
                except ValueError:
                    if not raw:
                        raise
                    # Agentic chat: an oversized or non-text result spoils only its own
                    # tool call. The other results of the loop stay usable (ADR-021 D5).
                    result_record["bad"] = True
                    fragments.clear()
    if len(uses) > _MAX_TOOL_RESULTS:
        raise ValueError("too many Harness tool results")
    if set(uses) != set(results) and not allow_incomplete:
        raise ValueError("Harness tool result is missing")
    parsed: list[HarnessToolResult] = []
    for tool_id, (name, _message_number, _block_index) in uses.items():
        record = results.get(tool_id)
        if record is None:
            continue  # the loop was cut before this call returned: nothing to reuse
        if record.get("bad"):
            parsed.append(HarnessToolResult(tool_use_id=tool_id, name=name, status="ERROR", payload={}, error_hint="unclassified"))
            continue
        fragments = record["fragments"]
        assert isinstance(fragments, list)
        joined = "".join(fragments)
        try:
            payload = _safe_tool_payload(joined, raw=raw)
        except ValueError:
            if not raw:
                raise
            # One unusable result must not discard the others in the loop.
            parsed.append(HarnessToolResult(tool_use_id=tool_id, name=name, status="ERROR", payload={}, error_hint=_tool_error_hint(joined)))
            continue
        if raw and record["status"] == "SUCCESS" and payload and _reports_its_own_failure(payload):
            parsed.append(HarnessToolResult(tool_use_id=tool_id, name=name, status="ERROR", payload={}, error_hint="tool_reported_error"))
            continue
        if record["status"] == "SUCCESS" and not payload:
            if raw:
                parsed.append(HarnessToolResult(tool_use_id=tool_id, name=name, status="ERROR", payload={}, error_hint="no_text"))
                continue
            raise ValueError("successful Harness tool result has no structured payload")
        if (
            not raw
            and record["status"] == "SUCCESS"
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
                error_hint=_tool_error_hint(joined) if record["status"] != "SUCCESS" else None,
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
            raise ValueError("session_id cannot be combined with a memory scope: the runtime session of a scoped call is always new")
        # A call made for a binding (memory scope or invocation scope) gets a NEW runtimeSessionId, like the agent chat (T9 step 11,
        # 2026-11-03): Harness keeps the history of a runtimeSessionId in its microVM even with memory disabled, so reusing the binding's
        # session id would build, outside the backend, a hidden per-session history of tool results (metadata, extracted fields, review
        # tasks). The binding's memory scope is NOT the runtime session: it still feeds actorId, the interceptor headers
        # (x-legaldesk-memory-session-id) and the invocation record. A caller that passes only ``session_id`` (smokes, probes) is
        # choosing its own session on purpose and keeps it.
        effective_session_id = validate_session_id(
            new_session_id() if memory_scope is not None else (session_id or new_session_id())
        )
        # Telemetry joins by the binding's session id when nothing better was given, as before.
        effective_correlation_id = correlation_id or (memory_scope.session_id if memory_scope is not None else effective_session_id)
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
        events: list[Mapping[str, Any]] = []
        try:
            if not isinstance(response, Mapping):
                raise ValueError("response must be an object")
            stream = response.get("stream")
            if isinstance(stream, (str, bytes, Mapping)) or not isinstance(stream, Iterable):
                raise ValueError("stream must be iterable")
            stream_bytes = 0
            for event in stream:
                if not isinstance(event, Mapping):
                    raise ValueError("event must be an object")
                if len(events) >= _MAX_STREAM_EVENTS:
                    raise StreamLimitExceeded("Harness stream event limit exceeded")
                try:
                    event_bytes = len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))
                except (TypeError, ValueError, UnicodeError) as exc:
                    raise ValueError("event cannot be bounded") from exc
                stream_bytes += event_bytes
                if stream_bytes > _MAX_STREAM_BYTES:
                    raise StreamLimitExceeded("Harness stream byte limit exceeded")
                events.append(event)
            text = _text_from_events(events)
            structured_metadata = _safe_structured_metadata(events)
            usage_records = _harness_usages(events)
            usage = sum_usage(usage_records)
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
            _record_stream_error(exc, effective_correlation_id, len(events))
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
            raise ClientStreamParserError("AgentCore returned an invalid event stream") from exc
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


# ---------------------------------------------------------------- agentic chat

AGENT_CHAT_MAX_ITERATIONS = 4
AGENT_CHAT_TIMEOUT_SECONDS = 90
AGENT_CHAT_MAX_TOKENS = 2048
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 24_000
MAX_HISTORY_TEXT_CHARS = 2_000
_MAX_QUESTION_CHARS = 8_000
_TRANSIENT_STREAM_CODES = frozenset({
    "throttlingexception", "serviceunavailableexception", "internalserverexception", "modeltimeoutexception",
})


@dataclass(frozen=True, slots=True)
class AgentToolCall:
    """One completed tool call of the loop, with its whole structured result."""

    tool_use_id: str
    name: str
    status: str
    payload: Mapping[str, object]
    error_hint: str | None = None  # closed label (ERROR_HINTS) of why a failed call failed; never the text

    @property
    def short_name(self) -> str:
        return self.name.rsplit("___", 1)[-1]


@dataclass(frozen=True, slots=True)
class AgentChatResult:
    """What the loop produced. ``loop_outcome`` uses the backend's closed vocabulary."""

    session_id: str
    correlation_id: str
    text: str
    loop_outcome: str  # completed | no_tool_called | failed | iterations_exhausted | timeout
    iterations: int
    stop_reason: str | None
    tool_calls: tuple[AgentToolCall, ...] = ()
    usage: HarnessUsage | None = None   # the TOTAL of the loop (sum of the model calls' blocks); None = unknown, never zero
    # Model calls the stream shows (assistant messages) and usage blocks it carried. Fewer blocks than calls means the total is a lower
    # bound (a cut stream, or a call that did not report): the cost estimate then adds an upper bound for the missing ones.
    model_calls: int = 0
    usage_records_count: int = 0
    # How many history messages were actually sent (after trimming); lets the caller tell
    # that a non-empty history was dropped.
    history_messages: int = 0
    # Closed diagnostics for a loop that did not finish (T9 step 12: nine data/mixed loops ended "failed" after the first tool and the
    # report could not say why). Short tool names and a label only, never text.
    unanswered_tools: tuple[str, ...] = ()   # tools the model asked for whose result never arrived
    stream_error_hint: str | None = None      # one of ERROR_HINTS, from the stream error if there was one
    search_query_comparisons: tuple[str, ...] = ()  # same / different / unavailable; never tool arguments


def _search_query_comparisons(events: list[Mapping[str, Any]], question: str) -> tuple[str, ...]:
    """Compare model-written search questions in memory; retain only closed labels.

    Converse-style toolUse input is JSON split across delta.toolUse.input frames.
    Missing, incomplete, duplicate or malformed arguments stay unknown.
    This projection cannot change routing, parsing, authorization or salvage.
    """
    comparisons: list[str] = []
    active: dict[int, tuple[int, list[str]]] = {}
    for event in events:
        if "messageStart" in event:
            active.clear()
        start = event.get("contentBlockStart")
        if isinstance(start, Mapping):
            detail = start.get("start")
            use = detail.get("toolUse") if isinstance(detail, Mapping) else None
            index = start.get("contentBlockIndex")
            if isinstance(use, Mapping) and type(index) is int:
                name = _canonical_tool_name(use.get("name"), None)
                if name and name.endswith("___search_documents"):
                    comparisons.append("unavailable")
                    active[index] = (len(comparisons) - 1, [])
        delta = event.get("contentBlockDelta")
        if isinstance(delta, Mapping) and type(delta.get("contentBlockIndex")) is int:
            record = active.get(delta["contentBlockIndex"])
            body = delta.get("delta")
            use = body.get("toolUse") if isinstance(body, Mapping) else None
            value = use.get("input") if isinstance(use, Mapping) else None
            if record and isinstance(value, str):
                record[1].append(value)
        stop = event.get("contentBlockStop")
        if isinstance(stop, Mapping) and type(stop.get("contentBlockIndex")) is int:
            record = active.pop(stop["contentBlockIndex"], None)
            if record:
                try:
                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError("duplicate argument")
                            result[key] = value
                        return result
                    arguments = json.loads("".join(record[1]), object_pairs_hook=unique)
                    value = arguments.get("question") if isinstance(arguments, Mapping) else None
                    if isinstance(value, str) and value.strip():
                        comparisons[record[0]] = "same" if value.strip() == question.strip() else "different"
                except (ValueError, TypeError, RecursionError):
                    pass
    return tuple(comparisons[:_MAX_TOOL_RESULTS])


def _unanswered_tool_names(events: list[Mapping[str, Any]], allowed_tools: tuple[str, ...] | None) -> tuple[str, ...]:
    """Short names of the tool uses in the stream that have no tool result (the parser drops them from the call log)."""

    uses: dict[str, str] = {}
    answered: set[str] = set()
    for event in events:
        start = event.get("contentBlockStart")
        detail = start.get("start") if isinstance(start, Mapping) else None
        if not isinstance(detail, Mapping):
            continue
        tool_use, tool_result = detail.get("toolUse"), detail.get("toolResult")
        if isinstance(tool_use, Mapping) and isinstance(tool_use.get("toolUseId"), str):
            canonical = _canonical_tool_name(tool_use.get("name"), allowed_tools)
            uses[tool_use["toolUseId"]] = canonical.rsplit("___", 1)[-1] if canonical else "other"
        if isinstance(tool_result, Mapping) and isinstance(tool_result.get("toolUseId"), str):
            answered.add(tool_result["toolUseId"])
    return tuple(name for tool_id, name in uses.items() if tool_id not in answered)


def _final_message_text(events: list[Mapping[str, Any]]) -> str:
    """Text of the last message that has any: the answer plan, not the reasoning before a tool call."""

    chunks: dict[int, list[str]] = {}
    message_number = -1
    for event in events:
        if "messageStart" in event:
            message_number += 1
        delta = event.get("contentBlockDelta", {}).get("delta", {}) if isinstance(event.get("contentBlockDelta"), Mapping) else {}
        if isinstance(delta, Mapping) and "text" in delta:
            chunks.setdefault(message_number, []).append(str(delta["text"]))
    return "".join(chunks[max(chunks)]) if chunks else ""


def _loop_shape(events: list[Mapping[str, Any]]) -> tuple[int, str | None]:
    """(assistant messages, stop reason of the last message)."""

    assistant_messages, stop_reason = 0, None
    for event in events:
        start = event.get("messageStart")
        if isinstance(start, Mapping) and start.get("role", "assistant") == "assistant":
            assistant_messages += 1
        stop = event.get("messageStop")
        if isinstance(stop, Mapping) and isinstance(stop.get("stopReason"), str):
            stop_reason = stop["stopReason"]
    return assistant_messages, stop_reason


def classify_loop(*, tool_calls: int, stop_reason: str | None, error: str | None) -> str:
    """Map the stream to the backend's closed loop outcomes.

    Harness signals a cut loop through its stop reason or a runtime error; the
    exact wording of the iteration and time limits is confirmed in the live
    block (PLAN_15 T9), so anything not clearly a clean end is treated as cut.
    """

    if error is not None:
        return "timeout" if "timeout" in error or "timed out" in error else "failed"
    if stop_reason in {"tool_use", "max_iterations_exceeded"}:
        return "iterations_exhausted"  # the model still wanted a tool when the budget ended
    if stop_reason not in (None, "end_turn", "stop_sequence"):
        return "failed"
    return "completed" if tool_calls else "no_tool_called"


def _agent_history(history: tuple[tuple[str, str], ...]) -> list[dict[str, Any]]:
    """Validate the backend's history and fit it to the budget; never raises on size.

    An oversized history must not make every later question of the conversation fail, so
    it is trimmed deterministically: each text is cut to ``MAX_HISTORY_TEXT_CHARS`` and the
    oldest messages are dropped, two at a time to keep the user/assistant pairs, until at
    most ``MAX_HISTORY_MESSAGES`` messages and ``MAX_HISTORY_CHARS`` characters remain. If
    nothing fits, the question goes without history. Malformed entries are still a
    programming error and raise.
    """

    if not isinstance(history, tuple):
        raise ValueError("history is invalid")
    cleaned: list[tuple[str, str]] = []
    for entry in history:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError("history entry is invalid")
        role, text = entry
        if role not in {"user", "assistant"} or not isinstance(text, str) or not text.strip():
            raise ValueError("history entry is invalid")
        cleaned.append((role, text.strip()[:MAX_HISTORY_TEXT_CHARS]))
    while cleaned and (
        len(cleaned) > MAX_HISTORY_MESSAGES
        or sum(len(text) for _, text in cleaned) > MAX_HISTORY_CHARS
        or cleaned[0][0] != "user"
    ):
        del cleaned[: 2 if len(cleaned) > 1 and cleaned[0][0] == "user" else 1]
    return [{"role": role, "content": [{"text": text}]} for role, text in cleaned]


_MATTER_SELECTOR = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def matter_selector_instruction(matter_id: str) -> str:
    """The server-built system instruction that tells the model the value of ``matterId``.

    Every Gateway tool of the agent requires ``matterId`` (the target schemas and the interceptor's raw-body check). The model was told
    never to pass it and was never given it, so its calls had no valid ``matterId``. The value is a selector, not authority: the interceptor
    compares it with the signed-in user's access and with the invocation record, so a model that passes another matter is refused.
    """

    if not isinstance(matter_id, str) or _MATTER_SELECTOR.fullmatch(matter_id) is None:
        raise ValueError("matter_id is not a safe selector")
    return (
        f"Matter selector for every tool call: {matter_id}. Pass exactly this value as the `matterId` argument of each tool you call. "
        "It is a selector, not an authorization: the backend checks it, and any other value makes the call fail."
    )


def invoke_agent_chat(
    invoker: "HarnessInvoker",
    message: str,
    *,
    invocation_scope: HarnessInvocationScope,
    system_prompt: tuple[Mapping[str, object], ...],
    history: tuple[tuple[str, str], ...] = (),
) -> AgentChatResult:
    """Run one read-only agent loop for the asynchronous chat (ADR-021 D4, D7).

    * no ``actorId``: Harness memory is not attached; the backend is the only
      owner of conversation memory and passes the last pairs as ``messages``;
    * the tool list, ``allowedTools`` and headers come only from the sealed
      scope; model, tools and skills are never overridable by the caller;
    * ``maxIterations`` 4 and ``timeoutSeconds`` 90 are fixed here.

    An error before the stream starts propagates (the worker decides if it is
    transient). A cut stream returns what completed so far so the worker can
    reuse a valid tool result (D5).
    """

    if not isinstance(message, str) or not message.strip() or len(message) > _MAX_QUESTION_CHARS:
        raise ValueError("message is invalid")
    if not isinstance(invocation_scope, HarnessInvocationScope):
        raise TypeError("invocation_scope must be a server-derived HarnessInvocationScope")
    if not isinstance(system_prompt, tuple) or not system_prompt:
        raise ValueError("the agent system prompt is required")
    # A NEW runtimeSessionId on every call (every job and every retry of a job). Harness keeps the conversation of a runtimeSessionId
    # in its microVM even with memory disabled (T9 step 11, 2026-11-03: history_retained), which would hold, outside the backend, the
    # previous turns and their tool results (document fragments). The backend is the only owner of conversation memory (ADR-021 D7)
    # and sends its own history. The binding's memory scope is NOT the runtime session: it still feeds the interceptor header
    # x-legaldesk-memory-session-id and the invocation record, whose check compares header and record, never the runtime session.
    session_id = validate_session_id(new_session_id())
    correlation_id = invocation_scope.correlation_id
    history_messages = _agent_history(history)
    request: dict[str, Any] = {
        "harnessArn": invoker.harness_arn,
        "runtimeSessionId": session_id,
        "messages": [*history_messages, {"role": "user", "content": [{"text": message.strip()}]}],
        "tools": [invocation_scope.remote_mcp_tool()],
        "allowedTools": list(invocation_scope.allowed_tools),
        "baggage": f"legaldesk.correlation_id={correlation_id}",
        "systemPrompt": [*system_prompt, {"text": matter_selector_instruction(invocation_scope.matter_id)}],
        "maxIterations": AGENT_CHAT_MAX_ITERATIONS,
        "maxTokens": AGENT_CHAT_MAX_TOKENS,
        "timeoutSeconds": AGENT_CHAT_TIMEOUT_SECONDS,
    }
    started_at = time.perf_counter()
    _record_agent_event(invoker.telemetry_sink, "agent", correlation_id, "started", operation="invoke_harness")
    try:
        response = invoker.client.invoke_harness(**request)
    except Exception:
        _record_agent_event(invoker.telemetry_sink, "agent", correlation_id, "error", operation="invoke_harness",
                            started_at=started_at, error_code="harness_invoke_failed")
        raise
    stream = response.get("stream") if isinstance(response, Mapping) else None
    if isinstance(stream, (str, bytes, Mapping)) or not isinstance(stream, Iterable):
        _record_agent_event(invoker.telemetry_sink, "agent", correlation_id, "error", operation="invoke_harness",
                            started_at=started_at, error_code="harness_malformed_response")
        raise HarnessInvocationError("AgentCore returned an invalid event stream")
    events: list[Mapping[str, Any]] = []
    stream_bytes, error = 0, None
    stream_error_hint = None
    try:
        for event in stream:
            if not isinstance(event, Mapping):
                raise ValueError("invalid Harness event")
            if len(events) >= _MAX_STREAM_EVENTS:
                raise StreamLimitExceeded("Harness stream event limit exceeded")
            stream_bytes += len(json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"))
            if stream_bytes > _MAX_STREAM_BYTES:
                raise StreamLimitExceeded("Harness stream byte limit exceeded")
            if "runtimeClientError" in event:
                message_text = event["runtimeClientError"].get("message", "") if isinstance(event["runtimeClientError"], Mapping) else ""
                raise HarnessInvocationError(str(message_text))
            events.append(event)
    except Exception as exc:
        _record_stream_error(exc, correlation_id, len(events))
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if isinstance(getattr(exc, "response", None), Mapping) else ""
        if not events and str(code).lower() in _TRANSIENT_STREAM_CODES:
            _record_agent_event(invoker.telemetry_sink, "agent", correlation_id, "error", operation="invoke_harness",
                                started_at=started_at, error_code="harness_stream_error")
            raise  # nothing was produced: a retry loses nothing
        error = str(exc).lower()
        stream_error_hint = "stream_limit_exceeded" if isinstance(exc, StreamLimitExceeded) else _tool_error_hint(error)
    try:
        calls = _structured_tool_results(
            events, allowed_tools=invocation_scope.allowed_tools, raw=True, allow_incomplete=True,
        )
        usage_records = _harness_usages(events)
    except ValueError as exc:
        _record_agent_event(invoker.telemetry_sink, "agent", correlation_id, "error", operation="invoke_harness",
                            started_at=started_at, error_code="harness_malformed_response")
        raise ClientStreamParserError("AgentCore returned an invalid event stream") from exc
    iterations, stop_reason = _loop_shape(events)
    outcome = classify_loop(tool_calls=len(calls), stop_reason=stop_reason, error=error)
    _record_agent_event(invoker.telemetry_sink, "agent", correlation_id, "succeeded" if error is None else "error",
                        operation="invoke_harness", started_at=started_at,
                        error_code=None if error is None else "harness_stream_error")
    return AgentChatResult(
        session_id=session_id, correlation_id=correlation_id, text=_final_message_text(events),
        loop_outcome=outcome, iterations=max(1, iterations), stop_reason=stop_reason,
        tool_calls=tuple(AgentToolCall(call.tool_use_id, call.name, call.status, call.payload, call.error_hint) for call in calls),
        usage=sum_usage(usage_records),
        # At least one: the invocation was made and answered, so a stream cut before its first message may still have billed a model call.
        model_calls=max(1, iterations),
        usage_records_count=len(usage_records),
        history_messages=len(history_messages),
        unanswered_tools=_unanswered_tool_names(events, invocation_scope.allowed_tools),
        stream_error_hint=stream_error_hint,
        search_query_comparisons=_search_query_comparisons(events, message),
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
