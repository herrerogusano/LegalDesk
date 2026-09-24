# Infrastructure

Infrastructure is defined as versioned CloudFormation templates under
`cloudformation/`. Phase 00 creates no AWS resources. Phase 06 adds a Bedrock
Guardrail and version; configuration, cost notes, and validation guidance are
in [phase-06-guardrails.md](phase-06-guardrails.md). Phase 07 adds a target-ready
human-review Lambda definition documented in
[phase-07-review-task.md](phase-07-review-task.md). Deployments and real service
invocations are separate from local tests and may incur AWS charges.
Phase 08 adds versioned artifact and Cognito dependencies plus the deployed
Gateway/MCP stack in
[phase-08-gateway-mcp.yaml](cloudformation/phase-08-gateway-mcp.yaml); context
propagation uses short-lived grants in the existing metadata table; live
synthetic validation proved both MCP and Lambda targets plus the Phase 01
Harness attachment. The standalone attachment fragment remains in
`phase-08-agent-attachment.yaml`, while the executable conditional update is
also captured in the Phase 01 Harness template.
Phase 09 adds the BYO short-term Memory template
([phase-09-memory.yaml](cloudformation/phase-09-memory.yaml)) with seven-day
event expiry and no strategies. The Phase 01 template conditionally attaches
that Memory with `EnablePhase09Memory=true`, requests a bounded
`MessagesCount=10`, and grants the exact Memory ARN only the required data-plane
actions. The deployed stack and live continuity/isolation evidence are recorded
in [phase-09-acceptance.md](../docs/phase-09-acceptance.md).
Phase 10 adds the local identity/isolation boundary and the reusable public
Cognito Authorization Code + PKCE client template
([phase-10-identity.yaml](cloudformation/phase-10-identity.yaml)). It reuses the
Phase 08 UserPool and existing metadata table. The public client is deployed
in `LegalDeskPhase10Identity`, and the Phase 08 Gateway accepts it as an
additional client without removing the existing M2M client. The live Gateway
update preserved all resources without replacement; synthetic authorized,
cross-matter, and direct-endpoint denial checks are recorded in
`docs/phase-10-acceptance.md`. OIDC verification uses the versioned core
`PyJWT[crypto]` dependency.
Phase 11 adds the small redacted observability stack
([phase-11-observability.yaml](cloudformation/phase-11-observability.yaml))
over those existing Lambda log groups. It was deployed as
`LegalDeskPhase11Observability` in `eu-west-1` with seven metric filters; no
raw AgentCore payload logging, new log group, or dashboard was enabled. The
deployment/smoke and teardown procedure are recorded in
[`phase-11-acceptance.md`](../docs/phase-11-acceptance.md) and
`phase-11-commands.md`.

Phase 14 adds the local-only public edge definition
([phase-14-public-edge.yaml](cloudformation/phase-14-public-edge.yaml)). It
creates no AWS resources until a separately approved change set is deployed.
The companion runbook ([phase-14-public-edge.md](../docs/phase-14-public-edge.md))
documents the private S3/OAC frontend, exact API routes, trusted-edge
contract, least privilege parameters, and the two-step callback/CORS
bootstrap.

The P14 document-security candidate
([phase-14-document-security.yaml](cloudformation/phase-14-document-security.yaml))
adds only the GuardDuty quarantine plan, exact EventBridge route, corroborating
Lambda, bounded retries/DLQ, and retained logs. It reuses the existing S3
bucket and DynamoDB table by parameter and has no KMS, WAF, public endpoint, or
reconciliation scheduler.

Release artifacts are assembled locally by `../scripts/package_release.py`
from the exact Python 3.12 constraints in
`../packaging/constraints-python312-manylinux-x86_64.txt`. The packager is
offline and emits a Lambda zip that can be supplied to both the public
application and malware-scan templates, plus a bounded four-file frontend zip
and a SHA-256 manifest. Uploading artifacts, publishing frontend files, and
CloudFront invalidation remain separately approved release operations.
