"""LegalDesk backend domain package."""

from .authorization import (
    AuthorizationDenied,
    InMemoryAuthorizationStore,
    RequestContext,
    VerifiedIdentity,
    build_request_context,
)

__all__ = [
    "AuthorizationDenied",
    "InMemoryAuthorizationStore",
    "RequestContext",
    "VerifiedIdentity",
    "build_request_context",
]
