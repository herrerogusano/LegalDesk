# Phase 14 public edge (local IaC tranche)

`infra/cloudformation/phase-14-public-edge.yaml` is a deployment definition,
not a deployment. It is intentionally parameterized around the existing
Phase 02 source bucket and metadata table, Phase 03 knowledge base, Phase 06
Guardrail, Phase 08 Gateway/identity dependencies, Phase 09 Memory, and Phase
01 Harness. It creates no replacement data plane for those resources.

The stack owns only:

- a private, encrypted, versioned S3 frontend bucket and CloudFront Origin
  Access Control (OAC);
- a CloudFront distribution with an S3 default behavior and uncached dynamic
  behaviors for exactly `/login`, `/callback`, `/logout`, and `/api*`;
- an API Gateway HTTP API v2, `$default` auto-deploy stage, bounded throttles,
  and the five exact routes `/login`, `/callback`, `/logout`, `/api`, and
  `/api/{proxy+}`;
- a version-pinned `python3.12` Lambda using
  `legaldesk.api_gateway.lambda_handler`; and
- retained, bounded-retention API access and Lambda log groups.

## Edge and application contract

CloudFront is the only intended public path to the API. It overwrites the
`X-LegalDesk-Trusted-Edge` origin header with the NoEcho
`TrustedEdgeSecret`; the Lambda receives the same value through
`LEGALDESK_TRUSTED_EDGE_VALUE`, compares it in constant time, and strips it
before WSGI business handling. A direct `execute-api` request therefore fails
the existing adapter's trusted-edge check. The API origin host is derived from
the HTTP API ID and is placed in `LEGALDESK_ALLOWED_HOSTS`; it is deliberately
different from the browser `PublicOrigin`/`LEGALDESK_ALLOWED_ORIGINS`.

The response headers policy provides HSTS, `nosniff`, `DENY` framing,
`strict-origin-when-cross-origin`, a restrictive baseline CSP, and a narrow
Permissions-Policy. `PresignedUploadOrigin` is the one exact source-bucket
HTTPS origin used in `connect-src`; no wildcard upload host is accepted. The application adapter
keeps its one-megabyte body limit and generic error responses. API Gateway and
Lambda are additionally bounded by the stage throttles and 29-second
integration/function timeouts.

CloudFront standard access logging is deliberately disabled: AWS documents
that it records the full query string, which would persist the OAuth
authorization code received at `/callback`. API Gateway instead emits a
field-selected access record containing only request ID, matched route key,
status, response size/latency, and integration status. It excludes raw path,
query, headers, cookies, IP, and user agent. Both that log group and the Lambda
log group use `LogRetentionDays` (default 30). CloudFront service metrics and
the application's redacted allowlist remain available without retaining the
callback secret.

## Immutable Lambda artifact

The template accepts only an existing artifact bucket, immutable key, and
object version; it does not upload code or create an artifact bucket. Artifact
assembly and dependency pinning remain a separate release-engineering task,
outside this local edge tranche.

## Deterministic two-step identity/CORS bootstrap

Avoid a circular dependency between the new edge hostname and existing Phase
10 Cognito callback/logout URLs or Phase 02 bucket CORS:

1. In a reviewed change set, deploy this stack with an approved, real HTTPS
   `PublicOrigin` placeholder (not `.invalid`) and no `ViewerDomainName` (the distribution's
   default hostname is used). Do not send user traffic during this step.
2. Read the stack output `CloudFrontDistributionDomainName`. Update this stack
   with `PublicOrigin=https://<that exact output>`; then update the existing
   Phase 10 `CallbackUrls`/`LogoutUrls` to the exact `/callback` and `/logout`
   paths and update Phase 02 `ApplicationOrigin` to the exact origin. Verify
   both change sets before enabling traffic.

Only after that bootstrap may an approved custom hostname be introduced:
provide a validated us-east-1 ACM certificate and `ViewerDomainName`, update
`PublicOrigin` to the custom HTTPS origin, then repeat the Phase 10/02 updates.
The stack output remains the source of truth for the API host/origin split.

## Deployment and rollback gate

This file and its tests are local-only. Before any deployment approval, review
the exact parameter values against `docs/phase-13-application-access.md`,
confirm that all catalog entries are fictional/public beta matters, inspect
the NoEcho marker handling, and verify the Lambda zip digest. Keep the prior
immutable Lambda S3 version and CloudFormation change set available for
rollback. Roll back the distribution/API/Lambda stack to the prior reviewed
version, then restore the prior Phase 10/02 origin values if the bootstrap
change was already applied. Teardown is not a default operation: frontend and
log buckets are retained, and existing data-plane stacks are never deleted by
this template.

The template intentionally contains no EventBridge reconciliation scheduler,
malware event rule, public signup resource, WAF, or real-data migration; those
remain separate approval gates.
