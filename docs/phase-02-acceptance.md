# Phase 02 acceptance — document pipeline

## Result

Phase 02 is complete using local and static AWS evidence. The plan marks the
AWS smoke test as optional, so no S3 bucket, DynamoDB table, IAM role, inference,
ingestion, or retrieval was created or invoked.

## Implemented

- `DocumentStatus` exposes `PENDING_UPLOAD`, `UPLOADED`, `PENDING_INGESTION`,
  `INDEXED`, and `FAILED`; transitions are validated by the metadata
  repository.
- The intended lifecycle is `PENDING_UPLOAD → UPLOADED → PENDING_INGESTION →
  INDEXED`, with `FAILED` for failures at the applicable stage.
- Upload authorization creates server-owned metadata in `PENDING_UPLOAD` and
  returns a presigned PUT URL. Confirmation looks up the document by the
  authorized tenant/matter scope and checks its stored S3 key with
  `HeadObject` (size, content type, and signed metadata) before transitioning
  it to `UPLOADED`; client-provided keys or statuses are never used. The old
  generic status-transition endpoint is rejected.
- `DocumentPipeline` is HTTP-neutral. It builds `RequestContext` before any
  validation or storage, so an unauthorized matter has no storage side effect.
- Document IDs and S3 keys are generated server-side. Keys are
  `tenants/{tenant}/matters/{matter}/documents/{documentId}/original`.
- Object storage and metadata are separate protocols. DynamoDB metadata never
  includes the body; its single-table keys are
  `TENANT#{tenant}#MATTER#{matter}` and `DOCUMENT#{documentId}`.
- Upload metadata validation requires a matching `.pdf`/`.txt` extension, an
  ISO date, a declared size within the limit, and a small fictional
  confidentiality allowlist without requiring the body. The local direct-upload
  helper derives the size from its body and additionally validates PDF magic
  bytes or UTF-8 text. Missing S3 objects do not advance the lifecycle.
- In-memory fakes support deterministic local tests; boto3 adapters are lazy
  and injectable and were not called in this phase.
- IaC defines encrypted, private, TLS-only S3 with a 30-day lifecycle,
  on-demand encrypted DynamoDB, and a least-privilege pipeline role with
  prefix-restricted object management (including `GetObject` only for upload
  confirmation). Stack teardown removes both resources after the bucket is
  emptied.
- Two small, clearly distinguishable fictional fixtures are included.

## Local evidence

Run from the repository root:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q backend/src tests
git diff --check
aws cloudformation validate-template --template-body file://infra/cloudformation/phase-02-document-pipeline.yaml --region eu-west-1
```

No AWS resources, inference, ingestion, or retrieval were used. The template
is reproducible but intentionally has no local smoke call. S3, DynamoDB,
CloudFormation, Lambda execution, requests, and stored data
can incur AWS charges if a future approved deployment is performed.

## Criteria mapping

The unit tests demonstrate User A → Matter A authorization, User A → Matter B
deny, server-side key generation, initial `PENDING_UPLOAD`, confirmation only
when the object exists, cross-matter confirmation denial, resistance to
client-driven status transitions, scope-bearing metadata, no body in metadata,
visible lifecycle status, authorized listing, validation failures, and storage
failure handling. CloudFormation validation demonstrates that the
infrastructure definition is reproducible; a live AWS smoke remains
deliberately optional.

Documents left in `PENDING_UPLOAD` are intentionally retained for now. Cleanup
of abandoned uploads is a documented production gap and is not automated in
Phase 2.
