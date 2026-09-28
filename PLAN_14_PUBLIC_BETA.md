# PLAN 14 — Authenticated public beta architecture and production gates

## Status, scope and authority

Status: **authenticated public-beta infrastructure deployed and browser-smoke
tested; production promotion remains gated**.

The approved product scope is an authenticated public beta for fictional or
public documents only. It is not a legal-data service and it does not assume
anonymous signup, self-service tenant creation, or real client data. Users and
matter memberships are provisioned and authorized server-side before use.

This plan follows Phase 13 and preserves the existing AgentCore, Cognito,
Gateway, Memory, Bedrock, S3 and DynamoDB boundaries. Deployment and bounded
test operations were performed under the user's operation-specific approvals;
this document does not authorize further AWS calls, inference, destructive
cleanup, or release promotion. The remaining gates below continue to apply.

## Approved target architecture

```text
Browser -- HTTPS --> CloudFront custom domain + ACM (+ approved edge limits)
                       |                    |
                       |                    +--> private S3 frontend bucket (OAC)
                       +--> API Gateway HTTP API --> Lambda application adapter
                                                       |
       Cognito public PKCE client ---------------------+
       existing private source S3 ---------------------+
       existing metadata/auth/state DynamoDB -----------+
       existing Bedrock KB/S3 Vectors/Guardrail --------+
       existing AgentCore Gateway/MCP/Review ------------+
       existing short-term Memory -----------------------+
       redacted CloudWatch telemetry --------------------+
```

The browser public origin is one approved HTTPS hostname. CloudFront routes
static assets to a private S3 bucket and `/login`, `/callback`, `/logout` plus
`/api/*` to an API Gateway HTTP API integration. The API origin host is an
explicit separate allowlist because an API Gateway integration can observe its
own origin/custom-domain `Host`, not the viewer host. CloudFront must inject
and overwrite the fixed `X-LegalDesk-Trusted-Edge` marker; its secret value is
configured without logging or output, and direct execute-api requests without
the marker fail. The application keeps OAuth tokens server-side and
validates the Cognito access token, issuer, scope and expiry. No browser value
establishes tenant, matter, document, conversation, correlation, or tool scope.

The existing metadata table is the initial durable-state target. New item
prefixes must be explicit and least-privilege scoped for sessions, OAuth state,
citation handles, conversation correlations, accepted review candidates,
accepted history IDs, and redacted audit records. Sensitive token/citation
material must be protected at rest and excluded from logs. Application expiry
checks remain authoritative; DynamoDB TTL is cleanup assistance, not an access
control.

Ingestion becomes asynchronous: the public request starts a bounded job and
returns an operation identifier; a separate status operation observes the job
and applies the existing document lifecycle. No public request waits for the
Bedrock polling loop. Abandoned uploads, stuck ingestion, and expired grants
are handled by a bounded reconciliation operation.

Exact-origin controls are required: secure cookies, restrictive CSP naming the
approved presigned-upload host(s), exact S3 CORS, HSTS at the TLS edge, API
body/rate limits, no redirects from the application transport, and no public
S3 access. WAF remains an explicit cost/approval choice, not an implicit
security substitute for backend authorization.

## Non-goals

- No anonymous access or public signup.
- No real legal/client documents, legal advice, or broad legal-quality claim.
- No long-term AgentCore Memory.
- No replacement of deterministic backend authorization with Cognito groups,
  model instructions, Guardrails, or edge policy.
- No new region, multi-region replication, custom vector store, or unrelated
  product features.

## Work packages

### P14-1 — Public edge and identity configuration

Design and implement the CloudFront/S3/API Gateway/Lambda deployment boundary,
public callback/logout configuration, secure cookies, CSP/HSTS, exact CORS,
edge request limits, and health/readiness behavior. Keep Cognito users and
memberships pre-provisioned; do not add anonymous signup. The local IaC
candidate is `infra/cloudformation/phase-14-public-edge.yaml`, and the
two-step callback/CORS bootstrap is documented in
`docs/phase-14-public-edge.md`. These artifacts do not constitute deployment
evidence. CloudFront request logging is disabled because it would retain the
OAuth callback query; field-selected API access logs provide bounded
operational evidence without query, cookie, header, or client identifiers.

### P14-2 — Durable state and stateless application operation

Replace process-local sources of truth with DynamoDB repositories using
namespaced items, conditional writes, bounded projections, and application
expiry checks. Cover sessions/OAuth state, citation handles, conversation
selectors/correlations, accepted history IDs, review candidates, and redacted
audit records. Preserve existing durable review tasks and conversation bindings.

### P14-3 — Asynchronous ingestion and reconciliation

Split ingestion start from status observation. Add bounded, idempotent
reconciliation for abandoned `PENDING_UPLOAD`, stale ingestion, and expired
Gateway grant/invocation records. Do not use unbounded scans or treat TTL as a
guaranteed deletion deadline. The public application exposes
`POST /api/matters/{matterId}/ingestions` and
`GET /api/matters/{matterId}/ingestions/{operationId}`; the legacy synchronous
`/sync` helper remains operator/loopback-only. A public start returns `202`
with a server-owned opaque operation ID and bounded client polling never waits
inside the request for Bedrock. Ambiguous provider starts and post-start
metadata failures retain the active canonical subject/tenant/matter/document
set binding and known job ID for recovery; that binding is cleared only after
a safe terminal transition or explicit reconciliation.

