# Five-minute LegalDesk walkthrough

This is a manual, synthetic-data checklist. It is intentionally not an
automated AWS run and must not be executed with real legal material.

| Time | Step | Evidence to show |
|---:|---|---|
| 0:00–0:45 | Upload a fictional contract through the authorized upload flow | Server-generated document ID/key; initial `PENDING_UPLOAD`; confirmation checks object existence before `UPLOADED` |
| 0:45–1:45 | Ask a question in the authorized matter | Retrieve-then-generate flow; answer contains only authorized evidence |
| 1:45–2:30 | Open the citations panel | Document ID plus page/section; no S3 key or document body leak |
| 2:30–3:15 | Invoke `list_matter_documents` through Gateway → MCP | Same tenant/matter scope; metadata-only response |
| 3:15–4:00 | Invoke `create_review_task` through the Lambda tool | Closed reason code; server-derived matter/user; idempotency-safe review ID |
| 4:00–5:00 | Inspect the audit pointer | Application `correlationId`, retrieval/tool/guardrail/final outcomes, latency, and no payloads |

Suggested fictional narrative: Alice selects `mat_sundial`, uploads a synthetic
agreement, asks about the fictional 17-day payment term, checks the citation,
lists metadata, and requests human review for an evidence gap. Then show that
Alice requesting `mat_glacier` is denied before retrieval/tool access.

The managed Harness trace is a separate provider-owned surface. Phase 11
disables managed ADOT detail after content extraction was observed; do not claim
that the application UUID is a provider trace ID.
