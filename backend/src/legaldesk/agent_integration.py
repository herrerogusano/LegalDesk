"""Trusted application binding for managed Harness tool transport.

The browser supplies neither the bearer token nor the Gateway tool
configuration.  The application creates this binding only after OIDC
verification, matter authorization, and conversation/Memory binding.  The
agent package consumes it through its sealed adapter boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Protocol
from uuid import UUID, uuid4

from .authorization import (
    AuthorizationDenied,
    AuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
)
from .gateway_interceptor import (
    GATEWAY_INVOCATION_TTL_SECONDS,
    GatewayGrantRepository,
    HarnessInvocationGrant,
)
from .memory import (
    ConversationBindingStore,
    MemoryScope,
    derive_memory_scope_for_identity,
)


_BINDING_FACTORY_TOKEN = object()
_APPLICATION_TOOL_ALLOWLISTS = {
    "metadata": (
        "@legaldesk_gateway/metadata-mcp___list_matter_documents",
        "@legaldesk_gateway/metadata-mcp___get_document_metadata",
    ),
    "review": (
        "@legaldesk_gateway/review-task-lambda___create_review_task",
        "@legaldesk_gateway/review-task-lambda___list_review_tasks",
        "@legaldesk_gateway/review-task-lambda___get_review_task",
        "@legaldesk_gateway/review-task-lambda___update_review_task",
    ),
}


@dataclass(frozen=True, slots=True, init=False)
class HarnessInvocationBinding:
    """Opaque, server-owned identity/matter/token binding for one request."""

    identity: VerifiedIdentity
    context: RequestContext
    memory_scope: MemoryScope
    gateway_url: str
    invocation_id: str
    allowed_tools: tuple[str, ...]
    bearer_token: str = field(repr=False)
    _seal: object

    @classmethod
    def _from_coherent_request(
        cls,
        identity: VerifiedIdentity,
        context: RequestContext,
        memory_scope: MemoryScope,
        *,
        bearer_token: str,
        gateway_url: str,
        invocation_id: str,
        allowed_tools: tuple[str, ...],
    ) -> "HarnessInvocationBinding":
        if type(identity) is not VerifiedIdentity or not identity.is_trusted():
            raise TypeError("identity must be verified")
        if type(context) is not RequestContext or not context.is_server_derived():
            raise AuthorizationDenied("access denied")
        if type(memory_scope) is not MemoryScope or not memory_scope._is_sealed():
            raise TypeError("memory_scope must be server-derived")
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise ValueError("bearer_token is required")
        if any(char in bearer_token for char in "\r\n") or len(bearer_token) > 16_384:
            raise ValueError("bearer_token is invalid")
        if not isinstance(gateway_url, str) or not gateway_url.startswith("https://"):
            raise ValueError("gateway_url must be HTTPS")
        try:
            invocation_id = str(UUID(invocation_id))
        except (ValueError, AttributeError) as exc:
            raise ValueError("invocation_id must be a UUID") from exc
        if (
            not isinstance(allowed_tools, tuple)
            or not allowed_tools
            or any(tool not in {item for items in _APPLICATION_TOOL_ALLOWLISTS.values() for item in items} for tool in allowed_tools)
        ):
            raise ValueError("allowed_tools is invalid")
        instance = object.__new__(cls)
        for name, value in {
            "identity": identity,
            "context": context,
            "memory_scope": memory_scope,
            "gateway_url": gateway_url,
            "invocation_id": invocation_id,
            "allowed_tools": allowed_tools,
            "bearer_token": bearer_token,
            "_seal": _BINDING_FACTORY_TOKEN,
        }.items():
            object.__setattr__(instance, name, value)
        return instance

    def _is_sealed(self) -> bool:
        return self._seal is _BINDING_FACTORY_TOKEN

    def gateway_headers(self) -> dict[str, str]:
        """Build fixed Gateway headers for deterministic backend actions."""

        if not self._is_sealed():
            raise TypeError("binding must be server-derived")
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "x-legaldesk-requested-matter-id": self.matter_id,
            "x-legaldesk-correlation-id": str(UUID(self.correlation_id)),
            "x-legaldesk-invocation-id": str(UUID(self.invocation_id)),
            "x-legaldesk-memory-actor-id": self.memory_scope.actor_id,
            "x-legaldesk-memory-session-id": self.memory_scope.session_id,
        }

    @property
    def verified_subject(self) -> str:
        return self.identity.subject

    @property
    def matter_id(self) -> str:
        return self.context.matter_id

    @property
    def correlation_id(self) -> str:
        return self.context.correlation_id

    def __repr__(self) -> str:
        return (
            "HarnessInvocationBinding("
            f"subject={self.identity.subject!r}, matter_id={self.context.matter_id!r}, "
            f"correlation_id={self.context.correlation_id!r}, invocation_id={self.invocation_id!r}, "
            f"gateway_url={self.gateway_url!r}, bearer_token=<redacted>)"
        )


def bind_harness_invocation(
    *,
    bearer_token: str,
    gateway_url: str,
    identity_verifier: "AuthorizationHeaderVerifier",
    requested_matter_id: str,
    conversation_id: str,
    session_selector: str,
    authorization_store: AuthorizationStore,
    conversation_store: ConversationBindingStore,
    invocation_repository: GatewayGrantRepository,
    correlation_id: str | None = None,
    application_action: str = "metadata",
) -> HarnessInvocationBinding:
    """Verify and derive one coherent application Harness invocation.

    The caller supplies only an opaque bearer token and untrusted selectors.
    OIDC verification, matter authorization, conversation binding, Memory
    derivation, and invocation-record issuance happen in this factory. This
    prevents independently supplied identity/context/scope values from being
    combined into a privileged request.
    """

    if application_action not in _APPLICATION_TOOL_ALLOWLISTS:
        raise ValueError("application_action is invalid")
    if not isinstance(bearer_token, str) or not bearer_token.strip():
        raise AuthorizationDenied("access denied")
    try:
        identity = identity_verifier.verify_authorization_header(
            f"Bearer {bearer_token}"
        )
    except Exception as exc:
        raise AuthorizationDenied("access denied") from exc
    if type(identity) is not VerifiedIdentity or not identity.is_trusted():
        raise AuthorizationDenied("access denied")
    try:
        context = build_request_context(
            identity,
            requested_matter_id,
            authorization_store,
            correlation_id=correlation_id,
        )
        memory_scope = derive_memory_scope_for_identity(
            identity,
            requested_matter_id,
            conversation_id,
            session_selector,
            authorization_store,
            conversation_store,
            correlation_id=context.correlation_id,
        )
        invocation_id = str(uuid4())
        invocation_repository.put_invocation(
            HarnessInvocationGrant(
                invocation_id=invocation_id,
                verified_subject=identity.subject,
                requested_matter_id=context.matter_id,
                correlation_id=context.correlation_id,
                memory_actor_id=memory_scope.actor_id,
                memory_session_id=memory_scope.session_id,
                allowed_tools=_APPLICATION_TOOL_ALLOWLISTS[application_action],
                expires_at=int(time.time()) + GATEWAY_INVOCATION_TTL_SECONDS,
            )
        )
    except AuthorizationDenied:
        raise
    except Exception as exc:
        raise AuthorizationDenied("access denied") from exc

    return HarnessInvocationBinding._from_coherent_request(
        identity,
        context,
        memory_scope,
        bearer_token=bearer_token,
        gateway_url=gateway_url,
        invocation_id=invocation_id,
        allowed_tools=_APPLICATION_TOOL_ALLOWLISTS[application_action],
    )


class AuthorizationHeaderVerifier(Protocol):
    def verify_authorization_header(self, authorization: object) -> VerifiedIdentity: ...


__all__ = [
    "AuthorizationHeaderVerifier",
    "HarnessInvocationBinding",
    "bind_harness_invocation",
]
