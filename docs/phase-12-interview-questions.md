# Interview questions and concise answers

## Why is authorization outside the model?

The model can select an intent or tool, but the backend derives tenant, user,
matter, memory, and conversation scope from verified identity and stored
membership. Guardrails and prompts are defense in depth, not authorization.

## Why retrieve before generating?

Retrieval gives the generation boundary bounded, citation-bearing evidence and
lets the system return insufficient evidence instead of inventing a clause.

## Why keep long-term memory disabled?

Raw legal text, legal conclusions, sensitive facts, secrets, and unnecessary PII
are not safe default memory. The current design keeps short-term continuity and
rejects long-term writes until a classification/allowlist decision exists.

## Why use S3 Vectors and fixed chunking?

It keeps the MVP metadata and cost profile simple. The trade-off is less
structural hierarchy than a custom index; changing chunking remains possible
because retrieval consumes a provider-neutral citation contract.

## What proves tenant isolation?

Every sensitive seam requires a server-derived `RequestContext`; retrieval,
MCP, review, conversation, Memory, and direct Harness tests include negative
cross-matter cases. The Phase 12 local suite has a 100% deny result for its
cross-matter cases.

## What would you run in AWS for Phase 12?

Only the approved small real-model subset, with an explicit call budget and
redaction/retention checks. The full evaluation remains local and deterministic.

## What is still a production gap?

The MVP needs a reviewed data-classification program, operational SLOs/cost
budgets, production identity operations, ingestion cleanup, and a durable audit
retention decision. Managed Harness internal trace details are intentionally
disabled; application allowlisted telemetry is the supported audit pointer.
