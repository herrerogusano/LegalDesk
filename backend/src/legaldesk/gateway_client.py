"""Deterministic, server-side MCP calls through AgentCore Gateway.

Explicit UI actions must not depend on Harness model tool selection. This
adapter sends one bounded JSON-RPC request to the configured Gateway URL while
reusing the sealed application binding used by the interceptor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID, uuid4

from .smoke_budget import SmokeBudget
from .identity import validate_https_endpoint


MCP_PROTOCOL_VERSION = "2025-03-26"
MAX_RESPONSE_BYTES = 64 * 1024
# AgentCore Gateway's target schema accepts only primitive properties.  Keep
# the IDP correction value opaque at that boundary, while retaining the raw
# value for local/direct calls and letting the review target apply the normal
# schema/type validation after decoding it.
MAX_IDP_PROPOSED_VALUE_JSON_BYTES = 16 * 1024
_TOOLS = {
    "list_matter_documents": "metadata-mcp___list_matter_documents",
    "get_document_metadata": "metadata-mcp___get_document_metadata",
    "create_review_task": "review-task-lambda___create_review_task",
    "list_review_tasks": "review-task-lambda___list_review_tasks",
    "get_review_task": "review-task-lambda___get_review_task",
    "update_review_task": "review-task-lambda___update_review_task",
}


class GatewayInvocationError(RuntimeError):
    """Safe, closed error for a failed Gateway dispatch or response."""

    def __init__(self, code: str = "gateway_unavailable") -> None:
        self.code = code
        super().__init__("Gateway operation failed")


def _gateway_idp_arguments(
    tool_name: str, arguments: Mapping[str, object]
) -> dict[str, object]:
    """Adapt the raw IDP decision to the deployed Gateway schema.

    ``proposedValue`` is intentionally not validated here: the review target
    owns field registry/type/evidence validation.  Gateway's CloudFormation
    schema cannot represent that value's scalar-or-array union, however, so
    the transport carries one strict JSON string.  This is a wire adapter,
    not a second business-rule implementation.
    """

    adapted = dict(arguments)
    decision = adapted.get("idpDecision")
    if tool_name != "update_review_task" or decision is None:
        return adapted
    if not isinstance(decision, Mapping):
        raise GatewayInvocationError("gateway_invalid_arguments")
    # The public/raw contract accepts ``proposedValue`` only.  A
    # ``proposedValueJson`` field is target-only wire data and must never be
    # accepted from an HTTP/local caller as if it were already adapted.
    if "proposedValueJson" in decision:
        raise GatewayInvocationError("gateway_invalid_arguments")
    if "proposedValue" not in decision:
        return adapted
    if decision.get("action") != "CORRECT":
        raise GatewayInvocationError("gateway_invalid_arguments")
    try:
        encoded = json.dumps(
            decision["proposedValue"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise GatewayInvocationError("gateway_invalid_arguments") from exc
    if len(encoded.encode("utf-8")) > MAX_IDP_PROPOSED_VALUE_JSON_BYTES:
        raise GatewayInvocationError("gateway_invalid_arguments")
    adapted_decision = dict(decision)
    adapted_decision.pop("proposedValue")
    adapted_decision["proposedValueJson"] = encoded
    adapted["idpDecision"] = adapted_decision
    return adapted


@dataclass(frozen=True, slots=True)
class GatewayHttpResponse:
    status: int
    content_type: str
    body: bytes


class GatewayTransport(Protocol):
    def post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> GatewayHttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise GatewayInvocationError("gateway_redirect")


class UrllibGatewayTransport:
    """Single-attempt HTTPS transport with redirects disabled."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirect())

    def post(
        self,
        url: str,
        body: bytes,
        headers: Mapping[str, str],
        timeout: float,
    ) -> GatewayHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with self._opener.open(request, timeout=timeout) as response:
                return GatewayHttpResponse(
                    int(response.status),
                    str(response.headers.get("Content-Type", "")),
                    _read_bounded(response),
                )
        except HTTPError as exc:
            # HTTPError is also a response; preserve only status and bounded
            # body parsing, never expose provider text in the application.
            body = _read_bounded(exc)
            return GatewayHttpResponse(int(exc.code), str(exc.headers.get("Content-Type", "")), body)
        except GatewayInvocationError:
            raise
        except (URLError, OSError, TimeoutError) as exc:
            raise GatewayInvocationError("gateway_transport") from exc


def _read_bounded(response: Any) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise GatewayInvocationError("gateway_response_too_large")
    return body


def _json_response(response: GatewayHttpResponse) -> Mapping[str, object]:
    content_type = response.content_type.split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayInvocationError("gateway_invalid_response") from exc
        if not isinstance(payload, Mapping):
            raise GatewayInvocationError("gateway_invalid_response")
        return payload
    if content_type == "text/event-stream":
        try:
            lines = response.body.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise GatewayInvocationError("gateway_invalid_response") from exc
        for line in reversed(lines):
            if line.startswith("data:"):
                try:
                    payload = json.loads(line[5:].strip())
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GatewayInvocationError("gateway_invalid_response") from exc
                if isinstance(payload, Mapping):
                    return payload
        raise GatewayInvocationError("gateway_invalid_response")
    raise GatewayInvocationError("gateway_content_type")


