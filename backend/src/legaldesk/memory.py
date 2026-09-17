"""Scoped AgentCore Memory adapter and LegalDesk retention policy.

Only short-term events are supported in Phase 09.  The scope is derived from
the already-authorized :class:`RequestContext`; callers cannot choose an
AgentCore actor identifier directly.  Every event is sent with
``extractionMode=SKIP`` so AgentCore does not extract long-term records.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
    require_authorized_context,
)


MEMORY_EVENT_EXPIRY_DAYS = 7
MAX_SELECTOR_LENGTH = 128
MAX_EVENT_TEXT_LENGTH = 8_000
_SELECTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT_ID = re.compile(r"^[0-9]+#[a-fA-F0-9]+$")
_ROLES = frozenset({"USER", "ASSISTANT", "TOOL", "OTHER"})


class LongTermMemoryDisabled(PermissionError):
    """Raised for every attempted long-term memory write or retrieval."""


class MemoryScopeError(ValueError):
    """Raised when an authorized memory scope cannot be derived."""


_SCOPE_FACTORY_TOKEN = object()


def _selector(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SELECTOR_LENGTH
        or _SELECTOR.fullmatch(value) is None
    ):
        raise MemoryScopeError(f"{name} must be an opaque selector")
    return value


def _opaque_id(prefix: str, *parts: str) -> str:
    # The hash is an identifier, not an authorization credential.  In
    # production the deployment must provide a secret derivation key at the
    # trusted edge; the local deterministic namespace keeps tests reproducible
    # without storing or printing tenant/matter/user identifiers.
    material = "\x1f".join(("legaldesk-memory-v1", *parts)).encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()
    return f"{prefix}-{digest[:48]}"


def _opaque_session_id(*parts: str) -> str:
    """Return a deterministic UUID accepted by both Memory and Harness."""

    material = "\x1f".join(("legaldesk-memory-session-v1", *parts)).encode("utf-8")
    return str(UUID(bytes=hashlib.sha256(material).digest()[:16]))


@dataclass(frozen=True, slots=True, init=False)
class MemoryScope:
    """Opaque AgentCore actor/session scope derived by the trusted backend."""

    actor_id: str
    session_id: str
    namespace: str
    _seal: object

    @classmethod
    def from_request_context(
        cls,
        context: RequestContext,
        *,
        conversation_id: str,
        session_selector: str | None = None,
        session_id: str | None = None,
        _factory_token: object | None = None,
    ) -> "MemoryScope":
        if _factory_token is not _SCOPE_FACTORY_TOKEN:
            raise TypeError("MemoryScope must be created by an authorized server scope")
        if type(context) is not RequestContext or not context.is_server_derived():
            raise TypeError("context must be a server-built RequestContext")
        conversation = _selector(conversation_id, "conversation_id")
        if (session_selector is None) == (session_id is None):
            raise MemoryScopeError("provide exactly one session selector")
        session = _selector(
            session_selector if session_selector is not None else session_id,
            "session_selector",
        )
        for field_name in ("user_id", "tenant_id", "matter_id"):
            if not isinstance(getattr(context, field_name), str) or not getattr(
                context, field_name
            ).strip():
                raise MemoryScopeError("context scope is incomplete")
        actor_id = _opaque_id(
            "ldactor", context.tenant_id, context.user_id, context.matter_id
        )
        session_id = _opaque_session_id(actor_id, conversation, session)
        instance = object.__new__(cls)
        object.__setattr__(instance, "actor_id", actor_id)
        object.__setattr__(instance, "session_id", session_id)
        object.__setattr__(instance, "namespace", f"legaldesk/{actor_id}/{session_id}")
        object.__setattr__(instance, "_seal", _SCOPE_FACTORY_TOKEN)
        return instance

    def _is_sealed(self) -> bool:
        """Internal capability check used by adapters at another package boundary."""

        return getattr(self, "_seal", None) is _SCOPE_FACTORY_TOKEN


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    """Server-side ownership record for an opaque conversation/session pair."""

    user_id: str
    tenant_id: str
    matter_id: str
    conversation_id: str
    session_selector: str


class ConversationBindingStore(Protocol):
    def bind(
        self,
        context: RequestContext,
        *,
        conversation_id: str,
        session_selector: str,
    ) -> ConversationBinding: ...

    def is_bound(
        self,
        *,
        context: RequestContext,
        conversation_id: str,
        session_selector: str,
    ) -> bool: ...


class InMemoryConversationBindingStore:
    """Local server-side conversation registry used by Phase 09 tests."""

    def __init__(self) -> None:
        self._bindings: set[ConversationBinding] = set()

    def bind(
        self,
        context: RequestContext,
        *,
        conversation_id: str,
        session_selector: str,
    ) -> ConversationBinding:
        if type(context) is not RequestContext or not context.is_server_derived():
            raise TypeError("context must be a server-built RequestContext")
        conversation = _selector(conversation_id, "conversation_id")
        session = _selector(session_selector, "session_selector")
        binding = ConversationBinding(
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            conversation_id=conversation,
            session_selector=session,
        )
        self._bindings.add(binding)
        return binding

    def is_bound(
        self,
        *,
        context: RequestContext,
        conversation_id: str,
        session_selector: str,
    ) -> bool:
        if type(context) is not RequestContext or not context.is_server_derived():
            return False
        try:
            conversation = _selector(conversation_id, "conversation_id")
            session = _selector(session_selector, "session_selector")
        except MemoryScopeError:
            return False
        return ConversationBinding(
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            conversation_id=conversation,
            session_selector=session,
        ) in self._bindings


def conversation_binding_partition_key(context: RequestContext, conversation_id: str) -> str:
    context = require_authorized_context(context)
    return f"CONVERSATION#{context.tenant_id}#{context.matter_id}#{context.user_id}#{conversation_id}"


def conversation_binding_sort_key(session_selector: str) -> str:
    return f"SESSION#{session_selector}"


class Boto3DynamoConversationBindingStore:
    """Durable conversation binding using the existing metadata table."""

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    @staticmethod
    def _item(binding: ConversationBinding) -> dict[str, object]:
        return {
            "pk": (
                f"CONVERSATION#{binding.tenant_id}#{binding.matter_id}#"
                f"{binding.user_id}#{binding.conversation_id}"
            ),
            "sk": conversation_binding_sort_key(binding.session_selector),
            "entityType": "ConversationBinding",
            "userId": binding.user_id,
            "tenantId": binding.tenant_id,
            "matterId": binding.matter_id,
            "conversationId": binding.conversation_id,
            "sessionSelector": binding.session_selector,
        }

    @staticmethod
    def _valid_item(item: object, binding: ConversationBinding) -> bool:
        if not isinstance(item, Mapping):
            return False
        expected = Boto3DynamoConversationBindingStore._item(binding)
        return all(item.get(key) == value for key, value in expected.items())

    def bind(
        self,
        context: RequestContext,
        *,
        conversation_id: str,
        session_selector: str,
    ) -> ConversationBinding:
        if type(context) is not RequestContext or not context.is_server_derived():
            raise TypeError("context must be a server-built RequestContext")
        conversation = _selector(conversation_id, "conversation_id")
        session = _selector(session_selector, "session_selector")
        binding = ConversationBinding(
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            matter_id=context.matter_id,
            conversation_id=conversation,
            session_selector=session,
        )
        try:
            self.table.put_item(
                Item=self._item(binding),
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
            return binding
        except Exception:
            try:
                response = self.table.get_item(
                    Key={
                        "pk": conversation_binding_partition_key(context, conversation),
                        "sk": conversation_binding_sort_key(session),
                    },
                    ConsistentRead=True,
                )
            except Exception as exc:
                raise AuthorizationDenied("conversation access denied") from exc
            item = response.get("Item") if isinstance(response, Mapping) else None
            if self._valid_item(item, binding):
                return binding
            raise AuthorizationDenied("conversation access denied")

    def is_bound(
        self,
        *,
        context: RequestContext,
        conversation_id: str,
        session_selector: str,
    ) -> bool:
        if type(context) is not RequestContext or not context.is_server_derived():
            return False
        try:
            conversation = _selector(conversation_id, "conversation_id")
            session = _selector(session_selector, "session_selector")
            response = self.table.get_item(
                Key={
                    "pk": conversation_binding_partition_key(context, conversation),
                    "sk": conversation_binding_sort_key(session),
                },
                ConsistentRead=True,
            )
            item = response.get("Item") if isinstance(response, Mapping) else None
            return self._valid_item(
                item,
                ConversationBinding(
                    user_id=context.user_id,
                    tenant_id=context.tenant_id,
                    matter_id=context.matter_id,
                    conversation_id=conversation,
                    session_selector=session,
                ),
            )
        except Exception:
            return False


def derive_memory_scope_for_identity(
    identity: VerifiedIdentity,
    matter_selector: str,
    conversation_id: str,
    session_selector: str,
    authorization_store: AuthorizationStore,
    conversation_store: ConversationBindingStore,
    *,
    correlation_id: str | None = None,
) -> MemoryScope:
    """Authorize a caller before deriving its opaque Memory scope.

    ``matter_selector``, conversation, and session values are untrusted
    selectors. Effective tenant and user values are loaded only by
    ``build_request_context`` from the verified identity and authorization
    store; there are deliberately no client-supplied tenant/user parameters.
    """

    if not isinstance(identity, VerifiedIdentity):
        raise TypeError("identity must be verified")
    context = build_request_context(
        identity,
        matter_selector,
        authorization_store,
        correlation_id=correlation_id,
    )
    if not conversation_store.is_bound(
        context=context,
        conversation_id=conversation_id,
        session_selector=session_selector,
    ):
        raise AuthorizationDenied("conversation access denied")
    return MemoryScope.from_request_context(
        context,
        conversation_id=conversation_id,
        session_selector=session_selector,
        _factory_token=_SCOPE_FACTORY_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    event_id: str
    scope: MemoryScope
    role: str
    text: str
    event_timestamp: datetime


class ShortTermMemory(Protocol):
    def append_event(self, scope: MemoryScope, *, role: str, text: str) -> MemoryEvent: ...

    def list_events(self, scope: MemoryScope) -> tuple[MemoryEvent, ...]: ...


class MemoryPolicy:
    """Data-minimization policy: short-term only, with no long-term API."""

    long_term_enabled = False

    @classmethod
    def reject_long_term(cls, operation: str = "write") -> None:
        raise LongTermMemoryDisabled(
            f"long-term memory is disabled by policy; cannot {operation} memory"
        )

    @classmethod
    def validate_short_term_text(cls, text: object) -> str:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("memory event text must not be empty")
        if len(text) > MAX_EVENT_TEXT_LENGTH:
            raise ValueError("memory event text is too long")
        return text.strip()


class InMemoryShortTermMemory:
    """Deterministic local fake used to prove continuity and isolation."""

    def __init__(self) -> None:
        self._events: dict[tuple[str, str], list[MemoryEvent]] = {}

    def append_event(self, scope: MemoryScope, *, role: str, text: str) -> MemoryEvent:
        _validate_scope(scope)
        if role not in _ROLES:
            raise ValueError("role is unsupported")
        safe_text = MemoryPolicy.validate_short_term_text(text)
        event = MemoryEvent(
            event_id=str(uuid4()),
            scope=scope,
            role=role,
            text=safe_text,
            event_timestamp=datetime.now(timezone.utc),
        )
        self._events.setdefault((scope.actor_id, scope.session_id), []).append(event)
        return event

    def list_events(self, scope: MemoryScope) -> tuple[MemoryEvent, ...]:
        _validate_scope(scope)
        return tuple(self._events.get((scope.actor_id, scope.session_id), ()))


class AgentCoreMemoryClient:
    """Small data-plane adapter for short-term AgentCore Memory events."""

    def __init__(self, memory_id: str, *, client: Any | None = None) -> None:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise ValueError("memory_id is required")
        self.memory_id = memory_id
        if client is None:
            import boto3

            client = boto3.client("bedrock-agentcore")
        self.client = client

    def append_event(self, scope: MemoryScope, *, role: str, text: str) -> Mapping[str, Any]:
        _validate_scope(scope)
        if role not in _ROLES:
            raise ValueError("role is unsupported")
        safe_text = MemoryPolicy.validate_short_term_text(text)
        return self.client.create_event(
            memoryId=self.memory_id,
            actorId=scope.actor_id,
            sessionId=scope.session_id,
            eventTimestamp=datetime.now(timezone.utc),
            payload=[
                {
                    "conversational": {
                        "content": {"text": safe_text},
                        "role": role,
                    }
                }
            ],
            # Conversation text is short-term context only in LegalDesk.
            extractionMode="SKIP",
        )

    def list_events(self, scope: MemoryScope, **kwargs: Any) -> Mapping[str, Any]:
        _validate_scope(scope)
        if set(kwargs) - {"maxResults", "nextToken", "includePayloads"}:
            raise ValueError("unsupported event listing options")
        max_results = kwargs.get("maxResults")
        if max_results is not None and (
            isinstance(max_results, bool)
            or not isinstance(max_results, int)
            or not 1 <= max_results <= 100
        ):
            raise ValueError("maxResults must be an integer between 1 and 100")
        next_token = kwargs.get("nextToken")
        if next_token is not None and (
            not isinstance(next_token, str) or not next_token or len(next_token) > 2048
        ):
            raise ValueError("nextToken must be a non-empty bounded string")
        include_payloads = kwargs.get("includePayloads")
        if include_payloads is not None and not isinstance(include_payloads, bool):
            raise ValueError("includePayloads must be a boolean")
        return self.client.list_events(
            memoryId=self.memory_id,
            actorId=scope.actor_id,
            sessionId=scope.session_id,
            **kwargs,
        )

    @staticmethod
    def _event_id(event_id: object) -> str:
        if not isinstance(event_id, str) or _EVENT_ID.fullmatch(event_id) is None:
            raise ValueError("event_id must match the AgentCore event ID format")
        return event_id

    def get_event(self, scope: MemoryScope, event_id: str) -> Mapping[str, Any]:
        _validate_scope(scope)
        event = self._event_id(event_id)
        return self.client.get_event(
            memoryId=self.memory_id,
            actorId=scope.actor_id,
            sessionId=scope.session_id,
            eventId=event,
        )

    def delete_event(self, scope: MemoryScope, event_id: str) -> Mapping[str, Any]:
        _validate_scope(scope)
        event = self._event_id(event_id)
        return self.client.delete_event(
            memoryId=self.memory_id,
            actorId=scope.actor_id,
            sessionId=scope.session_id,
            eventId=event,
        )

    def retrieve_memory_records(self, *args: Any, **kwargs: Any) -> None:
        # Deliberately no long-term retrieval path exists in this phase.
        del args, kwargs
        MemoryPolicy.reject_long_term("retrieve")


def _validate_scope(scope: MemoryScope) -> None:
    if not isinstance(scope, MemoryScope):
        raise TypeError("memory operations require a server-derived MemoryScope")
    if type(scope) is not MemoryScope or not scope._is_sealed():
        raise TypeError("memory operations require a server-derived MemoryScope")
    _selector(scope.actor_id, "actor_id")
    _selector(scope.session_id, "session_id")
    _selector(scope.namespace.replace("/", "-"), "namespace")


__all__ = [
    "AgentCoreMemoryClient",
    "Boto3DynamoConversationBindingStore",
    "InMemoryShortTermMemory",
    "LongTermMemoryDisabled",
    "MEMORY_EVENT_EXPIRY_DAYS",
    "MemoryEvent",
    "MemoryPolicy",
    "MemoryScope",
    "MemoryScopeError",
    "ConversationBinding",
    "ConversationBindingStore",
    "InMemoryConversationBindingStore",
    "derive_memory_scope_for_identity",
    "conversation_binding_partition_key",
    "conversation_binding_sort_key",
]
