# Phase 14 data governance runbook

Status: operator-only governance boundary implemented; the authenticated beta
is deployed, but no public destructive route exists. This document authorizes
no AWS operation.

## Beta data policy

The beta accepts only fictional or already-public documents. Real client/legal
data, anonymous signup, and document-body export are prohibited. The default
export is metadata-only: IDs, names, media type, date/jurisdiction,
confidentiality class, lifecycle/scan status, bounded size, timestamps, and
review-task metadata. It excludes document bodies, S3 keys, snapshots, answer
passages, notes, access/refresh tokens, cookies, and provider details. An
approved-user-data export flag currently fails closed because that policy is
not approved for beta.

## Operator authorization

`authorize_operator_scope()` first re-verifies a trusted identity against the
authoritative user and matter records, derives tenant/matter server-side, and
requires an operator/data-steward/admin role. The resulting sealed scope is
the only input accepted by governance operations. Browser tenant IDs, matter
IDs, object keys, and deletion selectors are never trusted.

## Export

`OperatorDataGovernanceService.export_matter_metadata()` uses the exact
authorized partition, bounded document/review queries, and a 256 KiB output
ceiling. It performs no scan and refuses to guess when the record limit is
exceeded. Export results are treated as operator output and must be stored in
an approved encrypted destination by the future operator job; this slice does
not create that destination.

## Delete document or matter

Document deletion first atomically tombstones the document as `FAILED`, so
retrieval is blocked, then removes the server-derived canonical object,
canonical metadata sidecar, quarantine object, and quarantine sidecar before
removing the exact Dynamo metadata item. Storage is cleaned before metadata
deletion so a provider failure leaves a retryable partial report rather than
hiding data.
Missing records and already-deleted objects are idempotent. Malformed or
cross-scope key bindings fail closed.

Matter deletion enumerates only the exact tenant/matter partitions with a
bounded query. It refuses to start beyond the configured limit, deletes each
document and review task by exact key, and reports partial failures without
crossing scope. Known in-memory/history/review-candidate/ingestion state is
removed through the state-store scope operation where indexed interfaces
allow it. State without a scope index remains subject to its application
expiry/TTL and is an explicit production follow-up; TTL is not treated as
proof of exact deletion.

The report sets `indexCleanupPending=true` when documents were deleted. This
is intentional: Bedrock Knowledge Bases may retain stale vectors until a
subsequent bounded synchronization observes the deletion. The retrieval gate
drops/fails closed for any result whose exact document is absent, not
`INDEXED`, or not malware-clean. In the public composition it additionally
HEAD-checks the server-owned canonical `Document.s3Key`; a missing or failed
HEAD blocks the complete response before evidence resolution/writing, and a
provider URI/key cannot redirect that check. Privacy deletion is not declared
complete until index-cleanup evidence exists.

## Review retention

Closed review tasks older than the approved cutoff may be archived through
`archive_closed_reviews()`. Archival preserves bounded workflow metadata but
strips snapshot, note, and resolution text and records `archivedAt`.
Unclosed tasks are never archived by this operation. Repeated runs are
idempotent; limits and conditional transitions are required.

## Incident and retry procedure

1. Keep the generic `GovernanceReport`; never log provider exceptions, object
   bodies, tokens, or export payloads.
2. If a report contains `object_cleanup`, `metadata_cleanup`,
   `review_cleanup`, or `state_cleanup`, retain the record and retry the same
   exact server-derived scope after the provider incident is resolved.
3. If a binding or authorization failure is reported, stop and investigate;
   do not broaden the selector or retry with browser-supplied tenant data.
4. Reconcile remaining canonical/quarantine objects and metadata using the
   bounded partition queries and server-owned IDs. Escalate any item that
   cannot be proven to belong to the target scope.

Promotion to `prod` remains blocked until retention periods, operator identity,
export destination, backup/legal-hold behavior, deletion evidence, and
cost/teardown ownership are separately approved.
