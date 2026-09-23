# Phase 07 acceptance — Human review Lambda tool

This phase was audited locally against `PLAN_07_LAMBDA_TOOL.md`, then deployed
with explicit approval as the review target required by Phase 08. Only
fictional synthetic data was used.

| Criterion | Expected | Actual local evidence | Status |
| --- | --- | --- | --- |
| Invocable Lambda | Two-argument AWS handler with a provider-neutral service boundary | `legaldesk.review_tasks.lambda_handler(event, context)` plus injectable handler tests | PASS (local) |
| Narrow tool contract | Closed `reasonCode` enum; bounded server-derived snapshot, due date and note; no browser-controlled scope/lifecycle/body fields | Strict schema/parser, bounded snapshot and override rejection in `tests/test_review_tasks.py` | PASS (local) |
| Authorized current matter | Effective user and tenant/matter derive from verified subject + bilateral Dynamo metadata membership before task access/write | Authorization adapter uses consistent `GetItem`; cross-matter tests assert no task is written | PASS (local) |
| Serializable authorization envelope | Envelope contains only verified-subject/matter selectors and UUID correlation; tenant/user IDs cannot be supplied | Exact-key parsing, opaque-selector validation, and forged-field rejection tests | PASS (local) |
| Persistence and least privilege | Existing metadata table; scoped `GetItem`/`PutItem`/`Query`/`UpdateItem`; projected summary query, conditional create/status update; scoped logs only | CloudFormation static tests and Dynamo adapter tests | PASS (static/local) |
| Idempotency/concurrency | Deterministic ID scoped to tenant, matter, creator, and retry key; retries compare tenant/matter/creator/reason and fail closed on malformed records | Deterministic retry, conflict, race, and malformed recovered-record tests | PASS (local) |
| Safe output | List returns bounded metadata only; get returns validated question/answer/citation snapshot without storage URI or document body | Handler/output, list projection and persistence-error tests | PASS (local) |
| AWS validation/smoke | Validate/deploy/invoke only with explicit AWS approval and synthetic data | Stack `LegalDeskPhase07ReviewTask` is `UPDATE_COMPLETE`; authorized Gateway create persisted an OPEN task and cross-matter create was denied | PASS |

Lambda, DynamoDB, S3 artifact storage, and CloudWatch Logs may incur charges.
The stack is retained because Phase 08 and later phases depend on it. This
repository contains no credentials or real legal data.
