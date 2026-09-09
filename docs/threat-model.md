# Threat model

## Assets and trust assumptions

Protected assets include document contents, metadata, identity mappings,
conversation scope, tool side effects, prompts, secrets, and audit integrity.
Only verified identity claims and server-side membership records are trusted.
Model output and retrieved/user-supplied text are never trusted for access
control.

## Threats and controls

| Threat | Example impact | Preventive controls | Detection/test |
|---|---|---|---|
| Prompt injection | A document says to expose another file or system prompt | Treat passages as quoted data; constrained prompt/tool interfaces; no direct datastore access | Malicious fixture and refusal evals |
| Cross-matter access | User A reads Matter B | Server-derived scope; bilateral membership; retrieval metadata filters; scoped storage/tools/memory | Mandatory negative unit and end-to-end tests |
| Forged tenant/matter IDs | Browser posts Tenant B identifiers | Ignore browser tenant; matter is only a selector; load authoritative ownership before use | Request-context tests |
| Poisoned document instructions | Embedded text attempts tool execution | Separate evidence from instructions; allowlisted tools; validate every call deterministically | Adversarial corpus |
| PII leakage | Sensitive content appears in answer or logs | Fictional data; Guardrails/redaction; minimal context and logging | PII evals and log inspection |
| Secret/log leakage | Tokens or full documents enter telemetry | Structured allowlisted audit fields; secret manager later; never log payload bodies | Logging tests/review |
| Unauthorized tool call | Model creates a task in another matter | Gateway/backend authorization; schema validation; idempotency; least privilege | Cross-matter tool tests |
| Hallucinated legal answer | Unsupported clause or individualized advice | Retrieve first; citations; not-found behavior; separate fact from interpretation; human review | Groundedness/not-found evals |
| Session/memory mix-up | Context crosses users or matters | Actor/session namespaces include authorized user and matter; restricted long-term memory | Cross-session tests |
| Resource enumeration | Different errors reveal foreign IDs | Generic access denial and non-distinguishing external responses | Unknown vs foreign ID test |

## Data never sent to the model

- credentials, access tokens, refresh tokens, signing material, or API keys;
- raw identity tokens or complete identity-provider claims;
- authorization tables or lists of other tenants/matters/users;
- secrets, environment dumps, internal stack traces, or infrastructure metadata;
- full audit logs and unrelated conversation/memory records;
- documents or passages outside the authorized matter;
- unnecessary PII or full document bodies when selected passages suffice.

No real client, confidential, personal, or privileged legal data is permitted in
this project.

## Residual risks

Phase 00 proves only local policy behavior. Retrieval filters, tools, memory,
identity verification, Guardrails, and cloud IAM require defense-in-depth tests
in their respective later phases.
