# LegalDesk `v1.0.0-beta.1`

Release date: 2026-09-29
Release status: **`CONSTRAINED_PROD_BETA_DEPLOYED`**

This is a constrained portfolio beta, not a production legal service or legal
advice. It is limited to pre-provisioned authenticated users and public or
wholly fictional documents.

## Included

- Tenant/matter-scoped authorization before retrieval and tool access.
- Quarantined upload and asynchronous indexing lifecycle with malware/content
  validation boundaries.
- Grounded RAG with inspectable citations and an explicit
  `insufficient_evidence` outcome.
- Bounded human-review task workflow through the authorized tool path.
- Cross-matter fail-closed checks and redacted technical audit events.
- Operational alarms, budget/quota controls, reconciliation and documented
  rollback/teardown procedures.
- Portfolio demo script and periodic operations checklist:
  [`portfolio-demo-v1.0.0-beta.1.md`](portfolio-demo-v1.0.0-beta.1.md) and
  [`phase-14-operations-checklist.md`](phase-14-operations-checklist.md).

## Recorded evidence

- Local verification: 574 tests passed (one Windows-specific symlink test
  skipped), 24/24 deterministic evaluations, and all 15 IaC templates lint
  clean.
- Final bounded public smoke: PASS; one chat, two citations, cross-matter
  `403`, 17 audit events, logout `200`, and `cleanupErrors=[]`.
- Final semantic holdout: 14/14 with 27 calls and zero retries; the immutable
  report and independent attestation hashes are recorded in
  [`production-readiness.md`](production-readiness.md).
- Release evidence baseline: app commit
  `0c8bb718e58811afff2085114dad7821cf573e3f`; artifact SHA-256
  `99d074e793335f2867266b07ae091cb110a4747b8ad51e9180b031a2091f4f31`.

## Explicit limits

- Users and matter memberships are pre-provisioned; anonymous signup and
  self-service tenant creation are disabled.
- Only fictional/public documents are allowed. No real legal/client data,
  legal advice or broad legal-quality claim is supported.
- No custom domain or validated ACM certificate is owned; strict TLS is not
  claimed beyond the documented default CloudFront posture.
- Long-term AgentCore Memory is disabled, and budgets/alarms are detection and
  prevention aids rather than hard billing stops.
- Further billable AWS operations, real-model runs, new regions, destructive
  cleanup or scope expansion require separate approval under
  [`AWS_COST_POLICY.md`](../AWS_COST_POLICY.md).

## Follow-up outside this release

Domain/ACM setup, real-data privacy/compliance approval, user lifecycle and
broader statistical/legal evaluation remain future work. They are not hidden
release blockers for this constrained fictional/public beta, but they must be
closed before changing the approved scope.
