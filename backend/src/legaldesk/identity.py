"""Cognito/OIDC bearer-token verification at the trusted application edge.

The verifier is deliberately injectable: local tests provide a fake signing
key and never access a discovery endpoint. Production wiring may use the
PyJWT JWKS client, which handles key rotation without home-grown crypto.
"""

from __future__ import annotations

import time
import base64
import hashlib
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit
from typing import Any, Protocol

from .authorization import AuthorizationDenied, VerifiedIdentity, _IDENTITY_FACTORY_TOKEN


class IdentityVerificationError(AuthorizationDenied):
    """Generic authentication failure that reveals no token or claim data."""


def validate_https_endpoint(value: object, *, field_name: str) -> str:
    """Validate a configured public HTTPS endpoint before any network call.

    Endpoint configuration is an authority boundary: credentials, fragments,
    query parameters, reserved ``.invalid`` hosts, and malformed URLs must not
    reach a client constructor or redirect.  Local loopback doubles use their
    own explicit adapters and do not pass through this AWS configuration gate.
    """

    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field_name} must be an HTTPS URL")
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char == "\\" for char in value):
        raise ValueError(f"{field_name} contains invalid URL characters")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        has_credentials = parsed.username is not None or parsed.password is not None
    except ValueError as exc:
        raise ValueError(f"{field_name} is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not hostname
        or has_credentials
        or parsed.query
        or parsed.fragment
        or hostname.lower() == "example.invalid"
        or hostname.lower().endswith(".invalid")
    ):
        raise ValueError(f"{field_name} must be a real HTTPS URL without credentials, query, or fragment")
    return value


def create_cognito_logout_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    logout_uri: str,
) -> str:
    """Build Cognito's hosted-UI logout URL from trusted server config.

    Cognito's logout endpoint is the same origin as the authorization endpoint,
    but uses the fixed ``/logout`` path.  The return URI is deliberately passed
    by the server and validated as an exact application landing URI; callers
    must not copy it from browser input.
    """

    endpoint = validate_https_endpoint(
        authorization_endpoint, field_name="authorization_endpoint"
    )
    if (
        not isinstance(client_id, str)
        or not client_id.strip()
        or any(ord(char) < 0x21 or ord(char) > 0x7E for char in client_id)
    ):
        raise ValueError("client_id is invalid")
    if not isinstance(logout_uri, str) or not logout_uri.strip():
        raise ValueError("logout_uri is required")
    try:
        parsed = urlsplit(logout_uri)
    except ValueError as exc:
        raise ValueError("logout_uri is malformed") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/logout"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("logout_uri must be the exact /logout URI")
    auth = urlsplit(endpoint)
    provider_endpoint = urlunsplit((auth.scheme, auth.netloc, "/logout", "", ""))
    return f"{provider_endpoint}?{urlencode({'client_id': client_id, 'logout_uri': logout_uri})}"


def pkce_code_challenge(code_verifier: str) -> str:
    """Return the RFC 7636 S256 challenge for a high-entropy verifier."""

    if (
        not isinstance(code_verifier, str)
        or not 43 <= len(code_verifier) <= 128
        or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" for char in code_verifier)
    ):
        raise ValueError("code_verifier is invalid")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class PkceAuthorizationRequest:
    code_verifier: str
    code_challenge: str
    state: str
    authorization_url: str


def create_pkce_authorization_request(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    scope: str = "openid legaldesk/use",
    state: str | None = None,
    code_verifier: str | None = None,
) -> PkceAuthorizationRequest:
    """Build a public-client Authorization Code + PKCE request."""

    authorization_endpoint = validate_https_endpoint(
        authorization_endpoint, field_name="authorization_endpoint"
    )
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id is required")
    if not isinstance(redirect_uri, str) or not redirect_uri.strip():
        raise ValueError("redirect_uri is required")
    verifier = secrets.token_urlsafe(64) if code_verifier is None else code_verifier
    challenge = pkce_code_challenge(verifier)
    request_state = secrets.token_urlsafe(32) if state is None else state
    if not isinstance(request_state, str) or not request_state:
        raise ValueError("state is invalid")
    query = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": request_state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return PkceAuthorizationRequest(verifier, challenge, request_state, f"{authorization_endpoint}?{query}")


class SigningKeyResolver(Protocol):
    def get_signing_key(self, token: str) -> Any: ...