### P14-4 — Security, privacy and document lifecycle

Complete the beta data classification, retention/deletion/export policy,
incident response and operator runbook. Add malware/content validation and a
quarantine boundary before indexing. Public uploads remain `PENDING_UPLOAD`
under a physical `quarantine/` prefix excluded from the Bedrock data source,
until an exact GuardDuty Malware Protection result is corroborated by a fresh
object `HEAD`, server-owned metadata, and the `GuardDutyMalwareScanStatus` tag;
only `NO_THREATS_FOUND` copies to the canonical source prefix and creates the
indexing sidecar. Threat, unsupported,
access-denied, and failed results delete/quarantine and persist `FAILED`, with
idempotent duplicate handling, schema-consistent `scanStatus`/verdict parsing,
conditional lifecycle transitions, and no cross-matter promotion. Keep
synthetic/public-only fixtures in tests and smoke runs.

The local operator governance slice adds sealed operator scopes, bounded
metadata-only export, exact document/matter deletion of canonical/quarantine
objects and metadata, known-scope state cleanup where indexed interfaces
allow, and closed-review archival that strips workflow text. It is not a
public destructive endpoint and does not close the deployment gate: operator
identity, export destination, legal holds, backup retention, and exact
production deletion evidence remain approval requirements. Deletion first
tombstones metadata to block stale vectors; retrieval revalidates every result
against live `INDEXED` + malware-clean metadata. Bedrock vector removal is a
separate bounded sync/reconciliation evidence item and is reported as
`indexCleanupPending` until proven.

### P14-5 — Independent semantic release evidence

Execute and independently review the frozen 14-case semantic holdout against
the exact release candidate, with a pre-approved model/profile, prompt hashes,
request ceilings, and metadata-only immutable reporting. The local
preflight/runner is `evals/phase14_holdout_runner.py`, with commands and
attestation rules in `docs/phase-14-holdout.md`; it performs no AWS calls until
explicit execution approval. Its forged-citation and role-reversal canaries
are separate from provider-success scoring, and reviewer identity provides
procedural separation rather than cryptographic proof of independence.

### P14-6 — SLO, budget and release operations

Define SLOs, alarms, budget alerts, dashboards/queries, backups, recovery,
change-set deployment, rollback, teardown, and ownership of retained shared
resources. The local candidate is `infra/cloudformation/phase-14-operations.yaml`
with the procedure in `docs/phase-14-operations-runbook.md`; it uses standard
metrics, an optional already-owned alarm topic, and direct-email Budget alerts
without creating SNS or dashboard resources. Keep production promotion closed
until all gates have attached provider evidence and owner sign-off.

The public application additionally reserves a durable, fail-closed monthly
tenant quota before uploads and potentially billable chat, ingestion, Harness,
or Gateway operations. The ledger uses the existing metadata table, atomic
conditional writes, UTC-month keys and server-owned limits; authorization
failures and read-only status checks do not consume quota. API throttling and
Budget alerts remain complementary controls rather than substitutes for this
preventive ceiling.

## Acceptance criteria

Phase 14 is complete only when every criterion below has release evidence:

1. **Public transport:** an HTTPS browser journey succeeds through the approved
   CloudFront hostname; OAuth callback/logout are public and exact; cookies are
   secure, HttpOnly and same-site; CSP, HSTS, S3 CORS and edge limits are
   verified; no anonymous route or signup exists.
2. **Scope and identity:** only pre-provisioned Cognito users/memberships work;
   matter/tenant/document/session/correlation values from the browser remain
   selectors; A→A and B→B allow while A→B, forged selectors, expired tokens,
   and foreign citations deny before business reads/writes.
3. **Durable state:** restart and multi-instance tests preserve valid sessions,
   OAuth state, conversations, citations, accepted history/review candidates,
   and redacted audit pointers; expired/replayed state fails closed; no token,
   full prompt, answer, passage, document body, or secret reaches logs.
4. **Ingestion:** start/status is asynchronous and bounded; lifecycle remains
   `PENDING_UPLOAD → UPLOADED → PENDING_INGESTION → INDEXED` (or `FAILED`);
   retries are idempotent and no public request waits on Bedrock polling.
5. **Reconciliation:** stale uploads/ingestion/grants are found through bounded
   authorized queries, cleaned or marked safely, and covered by a runbook;
   cleanup cannot cross tenant/matter boundaries and does not rely solely on
   eventual TTL deletion.
6. **Document safety/privacy:** malware/content validation, quarantine,
   exact event/HEAD/tag corroboration, retention, deletion/export, incident
   response, and review-task archival rules are approved and tested for the
   fictional/public beta data class.
7. **Semantic holdout:** all 14 frozen cases run against the release candidate;
   an independent reviewer signs the immutable metadata-only result and the
   approved threshold is met or the release is rejected.
8. **Operations/release:** SLO metrics and alarms, budget alerts, recovery and
   rollback are exercised with synthetic faults; change-set/IaC review,
   retained-resource ownership, and teardown evidence are complete.
9. **Cost gate:** the final resource inventory, numeric cost envelope, request
   limits, retention period, and teardown targets are approved before any AWS
   deployment or real-model evaluation.

## Stop conditions

Stop and request an ADR/user decision if the implementation requires a
different public hosting topology, new region, anonymous signup, real legal
data, long-term Memory, a new authorization source, broad IAM permissions, or
an unbounded/uncosted AWS operation. Do not begin a later plan automatically.
