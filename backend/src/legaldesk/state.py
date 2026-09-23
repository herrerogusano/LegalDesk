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
    "subject", "matterId", "correlationId", "operation", "timestampMs",
    "event_type", "outcome", "latency_ms", "count", "error_code",
    "correlation_id", "timestamp_ms", "prompt_version", "prompt_sha256",
    "resolver_prompt_version", "resolver_prompt_sha256",
    "writer_prompt_version", "writer_prompt_sha256",
})


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

    def append_audit(self, record: Mapping[str, object]) -> None: ...

    def list_audit(self, subject: str, *, limit: int = 1_000) -> tuple[dict[str, object], ...]: ...


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


class DynamoDBEphemeralStateStore:
    """Namespaced DynamoDB state adapter for the existing metadata table."""

    _PREFIX = "LEGALDESK#P14#STATE#"
    _AUDIT_ALLOWED = _SAFE_AUDIT_FIELDS
    _AUDIT_FIELDS = (
        "subject", "matterId", "correlationId", "operation", "timestampMs",
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


__all__ = [
    "CitationHandle",
    "ConversationRecord",
    "DynamoDBEphemeralStateStore",
    "EphemeralStateStore",
    "InMemoryEphemeralStateStore",
    "ReviewCandidateRecord",
    "SessionRecord",
]
