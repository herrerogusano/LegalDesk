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
import logging
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    Boto3DynamoAuthorizationStore,
    VerifiedIdentity,
    _gateway_identity_from_verified_subject,
    build_request_context,
)
from .observability import (
    TelemetryEventType,
    TelemetryOutcome,
    TelemetrySink,
    emit_telemetry,
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
GATEWAY_INVOCATION_ENTITY = "HarnessInvocationBinding"
GATEWAY_INVOCATION_TTL_SECONDS = 300
GATEWAY_EXPIRY_INDEX_ENTITY = "GatewayExpiryIndex"
GATEWAY_TOOL_DELIMITER = "___"
_APPLICATION_ALLOWED_TOOL_NAMES = frozenset(
    {
        "@legaldesk_gateway/metadata-mcp___list_matter_documents",
        "@legaldesk_gateway/metadata-mcp___get_document_metadata",
        "@legaldesk_gateway/review-task-lambda___create_review_task",
        "@legaldesk_gateway/review-task-lambda___list_review_tasks",
        "@legaldesk_gateway/review-task-lambda___get_review_task",
        "@legaldesk_gateway/review-task-lambda___update_review_task",
    }
)
MAX_RAW_GATEWAY_BODY_BYTES = 64 * 1024
_LOGGER = logging.getLogger(__name__)


def _target_for_gateway_tool(tool_name: object) -> GatewayTarget:
    """Resolve only the exact Gateway and target-local tool names."""

    if not isinstance(tool_name, str):
        raise AuthorizationDenied("access denied")
    expected = {
        **{
            f"{GatewayTarget.REVIEW_LAMBDA.value}{GATEWAY_TOOL_DELIMITER}{name}": GatewayTarget.REVIEW_LAMBDA
            for name in ("create_review_task", "list_review_tasks", "get_review_task", "update_review_task")
        },
        **{name: GatewayTarget.REVIEW_LAMBDA for name in ("create_review_task", "list_review_tasks", "get_review_task", "update_review_task")},
        f"{GatewayTarget.METADATA_MCP.value}{GATEWAY_TOOL_DELIMITER}list_matter_documents": (
            GatewayTarget.METADATA_MCP
        ),
        "list_matter_documents": GatewayTarget.METADATA_MCP,
        f"{GatewayTarget.METADATA_MCP.value}{GATEWAY_TOOL_DELIMITER}get_document_metadata": (
            GatewayTarget.METADATA_MCP
        ),
        "get_document_metadata": GatewayTarget.METADATA_MCP,
    }
    try:
        return expected[tool_name]
    except KeyError as exc:
        raise AuthorizationDenied("access denied") from exc


def _local_gateway_tool_name(tool_name: object) -> str:
    if not isinstance(tool_name, str):
        raise AuthorizationDenied("access denied")
    return tool_name.rsplit(GATEWAY_TOOL_DELIMITER, 1)[-1]


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
            "ttl": self.expires_at,
        }


class GatewayGrantRepository(Protocol):
    def put(self, grant: GatewayAuthorizationGrant) -> None: ...

    def get(self, grant_id: str) -> Mapping[str, object] | None: ...

    def delete(self, grant_id: str) -> None: ...

    def put_invocation(self, grant: "HarnessInvocationGrant") -> None: ...

    def get_invocation(self, invocation_id: str) -> Mapping[str, object] | None: ...

    def delete_invocation(self, invocation_id: str) -> None: ...

    def list_expired_candidates(self, *, now: float, limit: int) -> tuple[Mapping[str, object], ...]: ...


def gateway_grant_partition_key(grant_id: str) -> str:
    return f"GATEWAY#GRANT#{grant_id}"


