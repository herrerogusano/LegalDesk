# Phase 03 acceptance — Knowledge Base and authorized retrieval

## Result

Phase 03 passed local acceptance and the explicitly approved minimal AWS smoke.
The smoke resources were torn down; the separate production deployment and
ongoing cost profile remain unverified.

## Implemented

- Reproducible CloudFormation defines an Amazon Bedrock Knowledge Base, S3 data
  source, S3 VectorBucket and Index, and a Bedrock execution role.
- The stack uses the existing Phase 02 bucket as a required parameter. It does
  not create or delete the source bucket or metadata table.
- S3 Vectors stores 1024-dimensional `float32` vectors with cosine distance.
- The vector index reserves `AMAZON_BEDROCK_TEXT` and
  `AMAZON_BEDROCK_METADATA` as non-filterable keys; LegalDesk scope attributes
  remain filterable. The Knowledge Base references the index by `IndexArn`;
  CloudFormation's schema rejects supplying the ARN, name, and bucket ARN
  together because its supported alternatives overlap.
- Titan Text Embeddings V2 is configured at 1024 dimensions. Data source
  chunking has exactly two hierarchical levels: parent 1200 tokens, child 300
  tokens, and 60 overlap tokens. Child chunks improve retrieval precision and
  Bedrock can return broader parent context. AWS cautions against hierarchical
  chunking with S3 Vectors because parent/child metadata consumes vector
  metadata; high chunk sizes above 8000 combined tokens may exceed limits. This
  configuration totals 1500 tokens and uses a minimal synthetic dataset.
  Chunking changes require replacing the data source and re-syncing.
- Phase 02 keys now end in `original.txt` or `original.pdf`; the extension is
  derived from validated media type. After confirming the object with S3, the
  backend writes a neighboring `.metadata.json` sidecar containing tenant,
  matter, document, MIME, jurisdiction, and confidentiality attributes. All
  fields are filterable and excluded from embedding.
- The Bedrock role can list only the `tenants/` source prefix, read only
  `.../documents/*/original.*`, invoke only the selected embedding model, and
  access only the configured S3 Vector index. It does not grant clients or the
  model direct S3 access.
- `search_legal_documents` authorizes a verified identity and requested matter
  before Bedrock access, constructs an AND filter from server-side context,
  rechecks returned tenant/matter metadata, and normalizes passages, scores,
  citation IDs, document IDs, source URI, page/section, and allowlisted source
  metadata. Missing or cross-scope metadata fails closed; no matches return an
  empty tuple.
- The operator-only sync script requires explicit tenant/matter/document
  references, checks each original and metadata sidecar still exist in S3 and
  verifies original tenant/matter/document metadata plus sidecar document ID,
  then starts one data-source-wide ingestion job. It transitions selected
  `UPLOADED` or validated `FAILED` documents to `PENDING_INGESTION` after the
  job starts and only marks them `INDEXED` after Bedrock reports `COMPLETE` with
  zero failed documents. Failed
  or stopped jobs set `FAILED`; timeout and partial/unknown failures leave
  documents pending for reconciliation. The script does not scan DynamoDB and
  is not invoked automatically by tests or deployment.
- Cleanup of abandoned `PENDING_UPLOAD` documents remains a documented
  production improvement; no automated cleanup is introduced.

## Approved AWS smoke evidence

- Executed 2026-09-16 in `eu-west-1` after explicit cost approval. The smoke
  deployed `legaldesk-phase-02` and `legaldesk-phase-03`; Bedrock reported the
  Knowledge Base `ULCRQ1JBV8` `ACTIVE` and S3 data source `NJLUFNPTYB`
  `AVAILABLE`.
- Through the real `DocumentPipeline.upload` and AWS adapters, uploaded only
  the two synthetic fixtures. Sundial (`tnt_aurora` / `mat_sundial`) document
  `8ddc62ab-fa8f-4ccf-83da-c6e5d78e11e1`; Glacier (`tnt_borealis` /
  `mat_glacier`) document `5a864742-c280-445e-817c-f88fd6290b7a`. Both had an
  S3 original, Bedrock sidecar, and DynamoDB metadata.
