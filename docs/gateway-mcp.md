# Phase 08 — Gateway and remote MCP

Phase 08 adds a narrow tool surface behind AgentCore Gateway:

| Tool | Target | Input scope | Output |
|---|---|---|---|
| `list_matter_documents` | remote MCP server | `matterId` selector | safe document metadata |
| `get_document_metadata` | remote MCP server | `matterId`, `documentId` selectors | safe metadata for the current matter |
| `create_review_task` | Phase 07 Lambda | `matterId` selector, reason, due date and bounded server-derived snapshot | `reviewTaskId`, `status` |
| `list_review_tasks` | Phase 07 Lambda | `matterId` selector, bounded limit | summary metadata only (no snapshot) |
| `get_review_task` | Phase 07 Lambda | `matterId`, `reviewTaskId` selectors | one validated snapshot and metadata |
| `update_review_task` | Phase 07 Lambda | `matterId`, `reviewTaskId`, allowed status transition | updated task metadata |

The MCP implementation is JSON-RPC/HTTP-neutral in
`backend/src/legaldesk/mcp_server.py`. It uses the server-built
`RequestContext`, calls the existing metadata repository with that scope, and
returns no document body, S3 key, credentials, or hidden provider fields.
Repository results are checked again before serialization. The local routing
seam in `agent/src/legaldesk_agent/tool_router.py` maps the two metadata tools
to MCP. The review queue operations are deterministic backend actions through
Gateway/Lambda, not Harness agentic tools; the router performs no inference or
authorization.

## Context and trust boundary

Tool arguments never contain `tenantId`, `userId`, status, or task IDs. Public
schemas contain `matterId` only as an untrusted selector required for
deterministic authorization; the interceptor verifies it and targets never use
it as trusted scope. The Phase 07 Lambda keeps its original serialized `authorizedContext`
path for direct service use. The AgentCore target path instead uses an opaque,
short-lived `_legaldeskGrantId` injected into transformed tool arguments. The
grant contains no document body and is stored in the existing DynamoDB table;
the Lambda loads it consistently, validates entity/tool/TTL, then reloads User
and Matter records and runs bilateral `build_request_context` authorization
before any write.

The Gateway uses `CUSTOM_JWT` with parameterized discovery URL, audience,
client, and scope. The metadata Function URL remains `AWS_IAM`; its
allowlisted headers contain only a server-owned grant and selectors. The REQUEST interceptor derives the
subject from the Gateway-validated bearer token (ignoring client subject
headers), reauthorizes the requested matter against DynamoDB, creates a
correlation ID, and overwrites metadata headers. For the review and metadata
MCP targets it writes a short-lived grant and injects only its server-owned
opaque ID; attacker-supplied grant IDs are overwritten before target
invocation. The MCP Function URL ignores subject, matter, and correlation
headers for business calls and consumes the exact grant, including its tool,
subject, matter, and expiry binding. Provider
`client_context.custom` metadata is not treated as identity or authorization.
No token or request body is logged. Grants expire after five minutes and are
replayable during that TTL by design; correlation-derived idempotency limits
duplicate review writes. Grant cleanup remains a deferred operational task.

MCP `initialize`, `tools/list`, `ping`, and the `notifications/initialized`
notification are allowed without business scope so Gateway dynamic discovery
can complete; `tools/call` always requires the interceptor-built context. The
server and interceptor support MCP `2025-03-26`, paginated `tools/list`, and the
standard optional `_meta` object, while rejecting malformed or unknown fields.
Gateway-visible tool names use the provider-defined
`target-name___tool-name` prefix. The interceptor accepts only the exact
target/tool combinations documented above and fails closed for unknown,
unprefixed, or mismatched names.

Direct browser invocation and public Function URL access are not an accepted
deployment configuration. The live synthetic smoke confirmed that AgentCore
preserves the interceptor-owned opaque grant in the flat Lambda target event;
the Lambda consumes the grant, reloads authorization, and never accepts
model-supplied scope as authority.

## Reproducible configuration

`infra/cloudformation/phase-08-gateway-mcp.yaml` defines the Gateway, Lambda
target, IAM-authenticated metadata Function URL, remote MCP target, immutable
S3 code parameters, and least-privilege roles. It reuses the Phase 02 metadata
table and does not create another database. The existing Harness attachment
is represented both by conditional configuration in the Phase 01 template and
by [`infra/phase-08-agent-attachment.yaml`](../infra/phase-08-agent-attachment.yaml).
It grants only the parameterized Gateway/token-vault actions and the exact
AgentCore-managed OAuth secret prefix; no client secret is stored in Git.
The Gateway service role has the same action constrained to the fixed
`legaldeskgatewayphase08-*` ARN pattern, as required for `GATEWAY_IAM_ROLE`
outbound authorization without introducing a CloudFormation dependency cycle.
Its DynamoDB write permission is additionally restricted to
`GATEWAY#GRANT#*` partition keys.

The approved deployment uses dedicated Phase 08 artifact/Cognito stacks, the
existing Phase 02 table, and the Phase 07 Lambda. Gateway and both targets are
`READY`; Harness version 3 historically discovered the three deployed tools
and returned only the fictional `Synthetic notice.pdf` result. Direct
synthetic calls historically proved metadata list/get, review creation, and
cross-matter denial. The durable queue read/update workflow is covered by
local fakes and focused tests; it is not represented as newly deployed
evidence. Resources are
retained for later phases. They can incur Gateway, Harness/Bedrock, Lambda,
DynamoDB, Cognito, S3, Secrets Manager, and CloudWatch charges.

The AgentCore OAuth credential provider is a one-time control-plane binding
between the Cognito client and AgentCore's managed token vault. Its ARN/name
are parameterized in the Harness template, but creation requires supplying the
Cognito client secret outside CloudFormation. That secret must be read at
deployment time, sent directly to AgentCore, and never written to source,
shell output, or deployment artifacts.

For this phase, Cognito client credentials deliberately identify one synthetic
service actor: the access token's `sub` equals the app-client ID, and the smoke
seeded that subject in the existing authorization table. This remains a
fictional service-actor smoke path. Phase 10 now additionally deploys a public
Authorization Code + PKCE client and reuses the same Gateway authorization
boundary; production end-user onboarding and broader operational controls
remain subject to the Phase 10/11 acceptance gaps.

## Local protocol examples

The test server accepts standard JSON-RPC-shaped requests:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_document_metadata","arguments":{"documentId":"doc-sundial"}}}
```

Authorization is supplied by the adapter, never by either request body.