@dataclass(slots=True)
class InMemoryGatewayGrantRepository:
    grants: dict[str, Mapping[str, object]]
    invocations: dict[str, Mapping[str, object]]
    expiry_index: dict[tuple[str, str], Mapping[str, object]]

    def __init__(self) -> None:
        self.grants = {}
        self.invocations = {}
        self.expiry_index: dict[tuple[str, str], Mapping[str, object]] = {}

    @staticmethod
    def _expiry_item(*, record_id: str, kind: str, item: Mapping[str, object]) -> dict[str, object]:
        expires_at = item.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
            raise ValueError("gateway expiry is invalid")
        expires = int(expires_at)
        return {
            "pk": gateway_expiry_index_partition_key(expires),
            "sk": gateway_expiry_index_sort_key(expires, kind, record_id),
            "entityType": GATEWAY_EXPIRY_INDEX_ENTITY,
            "recordId": record_id,
            "kind": kind,
            "verifiedSubject": item.get("verifiedSubject"),
            "requestedMatterId": item.get("requestedMatterId"),
            "expiresAt": expires,
            "ttl": expires,
        }

    def put(self, grant: GatewayAuthorizationGrant) -> None:
        if grant.grant_id in self.grants:
            if self.grants[grant.grant_id] == grant.item:
                index = self._expiry_item(record_id=grant.grant_id, kind="grant", item=grant.item)
                self.expiry_index[(index["pk"], index["sk"])] = index
                return
            raise RuntimeError("grant collision")
        self.grants[grant.grant_id] = grant.item
        index = self._expiry_item(record_id=grant.grant_id, kind="grant", item=grant.item)
        self.expiry_index[(index["pk"], index["sk"])] = index

    def get(self, grant_id: str) -> Mapping[str, object] | None:
        return self.grants.get(grant_id)

    def delete(self, grant_id: str) -> None:
        item = self.grants.pop(grant_id, None)
        if item is not None:
            index = self._expiry_item(record_id=grant_id, kind="grant", item=item)
            self.expiry_index.pop((index["pk"], index["sk"]), None)

    def put_invocation(self, grant: "HarnessInvocationGrant") -> None:
        if grant.invocation_id in self.invocations:
            raise RuntimeError("invocation collision")
        self.invocations[grant.invocation_id] = grant.item
        index = self._expiry_item(record_id=grant.invocation_id, kind="invocation", item=grant.item)
        self.expiry_index[(index["pk"], index["sk"])] = index

    def get_invocation(self, invocation_id: str) -> Mapping[str, object] | None:
        return self.invocations.get(invocation_id)

    def delete_invocation(self, invocation_id: str) -> None:
        item = self.invocations.pop(invocation_id, None)
        if item is not None:
            index = self._expiry_item(record_id=invocation_id, kind="invocation", item=item)
            self.expiry_index.pop((index["pk"], index["sk"]), None)

    def list_expired_candidates(self, *, now: float, limit: int) -> tuple[Mapping[str, object], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 100:
            raise ValueError("gateway expiry query limit is invalid")
        cutoff = int(now)
        items = [
            item for item in self.expiry_index.values()
            if isinstance(item.get("expiresAt"), (int, float))
            and not isinstance(item.get("expiresAt"), bool)
            and float(item["expiresAt"]) <= cutoff
        ]
        items.sort(key=lambda item: (int(item["expiresAt"]), str(item.get("sk", ""))))
        return tuple(items[:limit])


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
        try:
            self.table.put_item(
                Item=grant.item,
                ConditionExpression="attribute_not_exists(pk)",
            )
            self._put_expiry_index(grant_id=grant.grant_id, kind="grant", item=grant.item)
        except Exception:
            existing = self.get(grant.grant_id)
            if existing != grant.item:
                raise
            # An idempotent retry also repairs a missing index entry.
            self._put_expiry_index(grant_id=grant.grant_id, kind="grant", item=grant.item)

    def get(self, grant_id: str) -> Mapping[str, object] | None:
        response = self.table.get_item(
            Key={"pk": gateway_grant_partition_key(grant_id), "sk": GATEWAY_GRANT_SORT_KEY},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None

    def delete(self, grant_id: str) -> None:
        item = self.get(grant_id)
        self.table.delete_item(Key={"pk": gateway_grant_partition_key(grant_id), "sk": GATEWAY_GRANT_SORT_KEY})
        if isinstance(item, Mapping):
            self._delete_expiry_index(grant_id=grant_id, kind="grant", item=item)

    def put_invocation(self, grant: "HarnessInvocationGrant") -> None:
        try:
            self.table.put_item(
                Item=grant.item,
                ConditionExpression="attribute_not_exists(pk)",
            )
            self._put_expiry_index(grant_id=grant.invocation_id, kind="invocation", item=grant.item)
        except Exception:
            existing = self.get_invocation(grant.invocation_id)
            if existing != grant.item:
                raise
            # If the primary write succeeded but the index write failed, a
            # retry must repair the index instead of treating the condition
            # failure as a permanent collision.
            self._put_expiry_index(grant_id=grant.invocation_id, kind="invocation", item=grant.item)

    def get_invocation(self, invocation_id: str) -> Mapping[str, object] | None:
        response = self.table.get_item(
            Key={"pk": gateway_invocation_partition_key(invocation_id), "sk": GATEWAY_GRANT_SORT_KEY},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None

    def delete_invocation(self, invocation_id: str) -> None:
        item = self.get_invocation(invocation_id)
        self.table.delete_item(Key={"pk": gateway_invocation_partition_key(invocation_id), "sk": GATEWAY_GRANT_SORT_KEY})
        if isinstance(item, Mapping):
            self._delete_expiry_index(grant_id=invocation_id, kind="invocation", item=item)

    def _put_expiry_index(self, *, grant_id: str, kind: str, item: Mapping[str, object]) -> None:
        expires_at = item.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float, Decimal)):
            raise ValueError("gateway expiry is invalid")
        expires = int(expires_at)
        self.table.put_item(
            Item={
                "pk": gateway_expiry_index_partition_key(expires),
                "sk": gateway_expiry_index_sort_key(expires, kind, grant_id),
                "entityType": GATEWAY_EXPIRY_INDEX_ENTITY,
                "recordId": grant_id,
                "kind": kind,
                "verifiedSubject": item.get("verifiedSubject"),
                "requestedMatterId": item.get("requestedMatterId"),
                "expiresAt": expires,
                "ttl": expires,
            },
        )

    def _delete_expiry_index(self, *, grant_id: str, kind: str, item: Mapping[str, object]) -> None:
        expires_at = item.get("expiresAt")
        if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float, Decimal)):
            return
        expires = int(expires_at)
        self.table.delete_item(
            Key={
                "pk": gateway_expiry_index_partition_key(expires),
                "sk": gateway_expiry_index_sort_key(expires, kind, grant_id),
            }
        )

    def list_expired_candidates(self, *, now: float, limit: int) -> tuple[Mapping[str, object], ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 < limit <= 100:
            raise ValueError("gateway expiry query limit is invalid")
        from boto3.dynamodb.conditions import Key

        cutoff = int(now)
        # Expiry partitions are UTC-day buckets. Query today and yesterday only;
        # DynamoDB TTL remains the bounded fallback for a missed older window.
        items: list[Mapping[str, object]] = []
        for day in (cutoff // 86_400, (cutoff // 86_400) - 1):
            response = self.table.query(
                KeyConditionExpression=(
                    Key("pk").eq(gateway_expiry_index_partition_key(day * 86_400))
                    & Key("sk").lte(f"EXP#{cutoff:013d}~")
                ),
                ProjectionExpression="#entity,#record,#kind,#subject,#matter,#expires,#pk,#sk",
                ExpressionAttributeNames={
                    "#entity": "entityType", "#record": "recordId", "#kind": "kind",
                    "#subject": "verifiedSubject", "#matter": "requestedMatterId",
                    "#expires": "expiresAt", "#pk": "pk", "#sk": "sk",
                },
                Limit=limit,
                ScanIndexForward=True,
                ConsistentRead=True,
            )
            page = response.get("Items", ()) if isinstance(response, Mapping) else ()
            items.extend(item for item in page if isinstance(item, Mapping))
            if len(items) >= limit:
                break
        items.sort(key=lambda item: (float(item.get("expiresAt", float("inf"))), str(item.get("sk", ""))))
        return tuple(items[:limit])


@dataclass(frozen=True, slots=True)
class HarnessInvocationGrant:
    """Short-lived opaque binding issued by the application for one request."""

    invocation_id: str
    verified_subject: str
    requested_matter_id: str
    correlation_id: str
    memory_actor_id: str
    memory_session_id: str
    allowed_tools: tuple[str, ...]
    expires_at: int

    @property
    def item(self) -> dict[str, object]:
        return {
            "pk": gateway_invocation_partition_key(self.invocation_id),
            "sk": GATEWAY_GRANT_SORT_KEY,
            "entityType": GATEWAY_INVOCATION_ENTITY,
            "verifiedSubject": self.verified_subject,
            "requestedMatterId": self.requested_matter_id,
            "correlationId": self.correlation_id,
            "memoryActorId": self.memory_actor_id,
            "memorySessionId": self.memory_session_id,
            "allowedTools": list(self.allowed_tools),
            "expiresAt": self.expires_at,
            "ttl": self.expires_at,
        }


def gateway_invocation_partition_key(invocation_id: str) -> str:
    return f"GATEWAY#INVOCATION#{invocation_id}"


def gateway_expiry_index_partition_key(expires_at: int | float) -> str:
    return f"GATEWAY#EXPIRY#{int(expires_at) // 86_400 * 86_400}"


def gateway_expiry_index_sort_key(expires_at: int | float, kind: str, record_id: str) -> str:
    return f"EXP#{int(expires_at):013d}#{kind.upper()}#{record_id}"


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


def _raw_gateway_body(raw_gateway_request: object) -> Mapping[str, object]:
    """Parse the AWS raw request only to recover the untrusted matter selector."""

    if not isinstance(raw_gateway_request, Mapping):
        raise AuthorizationDenied("access denied")
    raw_body = raw_gateway_request.get("body")
    if isinstance(raw_body, str):
        try:
            if len(raw_body.encode("utf-8")) > MAX_RAW_GATEWAY_BODY_BYTES:
                raise AuthorizationDenied("access denied")
            parsed = json.loads(raw_body)
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AuthorizationDenied("access denied") from exc
    elif isinstance(raw_body, Mapping):
        parsed = raw_body
    else:
        raise AuthorizationDenied("access denied")
    if not isinstance(parsed, Mapping):
        raise AuthorizationDenied("access denied")
    return parsed


def _normalize_arguments(value: object) -> Mapping[str, object]:
    """Normalize MCP arguments without accepting arbitrary serialized values."""

    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise AuthorizationDenied("access denied")
    try:
        if len(value.encode("utf-8")) > MAX_RAW_GATEWAY_BODY_BYTES:
            raise AuthorizationDenied("access denied")
        parsed = json.loads(value)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AuthorizationDenied("access denied") from exc
    if not isinstance(parsed, Mapping):
        raise AuthorizationDenied("access denied")
    return parsed


def _raw_matter_selector(raw_gateway_request: object) -> str:
    body = _raw_gateway_body(raw_gateway_request)
    params = body.get("params")
    arguments_value = params.get("arguments") if isinstance(params, Mapping) else None
    arguments = _normalize_arguments(arguments_value)
    matter_id = arguments.get("matterId")
    if not isinstance(matter_id, str) or not matter_id.strip():
        raise AuthorizationDenied("access denied")
    return matter_id


def _header_selector(headers: Mapping[object, object], name: str) -> object:
    """Read one selector header case-insensitively and reject ambiguity."""

    matches = [
        value
        for key, value in headers.items()
        if isinstance(key, str) and key.casefold() == name.casefold()
    ]
    if len(matches) > 1 and any(value != matches[0] for value in matches[1:]):
        raise AuthorizationDenied("access denied")
    return matches[0] if matches else None


def _header_correlation_id(headers: Mapping[object, object]) -> str | None:
    """Validate an optional application correlation header after auth inputs."""

    value = _header_selector(headers, "x-legaldesk-correlation-id")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuthorizationDenied("access denied")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise AuthorizationDenied("access denied") from exc


def _header_opaque_id(headers: Mapping[object, object], name: str) -> str | None:
    value = _header_selector(headers, name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise AuthorizationDenied("access denied")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise AuthorizationDenied("access denied") from exc


def _resolve_invocation_binding(
    headers: Mapping[object, object],
    *,
    subject: str,
    matter_id: str,
    grant_repository: GatewayGrantRepository | None,
) -> tuple[str | None, str | None, Mapping[str, object] | None]:
    """Resolve an application-issued scope before accepting its correlation.

    The legacy component path has no invocation ID and retains its generated
    correlation behavior. Application Harness calls carry an opaque record ID;
    only the record's subject/matter/memory scope/correlation are authoritative.
    """

    invocation_id = _header_opaque_id(headers, "x-legaldesk-invocation-id")
    header_correlation = _header_correlation_id(headers)
    if invocation_id is None:
        # A caller-controlled correlation/memory header is never a trusted
        # application scope. Legacy callers omit all of these headers and get
        # a newly generated correlation ID.
        if header_correlation is not None or any(
            _header_selector(headers, name) is not None
            for name in (
                "x-legaldesk-memory-actor-id",
                "x-legaldesk-memory-session-id",
            )
        ):
            raise AuthorizationDenied("access denied")
        return None, None, None
    if grant_repository is None:
        raise AuthorizationDenied("access denied")
    try:
        item = grant_repository.get_invocation(invocation_id)
    except Exception as exc:
        raise AuthorizationDenied("access denied") from exc
    if not isinstance(item, Mapping) or set(item) not in ({
        "pk", "sk", "entityType", "verifiedSubject", "requestedMatterId",
        "correlationId", "memoryActorId", "memorySessionId", "allowedTools",
        "expiresAt",
    }, {
        "pk", "sk", "entityType", "verifiedSubject", "requestedMatterId",
        "correlationId", "memoryActorId", "memorySessionId", "allowedTools",
        "expiresAt", "ttl",
    }) or any(
        (
            item.get("pk") != gateway_invocation_partition_key(invocation_id),
            item.get("sk") != GATEWAY_GRANT_SORT_KEY,
            item.get("entityType") != GATEWAY_INVOCATION_ENTITY,
        )
    ):
        raise AuthorizationDenied("access denied")
    if (
        item.get("verifiedSubject") != subject
        or item.get("requestedMatterId") != matter_id
        or not isinstance(item.get("correlationId"), str)
        or not isinstance(item.get("memoryActorId"), str)
        or not isinstance(item.get("memorySessionId"), str)
    ):
        raise AuthorizationDenied("access denied")
    expires_at = item.get("expiresAt")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float, Decimal)):
        raise AuthorizationDenied("access denied")
    if not math.isfinite(float(expires_at)) or float(expires_at) <= time.time():
        raise AuthorizationDenied("access denied")
    ttl = item.get("ttl", expires_at)
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float, Decimal)) or not math.isfinite(float(ttl)):
        raise AuthorizationDenied("access denied")
    try:
        correlation_id = str(UUID(item["correlationId"]))
    except (ValueError, AttributeError) as exc:
        raise AuthorizationDenied("access denied") from exc
    if header_correlation is not None and header_correlation != correlation_id:
        raise AuthorizationDenied("access denied")
    allowed_tools = item.get("allowedTools")
    if (
        not isinstance(allowed_tools, list)
        or not allowed_tools
        or any(tool not in _APPLICATION_ALLOWED_TOOL_NAMES for tool in allowed_tools)
    ):
        raise AuthorizationDenied("access denied")
    for header_name, item_name in (
        ("x-legaldesk-memory-actor-id", "memoryActorId"),
        ("x-legaldesk-memory-session-id", "memorySessionId"),
    ):
        value = _header_selector(headers, header_name)
        if value != item[item_name]:
            raise AuthorizationDenied("access denied")
    return invocation_id, correlation_id, item


