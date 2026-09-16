# Phase 03 — Knowledge Base and authorized retrieval

## Architecture

Phase 03 adds a Bedrock Knowledge Base with an S3 data source and an S3 Vectors
index. It uses Titan Text Embeddings V2 at 1024 dimensions in `eu-west-1`,
cosine distance, and two-level hierarchical chunking: parent chunks up to 1200
tokens, child chunks up to 300 tokens, and 60 overlap tokens. The
CloudFormation stack reuses the Phase 02 document bucket by name; it does not
create or own that bucket.
The Knowledge Base references the S3 Vectors index by `IndexArn`; CloudFormation
accepts either this ARN by itself or the vector bucket ARN plus index name, not
all three fields together.

The S3 Vectors index configures `AMAZON_BEDROCK_TEXT` and
`AMAZON_BEDROCK_METADATA` as non-filterable metadata keys, as required for the
Bedrock Knowledge Base integration and to keep its internal text/metadata out
of the filterable metadata budget. This does not disable application filters:
the sidecar's LegalDesk attributes, including `tenantId` and `matterId`, stay
filterable and are used for authorization-scoped retrieval.

The smaller child chunks improve retrieval precision; Bedrock can return their
broader parent chunks to preserve context. AWS does not recommend hierarchical
chunking with S3 Vectors in general because parent-child metadata consumes the
vector metadata budget, and high chunk sizes (over 8000 combined tokens) can
exceed metadata limits. This configuration uses 1200 + 300 = 1500 tokens, well
below that threshold, and the planned fictional dataset is very small. This is
a deliberate conservative choice exercised once with the fictional smoke
dataset. AWS CloudFormation documents chunking configuration updates as
replacement operations; changing this strategy after connecting the data
source requires replacing it and performing a new sync.

The object key now has a Bedrock-supported extension derived from the validated
media type:

```text
tenants/{tenantId}/matters/{matterId}/documents/{documentId}/original.txt
tenants/{tenantId}/matters/{matterId}/documents/{documentId}/original.pdf
```

After `HeadObject` confirms an uploaded object and its signed metadata, the
backend writes `{source-key}.metadata.json` beside it. The sidecar contains
`tenantId`, `matterId`, `documentId`, `mediaType`, `jurisdiction`, and
`confidentiality`. Every attribute is a typed Bedrock `STRING` with
`includeForEmbedding: false`; attributes are filterable metadata and are not
added to the embedding text. The sidecar write completes before upload
confirmation changes the document to `UPLOADED`. A sidecar write failure leaves
the document pending so confirmation can be retried.

The S3 data source includes the `tenants/` prefix. Bedrock's service role can
list only that prefix and read only objects matching the Phase 02 document
pattern `.../original.*`, which includes the supported source and its sidecar.
The role can invoke only the selected Titan embedding model and manage vectors
only in the configured LegalDesk index. IAM never grants the model or a client
direct S3 access.

## Authorized retrieval boundary

`legaldesk.retrieval.search_legal_documents` is HTTP-neutral. It accepts a
verified identity, an untrusted requested matter selector, and a query. It
resolves the effective user, tenant, and matter through the authorization store
before calling Bedrock. The caller cannot supply a tenant ID, S3 key, document
ID, or retrieval filter. The service builds this mandatory filter from the
server-created `RequestContext`:

```json
{
  "andAll": [
    {"equals": {"key": "tenantId", "value": "<stored tenant>"}},
    {"equals": {"key": "matterId", "value": "<authorized matter>"}}
  ]
}
```

The service also checks every returned result against the authorized tenant and
matter, and drops results with missing or mismatched scope metadata. This
fail-closed check protects against stale or misconfigured index data in addition
to Bedrock's server-built filter. An empty authorized result set is an empty
tuple; it is not an error.

Each retained passage has text, an optional relevance score, and a citation
containing a generated citation ID, document ID, S3 source URI when returned,
page number or section when available, and an allowlisted set of source
metadata. No raw result metadata outside that allowlist is passed through.

At the end of this phase, a future chat layer may pass only the selected
passage text and its citation/source metadata to a model. It will not pass S3
credentials, bucket access, a free tenant selector, or direct Knowledge Base
access to the model. Phase 03 does not construct prompts, call a generation
model, or implement chat.

## Document lifecycle and sync

The intended lifecycle remains:

```text
PENDING_UPLOAD → UPLOADED → PENDING_INGESTION → INDEXED
                         ↘ FAILED when an operation fails
```

