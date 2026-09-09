"""Deterministic request authorization and trusted context construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
from uuid import UUID, uuid4

from .domain.models import Matter, MatterStatus, User


class AuthorizationDenied(PermissionError):
    """Generic denial that avoids revealing cross-tenant resource existence."""


@dataclass(frozen=True, slots=True)
class VerifiedIdentity:
    """Identity claims after signature, issuer, audience, and expiry validation."""

    subject: str


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Server-derived context safe to pass to retrieval and tool boundaries."""

    correlation_id: str
    user_id: str
    tenant_id: str
    matter_id: str
    roles: frozenset[str]


class AuthorizationStore(Protocol):
    def get_user_by_subject(self, subject: str) -> User | None: ...

    def get_matter(self, matter_id: str) -> Matter | None: ...


@dataclass(slots=True)
class InMemoryAuthorizationStore:
    """Local fake used by Phase 00 tests; DynamoDB replaces it in later phases."""

    users_by_subject: Mapping[str, User]
    matters_by_id: Mapping[str, Matter]

    def get_user_by_subject(self, subject: str) -> User | None:
        return self.users_by_subject.get(subject)

    def get_matter(self, matter_id: str) -> Matter | None:
        return self.matters_by_id.get(matter_id)


def _normalize_correlation_id(value: str | None) -> str:
    if value is None:
        return str(uuid4())
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("correlation_id must be a UUID") from exc


def build_request_context(
    identity: VerifiedIdentity,
    requested_matter_id: str,
    store: AuthorizationStore,
    *,
    correlation_id: str | None = None,
) -> RequestContext:
    """Authorize a matter selector and return authoritative server-side scope.

    `requested_matter_id` is untrusted input. No tenant ID supplied by the
    browser is accepted. The tenant comes from the stored Matter only after the
    verified subject maps to a stored user and bilateral membership checks pass.
    """

    user = store.get_user_by_subject(identity.subject)
    matter = store.get_matter(requested_matter_id)

    if (
        user is None
        or matter is None
        or matter.status is not MatterStatus.ACTIVE
        or user.user_id not in matter.authorized_user_ids
        or matter.tenant_id not in user.tenant_ids
    ):
        raise AuthorizationDenied("access denied")

    return RequestContext(
        correlation_id=_normalize_correlation_id(correlation_id),
        user_id=user.user_id,
        tenant_id=matter.tenant_id,
        matter_id=matter.matter_id,
        roles=user.roles,
    )
