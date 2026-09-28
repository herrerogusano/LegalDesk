# Phase 14 operations and release runbook

## Status

This is a **local IaC candidate**, not deployment evidence. The operations
stack is `infra/cloudformation/phase-14-operations.yaml`; it reuses resource
names supplied as parameters and creates twelve standard CloudWatch alarms and
one monthly AWS Budget. It deliberately creates no SNS topic, dashboard, WAF,
or data resource. No AWS call is authorized by this document.

The alarms are visible-only when `ExistingAlarmTopicArn` is empty. If an
already-owned SNS topic is explicitly supplied, the same alarms enable that
topic as an action; this stack never creates or manages an SNS topic. The
Budget uses the supplied email directly and its 80%/100% notifications are
alerts, not a billing hard cap. It is deliberately account-wide because some
Bedrock/Marketplace charges cannot be reliably attributed by project tag; set
the threshold with the account's existing non-LegalDesk spend in mind.

## Beta SLOs and alert intent

These are release-candidate targets for the small authenticated fictional/public
beta, not a promise of legal-service availability:

| Signal | Beta target / alarm | Response |
| --- | --- | --- |
| HTTP API 5xx | zero expected; alarm on >=1 in 5 minutes | inspect API access log request ID and Lambda logs; rollback if persistent |
| HTTP API latency | p95 <=3 seconds for two 5-minute periods | inspect route and downstream latency; stop traffic if sustained |
| Application Lambda errors/throttles | zero errors and throttles in 5 minutes | check deployment, reserved/account concurrency and dependencies |
| Application Lambda duration | p95 <25 seconds for two periods (29-second API integration limit) | investigate slow provider/retrieval calls and disable traffic if needed |
| Malware Lambda errors/throttles | zero in 5 minutes | keep new documents quarantined; inspect retry/DLQ state |
| Reconciliation errors/throttles | zero in 5 minutes | keep cleanup disabled only after bounded operator review |
| Reconciliation duration | p95 <100 seconds for two periods (120-second timeout) | reduce scope/limit and investigate provider latency |
| Malware/reconciliation DLQ | zero visible messages | inspect original event and replay only with an idempotency review |
| Monthly spend | email at 80% and 100% of configured limit | stop optional AWS operations; budget is not a hard stop |

Missing CloudWatch data is `notBreaching` so an idle beta does not page as a
failure. This means the operational check must also confirm that the expected
traffic/heartbeat exists; an alarm in `INSUFFICIENT_DATA` is not evidence of
health.

## Local synthetic-fault exercise

Run these exercises without AWS or real-model calls:

1. Render the operations template with a local CloudFormation parser and
   assert all twelve alarms use native namespaces, bounded dimensions,
   `TreatMissingData: notBreaching`, and no SNS resource. Verify the optional
   alarm topic is empty for the local default.
2. In the API/application provider doubles, inject one 5xx, a timeout-shaped
   latency, and a Lambda error/throttle result. Confirm the local incident
   report maps each signal to the runbook response and never prints tokens,
   prompts, document bodies, or secrets.
3. Inject malware/reconciliation failures into the existing Lambda fakes and
   assert the retry boundary, DLQ-visible state and quarantine/failed
   lifecycle remain idempotent.
4. Exercise the budget parser with an 80% and 100% notification event. Confirm
   the close procedure stops optional inference/ingestion and records the
   release commit; it must not pretend that an alert prevented billing.

Provider-side exercise, only after a separate numeric cost approval, uses one
fictional/public document and one pre-provisioned beta user. Trigger a bounded
synthetic error in a disposable stage, verify the alarm and runbook evidence,
then delete only the recorded fixture. Do not use a real legal document or
deliberately corrupt the production data path.

## Release procedure

1. Record the commit SHA, template SHA-256 values, exact parameter fingerprint,
   artifact SHA-256/manifest and intended rollback artifact. Exclude tokens,
   cookies, prompts, answers, source documents and secrets.
2. Build artifacts offline with `scripts/package_release.py`; verify the
   emitted manifest and Lambda zip before upload. The Lambda code is immutable
   in the deployment parameters through S3 object version.
3. Run `sam validate --lint` for every Phase 14 template and the focused local
   tests. Review the synthesized CloudFormation and IAM changes.
4. Create a CloudFormation change set for the edge, document-security,
   reconciliation and operations stacks. Do not execute it until the Phase 14
   cost gate has an approved account/region, numeric ceiling, retention and
   teardown target.
5. After execution, verify outputs, alarm dimensions, budget email state,
   exact public origin/CSP/CORS, and the authenticated synthetic journey. The
   first release must remain limited to the beta tenant and pre-provisioned
   users.

## Rollback

Prefer a CloudFormation change-set rollback or stack update to the previous
known-good template and immutable Lambda S3 object version. Stop public traffic
and new uploads first; leave existing documents quarantined unless the
incident procedure authorizes a safe transition. Preserve the metadata table,
source bucket, Cognito pool, Gateway, Memory and Knowledge Base. A rollback is
not complete until a synthetic read/upload/reconciliation check is recorded
and no new 5xx or DLQ messages are observed during the bounded observation
window.

If a change-set update is stuck, use CloudFormation continue-update-rollback
only after identifying the exact failed resource. Never delete a shared data
resource to recover a failed application deployment.

## Backup and recovery ownership

The operations stack does not silently enable a new backup service. The Phase
02 table template enables DynamoDB point-in-time recovery; deployment evidence
must verify it is active and the data owner must record the permitted recovery
window and owner-approved restore test. The source bucket's encryption, lifecycle and
retained-object policy must likewise be verified from the deployed resource;
local template validation is not proof of backup coverage. A recovery drill
must restore into an isolated name, validate tenant/matter isolation and then
remove only that synthetic restore. Until that evidence exists, the recovery
gate remains open and production promotion is blocked.

## Teardown and retained ownership

Teardown is a recorded, owner-approved operation—not part of a failed test.
Stop traffic and inference, disable the reconciliation schedule, and remove
only synthetic/public beta objects, sidecars, vectors, state records, alarms,
budget and temporary artifacts. Verify quarantine and source prefixes,
noncurrent versions, DLQs, log groups and CloudFormation events independently.

The candidate templates mark frontend bucket, log groups, malware/reconciliation
DLQs and other durable resources with `DeletionPolicy: Retain` where they own
retained evidence. Before deleting a retained resource, record its owner,
retention period, legal hold status, export destination and recovery procedure.
The shared source bucket, metadata table, Cognito, Gateway, Memory and
Knowledge Base are never deleted by the Phase 14 teardown by default.

Ownership must be explicit before deployment: a named beta operator owns
alarm review and budget close; an application owner owns rollback; a data
owner owns retention/deletion/export approval. No production promotion is
allowed while any owner or retained-resource inventory is unknown.
