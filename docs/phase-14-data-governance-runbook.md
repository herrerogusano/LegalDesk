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

Agentic chat jobs (Phase 15, ADR-021 D4) are part of the matter scope. A job
holds the question while it is active and the rendered answer once it has
succeeded, and DynamoDB TTL (1 hour) is not an exact delete, so matter deletion
also removes them. They live under one partition per tenant and matter
(`LEGALDESK#P15#CHATJOB#<tenant>#MATTER#<matter>`), so the same bounded query
removes the jobs of **every subject** of the matter, not only the operator's.
Jobs are deleted in bounded batches (at most the operation limit, 100, per
call). Their count is added to `stateRecordsRemoved`. If more jobs remain after
a batch, the report carries the retryable failure `job_cleanup_incomplete` and
is **not** `ok`: a partial deletion is never reported as complete. Repeat the
same `delete_matter` call until it reports no failure; each call removes the
next batch, so a matter with any number of jobs ends up empty without waiting
for TTL. A provider error is reported as `job_cleanup`. The job record never stores a token or the session cookie value: it keeps only
a derived reference (the digest the durable store already keys sessions by)
until the job ends, and it is removed on success and on failure. Citation handles created for an agentic answer follow the existing
state deletion.

The report sets `indexCleanupPending=true` when documents were deleted. This
is intentional: Bedrock Knowledge Bases may retain stale vectors until a
subsequent bounded synchronization observes the deletion. The retrieval gate
drops/fails closed for any result whose exact document is absent, not
`INDEXED`, or not malware-clean. In the public composition it additionally
HEAD-checks the server-owned canonical `Document.s3Key`; a missing or failed
HEAD blocks the complete response before evidence resolution/writing, and a
provider URI/key cannot redirect that check. Privacy deletion is not declared
complete until index-cleanup evidence exists.

## Retention: no automatic expiry of beta documents

Decision (owner, 2026-11-03): **no automatic expiry** applies to canonical originals (`tenants/`). The Phase 02 rule `ExpireFictionalOriginals`
(30 days) deleted the object while the metadata record and the Knowledge Base entry stayed behind, so everything uploaded on 2026-09-28 became
a ghost: INDEXED with no object, and retrieval fails closed for the whole matter. Beta documents are deleted **only** through
`OperatorDataGovernanceService` (record, objects and, with the sync below, the Knowledge Base stay consistent). Only `quarantine/` uploads still
expire by lifecycle (one day; they have no record that matters and no vector).

A planned expiry of fictional files is acceptable as a concept, but never silently. It would have to come with the code that deletes record and
KB entry in step with the object. Two guards enforce that: `tests/test_phase15_document_retention.py` fails if the Phase 02 template has an
enabled expiry that can reach `tenants/` while `legaldesk.reconciliation.CANONICAL_EXPIRY_KEEPS_RECORDS_CONSISTENT` is `False` (it is today;
whoever builds that code sets it to `True` in the same change), and the reconciliation Lambda reads the **deployed** lifecycle rules on every run
and alarms while one can expire canonical originals.

## Ghost documents: confirm the cause and repair

A **ghost document** is a record that is `INDEXED` and malware-`CLEAN` whose canonical object is gone. The reconciliation Lambda (every 15 minutes)
looks for them and **only reports**: one query per configured matter (the same bounded keyed partition query it already makes for uploads, at most
`LimitPerScope` documents), one exact-key existence check per INDEXED+CLEAN document and one read of the bucket lifecycle rules. No scan, nothing
is written or deleted.

**Why it does not tombstone the record as FAILED.** The vector stays in the Knowledge Base (the role cannot delete from it), so retrieval would
still hit it and fail; and the alarm would go quiet while the inconsistency remained. Tombstoning is safe and idempotent, but it hides the finding
without repairing it. The repair is the governance delete plus a Knowledge Base sync, an operator step with an authorised identity. Report only.

### What the alarm and the logs say

Alarm `DocumentIntegrityAlarm` (stack `LegalDeskPhase14Reconciliation`, metric `DocumentIntegrityFindings`, console-only unless a topic is passed) is in
`ALARM` when a run found a ghost or while the bucket has a lifecycle rule that can expire canonical originals (that configuration is what makes
ghosts). Each ghost has its own line in the reconciliation log group (`/aws/lambda/<stack>-reconciliation`):

```json
{"ageDays":36,"code":"object_missing","documentId":"<uuid>","event":"document_object_missing","probableCause":"s3_lifecycle_expiration","status":"INDEXED"}
```

