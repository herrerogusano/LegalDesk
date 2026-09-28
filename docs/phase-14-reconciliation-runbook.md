# Phase 14 bounded reconciliation runbook

Status: **deployed and scheduled in `eu-west-1`; bounded synthetic exercise
passed on 2026-09-28**. Future updates remain approval-gated.

The deployed definition is `infra/cloudformation/phase-14-reconciliation.yaml`.
It runs `legaldesk.reconciliation_lambda.lambda_handler` from a versioned
artifact on a bounded EventBridge schedule, with a default reserved concurrency
of `1`, a
two-attempt retry policy, a retained log group and an encrypted SQS DLQ. The
Lambda receives no browser selectors. `UploadScopes` and `IngestionScopes` are
small deployment parameters, and every entry must match the single
`BetaTenantId`; malformed, duplicate or foreign entries fail closed.

`ReconciliationReservedConcurrency` defaults to `1`. A deployment may set it
to `0` only when the reviewed regional account concurrency quota is already a
stricter ceiling and cannot accept Lambda reservations; the one schedule
target, bounded scopes/limit and durable idempotency remain mandatory. Enable
the per-function reservation after the account quota is raised.

The reconciler receives an explicit batch of authorized tenant/matter scopes or
opaque operational candidate IDs. Each batch is bounded (default maximum: 100)
and uses document-partition queries or point reads. It never scans the shared
DynamoDB table. The scheduler supplies the candidate partitions from
deployment configuration; this module does not infer scope from browser input.

## Completed synthetic evidence — 2026-09-28

The supervised exercise invoked the deployed reconciler exactly once and
passed with `examined=6`, `changed=1`, `skipped=5`, `failed=0`, and cleanup
verified (`cleanup=true`). The fixture was synthetic and the report retained
only bounded counts and cleanup status.

## Uploads

Run `ReconciliationService.reconcile_pending_uploads` for a small list of
server-authorized `ReconciliationScope` values. Only `PENDING_UPLOAD` records
older than the approved stale threshold are considered. The public reconciler
deletes only keys under the exact beta `quarantine/tenants/{tenant}/matters/{matter}/`
prefix; canonical source keys are not cleanup targets. The original and
metadata sidecar are deleted first; metadata moves to `FAILED` only after both
deletes succeed. A failed delete, malformed timestamp or non-quarantine key
remains pending and is reported for retry. A second run is safe: terminal
records are skipped.

## Ingestion

Run `reconcile_ingestion_scopes` with bounded explicit subject/tenant/matter
partitions (or `reconcile_ingestion` with point-read candidates containing the
same server-owned binding). The state store's operation-scope index is queried
with a limit and each returned operation is rechecked by point read. Foreign
bindings are skipped before any provider call. Stale
`RECOVERY_PENDING`/`PENDING` operations and `STARTING` operations with a known
job ID use the normal single status path.
`STARTING` with no job ID is ambiguous and remains quarantined; it must not be
marked terminal because doing so could permit a duplicate provider job. Expired
or malformed records fail closed and are not deleted by reconciliation.

## Gateway grants and invocations

`Boto3DynamoGatewayGrantRepository` maintains a separate expiry index item for
each grant/invocation under a UTC-day partition. The scheduler queries at most
today and yesterday, with a bounded limit; it never scans the table. Before any
point-read, the candidate matter must be in the explicit union of the upload
and ingestion matter scopes for this beta run. Every permitted candidate is
then point-read and revalidated for exact entity shape, subject, matter, key
and numeric expiry before deletion. Foreign or malformed candidates remain
for investigation. Older missed index rows are bounded by the existing
DynamoDB TTL cleanup and are not an authorization dependency.

## Failure handling and evidence

Treat `ambiguous` and `failed` report entries as actionable. Retry the same
candidate after provider/storage recovery; do not broaden the scope or batch
limit. Preserve the report metadata (counts, opaque IDs, scope and timestamp)
without recording tokens, document bodies, signed URLs, passages or secrets.

The Phase 02 table enables TTL on `ttl`, and the deployed source bucket remains
unversioned with one-day quarantine and 30-day canonical lifecycle rules. S3
lifecycle is cleanup assistance, not a guaranteed deadline or access-control
decision. The current reconciler deliberately has no version-management path;
reconciliation must not treat a version ID as an authorization input. No AWS
call is part of local tests. The deployed schedule, IAM permissions and alarms
must be revalidated after every approved update; the bounded synthetic exercise
above is complete, while broader provider fault/recovery and owner sign-off
remain production-promotion gates.

## One-shot synthetic stale-upload exercise

`scripts/phase14_reconciliation_exercise.py` is the supervised operator
runner for exactly one synthetic upload fixture. Its default mode is offline
and performs only argument validation. The AWS mode is intentionally not
enabled by tests or deployment tooling. Before writing anything it reads the
deployed reconciliation Lambda configuration and requires the exact table,
bucket, beta tenant and upload `{tenantId,matterId}` scope to match the command
line. It then conditionally creates one fictional `PENDING_UPLOAD` item and
exactly two quarantine objects (`original.txt` and its metadata sidecar),
invokes the deployed Lambda once with a non-selector event, and requires
`uploads.changed == 1`, `uploads.failed == 0`, metadata `FAILED`, and both
objects absent. Finally it deletes only the exact fixture item and keys and
verifies their absence. It never scans DynamoDB/S3 and never targets a
canonical `tenants/` key.

Local preflight (zero AWS calls):

```powershell
python -B scripts/phase14_reconciliation_exercise.py `
  --function-name <deployed-reconciliation-function-name> `
  --table-name <deployed-metadata-table> `
  --bucket-name <deployed-source-bucket> `
  --tenant-id <deployed-beta-tenant> `
  --matter-id <authorized-beta-matter> `
  --document-id <reserved-v4-fixture-uuid>
```

The only approved live command is the same command with `--execute` and the
exact acknowledgement `--approval I_UNDERSTAND_ONE_LIVE_RECONCILIATION_CALL`.
Use a scoped operator profile and
the deployed region explicitly, for example `--region eu-west-1`. The command
may incur Lambda, DynamoDB and S3 request/storage charges; it is not a billing
cap. Do not reuse a document UUID or keys from an earlier attempt. If the
runner reports a cleanup failure, stop and inspect only the exact printed
fixture inputs before any retry; do not broaden the scope or invoke the
Lambda again under the same exercise.
