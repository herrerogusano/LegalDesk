# Production readiness gate

Status: **READY_FOR_CONSTRAINED_PROD_PROMOTION**.

This verdict is limited to the authenticated beta with pre-provisioned users
and fictional/public documents. It is not strict-TLS certification and it is
not certification for real legal/client data.

The Phase 14 public edge, document-security, reconciliation and operations
stacks are deployed in `eu-west-1`. A bounded Chrome-headless journey using a
short-lived technical identity passed Cognito login, indexed-document display,
one grounded chat with citations, cross-matter denial, audit and logout. This
closes the bounded holdout and operational evidence for the constrained beta.
The default CloudFront certificate is an explicitly accepted residual because
no custom domain is owned; strict TLS is not claimed.

The integrated application and its bounded Phase 13 AWS smoke are complete.
The Phase 14 local candidate adds durable state, asynchronous ingestion,
quarantine and content validation, bounded reconciliation, release packaging,
operational controls, a bounded holdout runner, fail-closed public endpoint
validation, browser security headers and strict retrieval response bounds. The
complete local suite passes with 574 tests (one platform-specific symlink test
skipped on Windows), all 15 CloudFormation/SAM templates pass lint, and the
deterministic evaluation remains 24/24 with zero AWS calls.

The loopback application is not the public service. Promotion was held closed
until all Phase 14 acceptance gates below had evidence attached. Those gates
now close only for the authenticated public beta: fictional/public documents,
pre-provisioned Cognito users, and no anonymous/public signup.

1. **P14-G1 — public edge and identity.** An approved CloudFront + private S3
   frontend + API Gateway HTTP API + Lambda deployment exists with a public
   HTTPS OAuth callback, secure cookies, restrictive CSP naming the exact
   presigned-upload host(s), HSTS at the edge, exact S3 CORS, reviewed edge
   limits, and no anonymous/public signup. Ingestion start/status is
   asynchronous and no public request waits on the Bedrock polling loop. The
   implementation and deployment evidence are in
   [`phase-14-public-edge.md`](phase-14-public-edge.md). The deployed edge and
   authenticated browser journey close the basic deployment path. The complete
   cookie/header/CSP/CORS and asynchronous-ingestion evidence set is attached
   in the release record. CloudFront standard request
   logs remain disabled because they would retain the OAuth callback query; the
   candidate uses field-selected API access logs without query, cookies,
   headers, IP, or user agent.
   The custom CloudFront domain and validated ACM certificate are not present.
   The AWS/repository owner explicitly accepts the default CloudFront TLS as a
   residual for this constrained beta; this gate does not claim strict TLS.
2. **P14-G2 — durable state and authorization.** Sessions, OAuth state, citation
   handles, conversation correlations, accepted history/review candidates and
   redacted audit records survive restart and multi-instance routing. Expiry,
   replay, cross-matter and foreign-citation checks fail closed; process-local
   dictionaries are not a production source of truth.
3. **P14-G3 — security, privacy and document lifecycle.** A formal
   security/privacy review, beta data classification and retention policy,
   exact GuardDuty Malware Protection event + S3 `HEAD`/tag corroboration,
   physical quarantine outside the Bedrock source prefix,
   malware/content validation and quarantine, incident response, and
   deletion/export controls are approved and tested. No real legal/client data
   is admitted by the beta policy. The local governance boundary is
   operator-only, metadata-export-only, bounded and fail-closed; it does not
   constitute approval of public destructive routes or a production export
   destination. Retrieval also revalidates every vector's live metadata;
   deletion remains `indexCleanupPending` until bounded Knowledge Base cleanup
   evidence exists. The public composition also checks the canonical
   server-owned S3 object with `HeadObject` before any retrieved passage can
   reach the resolver/writer, so lifecycle expiry cannot leave a usable stale
   vector.
   The local upload candidate also signs the exact server-validated
   `Content-Length`; the browser cannot authorize a larger object before the
   malware boundary, and confirmation still performs an exact `HeadObject`
   check.
4. **P14-G4 — bounded reconciliation.** Automated, authorized reconciliation
   handles abandoned `PENDING_UPLOAD`, stale ingestion and expired Gateway
   grants/invocations, with bounded queries, idempotent transitions and an
   operator runbook. The local candidate-driven implementation and procedure
   are in [`docs/phase-14-reconciliation-runbook.md`](phase-14-reconciliation-runbook.md).
   DynamoDB TTL is not treated as an authorization or exact deletion guarantee.
   The supervised synthetic exercise passed with one invocation,
   `examined=6`, `changed=1`, `skipped=5`, `failed=0`, and cleanup verified.