- Exactly one ingestion job (`RI5ESXMWAG`) completed `COMPLETE`, with 0 failed
  documents; DynamoDB showed both records as `INDEXED`.
- Two live `Retrieve` calls went through `search_legal_documents`: Sundial
  returned only its document under AND filter `(tnt_aurora, mat_sundial)`, and
  Glacier returned only its document under AND filter
  `(tnt_borealis, mat_glacier)`. No generation/chat model was called.
- One off-topic query still returned the nearest Sundial chunk. Thus live
  retrieval did not demonstrate an empty result; the current service has no
  relevance threshold. Empty-result normalization is covered by local tests.
- Teardown completed: both Phase 02 and Phase 03 stacks reached
  `DELETE_COMPLETE`, the Phase 03 vector bucket was absent, the four expected
  fixture objects/sidecars were removed from the exact Phase 02 bucket, and
  that bucket was absent. `legaldesk-phase-01` remained `UPDATE_COMPLETE`.
- The first Phase 03 change set was rejected before resource creation because
  the Knowledge Base supplied overlapping S3 Vectors configuration fields.
  The template now uses the supported `IndexArn`-only form, covered by a static
  IaC test. The failed review stack had no resources and was removed before the
  successful deployment.
- The smoke used one small two-document ingestion and three retrieval calls;
  S3, DynamoDB, S3 Vectors, embedding, ingestion, and retrieval could incur
  charges. Actual billing was not queried.

## Local evidence and criterion mapping

- Retrieval filter builder test asserts both tenant and matter equality clauses
  are joined with `andAll` and are derived from the authorization store.
- Retrieval tests prove Alice sees only Sundial, Bob sees only Glacier, a
  cross-matter request is denied before Bedrock, and cross-scope or missing
  scope results returned by a fake client are dropped.
- Tests cover evidence found, empty results, citation/source normalization,
  invalid queries, and rejection of caller-supplied tenant/custom filters.
- Document pipeline tests cover extension derivation, sidecar content and
  embedding exclusion, and sidecar writes after object confirmation.
- Ingestion tests show `INDEXED` is written only after a completed job with
  zero failed documents; partial and unknown failures remain pending.
- The CloudFormation template is statically validated. Tests use injected
  clients and synthetic fixtures. The live smoke used only fictional fixtures;
  no real legal data was used.
- Static IaC tests assert the two hierarchy sizes, overlap, absence of
  fixed-size chunking, and the required Bedrock non-filterable metadata keys in
  the Phase 03 template.

Run the local suite and static checks from the repository root:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q backend/src backend/scripts tests
git diff --check
aws cloudformation validate-template --template-body file://infra/cloudformation/phase-03-knowledge-base.yaml --region eu-west-1
aws cloudformation validate-template --template-body file://infra/cloudformation/phase-02-document-pipeline.yaml --region eu-west-1
```

The AWS `validate-template` calls validate definitions only and create no
resources. The separate approved smoke was deployed, exercised, and torn down
as recorded above. S3 Vectors, Bedrock Knowledge Base, embeddings, data
ingestion, and retrieval can incur charges.

## Operational limitations

- The smoke verified basic S3/Bedrock behavior, resource provisioning, model
  access for Titan embeddings, IAM trust, and end-to-end citations. Production
  reliability, larger datasets, ongoing costs, and retention/reconciliation
  behavior remain unverified.
- One sync job covers the full `tenants/` prefix. The script updates only the
  explicit documents selected by the operator. Completed jobs with partial
  failures do not identify which selected document failed, so those documents
  remain `PENDING_INGESTION` until a future reconciliation workflow exists.
- Abandoned uploads may remain in `PENDING_UPLOAD`; automatic cleanup is not
  part of this phase.
- Phase 02 expires S3 sources after 30 days. Vector deletion happens only after
  a later explicit data-source sync observes the deletion, so source/vector
  retention reconciliation remains an operational production gap.

No Phase 04 chat generation, prompts, RAG answer assembly, or later phase work
is included.
