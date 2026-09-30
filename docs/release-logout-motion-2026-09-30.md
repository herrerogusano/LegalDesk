# Logout and review-motion release — 2026-09-30

Scope: existing constrained fictional/public beta only. No RAG, prompts,
retrieval, Guardrails, IAM policies, document data, or model configuration changed.

## Changes

- Native review disclosures animate measured wrapper height without translating
  the title, with internal padding, interruption continuity and reduced-motion
  support. Review status updates preserve drafts/focus, show updating feedback,
  and animate the transfer to the resolved queue. Minor perceived motion remains
  a polish item noted during the owner's manual walkthrough, not a security gate.
- `POST /logout` retains CSRF protection, deletes the durable application session,
  clears its cookie, and returns a provider URL built only from server config.
  The browser then invokes Cognito `/logout` with `client_id` and the exact
  configured `PublicOrigin/logout` return URI. `GET /logout` only redirects home;
  it never invalidates a session. Failed POSTs remain visible instead of being
  masked by a navigation.
- API Gateway adds only the necessary `LogoutLandingRoute`. Existing
  `LogoutRoute` remains POST; retaining its logical ID avoids create/update
  conflicts. Cognito's existing allowed logout URI needs no change.

## Local evidence

- Focused logout URL, HTTP/CSRF/session, frontend, public IaC and API adapter:
  29 tests passed. Identity-isolation regression: 14 passed.
- Release packaging: 4 tests successful, including 1 platform-dependent skip.
  All 2,377 dependency input files compared against the prior deployed release
  manifest have identical SHA-256 values; runtime dependencies were not upgraded.
- Fresh offline Chromium journey passed at 375, 768, 1024, 1365 and 1440 pixels,
  including measured disclosure geometry, resolved-card rapid reversal,
  failed review update and failed logout, followed by successful logout.
- Document-view Node regression, JavaScript syntax checks, `git diff --check`,
  and `sam validate --lint` for the public-edge template passed.

## Deployment evidence

Application/frontend source commit: `dfa85e43cc24bbe1e152ccc76423b1950ed1259a`.

| Artifact | SHA-256 |
|---|---|
| Lambda zip | `d62bc81564ce878eefd29679a80126d398ed512dc9798bb4988085b01662108a` |
| Frontend zip | `32a8d3a81929230347c198e35640c5569c2f0060dcce12c5998f4847adc3e9c4` |

- `LegalDeskPhase14PublicEdge`, `eu-west-1`: `UPDATE_COMPLETE`.
- Reviewed change set `logout-motion-dfa85e43`: Lambda Code modification,
  GET logout route addition, dependent API IntegrationUri modification only;
  no replacement and no IAM change.
- Immutable Lambda version: `TSoCDD85EtPDArDp73KIC_wRkOXee.eX`, at
  `phase-14/dfa85e43cc24bbe1e152ccc76423b1950ed1259a/legaldesk-lambda.zip`.
- Lambda active/update successful; deployed CodeSha256 matches the artifact.
- CloudFront invalidation `I4419VO0J1WKUPVLKXL0OME5EK`: `Completed`.
  All four public static asset digests match the release files.
- Public GET logout landing returns to root; API exposes exact GET and POST
  logout routes.

## Real authentication-only evidence

The gated `tests/phase14_logout_auth_only.cjs` runner uses a temporary Cognito
identity without matter membership or business-data writes. Its transport
allowlist blocks chat, uploads, retrieval and tools. Credentials remain only in
process memory/environment, never reports, command arguments or Git.

Final result: `PASS`, local logout `200`, provider `/logout` observed,
post-logout `/api/me` denied, second login form visible, technical identity
deletion verified, `cleanupErrors=[]`, inference calls `0`.

Earlier attempts remain distinct: one launcher was blocked by a missing explicit
gate, and two auth checks failed during landing navigation. Adding closed-stage
diagnostics identified an evaluation race against the pre-logout document.
Waiting for the returned logged-out page before evaluating the session check
fixed the runner; no application changes or deployment retries were needed.
All temporary identities were deleted. The uncompleted second login's OAuth
state expires under the existing 10-minute application check and eventual TTL.

This evidence covers authentication only, not a new full real-provider RAG smoke.
Publication/auth requests and retained artifact versions can incur normal small
AWS charges; no new service capacity or recurring compute was added.

## Rollback

Previous immutable Lambda key:
`phase-14/58ecf162761680fe9c2f12dbfa0c365cf5894382/legaldesk-lambda.zip`;
version `iyY9N4gVK6.8nWZUP2sJawT5PTqMyPER`.

Previous frontend object versions in the existing frontend bucket:

| Key | Previous version |
|---|---|
| `index.html` | `jemksl7ZSvsa2T5wFdBPav9SL.5RmLZP` |
| `styles.css` | `Kl6YZWc4ZKJ._NphBYBXJVwZy9s.1SFy` |
| `app.js` | `em0oVYMLtyZGHR6vY.pbkI2HTmVQ44t.` |
| `citations.js` | `lWcsGy7Ph_RUCmXiGwM7Az8rZ8fU5.ZF` |

A rollback requires a reviewed change set and explicit restoration of these
frontend versions plus the five-path invalidation. Preserve shared data-plane
resources; do not delete user documents or stacks as a recovery mechanism.
