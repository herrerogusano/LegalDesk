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