5. **P14-G5 — independent semantic evidence.** The frozen 14-case semantic
   holdout runs against the exact release candidate and receives independent
   review. The bounded local runner, deterministic negative canaries and
   fail-closed procedural attestation gate are implemented in
   [`phase-14-holdout.md`](phase-14-holdout.md). The immutable first real-model
   runs accepted 11/14, 12/14 and 13/14 and remain immutable failed evidence.
   The final4 report passed 14/14 with 27 calls and zero retries against commit
   `0c8bb718...` and artifact `99d074e7...`; independent attestation v1.1.0 is
   `approved`. Exact hashes are recorded in the closure section below.
6. **P14-G6 — operations and release.** Production SLOs, alarms, budgets,
   recovery procedures, backup/retention ownership, verified deployment and
   rollback, and shared-resource teardown evidence are complete. The final
   smoke, isolated 62-item PITR restore/delete, rollback to `50dd36f`, forward
   recovery to `fe53da68...`, and confirmed SNS subscription are recorded in
   [`phase-14-operations-runbook.md`](phase-14-operations-runbook.md). The
   synthetic malware alarm was diagnosed, its routing/idempotency fix was
   deployed, and all twelve alarms returned to `OK`. The AWS/repository owner
   holds the solo-project governance roles; strict TLS remains an accepted,
   documented residual rather than a claim of this release.
7. **P14-G7 — cost and deployment authority.** Operation-specific approvals
   covered the recorded holdout, deployment and smoke. They do not establish a
   reusable budget: future billable operations still require their own approval.
   Monthly request/model ceilings and exact teardown targets remain enforced.

The final inventory, owner decision and consumed bounded execution envelope are
consolidated in
[`phase-14-production-approval.md`](phase-14-production-approval.md).

The loopback entry point rejects non-loopback hosts and is intentionally not a
production server. ADR-018 and `PLAN_14_PUBLIC_BETA.md` define the target
design. P14-G1 through P14-G7 have evidence for constrained promotion. The
cost inventory and operational procedure are in
`docs/phase-14-cost-operations.md`. The honest release verdict is now
`READY_FOR_CONSTRAINED_PROD_PROMOTION` for the authenticated fictional/public
beta. This does not certify real legal data and does not claim strict TLS.

## Final Phase 14 closure — 2026-09-29

- Release app commit: `0c8bb718e58811afff2085114dad7821cf573e3f`.
- Release artifact SHA-256: `99d074e793335f2867266b07ae091cb110a4747b8ad51e9180b031a2091f4f31`.
- Holdout final4: 14/14, 27 calls, zero retries; report SHA-256
  `503b867a355c4b95ca69cb23f5746d8b1a58935f35f7e0204cafacfcead404ef`.
- Independent approved attestation v1.1.0 SHA-256:
  `3d833232cabb1d290544009c31c7b9ecda0201264d9c331dc7d52cb799d48dc2`.
- Public smoke `phase14-public-smoke-20260929-final4-02.json`: `PASS`; one
  chat, two citations, cross-matter `403`, audit `17`, logout `200`, and
  `cleanupErrors=[]`; report SHA-256
  `1431503bd4a336bf552853af4cb8eb7b87a8cc488a44e59b7542e9624281d153`.
- Deployment: `UPDATE_COMPLETE`; only Lambda Code and dynamic API integration
  changed, with no replacement. Lambda S3 object version:
  `.VmAi0D4.baSxBpc61lKMBFkXobwoMyf`.
- Twelve alarms are `OK` with actions enabled. Governance signoff uses the
  AWS/repository owner role for this solo project; no personal name is inferred.
- No custom domain is owned. The default CloudFront TLS residual is accepted
  and documented; strict TLS is not claimed. Anonymous signup and real/client
  legal data remain out of scope.

## Local hardening evidence

- Public AWS/OIDC/Gateway endpoints must be real HTTPS URLs and cannot contain
  credentials, queries, fragments, control characters, backslashes or reserved
  `.invalid` hosts.
- Retrieval rejects provider responses above the configured five-result limit
  or 100,000 aggregate source characters before resolver/writer processing.
- Every HTTP response includes `no-store`, `nosniff`, frame denial, a
  no-referrer policy and a restrictive permissions policy.
- The loopback server remains HTTP-only, while public mode emits HSTS and the
  final CloudFront response policy must still enumerate approved
  presigned-upload hosts rather than use an unsafe broad CSP rule.
- Public mode separates the browser origin allowlist from approved API origin
  hosts and requires a constant-time-checked trusted-edge marker. CloudFront
  must inject/overwrite that marker without exposing it in IaC outputs or
  logs; direct API Gateway origin calls without it are denied.
- Presigned upload authorization binds the exact validated byte count into the
  SigV4 signed headers, while confirmation independently validates size, type
  and server-owned metadata.
- The local hardening tests make no AWS call. Deployment and the bounded public
  smoke are separately recorded evidence and do not imply authorization for
  another provider operation.
