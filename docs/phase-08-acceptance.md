# Phase 08 acceptance

Phase 08 was validated locally and with a minimal synthetic deployment in
`eu-west-1` on 2026-09-17. No real legal data was used.

| Criterion | Evidence | Status |
|---|---|---|
| MCP lifecycle, schemas, and strict inputs | `tests/test_mcp_server.py`; MCP `2025-03-26`; paginated live `tools/list` | PASS |
| Current-matter list/get | Unit tests plus live synthetic `mat_sundial` calls | PASS |
| Cross-matter requests fail before data access/write | Unit tests plus live `mat_other` calls returning 403 | PASS |
| Metadata excludes bodies, S3 keys, and secrets | Serialization regression tests and live response inspection | PASS |
| Review target reauthorizes before persistence | Review tests and synthetic Gateway-created OPEN task | PASS |
| Client scope/grant manipulation is overwritten | Interceptor/review tests and persisted synthetic task inspection | PASS |
| Exact MCP/Lambda tool routing | Router/interceptor tests and live three-tool discovery | PASS |
| Reproducible least-privilege IaC | Phase 01/07/08 CloudFormation and static policy tests | PASS |
| Agent reaches MCP through Gateway | Harness version 3 returned `Synthetic notice.pdf` for `mat_sundial` | PASS |
| Gateway reaches review Lambda | Live `create_review_task` returned an ID/status and persisted only in the authorized matter | PASS |
| Targets and Gateway operational | Gateway and both targets reported `READY` | PASS |

## Live resources retained

- `LegalDeskPhase07ReviewTask`: review Lambda, scoped role, and 14-day log group.
- `LegalDeskPhase08Artifacts`: private versioned artifact bucket with noncurrent
  version expiry.
- `LegalDeskPhase08Cognito`: minimal client-credentials issuer and app client.
- `LegalDeskPhase08Gateway`: AgentCore Gateway, request interceptor, metadata MCP
  Lambda/Function URL, two targets, scoped roles, and 14-day log groups.
- Phase 01 Harness `LegalDeskPhase01-7EMjvNs1PC`, version 3, attached to the
  Gateway with an exact three-tool allowlist.
- AgentCore OAuth credential provider `LegalDeskPhase08Cognito`. Its managed
  secret is not stored or printed by the repository.

The resources are intentionally retained because they are dependencies of later
phases. Lambda invocations/duration, Gateway and Harness/Bedrock use, DynamoDB
requests, Cognito token requests, S3 storage/requests, Secrets Manager, and
CloudWatch Logs can incur charges.

The OAuth provider itself is a one-time AgentCore control-plane binding because
its creation consumes the Cognito client secret. The Harness accepts its
provider ARN/name as parameters; the secret remains outside Git and templates.
The client-credentials token represents one synthetic service actor whose
`sub` equals the app-client ID; its fictional authorization rows were seeded in
the existing table for this smoke. It is not the future end-user identity
model. Per-user identity and isolation remain explicitly in Phase 10.

## Synthetic smoke evidence

The Gateway discovered `review-task-lambda___create_review_task`,
`metadata-mcp___list_matter_documents`, and
`metadata-mcp___get_document_metadata` across two MCP pages. The metadata calls
listed/read only `mat_sundial`; `mat_other` was denied. Review creation returned
only its task ID/status, ignored a client-supplied grant selector, and persisted
under the authorized tenant/matter. Finally, the Phase 01 Harness obtained its
OAuth token, loaded the paginated tool list, selected the metadata tool, and
returned the fictional filename `Synthetic notice.pdf`.

AgentCore currently negotiates MCP `2025-03-26`. The adapters accept standard
optional `_meta` and pagination cursors while rejecting unknown or malformed
parameters. Phase 09 was not started.
