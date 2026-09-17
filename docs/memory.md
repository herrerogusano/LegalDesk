# Phase 09 — AgentCore Memory boundary

LegalDesk uses AgentCore Memory only for short-term conversation events. The
server first builds an authorized `RequestContext`, then derives a deterministic
opaque `actorId` from the verified user plus the stored tenant/matter scope.
The conversation and session selectors are inputs to a second deterministic
opaque session identifier. Raw tenant, user, matter, conversation, and browser
session IDs are never sent as AgentCore actor identifiers.

The same authorized context and selectors derive the same scope, so a reload of
the same conversation can read the same short-term event stream. A different
session, user, or matter derives a different actor/session pair and cannot read
the first stream. Before derivation, the conversation/session pair must also
exist in the server-side conversation binding store for that exact authorized
user, tenant, and matter. This is defense in depth; authorization remains the
source of truth and Memory identifiers are not credentials.

## Retention and long-term policy

The Phase 09 CloudFormation template creates `AWS::BedrockAgentCore::Memory`
with `EventExpiryDuration: 7`. It intentionally declares no
`MemoryStrategies`, namespace templates, or long-term retrieval workflow.
Every data-plane event uses `extractionMode: SKIP`, which keeps the event in
short-term memory and excludes it from long-term extraction.

`MemoryPolicy.reject_long_term()` rejects every attempted long-term write or
retrieval, including raw legal text, legal conclusions, sensitive case facts,
secrets, and otherwise innocuous preferences. This conservative MVP policy
avoids silently turning conversation text into durable legal memory. Document
content and legal evidence remain owned by S3/Knowledge Base; authorization and
review state remain owned by DynamoDB.

## Harness integration

The existing Harness template remains memory-disabled by default. Setting
`EnablePhase09Memory=true` requires the exact `Phase09MemoryArn`, attaches the
Memory ARN, and conditionally grants only the five official Memory actions on
that ARN: `CreateEvent`, `DeleteEvent`, `GetEvent`, `ListEvents`, and
`RetrieveMemoryRecords`. The application does not expose an `actorId` CLI
option. `HarnessInvoker` accepts it only inside the typed
`HarnessMemoryScope` produced from a server-derived scope.

## Cleanup and production gaps

The dedicated Memory resource has explicit `DeletionPolicy: Delete` and
`UpdateReplacePolicy: Delete`; the seven-day event expiry is the normal cleanup
for retained short-term events. Abandoned conversation scopes need no custom
cleanup job in this phase. Before production, inject a secret derivation key at
the trusted edge (the local deterministic implementation is for reproducible
tests only), replace the in-memory conversation binding store with its durable
authenticated implementation, and revisit a narrowly allowlisted preference
strategy only with an approved data classification.

No AWS deployment or live Memory smoke is claimed by Phase 09 acceptance.
