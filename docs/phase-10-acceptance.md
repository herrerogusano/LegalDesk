# Phase 10 acceptance — identity and isolation

Status: local implementation complete; AWS deployment and smoke are intentionally
not run in Phase 10.

This phase adds reusable Cognito/OIDC verification, server-derived request
identity, exact conversation bindings, and an opaque short-lived grant for MCP.

| Boundary | Required authorization | Cross-matter / forged selector result |
|---|---|---|
| Upload and list | verified subject + bilateral tenant/matter membership | deny before storage/repository |
| Retrieval and citations | server `RequestContext`; results rechecked | drop/deny foreign matter |
| Review Lambda | exact Gateway grant, then membership reauthorization | deny forged, expired, or mismatched grant |
| MCP metadata | exact Gateway grant bound to tool, subject, matter, TTL | ignore free headers; deny |
| Memory/conversation | exact durable binding for user/tenant/matter/session | deny unknown or cross-owner pair |
| Direct Harness endpoint | IAM-only service path and typed server scope | browser IDs are not accepted |

The OIDC verifier uses the versioned core PyJWT[crypto] dependency with JWKS key resolution and requires
RS256, `kid`, issuer, `sub`, expiry, and the appropriate `aud` (ID token) or
`client_id`/`token_use`/scope (access token). Failures return a generic denial;
tokens and claims are never logged. AgentCore `CUSTOM_JWT` remains a trusted
signature boundary, but the interceptor reauthorizes matter membership.
Grants are short-lived and replayable within their five-minute TTL; review
idempotency constrains duplicate writes.

The public client flow uses the RFC 7636 S256 helper in
`legaldesk.identity`: it generates a verifier and state, derives the challenge,
and constructs an Authorization Code authorize URL. The verifier is retained
only by the client until token exchange.

`infra/cloudformation/phase-10-identity.yaml` creates only a public Cognito
Authorization Code + PKCE client and reuses the Phase 08 UserPool. The Phase 08
Gateway template accepts an optional additional client while preserving M2M.
No Phase 10 deployment or AWS smoke has been run. The operational audit view,
traces, metrics, and dashboards remain Phase 11 work.
