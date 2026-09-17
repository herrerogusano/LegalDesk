"""REQUEST interceptor seam for Gateway identity-to-scope propagation.

AgentCore validates the bearer token before invoking this function. The local
adapter therefore parses the already-validated JWT payload only to obtain its
``sub``; it does not claim to perform signature verification. Authorization is
still re-run against DynamoDB before any target receives transformed context.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    Boto3DynamoAuthorizationStore,
    VerifiedIdentity,
    build_request_context,
)


class GatewayTarget(StrEnum):
    REVIEW_LAMBDA = "review-task-lambda"
    METADATA_MCP = "metadata-mcp"


@dataclass(frozen=True, slots=True)
class InterceptorEnvelope:
    verifiedSubject: str
    requestedMatterId: str
    correlationId: str


GATEWAY_GRANT_ENTITY = "GatewayAuthorizationGrant"
GATEWAY_GRANT_SORT_KEY = "PROFILE"
GATEWAY_GRANT_TTL_SECONDS = 300
GATEWAY_TOOL_DELIMITER = "___"


def _target_for_gateway_tool(tool_name: object) -> GatewayTarget:
    """Resolve only exact Gateway-visible tool names and fail closed otherwise."""

    if not isinstance(tool_name, str):
        raise AuthorizationDenied("access denied")
    expected = {
        f"{GatewayTarget.REVIEW_LAMBDA.value}{GATEWAY_TOOL_DELIMITER}create_review_task": (
            GatewayTarget.REVIEW_LAMBDA
        ),
        f"{GatewayTarget.METADATA_MCP.value}{GATEWAY_TOOL_DELIMITER}list_matter_documents": (
            GatewayTarget.METADATA_MCP
        ),
        f"{GatewayTarget.METADATA_MCP.value}{GATEWAY_TOOL_DELIMITER}get_document_metadata": (
            GatewayTarget.METADATA_MCP
        ),
    }
    try:
        return expected[tool_name]
    except KeyError as exc:
        raise AuthorizationDenied("access denied") from exc


@dataclass(frozen=True, slots=True)
class GatewayAuthorizationGrant:
    """Opaque, short-lived authorization capability for a Lambda target."""

    grant_id: str
    verified_subject: str
    requested_matter_id: str
    correlation_id: str
    tool_name: str
    expires_at: int

    @property
    def item(self) -> dict[str, object]:
        return {
            "pk": gateway_grant_partition_key(self.grant_id),
            "sk": GATEWAY_GRANT_SORT_KEY,
            "entityType": GATEWAY_GRANT_ENTITY,
            "verifiedSubject": self.verified_subject,
            "requestedMatterId": self.requested_matter_id,
            "correlationId": self.correlation_id,
            "toolName": self.tool_name,
            "expiresAt": self.expires_at,
        }


class GatewayGrantRepository(Protocol):
    def put(self, grant: GatewayAuthorizationGrant) -> None: ...

    def get(self, grant_id: str) -> Mapping[str, object] | None: ...


def gateway_grant_partition_key(grant_id: str) -> str:
    return f"GATEWAY#GRANT#{grant_id}"


@dataclass(slots=True)
class InMemoryGatewayGrantRepository:
    grants: dict[str, Mapping[str, object]]

    def __init__(self) -> None:
        self.grants = {}

    def put(self, grant: GatewayAuthorizationGrant) -> None:
        if grant.grant_id in self.grants:
            raise RuntimeError("grant collision")
        self.grants[grant.grant_id] = grant.item

    def get(self, grant_id: str) -> Mapping[str, object] | None:
        return self.grants.get(grant_id)


class Boto3DynamoGatewayGrantRepository:
    """Grant adapter reusing the existing Phase 02 single-table layout."""

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    def put(self, grant: GatewayAuthorizationGrant) -> None:
        self.table.put_item(
            Item=grant.item,
            ConditionExpression="attribute_not_exists(pk)",
        )

    def get(self, grant_id: str) -> Mapping[str, object] | None:
        response = self.table.get_item(
            Key={"pk": gateway_grant_partition_key(grant_id), "sk": GATEWAY_GRANT_SORT_KEY},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None


def _decode_verified_subject(authorization: object) -> str:
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise AuthorizationDenied("access denied")
    token = authorization[7:].strip()
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise AuthorizationDenied("access denied")
    try:
        encoded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise AuthorizationDenied("access denied") from exc
    subject = claims.get("sub") if isinstance(claims, Mapping) else None
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 256:
        raise AuthorizationDenied("access denied")
    return subject


def _safe_error(event: Mapping[str, object], message: str = "access denied") -> dict[str, object]:
    body = event.get("mcp")
    gateway_request = body.get("gatewayRequest") if isinstance(body, Mapping) else None
    request_body = gateway_request.get("body") if isinstance(gateway_request, Mapping) else None
    request_id = request_body.get("id") if isinstance(request_body, Mapping) else None
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        request_id = None
    elif isinstance(request_id, str) and len(request_id) > 128:
        request_id = None
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayResponse": {
                "statusCode": 403,
                "body": {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32001, "message": message}},
            }
        },
    }


def transform_gateway_request(
    event: Mapping[str, object],
    *,
    target: GatewayTarget,
    authorization_store: AuthorizationStore,
    grant_repository: GatewayGrantRepository | None = None,
    correlation_id_factory: Any = uuid4,
    grant_id_factory: Any = uuid4,
) -> dict[str, object]:
    """Authorize a Gateway request and overwrite target context deterministically."""

    if not isinstance(event, Mapping):
        raise AuthorizationDenied("access denied")
    mcp = event.get("mcp")
    gateway_request = mcp.get("gatewayRequest") if isinstance(mcp, Mapping) else None
    if not isinstance(gateway_request, Mapping):
        raise AuthorizationDenied("access denied")
    headers = gateway_request.get("headers")
    body = gateway_request.get("body")
    if not isinstance(headers, Mapping) or not isinstance(body, Mapping):
        raise AuthorizationDenied("access denied")
    subject = _decode_verified_subject(headers.get("Authorization") or headers.get("authorization"))
    matter_id = headers.get("x-legaldesk-requested-matter-id")
    if not isinstance(matter_id, str) or not matter_id.strip():
        raise AuthorizationDenied("access denied")
    correlation_id = str(correlation_id_factory())
    try:
        UUID(correlation_id)
    except (ValueError, AttributeError) as exc:
        raise AuthorizationDenied("access denied") from exc
    context = build_request_context(
        VerifiedIdentity(subject),
        matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    envelope = InterceptorEnvelope(subject, context.matter_id, context.correlation_id)
    transformed_body = dict(body)
    if target is GatewayTarget.REVIEW_LAMBDA:
        if grant_repository is None:
            raise AuthorizationDenied("access denied")
        params = transformed_body.get("params")
        arguments = params.get("arguments") if isinstance(params, Mapping) else None
        if not isinstance(params, Mapping) or not isinstance(arguments, Mapping):
            raise AuthorizationDenied("access denied")
        grant_id = str(grant_id_factory())
        try:
            UUID(grant_id)
        except (ValueError, AttributeError) as exc:
            raise AuthorizationDenied("access denied") from exc
        grant = GatewayAuthorizationGrant(
            grant_id=grant_id,
            verified_subject=envelope.verifiedSubject,
            requested_matter_id=envelope.requestedMatterId,
            correlation_id=envelope.correlationId,
            tool_name="create_review_task",
            expires_at=int(time.time()) + GATEWAY_GRANT_TTL_SECONDS,
        )
        try:
            grant_repository.put(grant)
        except Exception as exc:
            raise AuthorizationDenied("access denied") from exc
        transformed_params = dict(params)
        transformed_arguments = dict(arguments)
        # Overwrite, never honor, a client/model supplied grant selector.
        transformed_arguments["_legaldeskGrantId"] = grant_id
        transformed_params["arguments"] = transformed_arguments
        transformed_body["params"] = transformed_params
    elif target is GatewayTarget.METADATA_MCP:
        transformed_headers = {
            str(key): value
            for key, value in headers.items()
            if str(key).lower()
            not in {
                "authorization",
                "x-legaldesk-verified-subject",
                "x-legaldesk-requested-matter-id",
                "x-legaldesk-correlation-id",
            }
        }
        transformed_headers.update(
            {
                "x-legaldesk-verified-subject": envelope.verifiedSubject,
                "x-legaldesk-requested-matter-id": envelope.requestedMatterId,
                "x-legaldesk-correlation-id": envelope.correlationId,
            }
        )
        return {
            "interceptorOutputVersion": "1.0",
            "mcp": {
                "transformedGatewayRequest": {
                    "headers": transformed_headers,
                    "body": transformed_body,
                }
            },
        }
    else:
        raise AuthorizationDenied("access denied")
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {"transformedGatewayRequest": {"body": transformed_body}},
    }


def _authorization_store_from_environment() -> AuthorizationStore | None:
    table_name = os.environ.get("REVIEW_TASK_TABLE_NAME") or os.environ.get(
        "DOCUMENT_METADATA_TABLE_NAME"
    )
    if not table_name:
        return None
    return Boto3DynamoAuthorizationStore(table_name)


def _grant_repository_from_environment() -> GatewayGrantRepository | None:
    table_name = os.environ.get("REVIEW_TASK_TABLE_NAME") or os.environ.get(
        "DOCUMENT_METADATA_TABLE_NAME"
    )
    if not table_name:
        return None
    return Boto3DynamoGatewayGrantRepository(table_name)


def gateway_request_interceptor(event: Mapping[str, object], _lambda_context: object) -> dict[str, object]:
    """AWS-compatible REQUEST interceptor; tokens and bodies are never logged."""

    if not isinstance(event, Mapping):
        return _safe_error({}, "access denied")
    try:
        mcp = event.get("mcp")
        gateway_request = mcp.get("gatewayRequest") if isinstance(mcp, Mapping) else None
        body = gateway_request.get("body") if isinstance(gateway_request, Mapping) else None
        tool_name = body.get("params", {}).get("name") if isinstance(body, Mapping) else None
        method = body.get("method") if isinstance(body, Mapping) else None
        if method in {"initialize", "tools/list", "ping", "notifications/initialized"}:
            # MCP capability discovery is not a business-scope operation. The
            # Function URL remains AWS_IAM-restricted; no caller selectors are
            # propagated during Gateway dynamic sync.
            return {
                "interceptorOutputVersion": "1.0",
                "mcp": {"transformedGatewayRequest": {"body": dict(body)}}
            }
        target = _target_for_gateway_tool(tool_name)
        store = _authorization_store_from_environment()
        if store is None:
            return _safe_error(event, "service unavailable")
        grants = _grant_repository_from_environment() if target is GatewayTarget.REVIEW_LAMBDA else None
        return transform_gateway_request(
            event,
            target=target,
            authorization_store=store,
            grant_repository=grants,
        )
    except AuthorizationDenied:
        return _safe_error(event)
    except Exception:
        return _safe_error(event)


__all__ = [
    "GatewayTarget",
    "GatewayAuthorizationGrant",
    "GatewayGrantRepository",
    "Boto3DynamoGatewayGrantRepository",
    "InMemoryGatewayGrantRepository",
    "gateway_grant_partition_key",
    "GATEWAY_GRANT_ENTITY",
    "GATEWAY_GRANT_SORT_KEY",
    "GATEWAY_GRANT_TTL_SECONDS",
    "GATEWAY_TOOL_DELIMITER",
    "InterceptorEnvelope",
    "gateway_request_interceptor",
    "transform_gateway_request",
]
