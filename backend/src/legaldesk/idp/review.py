"""IDP review, immutable human decisions, and selected-document lookup.

This module is deliberately separate from the human review tool.  A worker
may request review creation only with a short-lived, purpose-bound Gateway
capability containing durable references.  It cannot provide field values,
evidence, tenant identity, or a human identity.  Human decisions enter
through the existing authorized ``RequestContext`` path and are append-only.
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Callable, Mapping, Protocol
from uuid import UUID, uuid4

from ..authorization import RequestContext, require_authorized_context
from ..domain.models import ReviewTask, ReviewTaskStatus
from .models import (
    EvidenceAnchor,
    FieldAcceptance,
    IDPExtractionRun,
    IDPFieldResult,
)


IDP_REVIEW_CREATE_TOOL = "create_review_task"
IDP_REVIEW_PURPOSE = "idp-review-create"
IDP_REVIEW_SCOPE = "legaldesk-idp/review-create"
MAX_DECISION_REASON = 2_000
MAX_DECISION_VALUE_BYTES = 16_000
MAX_REVIEW_FIELDS = 64


class IDPReviewError(ValueError):
    """Expected, fail-closed review boundary error."""


class IDPDecisionAction(StrEnum):
    APPROVE = "APPROVE"
    CORRECT = "CORRECT"
    REJECT = "REJECT"


def _nonempty(value: object, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IDPReviewError(f"{name} is invalid")
    return value


def _sha(value: object, name: str) -> str:
    value = _nonempty(value, name, 64)
    if len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
        raise IDPReviewError(f"{name} is invalid")
    return value.lower()


def _json_size(value: object, name: str) -> None:
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise IDPReviewError(f"{name} is invalid") from exc
    if size > MAX_DECISION_VALUE_BYTES:
        raise IDPReviewError(f"{name} is too large")


@dataclass(frozen=True, slots=True)
class IDPReviewInvocation:
    """Server-created references carried by the machine Gateway capability."""

    tenant_id: str
    matter_id: str
    document_id: str
    run_id: str
    document_sha256: str
    correlation_id: str
    machine_client_id: str
    scope: str = IDP_REVIEW_SCOPE
    purpose: str = IDP_REVIEW_PURPOSE
    field_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("tenant_id", "matter_id", "document_id", "run_id", "machine_client_id", "correlation_id"):
            _nonempty(getattr(self, name), name)
        _sha(self.document_sha256, "document_sha256")
        try:
            UUID(self.correlation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise IDPReviewError("correlation_id is invalid") from exc
        if self.scope != IDP_REVIEW_SCOPE or self.purpose != IDP_REVIEW_PURPOSE:
            raise IDPReviewError("machine review capability purpose is invalid")
        if len(self.field_names) > MAX_REVIEW_FIELDS or any(
            not isinstance(item, str) or not item.strip() or len(item) > 128 for item in self.field_names
        ):
            raise IDPReviewError("field_names are invalid")
        if len(set(self.field_names)) != len(self.field_names):
            raise IDPReviewError("field_names contain duplicates")

    @property
    def idempotency_key(self) -> str:
        raw = "\x00".join((self.tenant_id, self.matter_id, self.document_id, self.run_id, *self.field_names))
        return "idp-review-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


@dataclass(frozen=True, slots=True)
class IDPFieldDecision:
    decision_id: str
    tenant_id: str
    matter_id: str
    document_id: str
    run_id: str
    document_sha256: str
    schema_version: str
    field_name: str
    action: IDPDecisionAction
    reviewer_user_id: str
    correlation_id: str
    previous: Mapping[str, object]
    proposed: Mapping[str, object] | None
    result: Mapping[str, object] | None
    evidence: tuple[EvidenceAnchor, ...]
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        for name in ("decision_id", "tenant_id", "matter_id", "document_id", "run_id", "schema_version", "field_name", "reviewer_user_id", "reason"):
            _nonempty(getattr(self, name), name, 256 if name != "reason" else MAX_DECISION_REASON)
        _sha(self.document_sha256, "document_sha256")
        if not isinstance(self.action, IDPDecisionAction):
            raise IDPReviewError("action is invalid")
        try:
            UUID(self.correlation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise IDPReviewError("correlation_id is invalid") from exc
        if self.created_at.tzinfo is None:
            raise IDPReviewError("created_at is invalid")
        if any(not isinstance(item, EvidenceAnchor) for item in self.evidence):
            raise IDPReviewError("evidence is invalid")
        _json_size(dict(self.previous), "previous")
        if self.proposed is not None:
            _json_size(dict(self.proposed), "proposed")
        if self.result is not None:
            _json_size(dict(self.result), "result")


class IDPDecisionRepository(Protocol):
    def save_decision(self, decision: IDPFieldDecision) -> IDPFieldDecision: ...

    def list_decisions(self, *, tenant_id: str, matter_id: str, document_id: str, field_name: str, limit: int = 100) -> tuple[IDPFieldDecision, ...]: ...


class InMemoryIDPDecisionRepository:
    """Append-only test adapter with the same scope checks as production."""

    def __init__(self) -> None:
        self.decisions: dict[tuple[str, str, str], IDPFieldDecision] = {}

    def save_decision(self, decision: IDPFieldDecision) -> IDPFieldDecision:
        key = (decision.tenant_id, decision.matter_id, decision.decision_id)
        existing = self.decisions.get(key)
        if existing is not None and existing != decision:
            raise IDPReviewError("decision is immutable")
        self.decisions[key] = decision
        return decision

    def list_decisions(self, *, tenant_id: str, matter_id: str, document_id: str, field_name: str, limit: int = 100) -> tuple[IDPFieldDecision, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise IDPReviewError("decision limit is invalid")
        values = [item for item in self.decisions.values() if item.tenant_id == tenant_id and item.matter_id == matter_id and item.document_id == document_id and item.field_name == field_name]
        values.sort(key=lambda item: (item.created_at, item.decision_id), reverse=True)
        return tuple(values[:limit])


class Boto3DynamoIDPDecisionRepository:
    """Append-only decision records in the existing metadata table."""

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise IDPReviewError("decision table is invalid")
        if table is None:
            import boto3
            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    @staticmethod
    def _key(decision: IDPFieldDecision) -> dict[str, str]:
        return {"pk": f"TENANT#{decision.tenant_id}#MATTER#{decision.matter_id}", "sk": f"IDP#DECISION#{decision.document_id}#{decision.field_name}#{decision.decision_id}"}

    @staticmethod
    def _item(decision: IDPFieldDecision) -> dict[str, object]:
        from .persistence import _dynamo_safe
        item = {
            **Boto3DynamoIDPDecisionRepository._key(decision), "entityType": "IDPFieldDecision", "decisionId": decision.decision_id,
            "tenantId": decision.tenant_id, "matterId": decision.matter_id, "documentId": decision.document_id,
            "runId": decision.run_id, "documentSha256": decision.document_sha256, "schemaVersion": decision.schema_version,
            "fieldName": decision.field_name, "action": decision.action.value, "reviewerUserId": decision.reviewer_user_id,
            "correlationId": decision.correlation_id, "previous": _dynamo_safe(dict(decision.previous)),
            "proposed": _dynamo_safe(dict(decision.proposed)) if decision.proposed is not None else None,
            "result": _dynamo_safe(dict(decision.result)) if decision.result is not None else None,
            "evidence": [{"page": anchor.page, "quote": anchor.quote, "contentSha256": anchor.content_sha256, "start": anchor.start, "end": anchor.end} for anchor in decision.evidence],
            "reason": decision.reason, "createdAt": decision.created_at.isoformat(),
        }
        return {key: value for key, value in item.items() if value is not None}

    def save_decision(self, decision: IDPFieldDecision) -> IDPFieldDecision:
        item = self._item(decision)
        try:
            self.table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)")
        except Exception:
            response = self.table.get_item(Key=self._key(decision), ConsistentRead=True)
            if not isinstance(response, Mapping) or response.get("Item") != item:
                raise IDPReviewError("decision is immutable") from None
        return decision

    def list_decisions(self, *, tenant_id: str, matter_id: str, document_id: str, field_name: str, limit: int = 100) -> tuple[IDPFieldDecision, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise IDPReviewError("decision limit is invalid")
        from boto3.dynamodb.conditions import Key
        response = self.table.query(KeyConditionExpression=Key("pk").eq(f"TENANT#{tenant_id}#MATTER#{matter_id}") & Key("sk").begins_with(f"IDP#DECISION#{document_id}#{field_name}#"), Limit=limit, ConsistentRead=True)
        values: list[IDPFieldDecision] = []
        for raw in response.get("Items", ()):
            if not isinstance(raw, Mapping):
                continue
            try:
                evidence = tuple(EvidenceAnchor(page=item["page"], quote=item["quote"], content_sha256=item["contentSha256"], start=item.get("start"), end=item.get("end")) for item in raw.get("evidence", ()))
                values.append(IDPFieldDecision(decision_id=raw["decisionId"], tenant_id=raw["tenantId"], matter_id=raw["matterId"], document_id=raw["documentId"], run_id=raw["runId"], document_sha256=raw["documentSha256"], schema_version=raw["schemaVersion"], field_name=raw["fieldName"], action=IDPDecisionAction(raw["action"]), reviewer_user_id=raw["reviewerUserId"], correlation_id=raw["correlationId"], previous=raw.get("previous", {}), proposed=raw.get("proposed"), result=raw.get("result"), evidence=evidence, reason=raw["reason"], created_at=datetime.fromisoformat(raw["createdAt"])))
            except (KeyError, TypeError, ValueError) as exc:
                raise IDPReviewError("decision record is malformed") from exc
        values.sort(key=lambda item: (item.created_at, item.decision_id), reverse=True)
        return tuple(values[:limit])


class IDPReviewGateway(Protocol):
    """Gateway client boundary; implementations must use M2M, never a user JWT."""

    def create_review_task(self, invocation: IDPReviewInvocation) -> Mapping[str, object]: ...


class IDPMachineTokenProvider(Protocol):
    def token(self) -> str: ...


class CognitoM2MTokenProvider:
    """One-attempt Cognito client-credentials provider using SecureString SSM."""

    def __init__(self, *, ssm_client: Any, parameter_name: str, token_endpoint: str, client_id: str, scope: str, timeout_seconds: float = 15.0) -> None:
        from ..identity import validate_https_endpoint
        if not all(isinstance(value, str) and value.strip() for value in (parameter_name, client_id, scope)):
            raise IDPReviewError("M2M token configuration is invalid")
        self.ssm, self.parameter_name, self.endpoint, self.client_id, self.scope = ssm_client, parameter_name, validate_https_endpoint(token_endpoint, field_name="token_endpoint"), client_id, scope
        self.timeout = timeout_seconds
        self._cached_token: str | None = None
        self._cached_until = 0.0

    def token(self) -> str:
        now = time.time()
        if self._cached_token is not None and now < self._cached_until:
            return self._cached_token
        response = self.ssm.get_parameter(Name=self.parameter_name, WithDecryption=True)
        raw = response.get("Parameter", {}).get("Value") if isinstance(response, Mapping) else None
        if not isinstance(raw, str) or not raw.strip():
            raise IDPReviewError("M2M secret is unavailable")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"client_secret": raw}
        secret = parsed.get("client_secret") if isinstance(parsed, Mapping) else None
        if not isinstance(secret, str) or not secret:
            raise IDPReviewError("M2M secret is invalid")
        body = urlencode({"grant_type": "client_credentials", "client_id": self.client_id, "client_secret": secret, "scope": self.scope}).encode()
        request = Request(self.endpoint, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, method="POST")
        class _NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
                raise IDPReviewError("token endpoint redirect denied")
        with build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
            raw_response = response.read(64 * 1024 + 1)
            if not isinstance(raw_response, bytes) or len(raw_response) > 64 * 1024:
                raise IDPReviewError("M2M token response is too large")
            try:
                payload = json.loads(raw_response.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise IDPReviewError("M2M token response is invalid") from exc
        token = payload.get("access_token") if isinstance(payload, Mapping) else None
        if not isinstance(token, str) or not token or len(token) > 16384:
            raise IDPReviewError("M2M token response is invalid")
        expires_in = payload.get("expires_in") if isinstance(payload, Mapping) else None
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)) or not 30 <= expires_in <= 3600:
            raise IDPReviewError("M2M token expiry is invalid")
        self._cached_token = token
        self._cached_until = time.time() + max(1.0, float(expires_in) - 15.0)
        return token


class IDPMachineGatewayClient:
    """Fixed-url, no-redirect/no-retry HTTP Gateway transport."""

    def __init__(self, *, gateway_url: str, token_provider: IDPMachineTokenProvider, transport: Any | None = None, timeout_seconds: float = 15.0) -> None:
        from ..identity import validate_https_endpoint
        from ..gateway_client import UrllibGatewayTransport
        self.url = validate_https_endpoint(gateway_url, field_name="gateway_url")
        self.tokens, self.transport, self.timeout = token_provider, transport or UrllibGatewayTransport(), timeout_seconds

    def dispatch(self, record: Any) -> Mapping[str, object]:
        from ..gateway_client import GatewayHttpResponse, MCP_PROTOCOL_VERSION, _json_response, _payload_from_result
        invocation_id = getattr(record, "invocation_id", None)
        matter_id = getattr(record, "requested_matter_id", None)
        if not isinstance(invocation_id, str) or not isinstance(matter_id, str):
            raise IDPReviewError("machine invocation is invalid")
        request_id = str(uuid4())
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": "review-task-lambda___create_review_task", "arguments": {"matterId": matter_id, "invocationId": invocation_id}}}, separators=(",", ":")).encode()
        response = self.transport.post(self.url, body, {"Authorization": f"Bearer {self.tokens.token()}", "Content-Type": "application/json", "Accept": "application/json", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION}, self.timeout)
        if not isinstance(response, GatewayHttpResponse) or response.status < 200 or response.status >= 300:
            raise IDPReviewError("machine Gateway dispatch failed")
        if not isinstance(response.body, bytes) or len(response.body) > 64 * 1024:
            raise IDPReviewError("machine Gateway response is too large")
        try:
            payload = _json_response(response)
            result = _payload_from_result(payload.get("result"))
        except Exception as exc:
            raise IDPReviewError("machine Gateway response is invalid") from exc
        if not isinstance(payload, Mapping) or payload.get("jsonrpc") != "2.0" or payload.get("id") != request_id or payload.get("error") is not None:
            raise IDPReviewError("machine Gateway response is invalid")
        return result


def dispatch_review_after_persist(*, run: IDPExtractionRun, job: Any, invocation_repository: Any, gateway: IDPMachineGatewayClient, machine_client_id: str, scope: str = IDP_REVIEW_SCOPE, ttl_seconds: int = 300, job_repository: Any | None = None) -> Mapping[str, object] | None:
    """Persist the invocation before one idempotent Gateway dispatch."""
    review_fields = tuple(sorted(name for name, field_result in run.fields.items() if field_result.acceptance is FieldAcceptance.REVIEW_REQUIRED))
    if not review_fields:
        return None
    from ..gateway_interceptor import IDPReviewInvocationRecord
    invocation_id = str(uuid4())
    record = IDPReviewInvocationRecord(invocation_id, machine_client_id, scope, IDP_REVIEW_PURPOSE, IDP_REVIEW_CREATE_TOOL, run.matter_id, run.document_id, run.run_id, run.document_sha256, job.correlation_id, review_fields, int(time.time()) + ttl_seconds, "PENDING")
    invocation_repository.put_idp_review_invocation(record)
    mark_delivery = getattr(job_repository, "record_review_delivery", None)
    if callable(mark_delivery):
        mark_delivery(job_id=job.job_id, state="PENDING", invocation_id=record.invocation_id)
    in_flight = IDPReviewInvocationRecord(record.invocation_id, record.machine_client_id, record.scope, record.purpose, record.tool_name, record.requested_matter_id, record.document_id, record.run_id, record.document_sha256, record.correlation_id, record.field_names, record.expires_at, "IN_FLIGHT")
    invocation_repository.update_idp_review_invocation(in_flight)
    if callable(mark_delivery):
        mark_delivery(job_id=job.job_id, state="IN_FLIGHT", invocation_id=record.invocation_id)
    try:
        result = gateway.dispatch(in_flight)
    except Exception:
        failed = IDPReviewInvocationRecord(record.invocation_id, record.machine_client_id, record.scope, record.purpose, record.tool_name, record.requested_matter_id, record.document_id, record.run_id, record.document_sha256, record.correlation_id, record.field_names, record.expires_at, "AMBIGUOUS")
        invocation_repository.update_idp_review_invocation(failed)
        if callable(mark_delivery):
            mark_delivery(job_id=job.job_id, state="AMBIGUOUS", invocation_id=record.invocation_id)
        raise
    sent = IDPReviewInvocationRecord(record.invocation_id, record.machine_client_id, record.scope, record.purpose, record.tool_name, record.requested_matter_id, record.document_id, record.run_id, record.document_sha256, record.correlation_id, record.field_names, record.expires_at, "SENT")
    invocation_repository.update_idp_review_invocation(sent)
    if callable(mark_delivery):
        task_id = result.get("reviewTaskId") if isinstance(result, Mapping) and isinstance(result.get("reviewTaskId"), str) else None
        mark_delivery(job_id=job.job_id, state="SENT", invocation_id=record.invocation_id, review_task_id=task_id)
    return result


def create_machine_review_task(*, invocation: IDPReviewInvocation, run: IDPExtractionRun, review_repository: Any, service_actor: str) -> Mapping[str, object]:
    """Create an additive review task from durable run references only.

    ``review_repository.save`` is the existing persistence primitive.  No
    human context is manufactured, and the task has no accepted-answer
    snapshot; the review target hydrates proposed fields by run reference.
    """
    _nonempty(service_actor, "service_actor")
    if (run.tenant_id, run.matter_id, run.document_id, run.run_id, run.document_sha256) != (invocation.tenant_id, invocation.matter_id, invocation.document_id, invocation.run_id, invocation.document_sha256):
        raise IDPReviewError("machine review scope does not match durable run")
    fields = invocation.field_names or tuple(sorted(name for name, item in run.fields.items() if item.acceptance is FieldAcceptance.REVIEW_REQUIRED))
    if not fields or any(name not in run.fields or run.fields[name].acceptance is not FieldAcceptance.REVIEW_REQUIRED for name in fields):
        raise IDPReviewError("machine review fields are invalid")
    task_id = "review-idp-" + hashlib.sha256(invocation.idempotency_key.encode("utf-8")).hexdigest()[:32]
    task = ReviewTask(
        review_task_id=task_id, tenant_id=run.tenant_id, matter_id=run.matter_id,
        created_by_user_id=service_actor, reason="insufficient_evidence",
        status=ReviewTaskStatus.OPEN, correlation_id=invocation.correlation_id,
        note=f"IDP run {run.run_id}; fields: {','.join(fields)}",
        source="IDP", idp_run_id=run.run_id, idp_document_id=run.document_id,
        idp_document_sha256=run.document_sha256, idp_field_names=tuple(fields),
        created_at=run.created_at, updated_at=run.created_at,
    )
    service_save = getattr(review_repository, "save_machine_task", None)
    if callable(service_save):
        service_save(task)
    else:
        review_repository.save(task)
    return {
        "reviewTaskId": task_id,
        "status": ReviewTaskStatus.OPEN.value,
        "createdAt": run.created_at.isoformat(),
        "runId": run.run_id,
        "fieldNames": list(fields),
        "correlationId": invocation.correlation_id,
    }


@dataclass(frozen=True, slots=True)
class IDPReviewService:
    decisions: IDPDecisionRepository

    def create_review_task(self, *, invocation: IDPReviewInvocation, run: IDPExtractionRun, gateway: IDPReviewGateway) -> Mapping[str, object]:
        if (run.tenant_id, run.matter_id, run.document_id, run.run_id, run.document_sha256) != (invocation.tenant_id, invocation.matter_id, invocation.document_id, invocation.run_id, invocation.document_sha256):
            raise IDPReviewError("review invocation does not match durable run")
        fields = tuple(invocation.field_names) or tuple(sorted(name for name, value in run.fields.items() if value.acceptance is FieldAcceptance.REVIEW_REQUIRED))
        if not fields or len(fields) > MAX_REVIEW_FIELDS or any(name not in run.fields for name in fields):
            raise IDPReviewError("review fields are not present in durable run")
        if any(run.fields[name].acceptance is not FieldAcceptance.REVIEW_REQUIRED for name in fields):
            raise IDPReviewError("review invocation includes a non-review field")
        # Only references leave this boundary.  The Gateway target loads the
        # immutable run and proposed values itself.
        return gateway.create_review_task(IDPReviewInvocation(
            tenant_id=invocation.tenant_id, matter_id=invocation.matter_id,
            document_id=invocation.document_id, run_id=invocation.run_id,
            document_sha256=invocation.document_sha256, correlation_id=invocation.correlation_id,
            machine_client_id=invocation.machine_client_id, field_names=fields,
        ))

    def record_human_decision(self, *, context: RequestContext, run: IDPExtractionRun, field_name: str, action: IDPDecisionAction, proposed: Mapping[str, object] | None, result: Mapping[str, object] | None, evidence: tuple[EvidenceAnchor, ...], reason: str) -> IDPFieldDecision:
        context = require_authorized_context(context)
        if (run.tenant_id, run.matter_id) != (context.tenant_id, context.matter_id):
            raise IDPReviewError("decision scope is invalid")
        field = run.fields.get(field_name)
        if field is None:
            raise IDPReviewError("field is not present in durable run")
        if action is IDPDecisionAction.CORRECT and proposed is None:
            raise IDPReviewError("correction requires a proposed value")
        if action is not IDPDecisionAction.CORRECT and proposed is not None:
            raise IDPReviewError("only corrections accept a proposed value")
        result_value = result if result is not None else {"value": field.value, "presence": field.presence.value, "origin": field.origin.value}
        decision = IDPFieldDecision(
            decision_id=str(uuid4()), tenant_id=run.tenant_id, matter_id=run.matter_id,
            document_id=run.document_id, run_id=run.run_id, document_sha256=run.document_sha256,
            schema_version=run.schema_version, field_name=field_name, action=action,
            reviewer_user_id=context.user_id, correlation_id=context.correlation_id,
            previous={"value": field.value, "presence": field.presence.value, "acceptance": field.acceptance.value},
            proposed=proposed, result=result_value, evidence=evidence, reason=reason,
        )
        return self.decisions.save_decision(decision)

    def effective_field(self, *, run: IDPExtractionRun, field_name: str) -> Mapping[str, object] | None:
        field = run.fields.get(field_name)
        if field is None:
            return None
        decisions = self.decisions.list_decisions(tenant_id=run.tenant_id, matter_id=run.matter_id, document_id=run.document_id, field_name=field_name)
        for decision in decisions:
            if decision.document_sha256 != run.document_sha256 or decision.schema_version != run.schema_version:
                continue
            if decision.action is IDPDecisionAction.REJECT:
                return {"field": field_name, "presence": "REJECTED", "acceptance": FieldAcceptance.REJECTED.value, "decisionId": decision.decision_id}
            value = decision.proposed if decision.action is IDPDecisionAction.CORRECT else decision.result
            if value is not None:
                return {"field": field_name, **dict(value), "acceptance": FieldAcceptance.HUMAN_CONFIRMED.value, "decisionId": decision.decision_id}
        return {"field": field_name, "value": field.value, "presence": field.presence.value, "origin": field.origin.value, "acceptance": field.acceptance.value, "evidence": tuple(field.evidence)}


@dataclass(frozen=True, slots=True)
class IDPQueryResult:
    status: str
    source: str
    field: str
    value: Mapping[str, object] | None = None
    fallback: object | None = None


def query_selected_document(*, context: RequestContext, document_id: str, field_name: str, repository: Any, review_service: IDPReviewService, rag_fallback: Callable[[RequestContext, str, str], object] | None = None) -> IDPQueryResult:
    """Return IDP-first data; fallback is additive and never writes IDP state."""

    context = require_authorized_context(context)
    if not isinstance(document_id, str) or not document_id.strip() or not isinstance(field_name, str) or not field_name.strip():
        raise IDPReviewError("document or field selector is invalid")
    runs = repository.list_runs(tenant_id=context.tenant_id, matter_id=context.matter_id, document_id=document_id, limit=20)
    for run in runs:
        value = review_service.effective_field(run=run, field_name=field_name)
        if value is not None and value.get("acceptance") not in {FieldAcceptance.UNAVAILABLE.value, FieldAcceptance.REJECTED.value}:
            return IDPQueryResult(status="AVAILABLE", source="IDP", field=field_name, value=value)
    if rag_fallback is not None:
        return IDPQueryResult(status="FALLBACK", source="RAG", field=field_name, fallback=rag_fallback(context, document_id, field_name))
    return IDPQueryResult(status="INSUFFICIENT_EVIDENCE", source="NONE", field=field_name)


__all__ = [name for name in globals() if not name.startswith("_")]
