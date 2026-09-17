# Phase 08 — Gateway and remote MCP

Phase 08 adds a narrow tool surface behind AgentCore Gateway:

| Tool | Target | Input scope | Output |
|---|---|---|---|
| `list_matter_documents` | remote MCP server | none | safe document metadata |
| `get_document_metadata` | remote MCP server | `documentId` only | safe metadata for the current matter |
| `create_review_task` | Phase 07 Lambda | `reasonCode` | `reviewTaskId`, `status` |

The MCP implementation is JSON-RPC/HTTP-neutral in
`backend/src/legaldesk/mcp_server.py`. It uses the server-built
`RequestContext`, calls the existing metadata repository with that scope, and
returns no document body, S3 key, credentials, or hidden provider fields.
Repository results are checked again before serialization. The local routing
seam in `agent/src/legaldesk_agent/tool_router.py` maps the two metadata tools
to MCP and review creation to Lambda; it performs no inference or
authorization.

## Context and trust boundary

Tool arguments never contain `tenantId`, `matterId`, `userId`, status, or task
IDs. The Phase 07 Lambda keeps its original serialized `authorizedContext`
path for direct service use. The AgentCore target path instead uses an opaque,
short-lived `_legaldeskGrantId` injected into transformed tool arguments. The
grant contains no document body and is stored in the existing DynamoDB table;
the Lambda loads it consistently, validates entity/tool/TTL, then reloads User
and Matter records and runs bilateral `build_request_context` authorization
before any write.

The Gateway uses `CUSTOM_JWT` with parameterized discovery URL, audience,
client, and scope. The metadata Function URL remains `AWS_IAM`; its
allowlisted headers are selectors only. The REQUEST interceptor derives the
subject from the Gateway-validated bearer token (ignoring client subject
headers), reauthorizes the requested matter against DynamoDB, creates a
correlation ID, and overwrites metadata headers. For the review target it
writes the grant and injects only its server-owned opaque ID; attacker-supplied
grant IDs are overwritten before target invocation. Provider
`client_context.custom` metadata is not treated as identity or authorization.
No token or request body is logged. Grants expire after five minutes; expiry plus deterministic
grant-derived idempotency prevents replay writes, while cleanup remains a
deferred operational task.

MCP `initialize`, `tools/list`, `ping`, and the `notifications/initialized`
notification are allowed without business scope so Gateway dynamic discovery
can complete; `tools/call` always requires the interceptor-built context.
Gateway-visible tool names use the provider-defined
`target-name___tool-name` prefix. The interceptor accepts only the three exact
target/tool combinations documented above and fails closed for unknown,
unprefixed, or mismatched names.

Direct browser invocation and public Function URL access are not an accepted
deployment configuration. Whether AgentCore preserves the transformed opaque
grant argument into the flat Lambda event is a live validation gap; it must be
proven with synthetic identities before any production-like deployment. If
that contract cannot be established, targets remain disabled rather than
accepting model-supplied scope.

## Reproducible configuration

`infra/cloudformation/phase-08-gateway-mcp.yaml` defines the Gateway, Lambda
target, IAM-authenticated metadata Function URL, remote MCP target, immutable
S3 code parameters, and least-privilege roles. It reuses the Phase 02 metadata
table and does not create another database. The existing Harness attachment
is described in [`infra/phase-08-agent-attachment.yaml`](../infra/phase-08-agent-attachment.yaml);
it updates the existing Harness and grants only the parameterized
`bedrock-agentcore:InvokeGateway` action without storing OAuth secrets.
The Gateway service role has the same action constrained to the fixed
`LegalDeskGatewayPhase08-*` ARN pattern, as required for `GATEWAY_IAM_ROLE`
outbound authorization without introducing a CloudFormation dependency cycle.
Its DynamoDB write permission is additionally restricted to
`GATEWAY#GRANT#*` partition keys.

No Gateway, Lambda, Function URL, DynamoDB, or MCP request was deployed or
invoked for local acceptance. These resources can incur charges; deployment
requires approval under `AWS_COST_POLICY.md`. A future smoke should use only
fictional data and one allowed call, one idempotent retry, and one cross-matter
denial, followed by teardown.

## Local protocol examples

The test server accepts standard JSON-RPC-shaped requests:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}
```

```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_document_metadata","arguments":{"documentId":"doc-sundial"}}}
```

Authorization is supplied by the adapter, never by either request body.
