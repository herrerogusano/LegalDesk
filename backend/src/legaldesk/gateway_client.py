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
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from .smoke_budget import SmokeBudget


MCP_PROTOCOL_VERSION = "2025-03-26"
MAX_RESPONSE_BYTES = 64 * 1024
_TOOLS = {
    "list_matter_documents": "metadata-mcp___list_matter_documents",
    "get_document_metadata": "metadata-mcp___get_document_metadata",
    "create_review_task": "review-task-lambda___create_review_task",
}


class GatewayInvocationError(RuntimeError):
    """Safe, closed error for a failed Gateway dispatch or response."""

    def __init__(self, code: str = "gateway_unavailable") -> None:
        self.code = code
        super().__init__("Gateway operation failed")


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
        parsed = urlsplit(gateway_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("gateway_url must be an HTTPS URL without credentials or query")
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
        from legaldesk_agent import HarnessInvocationScope

        scope = HarnessInvocationScope.from_derived(binding)
        qualified_name = _TOOLS.get(tool_name)
        if qualified_name is None or f"@legaldesk_gateway/{qualified_name}" not in scope.allowed_tools:
            raise GatewayInvocationError("gateway_tool_denied")
        if not isinstance(arguments, Mapping):
            raise GatewayInvocationError("gateway_invalid_arguments")
        request_id = request_id or str(uuid4())
        try:
            request_id = str(UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise GatewayInvocationError("gateway_invalid_request") from exc
        request_arguments = dict(arguments)
        request_arguments["matterId"] = scope.matter_id
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": qualified_name, "arguments": request_arguments},
        }
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = scope.gateway_headers()
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
            "correlationId": scope.correlation_id,
        }


__all__ = [
    "DirectGatewayInvoker",
    "GatewayHttpResponse",
    "GatewayInvocationError",
    "GatewayTransport",
    "MCP_PROTOCOL_VERSION",
    "UrllibGatewayTransport",
]
