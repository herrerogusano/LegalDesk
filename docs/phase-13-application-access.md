# Phase 13 — local application access boundary

Design/preflight only. No policy has been attached, resource deployed or live
inventory performed. The final approved smoke must substitute verified resource
ARNs and the two synthetic matters before enabling AWS mode.

The loopback application uses an operator-configured AWS profile, never browser
AWS credentials. Its credentials are technical transport credentials, not user
authorization. Every business operation still verifies the user and membership.
Do not use an administrator profile for the demo. The existing Phase 02 role
trusts Lambda; it is **not** a role that a desktop application can assume.

## Minimum application data-plane permissions

| Dependency | Required actions | Restriction |
|---|---|---|
| Existing metadata table | `dynamodb:GetItem`, `Query`, `PutItem`, `UpdateItem` | Exact table ARN; split read-only authorization keys from document/conversation/invocation writes |
| Synthetic source objects | `s3:PutObject`, `s3:GetObject` | Exact bucket and two synthetic matter prefixes; supported original and metadata-sidecar suffixes only |
| Knowledge Base | `bedrock:Retrieve`, `StartIngestionJob`, `GetIngestionJob` | Exact KB ARN; configured data source fixed server-side |
| Resolver/Writer model | `bedrock:InvokeModel` | Approved inference-profile ARN plus required regional foundation-model ARNs only |
| Contextual grounding | `bedrock:ApplyGuardrail` | Exact versioned Guardrail resource |
| Harness tools | `bedrock-agentcore:InvokeHarness`, dependent `InvokeAgentRuntime` | Exact existing Harness and underlying Runtime resources; user JWT and server-owned invocation binding passed separately |
| Short-term Memory | `bedrock-agentcore:CreateEvent`, `ListEvents` | Exact existing Memory resource; application-derived actor/session only |

Reconcile the action/resource combinations with the final application factory
and AWS service authorization reference before attachment. This table is not a
deployable IAM policy and does not authorize wildcard resources. Any required
additional action must be explained before the smoke.

Reference: AWS's [Harness access controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)
requires both Harness and underlying Runtime invocation permissions; see also
the [AgentCore authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_bedrock-agentcore.html).

For the existing DynamoDB table, separate statements should allow:

- Reads of the two users' `AUTH#USER#…` and two matters' `AUTH#MATTER#…` keys;
  no application writes to authorization mappings.
- Document operations under the exact `TENANT#…#MATTER#…` partitions.
- Conversation `GetItem`/`PutItem` under those users/matters' `CONVERSATION#…`
  prefixes, and invocation `PutItem` under `GATEWAY#INVOCATION#*`.
- No application `Scan`, table-management, review-row writes, IAM management,
  direct Review Lambda invocation or bypass of Gateway.

The interceptor retains its existing grant-only write permission
(`GATEWAY#GRANT#*`); invocation records do not require broader interceptor writes.
Gateway/Lambda execution roles remain separate from desktop credentials.
Cleanup/deployment operations belong to a separately approved operator session,
not to the running application's policy.

## Browser upload and operational limitations

The Phase 02 bucket template permits CORS only for the configured exact
`ApplicationOrigin` (default `http://localhost:8000`), `PUT`, and the exact
signed upload headers (content type, tenant/matter/document metadata, AES256).
This permits a browser to use the short-lived presigned upload; it does not make
objects public or grant access without a signature. No citation S3 URL is needed
because evidence inspection goes through the authorized application endpoint.
This template change is local only and still requires approved deployment.

Ingestion synchronizes a **whole configured data source**, not an individual
user's document. The approved smoke must use a dedicated synthetic source with
bounded contents; per-matter application authorization is not an AWS ingestion
partition. This remains an operator/demo workflow, not an unrestricted public
indexing service.

The loopback HTTP server is a local portfolio entry point, not a production TLS
edge. Production hosting, shared session persistence, distributed rate limiting,
automatic abandoned-upload cleanup and long-term Memory are outside this phase.
JWT signature/claim verification is not a separate token-revocation lookup.
Do not claim immediate provider-wide token revocation from local JWT validation;
local session logout and current matter membership checks are separate controls.
