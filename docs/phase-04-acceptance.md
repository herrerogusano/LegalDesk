# Phase 04 acceptance — grounded chat

## Criteria and local evidence

| Criterion | Evidence |
| --- | --- |
| Valid request, verified identity, and untrusted matter selector | `tests/test_chat.py`: opaque selector/question checks, exact payload fields, identity rejection, and authorization denial before retrieval. |
| Retrieve before generate; no model call without authorized evidence | `tests/test_chat.py`: empty retrieval, cross-scope result, and denied matter leave fake generator uncalled. |
| Answerable and ambiguous answers carry retrieved citations | `tests/test_chat.py`: answerable source metadata and ambiguous/cross-document citations within one matter. |
| Unanswerable, invented, duplicate, or malformed citations fail closed | `tests/test_chat.py`: canonical insufficient-evidence response and citation integrity cases. |
| Citation source name/ID/URI/page/section preserved | `tests/test_retrieval.py` and `tests/test_chat.py`; sidecar now provides `documentName`. |
| Custom metadata respects Bedrock Knowledge Bases' S3 Vectors budget | `tests/test_document_pipeline.py`: compact UTF-8 JSON for the flat seven-attribute map stays below 1 KiB for maximum filename/jurisdiction values; an oversized stored scope is rejected before metadata persistence or presigning. |
| UI presents response and citations | `frontend/index.html`, `citations.js`, `styles.css`; `node --check frontend/citations.js` passes. UI uses fictional sample data and no external dependencies. |

The sidecar contains seven filterable application attributes, including the
document name. Bedrock Knowledge Bases' documented S3 Vectors integration
limit is 1 KiB of custom metadata (filterable and non-filterable combined) and
35 keys per vector; that tighter limit governs this project. The local builder
budgets the compact UTF-8 JSON map before persistence/presigning and the sidecar
reuses the same map. Native S3 Vectors separately allows 2 KiB filterable,
40 KiB total metadata, 50 total keys, and 10 non-filterable keys per index; the
Phase 03 index reserves two non-filterable Bedrock keys. This is local schema
and serialization evidence only. No Bedrock transformation, ingestion, or live
AWS validation was performed. [Bedrock metadata support](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html) · [native S3 Vectors limits](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-vectors-limitations.html).

## Cost, resources, and gaps

No AWS resources were created or modified; no retrieval, ingestion, or model
inference was called. Local fakes/tests incur no AWS cost. Future Bedrock
retrieval and generation remain billable and require deliberate approval under
`AWS_COST_POLICY.md`.

There is no HTTP adapter, production UI integration, real generator, durable
conversation/session ownership, semantic grounding evaluation, or live AWS
validation. At the time Phase 04 was accepted, generation used a short interim
handling note; Phase 05 retired it and added the versioned source of truth in
[`prompts/legaldesk-system.md`](../prompts/legaldesk-system.md). Current prompt
acceptance and remaining provider-integration gaps are recorded in
[`phase-05-acceptance.md`](phase-05-acceptance.md).
