"""Deterministic request authorization and trusted context construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
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


def authorization_user_partition_key(subject: str) -> str:
    """Key convention for User records in the shared metadata table."""

    return f"AUTH#USER#{subject}"


def authorization_matter_partition_key(matter_id: str) -> str:
    """Key convention for Matter records in the shared metadata table."""

    return f"AUTH#MATTER#{matter_id}"


def authorization_profile_sort_key() -> str:
    return "PROFILE"


class Boto3DynamoAuthorizationStore:
    """Read-only authorization adapter for the existing Phase 02 table.

    This adapter deliberately performs no writes. Malformed records are
    treated as absent so authorization fails closed.
    """

    def __init__(self, table_name: str, *, table: Any | None = None) -> None:
        if not isinstance(table_name, str) or not table_name.strip():
            raise ValueError("table_name is required")
        self.table_name = table_name
        if table is None:
            import boto3

            table = boto3.resource("dynamodb").Table(table_name)
        self.table = table

    def _get(self, key: Mapping[str, str]) -> Mapping[str, object] | None:
        response = self.table.get_item(Key=dict(key), ConsistentRead=True)
        item = response.get("Item") if isinstance(response, Mapping) else None
        return item if isinstance(item, Mapping) else None

    @staticmethod
    def _strings(value: object) -> frozenset[str] | None:
        if not isinstance(value, (list, tuple, set, frozenset)):
            return None
        if any(not isinstance(item, str) or not item.strip() for item in value):
            return None
        return frozenset(value)

    def get_user_by_subject(self, subject: str) -> User | None:
        try:
            item = self._get(
                {
                    "pk": authorization_user_partition_key(subject),
                    "sk": authorization_profile_sort_key(),
                }
            )
            if item is None or item.get("entityType") != "User":
                return None
            user_id = item.get("userId")
            stored_subject = item.get("verifiedSubject", subject)
            tenant_ids = self._strings(item.get("tenantIds"))
            roles = self._strings(item.get("roles"))
            if (
                not isinstance(user_id, str)
                or not user_id.strip()
                or stored_subject != subject
                or tenant_ids is None
                or roles is None
            ):
                return None
            return User(user_id, stored_subject, tenant_ids, roles)
        except Exception:
            return None

    def get_matter(self, matter_id: str) -> Matter | None:
        try:
            item = self._get(
                {
                    "pk": authorization_matter_partition_key(matter_id),
                    "sk": authorization_profile_sort_key(),
                }
            )
            if item is None or item.get("entityType") != "Matter":
                return None
            tenant_id = item.get("tenantId")
            stored_matter_id = item.get("matterId", matter_id)
            name = item.get("name")
            users = self._strings(item.get("authorizedUserIds"))
            if (
                not isinstance(tenant_id, str)
                or not tenant_id.strip()
                or stored_matter_id != matter_id
                or not isinstance(name, str)
                or not name.strip()
                or users is None
            ):
                return None
            return Matter(
                matter_id=stored_matter_id,
                tenant_id=tenant_id,
                name=name,
                authorized_user_ids=users,
                status=MatterStatus(item.get("status", MatterStatus.ACTIVE)),
            )
        except Exception:
            return None


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
