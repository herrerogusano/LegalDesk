"""Small remote-MCP metadata server boundary for LegalDesk.

The transport adapter is intentionally JSON-RPC shaped but HTTP-neutral. A
trusted edge supplies the server-built :class:`RequestContext`; MCP arguments
never contain tenant or matter scope.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Mapping

from .authorization import (
    AuthorizationStore,
    Boto3DynamoAuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
)
from .documents import (
    Boto3DynamoDocumentMetadataRepository,
    Document,
    DocumentMetadataRepository,
)


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MCP_PROTOCOL_VERSION = "2025-06-18"
MCP_SERVER_NAME = "legaldesk-metadata"
MCP_SCHEMA_VERSION = "1"

LIST_MATTER_DOCUMENTS = "list_matter_documents"
GET_DOCUMENT_METADATA = "get_document_metadata"

LIST_MATTER_DOCUMENTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}
GET_DOCUMENT_METADATA_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "documentId": {
            "type": "string",
            "pattern": _OPAQUE_ID.pattern,
            "maxLength": 128,
        }
    },
    "required": ["documentId"],
}

MCP_TOOL_DEFINITIONS: tuple[dict[str, object], ...] = (
    {
        "name": LIST_MATTER_DOCUMENTS,
        "description": "List document metadata for the current matter.",
        "inputSchema": LIST_MATTER_DOCUMENTS_SCHEMA,
    },
    {
        "name": GET_DOCUMENT_METADATA,
        "description": "Get non-content metadata for one document.",
        "inputSchema": GET_DOCUMENT_METADATA_SCHEMA,
    },
)


class MCPRequestError(ValueError):
    """Safe client-side protocol error."""


@dataclass(frozen=True, slots=True)
class MCPServer:
    """JSON-RPC MCP endpoint using an already authorized request context."""

    metadata_repository: DocumentMetadataRepository

    def handle_jsonrpc(
        self,
        request: Mapping[str, object],
        *,
        request_context: RequestContext | None = None,
    ) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            return self._error(None, -32600, "invalid request", None)
        if request.get("jsonrpc") != "2.0":
            return self._error(request.get("id"), -32600, "invalid request", None)
        request_id = request.get("id")
        method = request.get("method")
        if method == "notifications/initialized" and "id" not in request:
            return None
        if not isinstance(method, str) or isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return self._error(request_id, -32600, "invalid request", request_context)
        try:
            if "params" in request and not isinstance(request.get("params"), Mapping):
                raise MCPRequestError("params must be an object")
            if method == "initialize":
                result: dict[str, object] = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": MCP_SERVER_NAME, "version": "1.0"},
                    "capabilities": {"tools": {"listChanged": False}},
                }
            elif method == "tools/list":
                self._strict_params(request.get("params", {}), expected=set())
                result = {"tools": [dict(tool) for tool in MCP_TOOL_DEFINITIONS]}
            elif method == "ping":
                self._strict_params(request.get("params", {}), expected=set())
                result = {}
            elif method == "tools/call":
                if not isinstance(request_context, RequestContext):
                    return self._error(request_id, -32001, "access denied", None)
                result = self._call_tool(request.get("params"), request_context)
            else:
                return self._error(request_id, -32601, "method not found", request_context)
            if isinstance(request_context, RequestContext):
                result["_meta"] = {"correlationId": request_context.correlation_id}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPRequestError:
            return self._error(request_id, -32602, "invalid parameters", request_context)
        except Exception:
            # Provider errors never expose stack traces or document data.
            return self._error(request_id, -32000, "metadata service unavailable", request_context)

    def _call_tool(
        self,
        raw_params: object,
        context: RequestContext,
    ) -> dict[str, object]:
        if not isinstance(raw_params, Mapping):
            raise MCPRequestError("params must be an object")
        if set(raw_params) != {"name", "arguments"}:
            raise MCPRequestError("tool call shape is invalid")
        name = raw_params.get("name")
        arguments = raw_params.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            raise MCPRequestError("tool call shape is invalid")
        if name == LIST_MATTER_DOCUMENTS:
            self._strict_params(arguments, expected=set())
            payload = {
                "documents": [
                    self._safe_document(document, context)
                    for document in self.metadata_repository.list_for_scope(
                        tenant_id=context.tenant_id,
                        matter_id=context.matter_id,
                    )
                    if self._in_scope(document, context)
                ]
            }
        elif name == GET_DOCUMENT_METADATA:
            self._strict_params(arguments, expected={"documentId"})
            document_id = arguments.get("documentId")
            if (
                not isinstance(document_id, str)
                or _OPAQUE_ID.fullmatch(document_id) is None
            ):
                raise MCPRequestError("document ID is invalid")
            document = self.metadata_repository.get_for_scope(
                tenant_id=context.tenant_id,
                matter_id=context.matter_id,
                document_id=document_id,
            )
            if not isinstance(document, Document) or not self._in_scope(document, context):
                raise MCPRequestError("document not found")
            payload = {"document": self._safe_document(document, context)}
        else:
            raise MCPRequestError("tool not found")
        return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}

    @staticmethod
    def _strict_params(value: object, *, expected: set[str]) -> None:
        if not isinstance(value, Mapping) or set(value) != expected:
            raise MCPRequestError("parameters are invalid")

    @staticmethod
    def _in_scope(document: object, context: RequestContext) -> bool:
        return (
            isinstance(document, Document)
            and document.tenant_id == context.tenant_id
            and document.matter_id == context.matter_id
        )

    @staticmethod
    def _safe_document(document: Document, context: RequestContext) -> dict[str, object]:
        if not MCPServer._in_scope(document, context):
            raise MCPRequestError("document not found")
        return {
            "documentId": document.document_id,
            "name": document.name,
            "mediaType": document.media_type,
            "jurisdiction": document.jurisdiction,
            "documentDate": document.document_date,
            "confidentiality": document.confidentiality,
            "status": document.status.value,
            "fileSizeBytes": document.file_size_bytes,
        }

    @staticmethod
    def _error(
        request_id: object,
        code: int,
        message: str,
        context: RequestContext | None,
    ) -> dict[str, object]:
        response: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
        if context is not None:
            response["_meta"] = {"correlationId": context.correlation_id}
        return response


def handle_metadata_request_for_identity(
    request: Mapping[str, object],
    identity: VerifiedIdentity,
    requested_matter_id: str,
    *,
    authorization_store: AuthorizationStore,
    metadata_repository: DocumentMetadataRepository,
    correlation_id: str | None = None,
) -> dict[str, object]:
    """Verified-edge adapter: authorize first, then dispatch the MCP request."""

    context = build_request_context(
        identity,
        requested_matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    return MCPServer(metadata_repository).handle_jsonrpc(
        request,
        request_context=context,
    )


def _mcp_repositories_from_environment(
) -> tuple[AuthorizationStore, DocumentMetadataRepository] | None:
    table_name = os.environ.get("DOCUMENT_METADATA_TABLE_NAME") or os.environ.get(
        "REVIEW_TASK_TABLE_NAME"
    )
    if (
        not table_name
        or os.environ.get("MCP_SCHEMA_VERSION", MCP_SCHEMA_VERSION) != MCP_SCHEMA_VERSION
    ):
        return None
    import boto3

    table = boto3.resource("dynamodb").Table(table_name)
    return (
        Boto3DynamoAuthorizationStore(table_name, table=table),
        Boto3DynamoDocumentMetadataRepository(table_name, table=table),
    )


def mcp_lambda_handler(event: Mapping[str, object], _lambda_context: object) -> dict[str, object]:
    """AWS Function URL adapter for the remote MCP server.

    The Function URL must use IAM authentication. Header values are only
    selectors; this handler reloads User/Matter from DynamoDB and never trusts
    a tenant or user supplied by the caller. Gateway header propagation and
    identity verification remain a deployment-time precondition.
    """

    if not isinstance(event, Mapping):
        return _http_json(400, {"error": "invalid_request"})
    headers = event.get("headers", {})
    body = event.get("body")
    if not isinstance(headers, Mapping) or not isinstance(body, str):
        return _http_json(403, {"error": "access_denied"})
    try:
        try:
            request = json.loads(body)
        except (TypeError, ValueError):
            return _http_json(400, {"error": "invalid_request"})
        if not isinstance(request, Mapping):
            return _http_json(400, {"error": "invalid_request"})
        lifecycle_method = request.get("method") in {
            "initialize",
            "tools/list",
            "ping",
            "notifications/initialized",
        }
        subject = headers.get("x-legaldesk-verified-subject")
        matter_id = headers.get("x-legaldesk-requested-matter-id")
        correlation_id = headers.get("x-legaldesk-correlation-id")
        if not lifecycle_method and not all(
            isinstance(value, str) for value in (subject, matter_id, correlation_id)
        ):
            return _http_json(403, {"error": "access_denied"})
        repositories = _mcp_repositories_from_environment()
        if repositories is None:
            return _http_json(503, {"error": "service_unavailable"})
        authorization_store, metadata_repository = repositories
        if lifecycle_method and not any(
            value is not None for value in (subject, matter_id, correlation_id)
        ):
            response = MCPServer(metadata_repository).handle_jsonrpc(request)
        else:
            response = handle_metadata_request_for_identity(
                request,
                VerifiedIdentity(subject),
                matter_id,
                authorization_store=authorization_store,
                metadata_repository=metadata_repository,
                correlation_id=correlation_id,
            )
        if response is None:
            return {"statusCode": 204, "headers": {"content-type": "application/json"}, "body": ""}
        status_code = 200 if "result" in response else 400
        return _http_json(status_code, response)
    except Exception:
        return _http_json(403, {"error": "access_denied"})


def _http_json(status_code: int, payload: Mapping[str, object]) -> dict[str, object]:
    """Return a Lambda Function URL response with a stable JSON content type."""

    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


__all__ = [
    "GET_DOCUMENT_METADATA",
    "GET_DOCUMENT_METADATA_SCHEMA",
    "LIST_MATTER_DOCUMENTS",
    "LIST_MATTER_DOCUMENTS_SCHEMA",
    "MCP_PROTOCOL_VERSION",
    "MCP_SERVER_NAME",
    "MCP_SCHEMA_VERSION",
    "MCPRequestError",
    "MCPServer",
    "MCP_TOOL_DEFINITIONS",
    "handle_metadata_request_for_identity",
    "mcp_lambda_handler",
]
