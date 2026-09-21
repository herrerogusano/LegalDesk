# Phase 03 — Knowledge Base and authorized retrieval

## Architecture

Phase 03 adds a Bedrock Knowledge Base with an S3 data source and an S3 Vectors
index. It uses Titan Text Embeddings V2 at 1024 dimensions in `eu-west-1`,
cosine distance, and fixed-size chunking with `MaxTokens: 800` and
`OverlapPercentage: 15`. The CloudFormation stack reuses the Phase 02 document
bucket by name; it does not create or own that bucket.
The Knowledge Base references the S3 Vectors index by `IndexArn`; CloudFormation
accepts either this ARN by itself or the vector bucket ARN plus index name, not
all three fields together.

The S3 Vectors index configures `AMAZON_BEDROCK_TEXT` and
`AMAZON_BEDROCK_METADATA` as non-filterable metadata keys, as required for the
Bedrock Knowledge Base integration and to keep its internal text/metadata out
of the filterable metadata budget. This does not disable application filters:
the sidecar's LegalDesk attributes, including `tenantId` and `matterId`, stay
filterable and are used for authorization-scoped retrieval.

Fixed-size chunking keeps the S3 Vectors metadata profile simpler by avoiding
hierarchical parent-child metadata, which AWS cautions can consume the vector
metadata budget. The 800-token maximum keeps passages bounded; 15% overlap
retains some context at chunk boundaries. Overlap repeats text between adjacent
chunks, so the resulting embedding and vector counts depend on the source
documents and these settings. This is a compatibility and cost-control tradeoff,
not a claim that fixed-size chunking is always cheaper: the actual cost impact
depends on document lengths, chunk count, embedding use, and vector storage.

The earlier approved smoke used the former hierarchical settings (parent 1200,
child 300, overlap 60 tokens). It is historical evidence for that configuration
only and does not validate the current fixed-size settings. This update has been
validated locally; it has not been deployed, ingested, or tested against AWS.
AWS CloudFormation documents chunking configuration updates as replacement
operations; changing this strategy after connecting the data source requires
replacing it and performing a new sync.

The object key now has a Bedrock-supported extension derived from the validated
media type:

```text
tenants/{tenantId}/matters/{matterId}/documents/{documentId}/original.txt
tenants/{tenantId}/matters/{matterId}/documents/{documentId}/original.pdf
```

After `HeadObject` confirms an uploaded object and its signed metadata, the
backend writes `{source-key}.metadata.json` beside it. The sidecar contains
`tenantId`, `matterId`, `documentId`, `documentName`, `mediaType`,
`jurisdiction`, and `confidentiality`. Every attribute is a typed Bedrock
`STRING` with `includeForEmbedding: false`; attributes are filterable metadata
and are not added to the embedding text. The builder serializes the flat map of
seven custom attributes as compact UTF-8 JSON and rejects it at 1 KiB or more;
upload initiation runs this check before metadata persistence and presigning,
and sidecar serialization reuses that builder. A local test exercises maximum
filename and jurisdiction lengths with fictional scope IDs. Bedrock Knowledge
Bases documents the stricter S3 Vectors integration limits as 1 KiB of custom
metadata (filterable and non-filterable combined) and 35 keys per vector; these
govern this project. Native S3 Vectors separately documents 2 KiB filterable,
40 KiB total metadata, 50 total keys, and 10 non-filterable keys per index.
The Phase 03 index reserves two non-filterable Bedrock keys. The check covers
our local custom map only; it does not inspect Bedrock's transformed vector
metadata or establish live ingestion behavior. See [Bedrock Knowledge Bases
metadata support](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html)
and [native S3 Vectors limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html).
The sidecar write completes before upload confirmation changes the document to
`UPLOADED`. A sidecar write failure leaves the document pending so confirmation
can be retried.

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

No AWS resources remain after the approved historical smoke. It deployed the
two phase stacks with the former hierarchical chunking settings, ingested the
two fictional fixtures once, made two scope-isolated retrieval calls plus one
off-topic query, then deleted the stacks and fixture objects. The off-topic
query returned the nearest chunk, so the live test does not establish
empty-result behavior; local tests cover that branch. No generative-model
inference or legal data was used. Actual charges were not queried, and
production-scale AWS behavior remains unverified. The current fixed-size update
was checked only with local tests and static checks; no AWS call was made for
this change.