def _normalize_tools_list_body(body: Mapping[str, object]) -> dict[str, object]:
    """Normalize only the null initial pagination cursor for MCP tools/list."""

    transformed = dict(body)
    if body.get("method") != "tools/list" or "params" not in body:
        return transformed
    params = body.get("params")
    if not isinstance(params, Mapping):
        raise AuthorizationDenied("access denied")
    param_names = set(params)
    if param_names - {"cursor", "_meta"}:
        raise AuthorizationDenied("access denied")
    if "_meta" in params and not isinstance(params.get("_meta"), Mapping):
        raise AuthorizationDenied("access denied")
    if "cursor" in params:
        cursor = params.get("cursor")
        if cursor is None:
            transformed["params"] = {
                key: value for key, value in params.items() if key != "cursor"
            }
        elif not isinstance(cursor, str):
            raise AuthorizationDenied("access denied")
    return transformed


_SAFE_DECISION_CODES = {"CROSS_MATTER", "ACCESS_DENIED", "SERVICE_UNAVAILABLE", "DENY", "ALLOW"}


def _safe_decision_metadata(
    *,
    decision: str,
    code: str,
    target_invoked: bool,
    tool_name: object = None,
) -> dict[str, object]:
    """Return bounded interceptor evidence; never include arbitrary errors/body text."""

    if decision not in {"ALLOW", "DENY"} or code not in _SAFE_DECISION_CODES:
        decision, code = "DENY", "ACCESS_DENIED"
    safe_tool = tool_name if isinstance(tool_name, str) and len(tool_name) <= 128 else None
    return {
        "authorizationDecision": decision,
        "authorizationCode": code,
        "targetInvoked": bool(target_invoked),
        "toolName": safe_tool,
        "toolCallId": None,
        "toolResult": "UNAVAILABLE",
        "guardrailDecision": "UNAVAILABLE",
        "stopReason": "UNAVAILABLE",
        "finalValidation": "UNAVAILABLE",
    }


