# LegalDesk — periodic operations checklist

This checklist is for the constrained authenticated beta only. It is an
operator checklist, not authorization for a new AWS operation. Use
pre-provisioned users and fictional/public documents; never use real legal
data. Record bounded metadata (date, release ID, status, counts and report
hashes) and never record tokens, cookies, prompts, answers, document bodies or
secret values.

## Before each demo or beta session

- [ ] Confirm the release ID is `CONSTRAINED_PROD_BETA_DEPLOYED` and the
      intended artifact/rollback reference is recorded.
- [ ] Confirm the user is pre-provisioned and the selected matter is an
      authorized fictional/public matter; anonymous signup remains disabled.
- [ ] Confirm the approved fixture and retention window. Do not upload a real
      client document or alter authorization/membership data for a demo.
- [ ] Check the public CloudFront root response, CloudFormation stack state and
      recent Lambda update/error/throttle state. The public API has no anonymous
      health route; do not treat its authenticated `403` or an idle
      `INSUFFICIENT_DATA` alarm as proof of health.
- [ ] Check all twelve alarms and the notification route; investigate any
      `ALARM` or unexpected `INSUFFICIENT_DATA` state before continuing.
- [ ] Confirm tenant quotas and request caps are unchanged and have headroom:
      uploads/bytes, chats, ingestion starts, Harness calls and Gateway calls.

## Daily while the beta is active

- [ ] Review API access logs, Lambda logs, ingestion/reconciliation logs and
      DLQ depth using request IDs and bounded metadata only.
- [ ] Verify logs contain no access tokens, cookies, prompts, answers, source
      passages, document bodies, presigned URLs or secrets.
- [ ] Review upload/indexing states for stuck `PENDING_UPLOAD`,
      `PENDING_INGESTION` or failed operations. Use bounded, authorized
      reconciliation; DynamoDB TTL is cleanup assistance, not access control.
- [ ] Confirm malware/content validation failures remain quarantined and that
      no object is promoted outside its authorized tenant/matter prefix.
- [ ] Review cross-matter denial and audit signals if the beta was exercised;
      stop if a foreign read, citation or tool operation is observed.
- [ ] Record any alarm transition, incident ID and remediation status without
      copying sensitive payloads.

## Weekly review

- [ ] Compare deployed stack/resource inventory with the release record; flag
      unexpected replacement, drift or new region/resource.
- [ ] Review Lambda duration/errors/throttles, API 5xx/latency, malware and
      reconciliation DLQs, and the bounded reconciliation report.
- [ ] Check current monthly spend, budget notifications and provider usage.
      Budgets and alarms detect spend; they are not hard billing stops.
- [ ] Review quota consumption and item-size/cardinality pressure from retry
      keys. Lower traffic or stop optional operations before a ceiling is hit.
- [ ] Confirm retained synthetic objects, sidecars, vectors and state records
      have an owner, purpose and expiry/cleanup target.
- [ ] Reconfirm the known limits: no real legal data, no anonymous signup, no
      long-term Memory, and no claim of strict TLS/custom-domain coverage.

## Monthly or before a release promotion

- [ ] Re-run the local/documentary validation appropriate to the change:
      `git diff --check`, focused tests, template lint/synth and deterministic
      evaluations. Do not run a provider operation merely to refresh this
      checklist.
- [ ] Review the cost envelope, account/region, retention, rollback artifact
      and teardown targets; obtain separate approval before any billable
      deployment, inference, ingestion or holdout.
- [ ] Verify the release record has commit/artifact/template hashes and
      bounded smoke/holdout evidence, with no secret or document content.
- [ ] Confirm rollback ownership and retained-resource ownership are explicit.
      Shared Cognito, Gateway, Memory, Knowledge Base, metadata table and
      source bucket are not teardown targets by default.

## Cleanup and close procedure

- [ ] Stop traffic and optional inference/ingestion before cleanup; disable a
      scheduled reconciliation job only under the approved procedure.
- [ ] Delete or reconcile only the recorded fictional/public fixtures, their
      authorized sidecars/vectors/state and temporary artifacts. Never run an
      unbounded bucket/table scan or cross-matter cleanup.
- [ ] Verify quarantine/source prefixes, DLQs, logs, alarm actions, budgets
      and stack events independently after the operation.
- [ ] Record final bounded counts and `cleanupErrors` status. A delete request
      or stack deletion alone is not proof that dependent objects are gone.
- [ ] Preserve the minimum release/incident evidence and remove sensitive
      local artifacts from the demo workstation according to the approved
      retention policy.

## Stop conditions

Stop and escalate before any further operation if a resource is outside the
approved inventory, the numeric cost envelope is missing, a quota/limit is
ambiguous, cleanup would be unbounded, an alarm or DLQ is unexplained, a
foreign matter is accessible, logs contain sensitive content, or anyone asks
to use real legal data, anonymous signup or a custom-domain/TLS claim not
covered by the release evidence.