def _payload_from_result(result: object) -> Mapping[str, object]:
    if not isinstance(result, Mapping):
        raise GatewayInvocationError("gateway_invalid_result")
    if result.get("isError") is True:
        raise GatewayInvocationError("gateway_tool_error")
    content = result.get("content")
    if content is None:
        payload = result
    else:
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
            raise GatewayInvocationError("gateway_invalid_result")
        block = content[0]
        fragment = block.get("json", block.get("text"))
        if isinstance(fragment, str):
            try:
                fragment = json.loads(fragment)
            except json.JSONDecodeError as exc:
                raise GatewayInvocationError("gateway_invalid_result") from exc
        payload = fragment
    if not isinstance(payload, Mapping):
        raise GatewayInvocationError("gateway_invalid_result")
    return dict(payload)


class DirectGatewayInvoker:
    """One deterministic MCP tools/call through AgentCore Gateway."""

    def __init__(
        self,
        gateway_url: str,
        *,
        transport: GatewayTransport | None = None,
        timeout_seconds: float = 15.0,
        budget: SmokeBudget | None = None,
    ) -> None:
        gateway_url = validate_https_endpoint(gateway_url, field_name="gateway_url")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 0 < timeout_seconds <= 60:
            raise ValueError("timeout_seconds is invalid")
        self.gateway_url = gateway_url
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport or UrllibGatewayTransport()
        self.budget = budget

    def invoke(
        self,
        binding: object,
        *,
        tool_name: str,
        arguments: Mapping[str, object],
        request_id: str | None = None,
    ) -> dict[str, object]:
        # Deterministic UI actions do not enter Harness, so validating their
        # binding through the agent package would incorrectly make the model's
        # tool allowlist the application router. Accept only the same sealed
        # backend capability and build its fixed transport headers directly.
        if (
            type(binding).__module__ != "legaldesk.agent_integration"
            or type(binding).__name__ != "HarnessInvocationBinding"
        ):
            raise GatewayInvocationError("gateway_invalid_binding")
        is_sealed = getattr(binding, "_is_sealed", None)
        if not callable(is_sealed) or not is_sealed():
            raise GatewayInvocationError("gateway_invalid_binding")
        binding_gateway_url = getattr(binding, "gateway_url", None)
        if binding_gateway_url != self.gateway_url:
            raise GatewayInvocationError("gateway_invalid_binding")
        allowed_tools = getattr(binding, "allowed_tools", None)
        qualified_name = _TOOLS.get(tool_name)
        if (
            qualified_name is None
            or not isinstance(allowed_tools, tuple)
            or f"@legaldesk_gateway/{qualified_name}" not in allowed_tools
        ):
            raise GatewayInvocationError("gateway_tool_denied")
        if not isinstance(arguments, Mapping):
            raise GatewayInvocationError("gateway_invalid_arguments")
        bearer_token = getattr(binding, "bearer_token", None)
        matter_id = getattr(binding, "matter_id", None)
        correlation_id = getattr(binding, "correlation_id", None)
        invocation_id = getattr(binding, "invocation_id", None)
        memory_scope = getattr(binding, "memory_scope", None)
        memory_is_sealed = getattr(memory_scope, "_is_sealed", None)
        if (
            not isinstance(bearer_token, str)
            or not bearer_token
            or len(bearer_token) > 16_384
            or any(char in bearer_token for char in "\r\n")
            or not isinstance(matter_id, str)
            or not matter_id
            or len(matter_id) > 128
            or not callable(memory_is_sealed)
            or not memory_is_sealed()
        ):
            raise GatewayInvocationError("gateway_invalid_binding")
        try:
            correlation_id = str(UUID(correlation_id))
            invocation_id = str(UUID(invocation_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise GatewayInvocationError("gateway_invalid_binding") from exc
        actor_id = getattr(memory_scope, "actor_id", None)
        memory_session_id = getattr(memory_scope, "session_id", None)
        if not isinstance(actor_id, str) or not actor_id or not isinstance(memory_session_id, str) or not memory_session_id:
            raise GatewayInvocationError("gateway_invalid_binding")
        request_id = request_id or str(uuid4())
        try:
            request_id = str(UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise GatewayInvocationError("gateway_invalid_request") from exc
        request_arguments = _gateway_idp_arguments(tool_name, arguments)
        request_arguments["matterId"] = matter_id
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": qualified_name, "arguments": request_arguments},
        }
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers_factory = getattr(binding, "gateway_headers", None)
        if not callable(headers_factory):
            raise GatewayInvocationError("gateway_invalid_binding")
        headers = headers_factory()
        headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        })
        call = lambda: self.transport.post(self.gateway_url, body, headers, self.timeout_seconds)
        response = self.budget.call("gateway", call) if self.budget is not None else call()
        if not isinstance(response, GatewayHttpResponse):
            raise GatewayInvocationError("gateway_invalid_transport")
        if response.status < 200 or response.status >= 300:
            raise GatewayInvocationError("gateway_http_error")
        payload = _json_response(response)
        if payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id:
            raise GatewayInvocationError("gateway_invalid_response")
        if payload.get("error") is not None:
            raise GatewayInvocationError("gateway_tool_error")
        result = _payload_from_result(payload.get("result"))
        return {
            "status": "SUCCESS",
            "tool": f"@legaldesk_gateway/{qualified_name}",
            "result": result,
            "correlationId": correlation_id,
        }


__all__ = [
    "DirectGatewayInvoker",
    "GatewayHttpResponse",
    "GatewayInvocationError",
    "GatewayTransport",
    "MCP_PROTOCOL_VERSION",
    "UrllibGatewayTransport",
]