`documentId` is the opaque UUID (no key, tenant, name or content). `ageDays` counts from `uploadedAt`. `probableCause` uses only facts the
reconciler can read; **only the first value names a cause, every other value says the cause is unknown**:

| `probableCause` | Meaning |
|---|---|
| `s3_lifecycle_expiration` | an enabled lifecycle rule can expire `tenants/` objects and the document is at least as old as its window: probable expiry |
| `unknown_younger_than_lifecycle_window` | such a rule exists but the document is younger than its window: not explained by it (manual deletion, a failed upload, another process) |
| `unknown_no_lifecycle_expiry` | no rule can expire canonical objects: not the lifecycle (look for a manual or application delete) |
| `unknown_lifecycle_date_rule` | the rule expires at a fixed date, so the window cannot be compared with the age |
| `unknown_lifecycle_unreadable` | the lifecycle rules could not be read (permission or API error): cause unknown |
| `unknown_age_unreadable` | the record has no readable upload time |

The summary line `document_objects_check` (`examined`, `missing`, `failed`, `canonicalExpiry`, `lifecycleWindowDays`, `unverified`, `findings`) is what the metric
sums. `unverified: true` (`failed > 0`, or `canonicalExpiry: unreadable`) means the check itself could not verify something: it counts as one finding, so **a detector that goes dark raises the alarm**. Investigate the role and the bucket; it does not mean a document is gone.

CloudWatch Logs Insights, to list the ghosts of the last day:

```
fields @timestamp, documentId, status, ageDays, probableCause
| filter event = "document_object_missing"
| sort @timestamp desc
```

### Confirm the cause (read-only)

```powershell
$bucket = "legaldeskphase02documents-documentbucket-ojlu4kvhlied"
aws s3api get-bucket-lifecycle-configuration --bucket $bucket        # which rules exist (the cause if `ExpireFictionalOriginals` or another tenants/ expiry is there)
aws s3api get-bucket-versioning --bucket $bucket                      # empty: unversioned, an expired object is gone for good
aws s3api list-object-versions --bucket $bucket --prefix "tenants/<tenant>/matters/<matter>/documents/<documentId>/"   # versions or delete markers, if any
aws bedrock-agent list-knowledge-base-documents --knowledge-base-id AQWCFL7239 --data-source-id XIA81RBIPG --max-results 100 --query "documentDetails[].[status,identifier.s3.uri]" --output text
```

A document that is `INDEXED` in the Knowledge Base listing with no object confirms the ghost. Compare `ageDays` with the lifecycle window before saying "expired".

### Repair: governance delete, then a Knowledge Base sync

Use `scripts/phase15_ghost_documents_cleanup.py`. Both commands are read-only unless `--execute` is given on `delete`.

```powershell
$env:LEGALDESK_OPERATOR_ACCESS_TOKEN = "<access token of the operator user>"   # never an argument; the application's worker environment variables must be set
python scripts/phase15_ghost_documents_cleanup.py audit  --tenant tnt_phase14_20260928 --matter mat_phase14_a_20260928                       # ids, record status, age, closed status
python scripts/phase15_ghost_documents_cleanup.py delete --tenant tnt_phase14_20260928 --matter mat_phase14_a_20260928 --document-id <uuid> --document-id <uuid>             # dry run
python scripts/phase15_ghost_documents_cleanup.py delete --tenant tnt_phase14_20260928 --matter mat_phase14_a_20260928 --document-id <uuid> --document-id <uuid> --execute   # delete
```

It builds the operator scope with `authorize_operator_scope`: **there is no bypass**. The identity must be a verified user whose authorization record
(`AUTH#USER#<subject>`) has a role in `operator`, `data_steward` or `admin`, whose `tenantIds` contain the matter's tenant, and whose `userId` is in the
matter's `authorizedUserIds`. The synthetic test user has `roles: ["member"]`, so **it does not qualify as it is**. The minimal, reversible way is a
conditional update of that one profile for the duration of the cleanup (owner-approved write; nothing else changes):

