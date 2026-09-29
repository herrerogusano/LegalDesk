# Phase 10 acceptance — identity and isolation

Status: implementation, AWS deployment, and synthetic smoke complete in
`eu-west-1`.

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

## AWS deployment evidence

Deployment was performed in account `344774635844`, region `eu-west-1`:

- `LegalDeskPhase10Identity`: `CREATE_COMPLETE`; change set added only
  `LegalDeskPublicClient` (`AWS::Cognito::UserPoolClient`). Public client ID:
  `101hke40t7n5easmh7i3g9265o`. The live client reports OAuth flow `code`,
  scopes `openid` and `legaldesk/use`, and `AllowedOAuthFlowsUserPoolClient=true`;
  the template sets `GenerateSecret=false`.
- `LegalDeskPhase08Gateway`: `UPDATE_COMPLETE`; the update change set listed
  `Replacement=False` for the Gateway, both Lambda functions, the Gateway role,
  and the MCP target. The Gateway ARN remained
  `arn:aws:bedrock-agentcore:eu-west-1:344774635844:gateway/legaldeskgatewayphase08-f17ddi2woq`.
- Clean, versioned artifacts were uploaded to the retained Phase 08 bucket:
  `phase-10/interceptor.zip` version
  `n_XIXaJDfdnrhbzG.4UMhF8_ug.eCksQ` and `phase-10/metadata-mcp.zip` version
  `yMzZXCqPkw_U4g0pZmMh5GEE4qs5hOGI`. Existing Phase 08 objects were not
  overwritten or deleted.

## Synthetic authorization smoke

Using the existing fictional M2M client and seeded fictional metadata only:

| Check | Result |
|---|---|
| Gateway call for `mat_sundial` with interceptor-owned grant | HTTP 200; returned `Synthetic notice.pdf` metadata |
| Same token requesting `mat_glacier` | HTTP 403; `access denied` |
| Direct metadata Function URL without IAM/grant | HTTP 403; `Forbidden` |

No Harness or model inference was used for this smoke. The direct endpoint
remains IAM-protected, while the Gateway performs JWT plus server-side
matter authorization.

The deployed resources and artifact/Lambda/Gateway requests can incur AWS
charges; retained resources remain subject to the project cost policy. The
public Cognito client itself adds no secret and no new UserPool/table. Cleanup
of the public client, phase-10 artifact versions, and retained Phase 08
resources is a future teardown decision. The operational audit view, traces,
metrics, dashboards, and broader deployment automation remain Phase 11 work.