def _safe_error(
    event: Mapping[str, object],
    message: str = "access denied",
    *,
    authorization_code: str = "ACCESS_DENIED",
) -> dict[str, object]:
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
    telemetry_sink: TelemetrySink | None = None,
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
    subject = _decode_verified_subject(_header_selector(headers, "authorization"))
    raw_gateway_request = mcp.get("rawGatewayRequest") if isinstance(mcp, Mapping) else None
    raw_matter_id = _raw_matter_selector(raw_gateway_request)
    params = body.get("params")
    arguments_value = params.get("arguments") if isinstance(params, Mapping) else None
    arguments = _normalize_arguments(arguments_value)
    body_matter_id = arguments.get("matterId")
    if body_matter_id is not None and not isinstance(body_matter_id, str):
        raise AuthorizationDenied("access denied")
    header_matter_id = _header_selector(headers, "x-legaldesk-requested-matter-id")
    if header_matter_id is not None and not isinstance(header_matter_id, str):
        raise AuthorizationDenied("access denied")
    # The raw body is the original caller-controlled selector. Any selector
    # that survived into the transformed body or headers must agree with it.
    if body_matter_id is not None and body_matter_id != raw_matter_id:
        raise AuthorizationDenied("access denied")
    if header_matter_id is not None and header_matter_id != raw_matter_id:
        raise AuthorizationDenied("access denied")
    matter_id = raw_matter_id
    # Application Harness calls must resolve a server-issued binding before
    # their correlation or Memory headers are accepted. Legacy component
    # calls retain generated correlation when no invocation ID is present.
    invocation_id, bound_correlation_id, invocation_record = _resolve_invocation_binding(
        headers,
        subject=subject,
        matter_id=matter_id,
        grant_repository=grant_repository,
    )
    correlation_id = bound_correlation_id or str(correlation_id_factory())
    try:
        correlation_id = str(UUID(correlation_id))
    except (ValueError, AttributeError) as exc:
        raise AuthorizationDenied("access denied") from exc
    context = build_request_context(
        _gateway_identity_from_verified_subject(subject),
        matter_id,
        authorization_store,
        correlation_id=correlation_id,
    )
    if not isinstance(params, Mapping):
        raise AuthorizationDenied("access denied")
    started_at = time.perf_counter()
    operation = _local_gateway_tool_name(params.get("name"))
    application_tool_name = (
        f"@legaldesk_gateway/{target.value}{GATEWAY_TOOL_DELIMITER}{operation}"
    )
    if invocation_record is not None and application_tool_name not in invocation_record["allowedTools"]:
        raise AuthorizationDenied("access denied")
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.AGENT,
        context.correlation_id,
        TelemetryOutcome.STARTED,
        operation="gateway_request",
    )
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.TOOL,
        context.correlation_id,
        TelemetryOutcome.STARTED,
        operation=operation,
    )

    def fail_after_start(error_code: str = "access_denied") -> None:
        """Close both started spans before denying a post-auth request."""

        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.AGENT,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="gateway_request",
            error_code=error_code,
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation=operation,
            error_code=error_code,
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.ERROR,
            context.correlation_id,
            TelemetryOutcome.ERROR,
            started_at=started_at,
            operation="gateway_request",
            error_code=error_code,
        )
        raise AuthorizationDenied("access denied")

    envelope = InterceptorEnvelope(subject, context.matter_id, context.correlation_id)
    transformed_body = dict(body)
    transformed_params = dict(params)
    # Preserve the Gateway-visible qualified name. AgentCore uses the
    # ``target___tool`` prefix to select the target after this interceptor
    # returns. Lambda targets expose that qualified name through provider
    # context, while aggregated MCP targets receive their local tool name from
    # Gateway. Rewriting it here would authorize the call and then make target
    # routing fail before the target is invoked.
    transformed_arguments = dict(arguments)
    transformed_params["arguments"] = transformed_arguments
    transformed_body["params"] = transformed_params
    if target is GatewayTarget.REVIEW_LAMBDA:
        if grant_repository is None:
            fail_after_start()
        # The Lambda target schema requires this selector for Gateway
        # validation. Reinsert only the already-compared raw selector; the
        # Lambda handler removes it before parsing and never uses it for scope.
        transformed_arguments["matterId"] = raw_matter_id
        # A user-facing review action is one idempotent capability for this
        # invocation. Repeated model/tool attempts reuse its invocation ID;
        # legacy component calls retain a fresh grant per request.
        grant_id = invocation_id or str(grant_id_factory())
        try:
            UUID(grant_id)
        except (ValueError, AttributeError) as exc:
            del exc
            fail_after_start()
        invocation_expiry = (
            invocation_record.get("expiresAt")
            if isinstance(invocation_record, Mapping)
            else None
        )
        expires_at = (
            int(invocation_expiry)
            if isinstance(invocation_expiry, (int, float, Decimal))
            and not isinstance(invocation_expiry, bool)
            else int(time.time()) + GATEWAY_GRANT_TTL_SECONDS
        )
        grant = GatewayAuthorizationGrant(
            grant_id=grant_id,
            verified_subject=envelope.verifiedSubject,
            requested_matter_id=envelope.requestedMatterId,
            correlation_id=envelope.correlationId,
            tool_name=operation,
            expires_at=expires_at,
        )
        try:
            grant_repository.put(grant)
        except Exception as exc:
            del exc
            fail_after_start()
        # Overwrite, never honor, a client/model supplied grant selector.
        transformed_arguments["_legaldeskGrantId"] = grant_id
    elif target is GatewayTarget.METADATA_MCP:
        grant_id: str | None = None
        if grant_repository is not None:
            grant_id = str(grant_id_factory())
            try:
                UUID(grant_id)
            except (ValueError, AttributeError) as exc:
                del exc
                fail_after_start()
            grant = GatewayAuthorizationGrant(
                grant_id=grant_id,
                verified_subject=envelope.verifiedSubject,
                requested_matter_id=envelope.requestedMatterId,
                correlation_id=envelope.correlationId,
                tool_name=_local_gateway_tool_name(params.get("name")),
                expires_at=int(time.time()) + GATEWAY_GRANT_TTL_SECONDS,
            )
            try:
                grant_repository.put(grant)
            except Exception as exc:
                del exc
                fail_after_start()
        # Scope is represented in the public Gateway schema but is only a
        # selector; the metadata target receives it through this short-lived
        # server-side capability. Legacy direct unit callers may still inspect
        # the trusted headers, but the AWS interceptor always supplies a grant.
        transformed_arguments.pop("matterId", None)
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
        transformed_headers.update({
            "x-legaldesk-verified-subject": envelope.verifiedSubject,
            "x-legaldesk-requested-matter-id": envelope.requestedMatterId,
            "x-legaldesk-correlation-id": envelope.correlationId,
        })
        if grant_id is not None:
            transformed_headers["x-legaldesk-grant-id"] = grant_id
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.AGENT,
            context.correlation_id,
            TelemetryOutcome.SUCCEEDED,
            started_at=started_at,
            operation="gateway_request",
        )
        emit_telemetry(
            telemetry_sink,
            TelemetryEventType.TOOL,
            context.correlation_id,
            TelemetryOutcome.SUCCEEDED,
            started_at=started_at,
            operation=operation,
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
        fail_after_start()
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.AGENT,
        context.correlation_id,
        TelemetryOutcome.SUCCEEDED,
        started_at=started_at,
        operation="gateway_request",
    )
    emit_telemetry(
        telemetry_sink,
        TelemetryEventType.TOOL,
        context.correlation_id,
        TelemetryOutcome.SUCCEEDED,
        started_at=started_at,
        operation=operation,
    )
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
    audit = {
        "method": "unknown",
        "toolRecognized": False,
        "authorizationPresent": False,
        "authorizationParsable": False,
        "matterSelectorPresent": False,
        "gatewayArgumentsMapping": False,
        "gatewayMatterPresent": False,
        "rawRequestMapping": False,
        "rawBodyKind": "missing",
        "rawJsonMapping": False,
        "rawArgumentsMapping": False,
        "rawMatterPresent": False,
        "authorizationDecision": "UNKNOWN",
        "authorizationCode": "UNAVAILABLE",
        "targetInvoked": False,
        "toolName": None,
        "selectorConsistent": False,
    }
    try:
        mcp = event.get("mcp")
        gateway_request = mcp.get("gatewayRequest") if isinstance(mcp, Mapping) else None
        body = gateway_request.get("body") if isinstance(gateway_request, Mapping) else None
        tool_name = body.get("params", {}).get("name") if isinstance(body, Mapping) else None
        method = body.get("method") if isinstance(body, Mapping) else None
        raw_gateway_request = (
            mcp.get("rawGatewayRequest") if isinstance(mcp, Mapping) else None
        )
        audit["rawRequestMapping"] = isinstance(raw_gateway_request, Mapping)
        raw_body = raw_gateway_request.get("body") if isinstance(raw_gateway_request, Mapping) else None
        audit["rawBodyKind"] = (
            "string" if isinstance(raw_body, str) else "mapping" if isinstance(raw_body, Mapping) else "other"
        )
        raw_parsed: object = raw_body
        if isinstance(raw_body, str) and len(raw_body.encode("utf-8")) <= MAX_RAW_GATEWAY_BODY_BYTES:
            try:
                raw_parsed = json.loads(raw_body)
            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                raw_parsed = None
        audit["rawJsonMapping"] = isinstance(raw_parsed, Mapping)
        raw_params = raw_parsed.get("params") if isinstance(raw_parsed, Mapping) else None
        raw_arguments = raw_params.get("arguments") if isinstance(raw_params, Mapping) else None
        normalized_raw_arguments: Mapping[str, object] | None = None
        audit["rawArgumentsMapping"] = isinstance(raw_arguments, Mapping)
        try:
            normalized_raw_arguments = _normalize_arguments(raw_arguments)
            audit["rawMatterPresent"] = isinstance(
                normalized_raw_arguments.get("matterId"), str
            )
        except AuthorizationDenied:
            pass
        gateway_params = body.get("params") if isinstance(body, Mapping) else None
        gateway_arguments = (
            gateway_params.get("arguments") if isinstance(gateway_params, Mapping) else None
        )
        normalized_gateway_arguments: Mapping[str, object] | None = None
        audit["gatewayArgumentsMapping"] = isinstance(gateway_arguments, Mapping)
        try:
            normalized_gateway_arguments = _normalize_arguments(gateway_arguments)
            audit["gatewayMatterPresent"] = isinstance(
                normalized_gateway_arguments.get("matterId"), str
            )
        except AuthorizationDenied:
            pass
        raw_selector = normalized_raw_arguments.get("matterId") if normalized_raw_arguments else None
        body_selector = normalized_gateway_arguments.get("matterId") if normalized_gateway_arguments else None
        header_selector = (
            _header_selector(gateway_request.get("headers"), "x-legaldesk-requested-matter-id")
            if isinstance(gateway_request, Mapping) and isinstance(gateway_request.get("headers"), Mapping)
            else None
        )
        # Gateway may sanitize the transformed body. Absence is therefore
        # consistent; only an explicit differing selector is inconsistent.
        audit["selectorConsistent"] = bool(
            isinstance(raw_selector, str)
            and (body_selector is None or body_selector == raw_selector)
            and (header_selector is None or header_selector == raw_selector)
        )
        try:
            _raw_matter_selector(raw_gateway_request)
            audit["matterSelectorPresent"] = True
        except AuthorizationDenied:
            pass
        audit["method"] = method if method in {"initialize", "tools/list", "tools/call", "ping", "notifications/initialized"} else "other"
        audit["toolName"] = tool_name if isinstance(tool_name, str) and len(tool_name) <= 128 else None
        if method in {"initialize", "tools/list", "ping", "notifications/initialized"}:
            # MCP capability discovery is not a business-scope operation. The
            # Function URL remains AWS_IAM-restricted; no caller selectors are
            # propagated during Gateway dynamic sync.
            lifecycle_body = _normalize_tools_list_body(body)
            return {
                "interceptorOutputVersion": "1.0",
                "mcp": {"transformedGatewayRequest": {"body": lifecycle_body}}
            }
        target = _target_for_gateway_tool(tool_name)
        audit["toolRecognized"] = True
        headers = gateway_request.get("headers") if isinstance(gateway_request, Mapping) else None
        authorization = (
            _header_selector(headers, "authorization")
            if isinstance(headers, Mapping)
            else None
        )
        audit["authorizationPresent"] = authorization is not None
        try:
            _decode_verified_subject(authorization)
            audit["authorizationParsable"] = True
        except AuthorizationDenied:
            pass
        store = _authorization_store_from_environment()
        if store is None:
            audit.update(_safe_decision_metadata(decision="DENY", code="SERVICE_UNAVAILABLE", target_invoked=False, tool_name=tool_name))
            _LOGGER.warning("gateway authorization unavailable %s", json.dumps(audit, sort_keys=True))
            return _safe_error(event, "service unavailable", authorization_code="SERVICE_UNAVAILABLE")
        grants = _grant_repository_from_environment()
        if grants is None:
            audit.update(_safe_decision_metadata(decision="DENY", code="SERVICE_UNAVAILABLE", target_invoked=False, tool_name=tool_name))
            _LOGGER.warning("gateway authorization unavailable %s", json.dumps(audit, sort_keys=True))
            return _safe_error(event, "service unavailable", authorization_code="SERVICE_UNAVAILABLE")
        response = transform_gateway_request(
            event,
            target=target,
            authorization_store=store,
            grant_repository=grants,
        )
        audit.update(_safe_decision_metadata(decision="ALLOW", code="ALLOW", target_invoked=False, tool_name=tool_name))
        _LOGGER.info("gateway authorization allowed %s", json.dumps(audit, sort_keys=True))
        return response
    except AuthorizationDenied:
        code = (
            "CROSS_MATTER"
            if audit["authorizationParsable"] and audit["matterSelectorPresent"] and audit["selectorConsistent"]
            else "ACCESS_DENIED"
        )
        audit.update(_safe_decision_metadata(decision="DENY", code=code, target_invoked=False, tool_name=tool_name))
        _LOGGER.warning("gateway authorization denied %s", json.dumps(audit, sort_keys=True))
        return _safe_error(event, authorization_code=code)
    except Exception:
        audit.update(_safe_decision_metadata(decision="DENY", code="ACCESS_DENIED", target_invoked=False, tool_name=tool_name))
        _LOGGER.exception("gateway authorization failed %s", json.dumps(audit, sort_keys=True))
        return _safe_error(event)


__all__ = [
    "GatewayTarget",
    "GatewayAuthorizationGrant",
    "HarnessInvocationGrant",
    "GatewayGrantRepository",
    "Boto3DynamoGatewayGrantRepository",
    "InMemoryGatewayGrantRepository",
    "gateway_grant_partition_key",
    "gateway_invocation_partition_key",
    "GATEWAY_GRANT_ENTITY",
    "GATEWAY_GRANT_SORT_KEY",
    "GATEWAY_GRANT_TTL_SECONDS",
    "GATEWAY_INVOCATION_ENTITY",
    "GATEWAY_INVOCATION_TTL_SECONDS",
    "GATEWAY_TOOL_DELIMITER",
    "InterceptorEnvelope",
    "gateway_request_interceptor",
    "transform_gateway_request",
]
