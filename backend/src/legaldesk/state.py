"""Durable application state for the public-beta HTTP boundary.

The Phase 13 loopback app used process-local dictionaries for state that must
survive a restart or a second application instance in a public deployment.
This module keeps the small in-memory implementation for local tests and adds
an explicit DynamoDB adapter.  The adapter uses only point reads and bounded
queries; it never scans the shared table.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .authorization import VerifiedIdentity


@dataclass(frozen=True, slots=True)
class SessionRecord:
    identity: VerifiedIdentity
    access_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    expires_at: float


@dataclass(frozen=True, slots=True)
class CitationHandle:
    handle: str
    subject: str
    tenant_id: str
    matter_id: str
    conversation_id: str
    document_id: str | None
    passage: str
    expires_at: float


ConversationRecord = tuple[str, str, str, str]
ReviewCandidateRecord = tuple[float, dict[str, object]]
_CONVERSATION_TTL_SECONDS = 7 * 24 * 60 * 60
_SAFE_AUDIT_FIELDS = frozenset({
    "subject", "tenantId", "matterId", "correlationId", "operation", "timestampMs",
    "event_type", "outcome", "latency_ms", "count", "error_code",
    "correlation_id", "timestamp_ms", "prompt_version", "prompt_sha256",
    "resolver_prompt_version", "resolver_prompt_sha256",
    "writer_prompt_version", "writer_prompt_sha256",
})


def ingestion_document_set_key(*, subject: str, tenant_id: str, matter_id: str, document_ids: tuple[str, ...]) -> str:
    """Derive the canonical active-ingestion binding from server scope."""

    return _digest("INGESTION_DOCUMENT_SET", subject, tenant_id, matter_id, tuple(sorted(document_ids)))


@dataclass(frozen=True, slots=True)
class IngestionOperationRecord:
    """Server-owned binding between one public operation and one KB job."""

    operation_id: str
    idempotency_key: str
    subject: str
    tenant_id: str
    matter_id: str
    document_ids: tuple[str, ...]
    correlation_id: str
    ingestion_job_id: str
    status: str
    provider_status: str
    created_at: float
    updated_at: float
    expires_at: float
    document_set_key: str = ""
    documents_updated: int = 0
    failed_document_count: int | None = None

    def __post_init__(self) -> None:
        if not self.document_set_key:
            object.__setattr__(
                self,
                "document_set_key",
                ingestion_document_set_key(
                    subject=self.subject,
                    tenant_id=self.tenant_id,
                    matter_id=self.matter_id,
                    document_ids=self.document_ids,
                ),
            )


class EphemeralStateStore(Protocol):
    """Application state contract; implementations own persistence and expiry."""

    def bound_state(self) -> None: ...

    def put_session(self, key: str, record: SessionRecord) -> None: ...

    def get_session(self, key: str) -> SessionRecord | None: ...

    def delete_session(self, key: str) -> None: ...

    def put_oauth_state(self, state: str, code_verifier: str, expires_at: float) -> None: ...

    def consume_oauth_state(self, state: str) -> tuple[str, float] | None: ...

    def put_citation(self, handle: CitationHandle, *, citation_key: tuple[str, str, str, str]) -> None: ...

    def get_citation(
        self,
        handle: str,
        *,
        subject: str | None = None,
        tenant_id: str | None = None,
        matter_id: str | None = None,
        conversation_id: str | None = None,
    ) -> CitationHandle | None: ...

    def get_citation_link(self, citation_key: tuple[str, str, str, str]) -> str | None: ...

    def bind_conversation(self, conversation_id: str, record: ConversationRecord, correlation_id: str) -> None: ...

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None: ...

    def set_conversation_correlation(self, conversation_id: str, correlation_id: str) -> None: ...

    def get_conversation_correlation(self, conversation_id: str) -> str | None: ...

    def add_history_event(self, key: tuple[str, str, str], event_id: str) -> None: ...

    def list_history_ids(self, key: tuple[str, str, str]) -> tuple[str, ...]: ...

    def put_review_candidate(self, key: tuple[str, str, str], record: ReviewCandidateRecord) -> None: ...

    def get_review_candidate(self, key: tuple[str, str, str]) -> ReviewCandidateRecord | None: ...

    def delete_review_candidate(self, key: tuple[str, str, str]) -> None: ...

    def put_ingestion_operation(self, record: IngestionOperationRecord) -> None: ...

    def get_ingestion_operation(self, operation_id: str) -> IngestionOperationRecord | None: ...

    def get_ingestion_operation_for_idempotency(
        self, *, subject: str, tenant_id: str, matter_id: str, idempotency_key: str
    ) -> IngestionOperationRecord | None: ...

    def get_ingestion_operation_for_document_set(self, document_set_key: str) -> IngestionOperationRecord | None: ...

    def list_ingestion_operations_for_scope(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> tuple[IngestionOperationRecord, ...]: ...

    def update_ingestion_operation(self, record: IngestionOperationRecord) -> None: ...

    def delete_ingestion_operation(self, record: IngestionOperationRecord) -> None: ...

    def append_audit(self, record: Mapping[str, object]) -> None: ...

    def list_audit(self, subject: str, *, limit: int = 1_000) -> tuple[dict[str, object], ...]: ...

    def delete_scope_state(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> int: ...


def _digest(*parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _now() -> float:
    return time.time()


class InMemoryEphemeralStateStore:
    """Bounded local state store retained for offline tests and demos."""

    def __init__(self, *, clock: Any = _now) -> None:
        self.clock = clock
        # These attributes intentionally remain available for compatibility
        # with the existing local test fixtures.  They belong to this store,
        # not to LoopbackLegalDeskApp.
        self.sessions: dict[str, SessionRecord] = {}
        self.oauth_states: dict[str, tuple[str, float]] = {}
        self.citation_handles: dict[str, CitationHandle] = {}
        self.citation_links: dict[tuple[str, str, str, str], str] = {}
        self.conversation_selectors: dict[str, ConversationRecord] = {}
        self.conversation_correlations: dict[str, str] = {}
        self.accepted_history_ids: dict[tuple[str, str, str], list[str]] = {}
        self.accepted_review_candidates: dict[tuple[str, str, str], ReviewCandidateRecord] = {}
        self.ingestion_operations: dict[str, IngestionOperationRecord] = {}
        self.ingestion_idempotency: dict[tuple[str, str, str, str], str] = {}
        self.active_ingestion_document_sets: dict[str, str] = {}
        self.ingestion_scope_index: dict[tuple[str, str, str], set[str]] = {}
        self.audit_records: list[dict[str, object]] = []

    def bound_state(self) -> None:
        now = self.clock()
        self.oauth_states = {
            key: value for key, value in self.oauth_states.items() if value[1] > now
        }
        self.sessions = {
            key: value for key, value in self.sessions.items() if value.expires_at > now
        }
        self.citation_handles = {
            key: value for key, value in self.citation_handles.items() if value.expires_at > now
        }
        self.citation_links = {
            key: value for key, value in self.citation_links.items() if value in self.citation_handles
        }
        self.accepted_review_candidates = {
            key: value for key, value in self.accepted_review_candidates.items() if value[0] > now
        }
        self.ingestion_operations = {
            key: value for key, value in self.ingestion_operations.items() if value.expires_at > now
        }
        self.ingestion_idempotency = {
            key: operation_id
            for key, operation_id in self.ingestion_idempotency.items()
            if operation_id in self.ingestion_operations
        }
        self.active_ingestion_document_sets = {
            key: operation_id
            for key, operation_id in self.active_ingestion_document_sets.items()
            if operation_id in self.ingestion_operations
        }
        self.ingestion_scope_index = {
            scope: {operation_id for operation_id in operation_ids if operation_id in self.ingestion_operations}
            for scope, operation_ids in self.ingestion_scope_index.items()
            if any(operation_id in self.ingestion_operations for operation_id in operation_ids)
        }
        if len(self.oauth_states) >= 256:
            raise RuntimeError("too many pending login attempts")
        if len(self.sessions) >= 256:
            raise RuntimeError("too many sessions")
        if len(self.citation_handles) >= 1_024:
            raise RuntimeError("too many citation handles")
        if len(self.conversation_selectors) >= 256:
            raise RuntimeError("too many conversations")

    def put_session(self, key: str, record: SessionRecord) -> None:
        self.sessions[key] = record

    def get_session(self, key: str) -> SessionRecord | None:
        record = self.sessions.get(key)
        if record is None or record.expires_at <= self.clock():
            self.sessions.pop(key, None)
            return None
        return record

    def delete_session(self, key: str) -> None:
        self.sessions.pop(key, None)

    def put_oauth_state(self, state: str, code_verifier: str, expires_at: float) -> None:
        self.oauth_states[state] = (code_verifier, expires_at)

    def consume_oauth_state(self, state: str) -> tuple[str, float] | None:
        record = self.oauth_states.pop(state, None)
        if record is None or record[1] <= self.clock():
            return None
        return record

    def put_citation(self, handle: CitationHandle, *, citation_key: tuple[str, str, str, str]) -> None:
        self.citation_handles[handle.handle] = handle
        self.citation_links[citation_key] = handle.handle

    def get_citation(
        self,
        handle: str,
        *,
        subject: str | None = None,
        tenant_id: str | None = None,
        matter_id: str | None = None,
        conversation_id: str | None = None,
    ) -> CitationHandle | None:
        record = self.citation_handles.get(handle)
        if record is None or record.expires_at <= self.clock():
            self.citation_handles.pop(handle, None)
            return None
        if any(
            expected is not None and expected != actual
            for expected, actual in (
                (subject, record.subject),
                (tenant_id, record.tenant_id),
                (matter_id, record.matter_id),
                (conversation_id, record.conversation_id),
            )
        ):
            return None
        return record

    def get_citation_link(self, citation_key: tuple[str, str, str, str]) -> str | None:
        handle = self.citation_links.get(citation_key)
        if handle is None or self.get_citation(handle) is None:
            self.citation_links.pop(citation_key, None)
            return None
        return handle

    def bind_conversation(self, conversation_id: str, record: ConversationRecord, correlation_id: str) -> None:
        self.conversation_selectors[conversation_id] = record
        self.conversation_correlations[conversation_id] = correlation_id

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        return self.conversation_selectors.get(conversation_id)

    def set_conversation_correlation(self, conversation_id: str, correlation_id: str) -> None:
        self.conversation_correlations[conversation_id] = correlation_id

    def get_conversation_correlation(self, conversation_id: str) -> str | None:
        return self.conversation_correlations.get(conversation_id)

    def add_history_event(self, key: tuple[str, str, str], event_id: str) -> None:
        accepted = self.accepted_history_ids.setdefault(key, [])
        if event_id not in accepted:
            accepted.append(event_id)
        if len(accepted) > 200:
            del accepted[: len(accepted) - 200]

    def list_history_ids(self, key: tuple[str, str, str]) -> tuple[str, ...]:
        return tuple(self.accepted_history_ids.get(key, ()))

    def put_review_candidate(self, key: tuple[str, str, str], record: ReviewCandidateRecord) -> None:
        self.accepted_review_candidates[key] = record

    def get_review_candidate(self, key: tuple[str, str, str]) -> ReviewCandidateRecord | None:
        record = self.accepted_review_candidates.get(key)
        if record is None or record[0] <= self.clock():
            self.accepted_review_candidates.pop(key, None)
            return None
        return record

    def delete_review_candidate(self, key: tuple[str, str, str]) -> None:
        self.accepted_review_candidates.pop(key, None)

    def put_ingestion_operation(self, record: IngestionOperationRecord) -> None:
        self.bound_state()
        idempotency_key = (record.subject, record.tenant_id, record.matter_id, record.idempotency_key)
        active_operation = self.active_ingestion_document_sets.get(record.document_set_key)
        if (
            record.operation_id in self.ingestion_operations
            or idempotency_key in self.ingestion_idempotency
            or active_operation is not None
        ):
            raise ValueError("ingestion operation already exists")
        self.ingestion_operations[record.operation_id] = record
        self.ingestion_idempotency[idempotency_key] = record.operation_id
        self.active_ingestion_document_sets[record.document_set_key] = record.operation_id
        self.ingestion_scope_index.setdefault((record.subject, record.tenant_id, record.matter_id), set()).add(record.operation_id)

    def get_ingestion_operation(self, operation_id: str) -> IngestionOperationRecord | None:
        record = self.ingestion_operations.get(operation_id)
        if record is None or record.expires_at <= self.clock():
            if record is not None:
                self.delete_ingestion_operation(record)
            return None
        return record

    def get_ingestion_operation_for_idempotency(
        self, *, subject: str, tenant_id: str, matter_id: str, idempotency_key: str
    ) -> IngestionOperationRecord | None:
        operation_id = self.ingestion_idempotency.get((subject, tenant_id, matter_id, idempotency_key))
        return self.get_ingestion_operation(operation_id) if operation_id else None

    def get_ingestion_operation_for_document_set(self, document_set_key: str) -> IngestionOperationRecord | None:
        operation_id = self.active_ingestion_document_sets.get(document_set_key)
        return self.get_ingestion_operation(operation_id) if operation_id else None

    def list_ingestion_operations_for_scope(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> tuple[IngestionOperationRecord, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= 100:
            raise ValueError("ingestion operation query limit is invalid")
        operation_ids = tuple(self.ingestion_scope_index.get((subject, tenant_id, matter_id), ()))[:limit]
        operations = tuple(
            operation
            for operation_id in operation_ids
            if (operation := self.get_ingestion_operation(operation_id)) is not None
        )
        return operations

    def update_ingestion_operation(self, record: IngestionOperationRecord) -> None:
        existing = self.ingestion_operations.get(record.operation_id)
        if existing is None or existing.expires_at <= self.clock():
            raise ValueError("ingestion operation is unavailable")
        if (existing.subject, existing.tenant_id, existing.matter_id, existing.idempotency_key) != (
            record.subject, record.tenant_id, record.matter_id, record.idempotency_key
        ):
            raise ValueError("ingestion operation binding cannot change")
        self.ingestion_operations[record.operation_id] = record
        self.ingestion_scope_index.setdefault((record.subject, record.tenant_id, record.matter_id), set()).add(record.operation_id)
        if record.status in {"INDEXED", "FAILED"} and self.active_ingestion_document_sets.get(record.document_set_key) == record.operation_id:
            self.active_ingestion_document_sets.pop(record.document_set_key, None)
        if record.status in {"INDEXED", "FAILED"}:
            self.ingestion_scope_index.get((record.subject, record.tenant_id, record.matter_id), set()).discard(record.operation_id)

    def delete_ingestion_operation(self, record: IngestionOperationRecord) -> None:
        self.ingestion_operations.pop(record.operation_id, None)
        self.ingestion_idempotency.pop((record.subject, record.tenant_id, record.matter_id, record.idempotency_key), None)
        self.ingestion_scope_index.get((record.subject, record.tenant_id, record.matter_id), set()).discard(record.operation_id)
        if self.active_ingestion_document_sets.get(record.document_set_key) == record.operation_id:
            self.active_ingestion_document_sets.pop(record.document_set_key, None)

    def append_audit(self, record: Mapping[str, object]) -> None:
        self.audit_records.append({
            str(key): value
            for key, value in record.items()
            if key in _SAFE_AUDIT_FIELDS and isinstance(value, (str, int, float, bool))
        })
        if len(self.audit_records) > 1_000:
            del self.audit_records[: len(self.audit_records) - 1_000]

    def list_audit(self, subject: str, *, limit: int = 1_000) -> tuple[dict[str, object], ...]:
        return tuple(item for item in self.audit_records if item.get("subject") == subject)[-max(1, min(limit, 1_000)) :]

    def delete_scope_state(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("state deletion limit is invalid")
        scope = (subject, tenant_id, matter_id)
        operation_ids = tuple(
            self.ingestion_scope_index.get(scope, set())
        )
        if len(operation_ids) > limit:
            raise ValueError("state deletion limit exceeded")
        for operation_id in operation_ids:
            operation = self.ingestion_operations.get(operation_id)
            if operation is not None:
                self.delete_ingestion_operation(operation)
        removed = len(operation_ids)
        if self.accepted_history_ids.pop(scope, None) is not None:
            removed += 1
        if self.accepted_review_candidates.pop(scope, None) is not None:
            removed += 1
        for conversation_id, record in tuple(self.conversation_selectors.items()):
            if record[:3] == scope:
                self.conversation_selectors.pop(conversation_id, None)
                self.conversation_correlations.pop(conversation_id, None)
                removed += 1
        for handle, citation in tuple(self.citation_handles.items()):
            if (citation.subject, citation.tenant_id, citation.matter_id) == scope:
                self.citation_handles.pop(handle, None)
                removed += 1
        self.citation_links = {
            key: handle for key, handle in self.citation_links.items()
            if handle in self.citation_handles
        }
        self.audit_records = [
            item for item in self.audit_records
            if not (
                item.get("subject") == subject
                and item.get("tenantId") == tenant_id
                and item.get("matterId") == matter_id
            )
        ]
        return removed


class DynamoDBEphemeralStateStore:
    """Namespaced DynamoDB state adapter for the existing metadata table."""

    _PREFIX = "LEGALDESK#P14#STATE#"
    _AUDIT_ALLOWED = _SAFE_AUDIT_FIELDS
    _AUDIT_FIELDS = (
        "subject", "tenantId", "matterId", "correlationId", "operation", "timestampMs",
        "event_type", "outcome", "latency_ms", "count", "error_code",
        "correlation_id", "timestamp_ms", "prompt_version", "prompt_sha256",
        "resolver_prompt_version", "resolver_prompt_sha256",
        "writer_prompt_version", "writer_prompt_sha256",
    )

    def __init__(self, table_name: str, *, table: Any | None = None, clock: Any = _now) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        self.clock = clock
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    @classmethod
    def _pk(cls, kind: str, value: object) -> str:
        return f"{cls._PREFIX}{kind}#{_digest(value)}"

    @classmethod
    def _key(cls, kind: str, value: object, suffix: str) -> dict[str, str]:
        return {"pk": cls._pk(kind, value), "sk": suffix}

    @staticmethod
    def _expires(item: Mapping[str, object]) -> float | None:
        value = item.get("expiresAt")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def bound_state(self) -> None:
        # Capacity is enforced at the edge in the public deployment.  Counting
        # all sessions would require an unbounded scan, which this adapter
        # deliberately never performs.
        return None

    def put_session(self, key: str, record: SessionRecord) -> None:
        self.table.put_item(
            Item={
                **self._key("SESSION", key, "RECORD"),
                "entityType": "P14Session",
                "subject": record.identity.subject,
                "accessToken": record.access_token,
                "csrfToken": record.csrf_token,
                "expiresAt": record.expires_at,
                "ttl": max(1, int(record.expires_at)),
            },
        )

    def get_session(self, key: str) -> SessionRecord | None:
        response = self.table.get_item(Key=self._key("SESSION", key, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping):
            return None
        expires_at = self._expires(item)
        subject, token, csrf = item.get("subject"), item.get("accessToken"), item.get("csrfToken")
        if expires_at is None or expires_at <= self.clock() or not all(isinstance(value, str) and value for value in (subject, token, csrf)):
            return None
        return SessionRecord(VerifiedIdentity(subject), token, csrf, expires_at)

    def delete_session(self, key: str) -> None:
        self.table.delete_item(Key=self._key("SESSION", key, "RECORD"))

    def put_oauth_state(self, state: str, code_verifier: str, expires_at: float) -> None:
        self.table.put_item(
            Item={
                **self._key("OAUTH", state, "RECORD"),
                "entityType": "P14OAuthState",
                "codeVerifier": code_verifier,
                "expiresAt": expires_at,
                "ttl": max(1, int(expires_at)),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    def consume_oauth_state(self, state: str) -> tuple[str, float] | None:
        try:
            response = self.table.delete_item(
                Key=self._key("OAUTH", state, "RECORD"),
                ConditionExpression="attribute_exists(pk)",
                ReturnValues="ALL_OLD",
            )
        except Exception:
            return None
        item = response.get("Attributes") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping):
            return None
        expires_at = self._expires(item)
        verifier = item.get("codeVerifier")
        if expires_at is None or expires_at <= self.clock() or not isinstance(verifier, str) or not verifier:
            return None
        return verifier, expires_at

    def put_citation(self, handle: CitationHandle, *, citation_key: tuple[str, str, str, str]) -> None:
        self.table.put_item(
            Item={
                **self._key("CITATION", handle.handle, "RECORD"),
                "entityType": "P14Citation",
                "handle": handle.handle,
                "subject": handle.subject,
                "tenantId": handle.tenant_id,
                "matterId": handle.matter_id,
                "conversationId": handle.conversation_id,
                "documentId": handle.document_id,
                "passage": handle.passage,
                "expiresAt": handle.expires_at,
                "ttl": max(1, int(handle.expires_at)),
            },
        )
        self.table.put_item(
            Item={
                **self._key("CITATION_LINK", citation_key, "RECORD"),
                "entityType": "P14CitationLink",
                "handle": handle.handle,
                "subject": citation_key[0],
                "conversationId": citation_key[1],
                "correlationId": citation_key[2],
                "citationId": citation_key[3],
                "expiresAt": handle.expires_at,
                "ttl": max(1, int(handle.expires_at)),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    def get_citation(
        self,
        handle: str,
        *,
        subject: str | None = None,
        tenant_id: str | None = None,
        matter_id: str | None = None,
        conversation_id: str | None = None,
    ) -> CitationHandle | None:
        response = self.table.get_item(Key=self._key("CITATION", handle, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        if not isinstance(item, Mapping) or self._expires(item) is None or self._expires(item) <= self.clock():
            return None
        values = tuple(item.get(name) for name in ("handle", "subject", "tenantId", "matterId", "conversationId", "passage"))
        if not all(isinstance(value, str) and value for value in values):
            return None
        document_id = item.get("documentId")
        if document_id is not None and not isinstance(document_id, str):
            return None
        record = CitationHandle(values[0], values[1], values[2], values[3], values[4], document_id, values[5], self._expires(item))
        if any(
            expected is not None and expected != actual
            for expected, actual in (
                (subject, record.subject),
                (tenant_id, record.tenant_id),
                (matter_id, record.matter_id),
                (conversation_id, record.conversation_id),
            )
        ):
            return None
        return record

    def get_citation_link(self, citation_key: tuple[str, str, str, str]) -> str | None:
        response = self.table.get_item(Key=self._key("CITATION_LINK", citation_key, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        handle = item.get("handle") if isinstance(item, Mapping) else None
        expires_at = self._expires(item) if isinstance(item, Mapping) else None
        if not isinstance(handle, str) or expires_at is None or expires_at <= self.clock():
            return None
        return handle if self.get_citation(handle) is not None else None

    def bind_conversation(self, conversation_id: str, record: ConversationRecord, correlation_id: str) -> None:
        subject, tenant_id, matter_id, selector = record
        expires_at = self.clock() + _CONVERSATION_TTL_SECONDS
        self.table.put_item(
            Item={
                **self._key("CONVERSATION", conversation_id, "RECORD"),
                "entityType": "P14Conversation",
                "subject": subject,
                "tenantId": tenant_id,
                "matterId": matter_id,
                "selector": selector,
                "conversationId": conversation_id,
                "correlationId": correlation_id,
                "expiresAt": expires_at,
                "ttl": max(1, int(expires_at)),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        response = self.table.get_item(Key=self._key("CONVERSATION", conversation_id, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        expires_at = self._expires(item) if isinstance(item, Mapping) else None
        if not isinstance(item, Mapping) or expires_at is None or expires_at <= self.clock():
            return None
        values = tuple(item.get(name) for name in ("subject", "tenantId", "matterId", "selector")) if isinstance(item, Mapping) else ()
        return tuple(values) if len(values) == 4 and all(isinstance(value, str) and value for value in values) else None

    def set_conversation_correlation(self, conversation_id: str, correlation_id: str) -> None:
        self.table.update_item(
            Key=self._key("CONVERSATION", conversation_id, "RECORD"),
            UpdateExpression="SET #correlation = :correlation",
            ExpressionAttributeNames={"#correlation": "correlationId"},
            ExpressionAttributeValues={":correlation": correlation_id},
            ConditionExpression="attribute_exists(pk)",
        )

    def get_conversation_correlation(self, conversation_id: str) -> str | None:
        response = self.table.get_item(Key=self._key("CONVERSATION", conversation_id, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        expires_at = self._expires(item) if isinstance(item, Mapping) else None
        if not isinstance(item, Mapping) or expires_at is None or expires_at <= self.clock():
            return None
        value = item.get("correlationId") if isinstance(item, Mapping) else None
        return value if isinstance(value, str) else None

    def add_history_event(self, key: tuple[str, str, str], event_id: str) -> None:
        self.table.put_item(
            Item={
                **self._key("HISTORY", key, f"EVENT#{_digest(event_id)}"),
                "entityType": "P14HistoryEvent",
                "eventId": event_id,
                "expiresAt": self.clock() + 7 * 24 * 60 * 60,
                "ttl": max(1, int(self.clock()) + 7 * 24 * 60 * 60),
            },
            ConditionExpression="attribute_not_exists(pk)",
        )

    def list_history_ids(self, key: tuple[str, str, str]) -> tuple[str, ...]:
        from boto3.dynamodb.conditions import Key

        response = self.table.query(
            KeyConditionExpression=Key("pk").eq(self._pk("HISTORY", key)) & Key("sk").begins_with("EVENT#"),
            ProjectionExpression="#eventId, #expiresAt",
            ExpressionAttributeNames={"#eventId": "eventId", "#expiresAt": "expiresAt"},
            Limit=200,
            ScanIndexForward=False,
        )
        items = response.get("Items", ()) if isinstance(response, Mapping) else ()
        now = self.clock()
        return tuple(
            item["eventId"]
            for item in items
            if isinstance(item, Mapping)
            and isinstance(item.get("eventId"), str)
            and self._expires(item) is not None
            and self._expires(item) > now
        )

    def put_review_candidate(self, key: tuple[str, str, str], record: ReviewCandidateRecord) -> None:
        expires_at, candidate = record
        encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 300_000:
            raise ValueError("review candidate is too large")
        self.table.put_item(
            Item={
                **self._key("REVIEW_CANDIDATE", key, "RECORD"),
                "entityType": "P14ReviewCandidate",
                "candidateJson": encoded,
                "expiresAt": expires_at,
                "ttl": max(1, int(expires_at)),
            },
        )

    def get_review_candidate(self, key: tuple[str, str, str]) -> ReviewCandidateRecord | None:
        response = self.table.get_item(Key=self._key("REVIEW_CANDIDATE", key, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        expires_at = self._expires(item) if isinstance(item, Mapping) else None
        encoded = item.get("candidateJson") if isinstance(item, Mapping) else None
        if expires_at is None or expires_at <= self.clock() or not isinstance(encoded, str):
            return None
        try:
            candidate = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return (expires_at, dict(candidate)) if isinstance(candidate, Mapping) else None

    def delete_review_candidate(self, key: tuple[str, str, str]) -> None:
        self.table.delete_item(Key=self._key("REVIEW_CANDIDATE", key, "RECORD"))

    @staticmethod
    def _operation_item(record: IngestionOperationRecord) -> dict[str, object]:
        return {
            **DynamoDBEphemeralStateStore._key("INGESTION_OPERATION", record.operation_id, "RECORD"),
            "entityType": "P14IngestionOperation",
            "operationId": record.operation_id,
            "idempotencyKey": record.idempotency_key,
            "subject": record.subject,
            "tenantId": record.tenant_id,
            "matterId": record.matter_id,
            "documentIds": list(record.document_ids),
            "correlationId": record.correlation_id,
            "ingestionJobId": record.ingestion_job_id,
            "status": record.status,
            "providerStatus": record.provider_status,
            "createdAt": record.created_at,
            "updatedAt": record.updated_at,
            "expiresAt": record.expires_at,
            "documentSetKey": record.document_set_key,
            "documentsUpdated": record.documents_updated,
            "failedDocumentCount": record.failed_document_count,
            "ttl": max(1, int(record.expires_at)),
        }

    @classmethod
    def _operation_scope_item(cls, record: IngestionOperationRecord) -> dict[str, object]:
        return {
            **cls._key("INGESTION_SCOPE", (record.subject, record.tenant_id, record.matter_id), f"OP#{record.operation_id}"),
            "entityType": "P14IngestionScopeIndex",
            "operationId": record.operation_id,
            "subject": record.subject,
            "tenantId": record.tenant_id,
            "matterId": record.matter_id,
            "expiresAt": record.expires_at,
            "ttl": max(1, int(record.expires_at)),
        }

    def _delete_owned_ingestion_item(self, key: dict[str, str], operation_id: str) -> None:
        response = self.table.get_item(Key=key, ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        if isinstance(item, Mapping) and item.get("operationId") == operation_id:
            self.table.delete_item(Key=key)

    @classmethod
    def _operation_from_item(cls, item: Mapping[str, object]) -> IngestionOperationRecord | None:
        expires_at = cls._expires(item)
        values = tuple(
            item.get(name)
            for name in (
                "operationId", "idempotencyKey", "subject", "tenantId", "matterId",
                "correlationId", "ingestionJobId", "status", "providerStatus",
            )
        )
        document_set_key = item.get("documentSetKey")
        document_ids = item.get("documentIds")
        created_at, updated_at = item.get("createdAt"), item.get("updatedAt")
        documents_updated, failed_count = item.get("documentsUpdated", 0), item.get("failedDocumentCount")
        valid_job_binding = (
            isinstance(values[6], str)
            and (
                bool(values[6].strip())
                or (values[6] == "" and values[7] == "STARTING" and values[8] == "STARTING")
            )
        )
        if (
            expires_at is None
            or not all(isinstance(value, str) and value for value in values[:6] + values[7:])
            or not valid_job_binding
            or not isinstance(document_set_key, str) or not document_set_key
            or values[7] not in {"STARTING", "RECOVERY_PENDING", "PENDING", "INDEXED", "FAILED"}
            or values[8] not in {"STARTING", "IN_PROGRESS", "RUNNING", "STOPPING", "COMPLETE", "FAILED", "STOPPED"}
            or not isinstance(document_ids, list)
            or not 1 <= len(document_ids) <= 20
            or not all(isinstance(value, str) and value for value in document_ids)
            or not isinstance(created_at, (int, float)) or isinstance(created_at, bool)
            or not isinstance(updated_at, (int, float)) or isinstance(updated_at, bool)
            or not isinstance(documents_updated, int) or isinstance(documents_updated, bool)
            or (failed_count is not None and (not isinstance(failed_count, int) or isinstance(failed_count, bool)))
        ):
            return None
        if document_set_key != ingestion_document_set_key(
            subject=values[2], tenant_id=values[3], matter_id=values[4], document_ids=tuple(document_ids),
        ):
            return None
        return IngestionOperationRecord(
            operation_id=values[0], idempotency_key=values[1], subject=values[2], tenant_id=values[3],
            matter_id=values[4], document_ids=tuple(document_ids), correlation_id=values[5],
            ingestion_job_id=values[6], status=values[7], provider_status=values[8],
            created_at=float(created_at), updated_at=float(updated_at), expires_at=expires_at,
            document_set_key=document_set_key,
            documents_updated=documents_updated, failed_document_count=failed_count,
        )

    def put_ingestion_operation(self, record: IngestionOperationRecord) -> None:
        self.table.put_item(Item=self._operation_item(record), ConditionExpression="attribute_not_exists(pk)")
        try:
            self.table.put_item(
                Item={
                    **self._operation_item(record),
                    "pk": self._pk("INGESTION_IDEMPOTENCY", (record.subject, record.tenant_id, record.matter_id, record.idempotency_key)),
                    "sk": "RECORD",
                    "entityType": "P14IngestionIdempotency",
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            self.table.put_item(
                Item={
                    "pk": self._pk("INGESTION_ACTIVE", record.document_set_key),
                    "sk": "RECORD",
                    "entityType": "P14ActiveIngestion",
                    "operationId": record.operation_id,
                    "documentSetKey": record.document_set_key,
                    "expiresAt": record.expires_at,
                    "ttl": max(1, int(record.expires_at)),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
            self.table.put_item(
                Item=self._operation_scope_item(record),
                ConditionExpression="attribute_not_exists(pk)",
            )
        except Exception:
            self.table.delete_item(Key=self._key("INGESTION_OPERATION", record.operation_id, "RECORD"))
            idempotency_key = {"pk": self._pk("INGESTION_IDEMPOTENCY", (record.subject, record.tenant_id, record.matter_id, record.idempotency_key)), "sk": "RECORD"}
            existing = self.table.get_item(Key=idempotency_key, ConsistentRead=True)
            existing_item = existing.get("Item") if isinstance(existing, Mapping) else None
            if isinstance(existing_item, Mapping) and existing_item.get("operationId") == record.operation_id:
                self.table.delete_item(Key=idempotency_key)
            active_key = {"pk": self._pk("INGESTION_ACTIVE", record.document_set_key), "sk": "RECORD"}
            active = self.table.get_item(Key=active_key, ConsistentRead=True)
            active_item = active.get("Item") if isinstance(active, Mapping) else None
            if isinstance(active_item, Mapping) and active_item.get("operationId") == record.operation_id:
                self.table.delete_item(Key=active_key)
            scope_key = self._key(
                "INGESTION_SCOPE", (record.subject, record.tenant_id, record.matter_id),
                f"OP#{record.operation_id}",
            )
            scope_item_response = self.table.get_item(Key=scope_key, ConsistentRead=True)
            scope_item = scope_item_response.get("Item") if isinstance(scope_item_response, Mapping) else None
            if isinstance(scope_item, Mapping) and scope_item.get("operationId") == record.operation_id:
                self.table.delete_item(Key=scope_key)
            raise

    def get_ingestion_operation(self, operation_id: str) -> IngestionOperationRecord | None:
        response = self.table.get_item(Key=self._key("INGESTION_OPERATION", operation_id, "RECORD"), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        record = self._operation_from_item(item) if isinstance(item, Mapping) else None
        return record if record is not None and record.expires_at > self.clock() else None

    def get_ingestion_operation_for_idempotency(
        self, *, subject: str, tenant_id: str, matter_id: str, idempotency_key: str
    ) -> IngestionOperationRecord | None:
        response = self.table.get_item(
            Key={"pk": self._pk("INGESTION_IDEMPOTENCY", (subject, tenant_id, matter_id, idempotency_key)), "sk": "RECORD"},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        operation_id = item.get("operationId") if isinstance(item, Mapping) else None
        expires_at = self._expires(item) if isinstance(item, Mapping) else None
        if expires_at is None or expires_at <= self.clock():
            return None
        return self.get_ingestion_operation(operation_id) if isinstance(operation_id, str) else None

    def get_ingestion_operation_for_document_set(self, document_set_key: str) -> IngestionOperationRecord | None:
        key = {"pk": self._pk("INGESTION_ACTIVE", document_set_key), "sk": "RECORD"}
        response = self.table.get_item(Key=key, ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        expires_at = self._expires(item) if isinstance(item, Mapping) else None
        operation_id = item.get("operationId") if isinstance(item, Mapping) else None
        if expires_at is None or expires_at <= self.clock() or not isinstance(operation_id, str):
            if isinstance(item, Mapping) and (expires_at is None or expires_at <= self.clock()):
                self.table.delete_item(Key=key)
            return None
        return self.get_ingestion_operation(operation_id)

    def list_ingestion_operations_for_scope(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> tuple[IngestionOperationRecord, ...]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= 100:
            raise ValueError("ingestion operation query limit is invalid")
        from boto3.dynamodb.conditions import Key

        response = self.table.query(
            KeyConditionExpression=(
                Key("pk").eq(self._pk("INGESTION_SCOPE", (subject, tenant_id, matter_id)))
                & Key("sk").begins_with("OP#")
            ),
            ProjectionExpression="#operationId, #expiresAt",
            ExpressionAttributeNames={"#operationId": "operationId", "#expiresAt": "expiresAt"},
            Limit=limit,
            ConsistentRead=True,
        )
        items = response.get("Items", ()) if isinstance(response, Mapping) else ()
        now = self.clock()
        operations: list[IngestionOperationRecord] = []
        for item in items:
            if not isinstance(item, Mapping) or self._expires(item) is None or self._expires(item) <= now:
                continue
            operation_id = item.get("operationId")
            if isinstance(operation_id, str):
                operation = self.get_ingestion_operation(operation_id)
                if operation is not None and (
                    operation.subject, operation.tenant_id, operation.matter_id
                ) == (subject, tenant_id, matter_id):
                    operations.append(operation)
        return tuple(operations)

    def update_ingestion_operation(self, record: IngestionOperationRecord) -> None:
        self.table.put_item(Item=self._operation_item(record), ConditionExpression="attribute_exists(pk)")
        if record.status in {"INDEXED", "FAILED"}:
            self._delete_owned_ingestion_item(
                {"pk": self._pk("INGESTION_ACTIVE", record.document_set_key), "sk": "RECORD"},
                record.operation_id,
            )
            self._delete_owned_ingestion_item(
                self._key(
                    "INGESTION_SCOPE", (record.subject, record.tenant_id, record.matter_id),
                    f"OP#{record.operation_id}",
                ),
                record.operation_id,
            )

    def delete_ingestion_operation(self, record: IngestionOperationRecord) -> None:
        self.table.delete_item(Key=self._key("INGESTION_OPERATION", record.operation_id, "RECORD"))
        self.table.delete_item(
            Key={"pk": self._pk("INGESTION_IDEMPOTENCY", (record.subject, record.tenant_id, record.matter_id, record.idempotency_key)), "sk": "RECORD"}
        )
        self._delete_owned_ingestion_item(
            {"pk": self._pk("INGESTION_ACTIVE", record.document_set_key), "sk": "RECORD"},
            record.operation_id,
        )
        self._delete_owned_ingestion_item(
            self._key(
                "INGESTION_SCOPE", (record.subject, record.tenant_id, record.matter_id),
                f"OP#{record.operation_id}",
            ),
            record.operation_id,
        )

    def append_audit(self, record: Mapping[str, object]) -> None:
        subject = record.get("subject")
        if not isinstance(subject, str) or not subject:
            raise ValueError("audit subject is required")
        safe = {
            str(key): value
            for key, value in record.items()
            if key in self._AUDIT_ALLOWED and isinstance(value, (str, int, float, bool))
        }
        timestamp = safe.get("timestampMs", safe.get("timestamp_ms", int(self.clock() * 1000)))
        item = {
            **self._key("AUDIT", subject, f"EVENT#{int(timestamp):013d}#{uuid4().hex}"),
            "entityType": "P14Audit",
            **safe,
        }
        self.table.put_item(Item=item)

    def list_audit(self, subject: str, *, limit: int = 1_000) -> tuple[dict[str, object], ...]:
        from boto3.dynamodb.conditions import Key

        bounded = max(1, min(int(limit), 1_000))
        response = self.table.query(
            KeyConditionExpression=Key("pk").eq(self._pk("AUDIT", subject)) & Key("sk").begins_with("EVENT#"),
            ProjectionExpression=", ".join(f"#audit{index}" for index, _field in enumerate(self._AUDIT_FIELDS)),
            ExpressionAttributeNames={f"#audit{index}": field for index, field in enumerate(self._AUDIT_FIELDS)},
            Limit=bounded,
            ScanIndexForward=False,
        )
        items = response.get("Items", ()) if isinstance(response, Mapping) else ()
        return tuple(dict(item) for item in items if isinstance(item, Mapping))

    def delete_scope_state(
        self, *, subject: str, tenant_id: str, matter_id: str, limit: int
    ) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("state deletion limit is invalid")
        removed = 0
        from boto3.dynamodb.conditions import Key

        history_pk = self._pk("HISTORY", (subject, tenant_id, matter_id))
        history = self.table.query(
            KeyConditionExpression=Key("pk").eq(history_pk) & Key("sk").begins_with("EVENT#"),
            ProjectionExpression="#pk,#sk",
            ExpressionAttributeNames={"#pk": "pk", "#sk": "sk"},
            Limit=limit + 1,
            ConsistentRead=True,
        )
        history_items = history.get("Items", ()) if isinstance(history, Mapping) else ()
        if len(history_items) > limit:
            raise ValueError("state deletion limit exceeded")
        for item in history_items:
            if isinstance(item, Mapping) and isinstance(item.get("pk"), str) and isinstance(item.get("sk"), str):
                self.table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})
                removed += 1
        self.table.delete_item(Key=self._key("REVIEW_CANDIDATE", (subject, tenant_id, matter_id), "RECORD"))
        operations = self.list_ingestion_operations_for_scope(
            subject=subject, tenant_id=tenant_id, matter_id=matter_id, limit=min(limit, 100)
        )
        if len(operations) >= limit:
            raise ValueError("state deletion limit exceeded")
        for operation in operations:
            self.delete_ingestion_operation(operation)
            removed += 1
        return removed


__all__ = [
    "CitationHandle",
    "ConversationRecord",
    "DynamoDBEphemeralStateStore",
    "EphemeralStateStore",
    "IngestionOperationRecord",
    "InMemoryEphemeralStateStore",
    "ReviewCandidateRecord",
    "SessionRecord",
]
