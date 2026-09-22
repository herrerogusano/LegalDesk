# Production readiness gate

Status: **release candidate hardened; production deployment is blocked**.

The integrated application and its bounded AWS smoke are complete. The local
hardening pass adds fail-closed public endpoint validation, browser security
headers, and strict retrieval response bounds. The complete local suite passes
with 413 tests and the deterministic evaluation remains 24/24 with zero AWS
calls.

This status deliberately does not mean that the loopback application can be
exposed to legal users. Promotion from `developer` to `prod` must remain closed
until all of the following deployment gates have evidence attached to the
release:

1. An approved TLS hosting architecture with a public OAuth callback, secure
   cookies, a restrictive CSP for the selected upload hosts, and reviewed edge
   request limits.
2. Durable, multi-instance storage for sessions, OAuth state, citation handles
   and audit records. Process-local dictionaries are not an acceptable
   production source of truth.
3. A formal security/privacy review, data classification and retention policy,
   malware/content validation, incident response and deletion/export controls.
4. Automated reconciliation for abandoned `PENDING_UPLOAD` documents and
   expired Gateway grants, with bounded permissions and an operator runbook.
5. Execution and independent review of the frozen 14-case semantic holdout
   against the release candidate. The lexical oracle and earlier bounded smoke
   are not substitutes for this gate.
6. Production SLOs, alarms, budgets, recovery procedures and a verified
   deployment/rollback plan.

The loopback entry point rejects non-loopback hosts and is intentionally not a
production server. Creating the hosting and durable-state design is an
architectural change and may create billable AWS resources, so it requires an
approved ADR and cost envelope before implementation. Until then the honest
release verdict remains `NOT_READY_FOR_PROD`.

## Local hardening evidence

- Public AWS/OIDC/Gateway endpoints must be real HTTPS URLs and cannot contain
  credentials, queries, fragments, control characters, backslashes or reserved
  `.invalid` hosts.
- Retrieval rejects provider responses above the configured five-result limit
  or 100,000 aggregate source characters before resolver/writer processing.
- Every HTTP response includes `no-store`, `nosniff`, frame denial, a
  no-referrer policy and a restrictive permissions policy.
- CSP and HSTS are intentionally not asserted by the loopback server: HSTS is
  meaningful only at the TLS edge, while the final CSP must enumerate the
  approved presigned-upload hosts rather than use an unsafe broad rule.
- No AWS call, resource creation, deployment or inference is part of this local
  hardening pass.