An explicit operator sync selects tenant/matter/document references from
stored scopes, starts one data-source-wide Bedrock ingestion job, and marks
only those selected `UPLOADED` documents `PENDING_INGESTION` after Bedrock
returns a job ID. Before starting, it uses `HeadObject` to confirm that each
selected original and `.metadata.json` sidecar still exists, has a positive
size, and that the sidecar has JSON content type. The original's S3 user
metadata must match the stored tenant, matter, and document IDs; the sidecar's
S3 user metadata document ID must match the selected document. This protects
against scope-mismatched objects and an original removed by S3 lifecycle expiry
being marked indexed after an otherwise successful empty sync. A `FAILED`
document may be retried, but only after the same source and sidecar checks pass.
One invocation can include documents from both fictional matters, avoiding a
second full data-source sync. The process marks them
`INDEXED` only after the job reports `COMPLETE` and
`numberOfDocumentsFailed` is zero. A failed/stopped job marks the selected
documents `FAILED`. A timeout leaves them `PENDING_INGESTION`; a completed job
with partial or unavailable failure statistics also leaves them pending for
operator reconciliation. Starting a sync never marks a document indexed.

The sync script is an operator workflow, not an HTTP route. It requires an
explicit list of tenant/matter/document references and does not scan DynamoDB.
It uses the regional boto3 `bedrock-agent` control-plane client for
`StartIngestionJob` and `GetIngestionJob` (not `bedrock-agent-runtime`).
The operator's execution identity needs `bedrock:StartIngestionJob` and
`bedrock:GetIngestionJob` on the approved Knowledge Base, `dynamodb:GetItem` and
`dynamodb:UpdateItem` on the Phase 02 metadata table, and `s3:GetObject` only on
the supported original and sidecar key patterns. These permissions are for the
operator invocation; the Bedrock execution role remains separate.
The Bedrock sync itself crawls the configured `tenants/` prefix. Do not run a
second sync while one is in progress. The service updates only the explicitly
selected documents.

## Deliberate setup and sync commands

Install the optional AWS SDK dependency for an approved operator run:

```powershell
python -m pip install -e ".[aws]"
```

Static template validation does not create resources:

```powershell
aws cloudformation validate-template --template-body file://infra/cloudformation/phase-03-knowledge-base.yaml --region eu-west-1
```

The following deployment creates a Knowledge Base, S3 Vectors bucket and
index, IAM role, and data source. These resources and later ingestion/retrieval
can incur charges. Do not run it until deployment and associated spend are
explicitly approved. Use the Phase 02 bucket name as the parameter:

```powershell
aws cloudformation deploy --template-file infra/cloudformation/phase-03-knowledge-base.yaml --stack-name legaldesk-phase-03 --capabilities CAPABILITY_NAMED_IAM --parameter-overrides DocumentBucketName=<Phase02DocumentBucketName> --region eu-west-1
```

After an approved deployment, copy the output IDs and explicit document IDs
from the metadata store, then run one deliberate sync for a matter:

```powershell
python backend/scripts/sync_knowledge_base.py --knowledge-base-id <KnowledgeBaseId> --data-source-id <DataSourceId> --table-name <Phase02DocumentMetadataTable> --document-bucket <Phase02DocumentBucketName> --document <tenantId>,<matterId>,<documentId> --document <otherTenantId>,<otherMatterId>,<otherDocumentId> --region eu-west-1
```

The script starts a billable ingestion job and polls until completion or its
30-minute timeout. It is not run automatically by tests or deployment. A
timeout, failed job, or partial document failure is reported without claiming
the documents are indexed. Retrieval uses the `Retrieve` API through the
authorized Python service; do not test it in a loop.

## Teardown

The Phase 03 stack owns the Knowledge Base, S3 data source, S3 Vectors index,
vector bucket, and execution role. The data source uses
`DataDeletionPolicy: DELETE`, so deleting it also requests deletion of its
vectors. The Phase 02 document bucket and metadata table are outside this stack
and remain intact. After confirming the right stack and that no further
retrieval is needed, delete the stack:

```powershell
aws cloudformation delete-stack --stack-name legaldesk-phase-03 --region eu-west-1
aws cloudformation describe-stacks --stack-name legaldesk-phase-03 --region eu-west-1
```

S3 vector buckets must be empty before deletion. If stack teardown reports a
non-empty vector bucket, inspect the vector index and data source state before
retrying teardown; do not delete the Phase 02 source objects as a cleanup step.

## Cost and verification status

No AWS resources remain after the approved smoke. The live smoke deployed the
two phase stacks, ingested the two fictional fixtures once, made two scope-isolated
retrieval calls plus one off-topic query, then deleted the stacks and fixture
objects. The off-topic query returned the nearest chunk, so the live test does
not establish empty-result behavior; local tests cover that branch. No
generative-model inference or legal data was used. Actual charges were not
queried, and production-scale AWS behavior remains unverified.

Abandoned uploads can still remain in `PENDING_UPLOAD`; Phase 02 cleanup is not
automated. A future production improvement is a bounded cleanup job that checks
age and stored ownership before deleting an abandoned original and sidecar.
The Phase 02 bucket also expires source objects after 30 days. S3 expiry does
not itself run a Bedrock sync, so stored vectors can outlive their source until
the next deliberate sync; source deletion/expiry reconciliation should be
scheduled before production retention policies are enabled.