class PyJwtJwksKeyResolver:
    """PyJWT JWKS resolver with standard OIDC key rotation support."""

    def __init__(self, jwks_url: str, *, cache_jwk_set: bool = True) -> None:
        jwks_url = validate_https_endpoint(jwks_url, field_name="jwks_url")
        try:
            from jwt import PyJWKClient
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise RuntimeError("PyJWT[crypto] is required for OIDC verification") from exc
        self._client = PyJWKClient(jwks_url, cache_jwk_set=cache_jwk_set)

    def get_signing_key(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


@dataclass(frozen=True, slots=True)
class OidcVerifierConfig:
    issuer: str
    audience: str | None = None
    client_id: str | None = None
    required_scope: str | None = None
    allowed_token_use: frozenset[str] = frozenset({"access", "id"})
    leeway_seconds: int = 30

    def __post_init__(self) -> None:
        validate_https_endpoint(self.issuer, field_name="issuer")
        if self.audience is None and self.client_id is None:
            raise ValueError("audience or client_id is required")
        if self.audience is not None and (
            not isinstance(self.audience, str) or not self.audience.strip()
        ):
            raise ValueError("audience is invalid")
        if self.client_id is not None and (
            not isinstance(self.client_id, str) or not self.client_id.strip()
        ):
            raise ValueError("client_id is invalid")
        if self.required_scope is not None and (
            not isinstance(self.required_scope, str) or not self.required_scope.strip()
        ):
            raise ValueError("required_scope is invalid")
        if not self.allowed_token_use or not self.allowed_token_use <= frozenset({"access", "id"}):
            raise ValueError("allowed_token_use is invalid")
        if "access" in self.allowed_token_use and self.client_id is None:
            raise ValueError("client_id is required for access tokens")
        if "id" in self.allowed_token_use and self.audience is None:
            raise ValueError("audience is required for ID tokens")
        if (
            isinstance(self.leeway_seconds, bool)
            or not isinstance(self.leeway_seconds, int)
            or not 0 <= self.leeway_seconds <= 300
        ):
            raise ValueError("leeway_seconds is invalid")


class OidcTokenVerifier:
    """Verify Cognito/OIDC JWTs and return a trusted ``VerifiedIdentity``."""

    def __init__(self, config: OidcVerifierConfig, key_resolver: SigningKeyResolver) -> None:
        self.config = config
        self.key_resolver = key_resolver

    @staticmethod
    def _jwt() -> Any:
        try:
            import jwt
        except ImportError as exc:  # pragma: no cover - dependency boundary
            raise RuntimeError("PyJWT[crypto] is required for OIDC verification") from exc
        return jwt

    def verify_authorization_header(self, authorization: object) -> VerifiedIdentity:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise IdentityVerificationError("access denied")
        token = authorization[7:].strip()
        if not token:
            raise IdentityVerificationError("access denied")
        return self.verify_token(token)

    def verify_token(self, token: str) -> VerifiedIdentity:
        jwt = self._jwt()
        try:
            if not isinstance(token, str) or not token:
                raise ValueError("token")
            header = jwt.get_unverified_header(token)
            if (
                not isinstance(header, dict)
                or header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not header["kid"]
            ):
                raise ValueError("header")
            key = self.key_resolver.get_signing_key(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
            if not isinstance(unverified, dict):
                raise ValueError("claims")
            token_use = unverified.get("token_use")
            if token_use not in self.config.allowed_token_use:
                raise ValueError("token_use")
            if token_use == "id" and self.config.audience is None:
                raise ValueError("audience")
            # Cognito access tokens use client_id; ID tokens use aud.
            expected_audience = self.config.audience
            verify_audience = expected_audience is not None and token_use == "id"
            claims = jwt.decode(
                token,
                key=key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                audience=expected_audience if verify_audience else None,
                options={
                    "require": ["sub", "iss", "exp", "iat"],
                    "verify_aud": verify_audience,
                },
                leeway=self.config.leeway_seconds,
            )
            if not isinstance(claims, dict):
                raise ValueError("claims")
            subject = claims.get("sub")
            if not isinstance(subject, str) or not subject.strip() or len(subject) > 256:
                raise ValueError("sub")
            if token_use == "access" and claims.get("client_id") != self.config.client_id:
                raise ValueError("client_id")
            if token_use == "access" and self.config.audience is not None:
                # Access tokens have no aud in Cognito; if present, reject a
                # mismatch rather than treating a foreign audience as valid.
                aud = claims.get("aud")
                if aud is not None and aud != self.config.audience:
                    raise ValueError("aud")
            scope = claims.get("scope", "")
            if not isinstance(scope, str):
                raise ValueError("scope")
            scopes = frozenset(item for item in scope.split() if item)
            if self.config.required_scope is not None and self.config.required_scope not in scopes:
                raise ValueError("scope")
            now = time.time()
            iat = claims.get("iat")
            if iat is not None and (
                isinstance(iat, bool)
                or not isinstance(iat, (int, float))
                or iat > now + self.config.leeway_seconds
            ):
                raise ValueError("iat")
            return VerifiedIdentity._from_verified_claims(
                subject=subject,
                issuer=self.config.issuer,
                client_id=claims.get("client_id") if isinstance(claims.get("client_id"), str) else None,
                token_use=token_use,
                scopes=scopes,
                _factory_token=_IDENTITY_FACTORY_TOKEN,
            )
        except IdentityVerificationError:
            raise
        except Exception as exc:
            raise IdentityVerificationError("access denied") from exc


__all__ = [
    "create_cognito_logout_url",
    "IdentityVerificationError",
    "PkceAuthorizationRequest",
    "OidcTokenVerifier",
    "OidcVerifierConfig",
    "PyJwtJwksKeyResolver",
    "SigningKeyResolver",
    "create_pkce_authorization_request",
    "pkce_code_challenge",
    "validate_https_endpoint",
]
