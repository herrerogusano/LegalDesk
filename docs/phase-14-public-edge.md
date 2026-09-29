# Phase 14 public edge

`infra/cloudformation/phase-14-public-edge.yaml` is the deployment definition
used by the public-beta stack. It is intentionally parameterized around the
existing Phase 02 source bucket and metadata table, Phase 03 knowledge base, Phase 06
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
HTTPS origin used in `connect-src`; no wildcard upload host is accepted. Upload
authorization presigns the server-validated byte count as a SigV4
`content-length` header; the browser supplies that header from the selected
file and the frontend does not override it. Confirmation still performs an
exact `HeadObject` size check. The application adapter keeps its one-megabyte
body limit and generic error responses. API Gateway and
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

## Immutable release artifacts

`scripts/package_release.py` is the only release packager for this tranche. It
does not invoke AWS, pip, or a network client. Given a dependency directory
prepared externally on Linux, it creates:

- `legaldesk-lambda.zip`, containing the `legaldesk/` backend package, the
  `legaldesk_agent/` package, `prompts/legaldesk-system.md`, and the prepared
  runtime dependencies. The same zip is valid for the application Lambda and
  the malware corroboration Lambda because it contains
  `legaldesk.malware_scan_lambda.lambda_handler`.
- `legaldesk-frontend.zip`, containing exactly `index.html`, `styles.css`,
  `app.js`, and `citations.js`.
- `legaldesk-release-manifest.json`, with per-file and whole-artifact
  SHA-256 digests.

The archive writer sorts paths, fixes timestamps to the ZIP epoch, uses fixed
permissions, and rejects symlinks, duplicate archive paths, unsafe paths,
caches, tests, and secret-looking files. The output directory must be treated
as a build output and is not part of either artifact.

The dependency input is pinned in
`packaging/constraints-python312-manylinux-x86_64.txt`. In a clean Linux
x86_64/Python 3.12 environment, a release operator may prepare it with
`pip download --only-binary=:all: --platform manylinux_2_17_x86_64
--implementation cp --python-version 3.12` followed by an offline
`pip install --no-index --find-links ... --target ... -r` using that exact
constraints file. The packager itself must then be run offline:

```text
python3.12 scripts/package_release.py \
  --dependency-root /opt/legaldesk-python312-deps \
  --output-dir dist/legaldesk-release
```

Before accepting the result, import `boto3`, `botocore`, `jwt`,
`cryptography`, and `cffi` from the prepared dependency directory, verify the
versions against the constraints file, inspect the manifest, and compare the
reported Lambda SHA-256 with the immutable S3 object version later supplied
to CloudFormation. No dependency download or artifact upload is performed by
local tests.

The frontend zip is a release/verification artifact, not a file served by
CloudFront. In a separately approved deployment step, extract only those four
files into a clean staging directory and upload them explicitly to the
private frontend bucket (never make the bucket public). After the upload,
invalidate `/`, `/index.html`, `/styles.css`, `/app.js`, and `/citations.js`
for the reviewed distribution. Preserve the manifest and resulting object
version/digest for rollback; do not run this upload or invalidation as part of
Phase 14 local validation.

## Immutable Lambda artifact

The template accepts only an existing artifact bucket, immutable key, and
object version; it does not upload code or create an artifact bucket. The
packaging workflow above produces the exact zip and digest; uploading it to a
versioned artifact bucket and passing its key/version remain separate,
explicitly approved release steps outside this local edge tranche.

When `ResolverModelArn` or `WriterModelArn` names a cross-region inference
profile, `bedrock:InvokeModel` must also cover every foundation-model region to
which that profile can route. The template keeps the action and model ID exact
while permitting a region wildcard only in the corresponding
`*FoundationModelArn` parameter, for example
`arn:aws:bedrock:*::foundation-model/<exact-model-id>`. A single regional model
ARN is insufficient for a cross-region profile and fails closed at inference.

On 2026-09-28, a read-only `GetInferenceProfile` check showed the active
`eu.anthropic.claude-sonnet-4-6` profile routing to the exact Sonnet 4.6
foundation-model ID in `eu-north-1`, `eu-west-3`, `eu-south-1`, `eu-south-2`,
`eu-west-1`, and `eu-central-1`. The deployed role therefore uses
`arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6`: the action,
resource type and model ID remain exact, while the region is wildcarded only
to permit the provider-controlled routing set. A same-day CloudFormation
read-only check recorded `LegalDeskPhase14PublicEdge` as `UPDATE_COMPLETE`.

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

The public-beta stack has been deployed under operation-specific approval, but
that does not close the production gate. Before any subsequent deployment or
promotion, review the exact parameter values against
`docs/phase-13-application-access.md`, confirm that all catalog entries are
fictional/public beta matters, inspect the NoEcho marker handling, and verify
the Lambda zip digest. Keep the prior
immutable Lambda S3 version and CloudFormation change set available for
rollback. Roll back the distribution/API/Lambda stack to the prior reviewed
version, then restore the prior Phase 10/02 origin values if the bootstrap
change was already applied. Teardown is not a default operation: frontend and
log buckets are retained, and existing data-plane stacks are never deleted by
this template.

The template intentionally contains no EventBridge reconciliation scheduler,
malware event rule, public signup resource, WAF, or real-data migration; those
remain separate approval gates.
