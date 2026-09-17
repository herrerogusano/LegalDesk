"""Deterministic request authorization and trusted context construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from uuid import UUID, uuid4

from .domain.models import Matter, MatterStatus, User


class AuthorizationDenied(PermissionError):
    """Generic denial that avoids revealing cross-tenant resource existence."""


_IDENTITY_FACTORY_TOKEN = object()
_CONTEXT_FACTORY_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class VerifiedIdentity:
    """Identity claims after signature, issuer, audience, and expiry validation."""

    subject: str
    issuer: str | None
    client_id: str | None
    token_use: str | None
    scopes: frozenset[str]
    _trust: object

    def __init__(self, subject: str) -> None:
        """Reject free-form identities at production boundaries.

        This constructor intentionally creates an untrusted value. Production
        adapters must use the capability-gated factories in this module.
        """

        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject is required")
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "issuer", None)
        object.__setattr__(self, "client_id", None)
        object.__setattr__(self, "token_use", None)
        object.__setattr__(self, "scopes", frozenset())
        object.__setattr__(self, "_trust", None)

    @classmethod
    def _from_verified_claims(
        cls,
        *,
        subject: str,
        issuer: str,
        client_id: str | None,
        token_use: str | None,
        scopes: frozenset[str],
        _factory_token: object,
    ) -> "VerifiedIdentity":
        if _factory_token is not _IDENTITY_FACTORY_TOKEN:
            raise TypeError("identity must be created by a verified adapter")
        if not isinstance(subject, str) or not subject.strip():
            raise ValueError("subject is required")
        if not isinstance(issuer, str) or not issuer.strip():
            raise ValueError("issuer is required")
        instance = object.__new__(cls)
        object.__setattr__(instance, "subject", subject)
        object.__setattr__(instance, "issuer", issuer)
        object.__setattr__(instance, "client_id", client_id)
        object.__setattr__(instance, "token_use", token_use)
        object.__setattr__(instance, "scopes", frozenset(scopes))
        object.__setattr__(instance, "_trust", _IDENTITY_FACTORY_TOKEN)
        return instance

    def is_trusted(self) -> bool:
        return self._trust is _IDENTITY_FACTORY_TOKEN

def _gateway_identity_from_verified_subject(subject: str) -> VerifiedIdentity:
    """Internal Gateway adapter boundary after CUSTOM_JWT verification."""
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("subject is required")
    return VerifiedIdentity._from_verified_claims(
        subject=subject,
        issuer="gateway-custom-jwt",
        client_id=None,
        token_use=None,
        scopes=frozenset(),
        _factory_token=_IDENTITY_FACTORY_TOKEN,
    )

@dataclass(frozen=True, slots=True, init=False)
class RequestContext:
    """Server-derived context safe to pass to retrieval and tool boundaries."""

    correlation_id: str
    user_id: str
    tenant_id: str
    matter_id: str
    roles: frozenset[str]
    _seal: object

    def __init__(
        self,
        correlation_id: str,
        user_id: str,
        tenant_id: str,
        matter_id: str,
        roles: frozenset[str],
    ) -> None:
        """Create an untrusted value; sensitive seams require the factory seal."""

        object.__setattr__(self, "correlation_id", correlation_id)
        object.__setattr__(self, "user_id", user_id)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "matter_id", matter_id)
        object.__setattr__(self, "roles", roles)
        object.__setattr__(self, "_seal", None)

    @classmethod
    def _from_authorized(
        cls,
        *,
        correlation_id: str,
        user_id: str,
        tenant_id: str,
        matter_id: str,
        roles: frozenset[str],
        _factory_token: object,
    ) -> "RequestContext":
        if _factory_token is not _CONTEXT_FACTORY_TOKEN:
            raise TypeError("RequestContext must be created by the authorization boundary")
        instance = object.__new__(cls)
        for name, value in {
            "correlation_id": correlation_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "matter_id": matter_id,
            "roles": roles,
            "_seal": _CONTEXT_FACTORY_TOKEN,
        }.items():
            object.__setattr__(instance, name, value)
        return instance

    def is_server_derived(self) -> bool:
        return self._seal is _CONTEXT_FACTORY_TOKEN


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

    if type(identity) is not VerifiedIdentity or not identity.is_trusted():
        raise AuthorizationDenied("access denied")
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

    return RequestContext._from_authorized(
        correlation_id=_normalize_correlation_id(correlation_id),
        user_id=user.user_id,
        tenant_id=matter.tenant_id,
        matter_id=matter.matter_id,
        roles=user.roles,
        _factory_token=_CONTEXT_FACTORY_TOKEN,
    )


def require_authorized_context(context: object) -> RequestContext:
    """Return only the exact sealed context produced by authorization."""

    if type(context) is not RequestContext or not context.is_server_derived():
        raise AuthorizationDenied("access denied")
    return context
