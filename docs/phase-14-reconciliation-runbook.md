# Phase 14 bounded reconciliation runbook

Status: **local implementation only; deployment remains approval-gated**.

The reconciler receives an explicit batch of authorized tenant/matter scopes or
opaque operational candidate IDs. Each batch is bounded (default maximum: 100)
and uses document-partition queries or point reads. It never scans the shared
DynamoDB table. A scheduler/operator must supply the candidate partition; this
module does not infer scope from browser input.

## Uploads

Run `ReconciliationService.reconcile_pending_uploads` for a small list of
server-authorized `ReconciliationScope` values. Only `PENDING_UPLOAD` records
older than the approved stale threshold are considered. The original and
metadata sidecar are deleted first; metadata moves to `FAILED` only after both
deletes succeed. A failed delete or malformed timestamp remains pending and is
reported for retry. A second run is safe: terminal records are skipped.

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

Run `reconcile_gateway` only with bounded point-read candidates from an
authorized operational source. A record is deleted only when its entity shape,
subject, matter, key and numeric expiry all match and the expiry is in the
past. Malformed or scope-mismatched records remain for investigation; DynamoDB
TTL is cleanup assistance only. The current Gateway schema has no discovery
index, so candidate enumeration is an explicit operational input rather than a
table scan.

## Failure handling and evidence

Treat `ambiguous` and `failed` report entries as actionable. Retry the same
candidate after provider/storage recovery; do not broaden the scope or batch
limit. Preserve the report metadata (counts, opaque IDs, scope and timestamp)
without recording tokens, document bodies, signed URLs, passages or secrets.

No AWS call is part of local tests. Production scheduling, IAM permissions,
alarms, candidate-source/index design and deployment require the Phase 14
deployment approval gate.
