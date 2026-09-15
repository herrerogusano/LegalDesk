# Phase 02 acceptance — document pipeline

## Result

Phase 02 is complete using local and static AWS evidence. The plan marks the
AWS smoke test as optional, so no S3 bucket, DynamoDB table, IAM role, inference,
ingestion, or retrieval was created or invoked.

## Implemented

- `DocumentStatus` exposes `UPLOADED`, `PENDING_INGESTION`, `INDEXED`, and
  `FAILED`; transitions are validated by the metadata repository.
- `DocumentPipeline` is HTTP-neutral. It builds `RequestContext` before any
  validation or storage, so an unauthorized matter has no storage side effect.
- Document IDs and S3 keys are generated server-side. Keys are
  `tenants/{tenant}/matters/{matter}/documents/{documentId}/original`.
- Object storage and metadata are separate protocols. DynamoDB metadata never
  includes the body; its single-table keys are
  `TENANT#{tenant}#MATTER#{matter}` and `DOCUMENT#{documentId}`.
- Upload validation requires a matching `.pdf`/`.txt` extension, PDF magic
  bytes or UTF-8 text, an ISO date, and a small fictional confidentiality
  allowlist. If metadata persistence fails after S3 succeeds, the service
  performs best-effort `DeleteObject` cleanup while preserving the stable
  metadata error; storage failures are recorded as `FAILED` metadata when
  possible.
- In-memory fakes support deterministic local tests; boto3 adapters are lazy
  and injectable and were not called in this phase.
- IaC defines encrypted, private, TLS-only S3 with a 30-day lifecycle,
  on-demand encrypted DynamoDB, and a least-privilege pipeline role with
  prefix-restricted object cleanup. Stack teardown removes both resources
  after the bucket is emptied.
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

The unit tests demonstrate User A → Matter A upload, User A → Matter B deny,
server-side key generation, scope-bearing metadata, no body in metadata,
visible lifecycle status, authorized listing, validation failures, and storage
failure handling. CloudFormation validation demonstrates that the infrastructure
definition is reproducible; a live AWS smoke remains deliberately optional.
