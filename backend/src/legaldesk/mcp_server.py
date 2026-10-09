"""Small remote-MCP metadata server boundary for LegalDesk.

The transport adapter is intentionally JSON-RPC shaped but HTTP-neutral. A
trusted edge supplies the server-built :class:`RequestContext`; MCP arguments
never contain tenant or matter scope.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    Boto3DynamoAuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    _gateway_identity_from_verified_subject,
    build_request_context,
    require_authorized_context,
)
from .gateway_interceptor import (
    GATEWAY_GRANT_ENTITY,
    GATEWAY_GRANT_SORT_KEY,
    GatewayGrantRepository,
    Boto3DynamoGatewayGrantRepository,
    gateway_grant_partition_key,
)
from .documents import (
    Boto3DynamoDocumentMetadataRepository,
    Document,
    DocumentMetadataRepository,
)
from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
)


_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
# AgentCore Gateway currently supports the March 2025 MCP revision.
MCP_PROTOCOL_VERSION = "2025-03-26"
MCP_SERVER_NAME = "legaldesk-metadata"
MCP_SCHEMA_VERSION = "1"

LIST_MATTER_DOCUMENTS = "list_matter_documents"
GET_DOCUMENT_METADATA = "get_document_metadata"

LIST_MATTER_DOCUMENTS_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "matterId": {
            "type": "string",
            "pattern": _OPAQUE_ID.pattern,
            "maxLength": 128,
            "description": "Matter to query.",
        }
    },
    "required": ["matterId"],
}
GET_DOCUMENT_METADATA_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "documentId": {
            "type": "string",
            "pattern": _OPAQUE_ID.pattern,
            "maxLength": 128,
        },
        "matterId": {
            "type": "string",
            "pattern": _OPAQUE_ID.pattern,
            "maxLength": 128,
            "description": "Matter containing the document.",
        },
        "historyLimit": {
            "type": "integer", "minimum": 1, "maximum": 20,
            "description": "Optional bounded IDP extraction history page size.",
        },
        "historyCursor": {
            "type": "string", "maxLength": 4096,
            "description": "Opaque server-issued IDP history cursor.",
        },
    },
    "required": ["documentId", "matterId"],
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
    telemetry_sink: TelemetrySink | None = None
    idp_repository: Any | None = None
    idp_review_service: Any | None = None

    def handle_jsonrpc(
        self,
        request: Mapping[str, object],
        *,
        request_context: RequestContext | None = None,
    ) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            return self._error(None, -32600, "invalid request", None)
        if request_context is not None:
            try:
                request_context = require_authorized_context(request_context)
            except AuthorizationDenied:
                request_context = None
            if request.get("id") is not None:
                if request_context is None:
                    return self._error(request.get("id"), -32001, "access denied", None)
        if request.get("jsonrpc") != "2.0":
            return self._error(request.get("id"), -32600, "invalid request", None)
        request_id = request.get("id")
        method = request.get("method")
        # JSON-RPC notifications never receive a response. Unknown or
        # side-effecting notification methods are ignored rather than run.
        if "id" not in request:
            return None
        if not isinstance(method, str) or isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            return self._error(request_id, -32600, "invalid request", request_context)
        try:
            if method == "initialize":
                result: dict[str, object] = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": MCP_SERVER_NAME, "version": "1.0"},
                    "capabilities": {"tools": {"listChanged": False}},
                }
            elif method == "tools/list":
                params = request.get("params", {})
                self._strict_params(
                    params,
                    expected=set(),
                    optional={"cursor", "_meta"},
                )
                if (
                    isinstance(params, Mapping)
                    and "cursor" in params
                    and params.get("cursor") is not None
                    and not isinstance(params.get("cursor"), str)
                ):
                    raise MCPRequestError("cursor must be a string or null")
                result = {"tools": [dict(tool) for tool in MCP_TOOL_DEFINITIONS]}
            elif method == "ping":
                self._strict_params(
                    request.get("params", {}),
                    expected=set(),
                    optional={"_meta"},
                )
                result = {}
            elif method == "tools/call":
                if request_context is None:
                    return self._error(request_id, -32001, "access denied", None)
                result = self._call_tool(request.get("params"), request_context)
            else:
                return self._error(request_id, -32601, "method not found", request_context)
            if request_context is not None:
                result["_meta"] = {"correlationId": request_context.correlation_id}
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPRequestError:
            if request_context is not None and method == "tools/call":
                emit_telemetry(
                    self.telemetry_sink,
                    TelemetryEventType.TOOL,
                    request_context.correlation_id,
                    TelemetryOutcome.ERROR,
                    operation="mcp_tool_call",
                    error_code="invalid_parameters",
                )
            return self._error(request_id, -32602, "invalid parameters", request_context)
        except Exception:
            # Provider errors never expose stack traces or document data.
            if request_context is not None:
                if method == "tools/call":
                    # _call_tool emits STARTED before touching the provider;
                    # close that span as a TOOL error so ToolErrors metrics
                    # remain faithful. Keep the generic ERROR pointer too.
                    emit_telemetry(
                        self.telemetry_sink,
                        TelemetryEventType.TOOL,
                        request_context.correlation_id,
                        TelemetryOutcome.ERROR,
                        operation="mcp_tool_call",
                        error_code="mcp_service_unavailable",
                    )
                emit_telemetry(
                    self.telemetry_sink,
                    TelemetryEventType.ERROR,
                    request_context.correlation_id,
                    TelemetryOutcome.ERROR,
                    operation="mcp_tool_call" if method == "tools/call" else "mcp_request",
                    error_code="mcp_service_unavailable",
                )
            return self._error(request_id, -32000, "metadata service unavailable", request_context)

    def _call_tool(
        self,
        raw_params: object,
        context: RequestContext,
    ) -> dict[str, object]:
        context = require_authorized_context(context)
        started_at = time.perf_counter()
        emit_telemetry(
            self.telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.STARTED,
            operation="mcp_tool_call",
        )
        if not isinstance(raw_params, Mapping):
            raise MCPRequestError("params must be an object")
        param_names = set(raw_params)
        if not {"name", "arguments"}.issubset(param_names) or param_names - {
            "name",
            "arguments",
            "_meta",
        }:
            raise MCPRequestError("tool call shape is invalid")
        if "_meta" in raw_params and not isinstance(raw_params.get("_meta"), Mapping):
            raise MCPRequestError("tool call metadata is invalid")
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
            self._strict_params(arguments, expected={"documentId"}, optional={"historyLimit", "historyCursor"})
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
            if self.idp_repository is not None:
                payload["idp"] = self._idp_projection(
                    context=context,
                    document_id=document_id,
                    document=document,
                    history_limit=arguments.get("historyLimit"),
                    history_cursor=arguments.get("historyCursor"),
                )
        else:
            raise MCPRequestError("tool not found")
        result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}
        emit_telemetry(
            self.telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.SUCCEEDED,
            started_at=started_at,
            operation=name,
            count=len(payload.get("documents", ())) if isinstance(payload.get("documents"), list) else None,
        )
        return result

    def _idp_projection(
        self,
        *,
        context: RequestContext,
        document_id: str,
        document: Document | None = None,
        history_limit: object = None,
        history_cursor: object = None,
    ) -> dict[str, object]:
        """Build a bounded current IDP projection from server-owned runs."""

        if history_limit is None:
            limit = 1
        elif isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 1 <= history_limit <= 20:
            raise MCPRequestError("history limit is invalid")
        else:
            limit = history_limit
        if history_cursor is not None and (not isinstance(history_cursor, str) or not history_cursor.strip()):
            raise MCPRequestError("history cursor is invalid")
        page_method = getattr(self.idp_repository, "list_run_history", None)
        pointer_run_id = getattr(document, "idp_run_id", None) if document is not None else None
        pointer_status = getattr(document, "idp_status", None) if document is not None else None
        pointer_hash = getattr(document, "idp_document_sha256", None) if document is not None else None
        pointer_source = getattr(document, "idp_source_key", None) if document is not None else None
        # A persisted document pointer is authoritative for the current
        # generation.  History is only a separately paged audit view and must
        # never replace the pointer when a cursor is supplied.
        pointed_run = None
        if pointer_run_id and hasattr(self.idp_repository, "get_run"):
            pointed_run = self.idp_repository.get_run(
                tenant_id=context.tenant_id,
                matter_id=context.matter_id,
                document_id=document_id,
                run_id=pointer_run_id,
            )
            if pointed_run is not None and (
                pointer_hash is not None and pointed_run.document_sha256 != pointer_hash
                or pointer_source is not None and pointed_run.source_key != pointer_source
            ):
                # Never expose a run whose durable identity disagrees with
                # the current document projection.
                pointed_run = None

        if callable(page_method):
            current_page = page_method(tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id, limit=1, cursor=None)
            current_runs = tuple(getattr(current_page, "runs", ()))
            page = page_method(tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id, limit=limit, cursor=history_cursor) if history_limit is not None else current_page
            runs = tuple(getattr(page, "runs", ()))
            next_cursor = getattr(page, "next_cursor", None)
        else:
            current_runs = tuple(self.idp_repository.list_runs(tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id, limit=1))
            runs = tuple(self.idp_repository.list_runs(tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id, limit=limit)) if history_limit is not None else current_runs
            next_cursor = None

        def field_payload(name: str, result: Any) -> dict[str, object]:
            if isinstance(result, Mapping):
                return {
                    "name": name, "value": result.get("value"),
                    "presence": result.get("presence"), "origin": result.get("origin"),
                    "acceptance": result.get("acceptance"),
                    "provenance": dict(result.get("provenance", {}) or {}),
                    "evidence": [
                        {"page": item.page, "quote": item.quote[:2_000], "contentSha256": item.content_sha256, "start": item.start, "end": item.end}
                        for item in tuple(result.get("evidence", ()))[:8]
                    ],
                    "reason": result.get("reason", ""),
                }
            evidence = []
            for anchor in tuple(getattr(result, "evidence", ()))[:8]:
                evidence.append({
                    "page": anchor.page, "quote": anchor.quote[:2_000],
                    "contentSha256": anchor.content_sha256,
                    "start": anchor.start, "end": anchor.end,
                })
            return {
                "name": name, "value": getattr(result, "value", None),
                "presence": getattr(getattr(result, "presence", None), "value", None),
                "origin": getattr(getattr(result, "origin", None), "value", None),
                "acceptance": getattr(getattr(result, "acceptance", None), "value", None),
                "provenance": dict(getattr(result, "provenance", {}) or {}),
                "evidence": evidence,
                "reason": getattr(result, "reason", ""),
            }

        current = pointed_run if pointer_run_id else (current_runs[0] if current_runs else None)
        effective_fields = {}
        if current is not None:
            if self.idp_review_service is not None and hasattr(self.idp_review_service, "effective_fields_for_run"):
                effective_fields = dict(self.idp_review_service.effective_fields_for_run(run=current))
            else:
                for name, field_result in (getattr(current, "fields", {}) or {}).items():
                    effective_fields[name] = self.idp_review_service.effective_field(run=current, field_name=name) if self.idp_review_service is not None else field_result
        from .idp.models import public_idp_status
        current_status = pointer_status or (public_idp_status(current.status) if current is not None else None)
        # A terminal failure/skip or an in-flight generation may leave an
        # older run reference on the document projection for auditability.
        # Never present that stale run as usable current IDP data.  Only a
        # completed or review-required generation may expose fields.
        if current_status not in {"IDP_COMPLETED", "IDP_REVIEW_REQUIRED"}:
            current = None
        projection: dict[str, object] = {
            "status": current_status,
            "runId": getattr(current, "run_id", None),
            "documentSha256": getattr(current, "document_sha256", None),
            "documentType": getattr(getattr(current, "document_type", None), "value", None),
            "schemaVersion": getattr(current, "schema_version", None),
            "fields": [field_payload(name, result) for name, result in effective_fields.items()][:64] if current else [],
        }
        if current_status is None:
            projection["enabled"] = False
        if history_limit is not None:
            projection["history"] = [
                {"runId": run.run_id, "documentSha256": run.document_sha256, "schemaVersion": run.schema_version,
                 "status": public_idp_status(run.status), "createdAt": run.created_at.isoformat()}
                for run in runs
            ]
            projection["nextCursor"] = next_cursor
        return projection

    @staticmethod
    def _strict_params(
        value: object,
        *,
        expected: set[str],
        optional: set[str] | None = None,
    ) -> None:
        allowed_optional = optional or set()
        if (
            not isinstance(value, Mapping)
            or not expected.issubset(set(value))
            or set(value) - expected - allowed_optional
        ):
            raise MCPRequestError("parameters are invalid")
        if "_meta" in value and not isinstance(value.get("_meta"), Mapping):
            raise MCPRequestError("metadata must be an object")

    @staticmethod
    def _in_scope(document: object, context: RequestContext) -> bool:
        try:
            context = require_authorized_context(context)
        except AuthorizationDenied:
            return False
        return (
            isinstance(document, Document)
            and document.tenant_id == context.tenant_id
            and document.matter_id == context.matter_id
        )

    @staticmethod
    def _safe_document(document: Document, context: RequestContext) -> dict[str, object]:
        context = require_authorized_context(context)
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
            "uploadedAt": document.uploaded_at.isoformat(),
        }

    @staticmethod
    def _error(
        request_id: object,
        code: int,
        message: str,
        context: RequestContext | None,
    ) -> dict[str, object]:
        if context is not None:
            try:
                context = require_authorized_context(context)
            except AuthorizationDenied:
                context = None
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
    telemetry_sink: TelemetrySink | None = None,
    idp_repository: Any | None = None,
    idp_review_service: Any | None = None,
) -> dict[str, object]:
    """Verified-edge adapter: authorize first, then dispatch the MCP request."""

    context = build_request_context(
        identity,
        requested_matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    return MCPServer(metadata_repository, telemetry_sink=telemetry_sink, idp_repository=idp_repository, idp_review_service=idp_review_service).handle_jsonrpc(
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
        Boto3DynamoDocumentMetadataRepository(
            table_name,
            table=table,
            boto3_backed=True,
        ),
    )


def _idp_artifact_store_from_environment(*, metadata_repository: Any) -> tuple[Any | None, Any | None]:
    """Return the production page-text verifier when the IDP bucket is configured."""
    bucket = (os.environ.get("LEGALDESK_IDP_ARTIFACT_BUCKET") or os.environ.get("LEGALDESK_SOURCE_BUCKET") or "").strip()
    if not bucket:
        return None, None
    import boto3
    from botocore.config import Config
    from .idp.artifacts import Boto3S3IDPArtifactStore
    from .idp.runtime import Boto3IDPDocumentReader
    s3 = boto3.client(
        "s3",
        region_name=os.environ.get("AWS_REGION"),
        config=Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 1, "mode": "standard"}),
    )
    return (
        Boto3S3IDPArtifactStore(bucket, client=s3),
        Boto3IDPDocumentReader(metadata_repository, s3, bucket_name=os.environ.get("LEGALDESK_SOURCE_BUCKET") or bucket, max_bytes=20 * 1024 * 1024),
    )


def _mcp_grant_repository_from_environment() -> GatewayGrantRepository | None:
    table_name = os.environ.get("DOCUMENT_METADATA_TABLE_NAME") or os.environ.get(
        "REVIEW_TASK_TABLE_NAME"
    )
    if not table_name:
        return None
    return Boto3DynamoGatewayGrantRepository(table_name)


def _validated_mcp_grant(
    repository: GatewayGrantRepository,
    grant_id: object,
    request: Mapping[str, object],
) -> tuple[str, str, str] | None:
    """Load one exact, short-lived Gateway capability; headers are ignored."""

    if not isinstance(grant_id, str):
        return None
    try:
        UUID(grant_id)
    except (ValueError, AttributeError):
        return None
    item = repository.get(grant_id)
    if not isinstance(item, Mapping):
        return None
    required = {
        "pk", "sk", "entityType", "verifiedSubject", "requestedMatterId",
        "correlationId", "toolName", "expiresAt",
    }
    if set(item) not in (required, required | {"ttl"}) or item.get("pk") != gateway_grant_partition_key(grant_id):
        return None
    if item.get("sk") != GATEWAY_GRANT_SORT_KEY or item.get("entityType") != GATEWAY_GRANT_ENTITY:
        return None
    tool_name = item.get("toolName")
    subject = item.get("verifiedSubject")
    matter_id = item.get("requestedMatterId")
    correlation_id = item.get("correlationId")
    expires_at = item.get("expiresAt")
    if (
        tool_name not in {LIST_MATTER_DOCUMENTS, GET_DOCUMENT_METADATA}
        or not all(isinstance(value, str) and value for value in (subject, matter_id, correlation_id))
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, (int, float, Decimal))
        or not math.isfinite(float(expires_at))
        or expires_at <= time.time()
        or (
            "ttl" in item
            and (isinstance(item.get("ttl"), bool) or not isinstance(item.get("ttl"), (int, float, Decimal))
                 or not math.isfinite(float(item.get("ttl"))))
        )
    ):
        return None
    params = request.get("params")
    request_tool = params.get("name") if isinstance(params, Mapping) else None
    if request_tool != tool_name:
        return None
    return subject, matter_id, correlation_id
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
    normalized_headers = _normalize_headers(headers)
    if normalized_headers is None:
        return _http_json(400, {"error": "invalid_request"})
    encoded = event.get("isBase64Encoded", False)
    if not isinstance(encoded, bool):
        return _http_json(400, {"error": "invalid_request"})
    if encoded:
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return _http_json(400, {"error": "invalid_request"})
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
        repositories = _mcp_repositories_from_environment()
        if repositories is None:
            return _http_json(503, {"error": "service_unavailable"})
        authorization_store, metadata_repository = repositories
        table_name = os.environ.get("DOCUMENT_METADATA_TABLE_NAME") or os.environ.get("REVIEW_TASK_TABLE_NAME")
        idp_repository = None
        idp_review_service = None
        if table_name:
            try:
                from .idp.persistence import Boto3DynamoIDPRepository
                from .idp.review import Boto3DynamoIDPDecisionRepository, IDPReviewService
                shared_table = getattr(metadata_repository, "table", None)
                idp_repository = Boto3DynamoIDPRepository(table_name, table=shared_table)
                artifact_store, source_reader = _idp_artifact_store_from_environment(metadata_repository=metadata_repository)
                idp_review_service = IDPReviewService(
                    Boto3DynamoIDPDecisionRepository(table_name, table=shared_table),
                    artifact_store=artifact_store,
                    source_reader=source_reader,
                )
            except Exception:
                return _http_json(503, {"error": "service_unavailable"})
        if lifecycle_method:
            response = MCPServer(metadata_repository, idp_repository=idp_repository, idp_review_service=idp_review_service).handle_jsonrpc(request)
        else:
            grant_repository = _mcp_grant_repository_from_environment()
            grant = (
                _validated_mcp_grant(
                    grant_repository,
                    normalized_headers.get("x-legaldesk-grant-id"),
                    request,
                )
                if grant_repository is not None
                else None
            )
            if grant is None:
                return _http_json(403, {"error": "access_denied"})
            subject, matter_id, correlation_id = grant
            response = handle_metadata_request_for_identity(
                request,
                _gateway_identity_from_verified_subject(subject),
                matter_id,
                authorization_store=authorization_store,
                metadata_repository=metadata_repository,
                correlation_id=correlation_id,
                idp_repository=idp_repository,
                idp_review_service=idp_review_service,
            )
        if response is None:
            return {
                "statusCode": 204,
                "headers": {
                    "content-type": "application/json",
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
                },
                "body": "",
            }
        status_code = 200 if "result" in response else 400
        return _http_json(status_code, response)
    except Exception:
        return _http_json(403, {"error": "access_denied"})


def _http_json(status_code: int, payload: Mapping[str, object]) -> dict[str, object]:
    """Return a Lambda Function URL response with a stable JSON content type."""

    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        },
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _normalize_headers(headers: Mapping[object, object]) -> dict[str, object] | None:
    """Normalize Function URL headers without accepting ambiguous duplicates."""

    normalized: dict[str, object] = {}
    for key, value in headers.items():
        if not isinstance(key, str):
            return None
        normalized_key = key.casefold()
        if normalized_key in normalized:
            return None
        normalized[normalized_key] = value
    return normalized


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
