# Phase 14 document-security runbook

This runbook applies to the authenticated fictional/public beta only. It does
not authorize real legal data or an AWS deployment.

## Normal path

1. The backend issues a server-owned key and metadata record in
   `PENDING_UPLOAD`; public PUTs target `quarantine/`, outside the Bedrock
   source prefix `tenants/`.
2. Upload confirmation validates the object `HEAD`, size, media type, and
   tenant/matter/document metadata but does not make it indexable in public
   mode.
3. The exact GuardDuty Malware Protection result is corroborated by a fresh
   `HEAD` and the `GuardDutyMalwareScanStatus` object tag. Only
   `NO_THREATS_FOUND` copies the object server-side to its canonical `tenants/`
   key, permits the canonical sidecar and `UPLOADED` transition, then deletes
   the quarantine object.
   Event parsing requires `scanStatus` (`COMPLETED`, `SKIPPED`, or `FAILED`)
   and the matching `scanResultDetails.scanResultStatus`; status reasons are
   bounded and never logged verbatim.

## Fail-closed and incident handling

Unknown envelope fields, wrong account/region/bucket/key, scope mismatch,
missing or mismatched tags, changed ETag/version, malformed metadata, and
provider read errors do not promote an object. Operators should retain only
bounded event ID/status/scope metadata in audit records; never log tokens,
document bodies, or full provider responses.

`THREATS_FOUND`, `UNSUPPORTED`, `ACCESS_DENIED`, and `FAILED` delete the
original and sidecar when cleanup succeeds and persist `FAILED`. The metadata
transition is conditional on `PENDING_UPLOAD + PENDING`; a reconciler or
concurrent event therefore wins or loses explicitly rather than being
overwritten by an unconditional save. Cleanup or metadata persistence failure
is an incident: metadata remains non-indexable, the object may remain for
bounded retry/quarantine, and no client retry may start ingestion. Duplicate
terminal events retry cleanup. A later clean event cannot resurrect a failed
document.

If copy, sidecar, conditional metadata transition, or quarantine deletion
fails, the record remains `PENDING_UPLOAD` or `FAILED` and ingestion remains
blocked; retry/reconciliation may safely repeat the exact operation. A threat
event arriving after a completed clean transition is treated as a conditional
conflict: canonical/quarantine objects are removed fail-closed and the
metadata conflict is surfaced for operator reconciliation rather than allowing
an unsafe overwrite.

## Deployment gate

`infra/cloudformation/phase-14-document-security.yaml` is the local IaC
candidate. It reuses the existing table and bucket, creates one
`AWS::GuardDuty::MalwareProtectionPlan` for the exact
`quarantine/tenants/{BetaTenantId}/` prefix with tagging enabled, and routes
only the exact account/region/bucket/detail-type EventBridge result to
`legaldesk.malware_scan_lambda.lambda_handler`. The Lambda constructs only
the Dynamo metadata and S3 adapters from environment variables; the event is
never treated as proof of safety. It re-reads metadata, HEAD, and tags through
`MalwareScanEventHandler`.

The Lambda role has only metadata `GetItem`/`UpdateItem` on the beta tenant's
partition prefix and exact beta-prefix S3 read/tag/write/delete permissions.
The GuardDuty service role follows the provider-required setup contract: it
manages only GuardDuty's named EventBridge rule, enables notifications on the
one source bucket, writes the fixed validation object, checks that bucket, and
scans/tags only the beta quarantine object prefix. A three-attempt, one-hour EventBridge
retry policy sends failures to a retained, SQS-managed-encryption DLQ; the
Lambda has reserved concurrency five and a bounded CloudWatch log retention.
No KMS key or permission is added because the existing bucket uses SSE-S3.

Before deployment approval, validate the immutable Lambda artifact parameters,
the account/region and one fictional `BetaTenantId`, the exact quarantine
prefix, and that the Bedrock data source still targets only `tenants/`. Review
DLQ ownership and alerting, then deploy through a reviewed CloudFormation
change set. This local tranche creates no resources and performs no AWS calls.