```powershell
# key.json  : {"pk":{"S":"AUTH#USER#<subject>"},"sk":{"S":"PROFILE"}}
# grant.json: {":r":{"L":[{"S":"data_steward"}]},":t":{"S":"User"},":v":{"S":"data_steward"}}
aws dynamodb update-item --table-name $table --key file://key.json --update-expression "SET roles = list_append(roles, :r)" --condition-expression "entityType = :t AND NOT contains(roles, :v)" --expression-attribute-values file://grant.json
# ... run the tool ... then revert (revert.json: {":orig":{"L":[{"S":"member"}]},":v":{"S":"data_steward"}}):
aws dynamodb update-item --table-name $table --key file://key.json --update-expression "SET roles = :orig" --condition-expression "contains(roles, :v)" --expression-attribute-values file://revert.json
```

Only the application's governance code reads these roles (`OPERATOR_ROLES`); no public route uses them. A dedicated operator user is the cleaner alternative if one exists.
Also needed: local AWS credentials that can read/write the metadata table and delete the document objects (the operator's own, not the application's).

The tool refuses a document whose object **exists** (`refused_object_present`), is idempotent (an id without a record is `already_absent`), handles at most 10 ids,
and prints closed statuses only. After `--execute` it prints the two commands that finish the repair: the sync
(`aws bedrock-agent start-ingestion-job --knowledge-base-id AQWCFL7239 --data-source-id XIA81RBIPG`) and, per document, a `list-knowledge-base-documents`
query that **must print nothing** once the sync has completed (`get-ingestion-job` shows `COMPLETE`). Until then retrieval still blocks on the stale vector.
Records that are `FAILED` and have no object (they appear in the inventory with status Failed) are deleted the same way. Privacy deletion is not complete until the
sync evidence exists (see "Delete document or matter").

### Deploying the two stacks that carry this

1. **Phase 02 (`LegalDeskPhase02Documents`) — urgent, objects expire from 2026-11-08.** The repository holds no service role or pipeline for this stack: it was updated before
   (Phase 13 smoke) by the owner with a reviewed change set under their own credentials. Check `aws cloudformation describe-stacks --stack-name LegalDeskPhase02Documents --query "Stacks[0].RoleARN"` (none expected).
   Create a change set from `infra/cloudformation/phase-02-document-pipeline.yaml` with `ApplicationOrigin` `UsePreviousValue`. Read first `aws cloudformation describe-type --type RESOURCE --type-name AWS::S3::Bucket` and confirm
   `LifecycleConfiguration` is not in `createOnlyProperties` (the tool does not carry that schema). Expected: one `Modify` of `DocumentBucket`, property `LifecycleConfiguration`, `Replacement` `False`, nothing else
   (`check-change-set --profile phase02-no-canonical-expiry`). After: `get-bucket-lifecycle-configuration` shows only `ExpireQuarantineUploads`. The bucket has `DeletionPolicy: Retain`; a rolled-back update changes nothing in the data.
   Objects that already expired cannot come back (the bucket is unversioned); removing the rule saves the ones that have not.
2. **Reconciliation (`LegalDeskPhase14Reconciliation`).** New artifact (`ReconciliationCode*` parameters), the other parameters `UsePreviousValue`. Expected (`check-change-set --profile reconciliation-object-integrity`): `Add`
   `DocumentIntegrityMetricFilter` and `DocumentIntegrityAlarm`; `Modify` `ReconciliationFunction` (`Code`) and `ReconciliationExecutionRole` (`Policies`), `Replacement` `False`; the IAM change is exactly two statements,
   `s3:ListBucket` on the bucket conditioned on `s3:prefix` = the beta canonical `original.pdf`/`original.txt` key patterns, and `s3:GetLifecycleConfiguration` on the bucket (read-only, no object access, no wildcard).
   Cost: the metric filter creates one custom metric (about 0.30 USD a month) and the alarm adds about 0.10 USD a month: about 0.40 USD, 0 inside the CloudWatch free tier;
   `EnableDocumentIntegrityAlarm=false` removes both and keeps the log lines. Existing alarms and their cost do not change.

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
   `review_cleanup`, `state_cleanup`, `job_cleanup`, or `job_cleanup_incomplete`, retain the record and retry the same
   exact server-derived scope after the provider incident is resolved.
3. If a binding or authorization failure is reported, stop and investigate;
   do not broaden the selector or retry with browser-supplied tenant data.
4. Reconcile remaining canonical/quarantine objects and metadata using the
   bounded partition queries and server-owned IDs. Escalate any item that
   cannot be proven to belong to the target scope.

Promotion to `prod` remains blocked until retention periods, operator identity,
export destination, backup/legal-hold behavior, deletion evidence, and
cost/teardown ownership are separately approved.
