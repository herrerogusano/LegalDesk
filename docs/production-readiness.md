# Production readiness gate

Status: **authenticated public beta deployed and smoke-tested; production
promotion remains blocked**.

The Phase 14 public edge, document-security, reconciliation and operations
stacks are deployed in `eu-west-1`. A bounded Chrome-headless journey using a
short-lived technical identity passed Cognito login, indexed-document display,
one grounded chat with citations, cross-matter denial, audit and logout. This
does not close the independent holdout, operational drill, privacy review or
production-promotion gates; the verdict remains `NOT_READY_FOR_PROD`.

The integrated application and its bounded Phase 13 AWS smoke are complete.
The Phase 14 local candidate adds durable state, asynchronous ingestion,
quarantine and content validation, bounded reconciliation, release packaging,
operational controls, a bounded holdout runner, fail-closed public endpoint
validation, browser security headers and strict retrieval response bounds. The
complete local suite passes with 552 tests (one platform-specific symlink test
skipped on Windows), all 15 CloudFormation/SAM templates pass lint, and the
deterministic evaluation remains 24/24 with zero AWS calls.

This status deliberately does not mean that the loopback application can be
exposed to legal users. Promotion from `developer` to `prod` must remain closed
until all Phase 14 acceptance gates below have evidence attached to the release.
These gates apply to the approved authenticated public beta only: fictional or
public documents, pre-provisioned Cognito users, and no anonymous/public signup.

1. **P14-G1 — public edge and identity.** An approved CloudFront + private S3
   frontend + API Gateway HTTP API + Lambda deployment exists with a public
   HTTPS OAuth callback, secure cookies, restrictive CSP naming the exact
   presigned-upload host(s), HSTS at the edge, exact S3 CORS, reviewed edge
   limits, and no anonymous/public signup. Ingestion start/status is
   asynchronous and no public request waits on the Bedrock polling loop. The
   implementation and deployment evidence are in
   [`phase-14-public-edge.md`](phase-14-public-edge.md). The deployed edge and
   authenticated browser journey close the basic deployment path, but the
   complete cookie/header/CSP/CORS and asynchronous-ingestion evidence set
   remains to be attached before this gate closes. CloudFront standard request
   logs remain disabled because they would retain the OAuth callback query; the
   candidate uses field-selected API access logs without query, cookies,
   headers, IP, or user agent.
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
   evidence exists.
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
5. **P14-G5 — independent semantic evidence.** The frozen 14-case semantic
   holdout runs against the exact release candidate and receives independent
   review. The bounded local runner, deterministic negative canaries and
   fail-closed procedural attestation gate are implemented in
   [`phase-14-holdout.md`](phase-14-holdout.md). The immutable first real-model
   run accepted 11/14 cases against an earlier commit and cannot be approved;
   no passing final-candidate report or reviewer attestation exists. The
   lexical oracle, deterministic suite and bounded smoke are not substitutes
   for this gate.
6. **P14-G6 — operations and release.** Production SLOs, alarms, budgets,
   recovery procedures, backup/retention ownership, verified deployment and
   rollback, and shared-resource teardown evidence are complete. The local
   stack is deployed and its procedure is in
   [`phase-14-operations-runbook.md`](phase-14-operations-runbook.md); fault,
   rollback and recovery evidence plus owner sign-off remain open.
7. **P14-G7 — cost and deployment authority.** Earlier operation-specific
   approvals covered the completed deployment and bounded test runs, but do not
   establish a reusable production budget. The final inventory, numeric cost
   envelope, request/model/retention ceilings and exact teardown targets must
   be approved before further billable operations or promotion.

The loopback entry point rejects non-loopback hosts and is intentionally not a
production server. ADR-018 and `PLAN_14_PUBLIC_BETA.md` define the target
design; further AWS operations, final inference evidence and production
promotion remain gated by P14-G1 through P14-G7. The cost inventory and
operational procedure are in `docs/phase-14-cost-operations.md`. Until all
gates have evidence, the honest release verdict remains `NOT_READY_FOR_PROD`.

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