Abandoned uploads can still remain in `PENDING_UPLOAD`; Phase 02 cleanup is not
automated. A future production improvement is a bounded cleanup job that checks
age and stored ownership before deleting an abandoned original and sidecar.
The Phase 02 bucket also expires source objects after 30 days. S3 expiry does
not itself run a Bedrock sync, so stored vectors can outlive their source until
the next deliberate sync; source deletion/expiry reconciliation should be
scheduled before production retention policies are enabled.

## Phase 04 — Grounded chat and citations

`legaldesk.chat.parse_chat_request` accepts exactly `conversationId`,
`sessionId`, `matterId`, and `question`. Conversation and session selectors
must be 1–128 ASCII token characters; the matter selector has the same bounded
syntax but remains untrusted. The parser rejects unknown fields such as
`tenantId` and retrieval filters. The API adapter must supply a
`VerifiedIdentity` only after identity-provider signature, issuer, audience,
and expiry checks. Phase 04 is HTTP-neutral and does not implement that
adapter.

`answer_question` calls the existing authorized retrieval service before the
generator. Retrieval derives scope from the authorization store and filters
and rechecks tenant/matter server-side. An empty result returns this canonical
response without invoking a model:

```json
{
  "answer": "No se encontró evidencia suficiente en los documentos autorizados para responder.",
  "citations": [],
  "evidenceStatus": "insufficient_evidence",
  "disclaimerRequired": true,
  "promptVersion": "1.2.0",
  "promptSha256": "<sha256 of the loaded prompt artifact>"
}
```

For non-empty results, generation receives the validated server-loaded prompt
artifact, the question, and each retrieval-issued citation ID plus passage
text. The only prompt source of truth is
[`prompts/legaldesk-system.md`](../prompts/legaldesk-system.md); the file has
an explicit ID and semantic version. The filesystem provider enforces a
32-KiB limit, strict UTF-8, closed metadata fields, valid ID/version, and
non-empty text. Its path is configured by the server and cannot be selected
through browser input. Missing or invalid prompt configuration fails closed
without calling the generator.

Tenant/matter IDs, conversation/session selectors, document metadata,
credentials, provider clients, and retrieval filters do not cross the
generation boundary. `GenerationRequest` carries a `SystemPromptArtifact`
with content, version, and SHA-256; `ChatResponse` carries the version and
hash for traceability, without logging or returning the prompt body. Prompt
language guides model behavior and tools; it does not implement authorization.

The generator returns exactly `answer`, `citationIds`, and `evidenceStatus`
(`answerable`, `ambiguous`, or `insufficient_evidence`). The backend accepts
only unique citation IDs present in the retrieved set. `answerable` and
`ambiguous` require at least one valid citation. For `insufficient_evidence`,
an empty citation list returns the canonical no-evidence response; valid cited
passages preserve a partial explanation of what the documents establish and
what remains unsupported. A malformed response, invented/duplicate citation,
or unsupported status fails closed to the canonical response. The backend maps
accepted IDs to its own citation records, preserving document ID, name when
supplied, S3 URI, page, and section. Browser-facing citations omit tenant/matter
metadata. Every response currently sets `disclaimerRequired` to true because
the prototype is not legal advice.

Local test coverage includes answerable, ambiguous, insufficient evidence with
and without cited partial support, empty retrieval/no model call, invalid
citations, cross-document citations within one matter, cross-matter filtering,
and authorization denial before retrieval. `frontend/index.html` is a
responsive citation-panel example with fictional data; it can render the
response shape but is not connected to an API.

No AWS resources were created or modified in Phase 04. No Knowledge Base
retrieval or real-model inference was run. Unit tests use injected fakes, so
they add no AWS charges. Future AWS retrieval and text-generation calls are
billable and must be deliberate under `AWS_COST_POLICY.md`.

Remaining gaps are explicit: there is no HTTP/API adapter, durable conversation
or session ownership/binding, real provider generator, model-based grounding or
prompt-injection evaluation, AWS smoke, or production UI integration. Prompt
policy structure is covered locally; those tests do not claim a model obeys
the prompt. The Phase 01 Harness keeps its isolated minimal demonstration
prompt; it is not wired to the Phase 05 provider. A future generator adapter
must map `GenerationRequest.system_prompt` to the provider's system-message
field. The lexical selector checks do not establish ownership of a conversation
or session; persistence and actor/session/matter binding need a later phase
before multi-request use.
