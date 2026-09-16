# Infrastructure

Infrastructure is defined as versioned CloudFormation templates under
`cloudformation/`. Phase 00 creates no AWS resources. Phase 06 adds a Bedrock
Guardrail and version; configuration, cost notes, and validation guidance are
in [phase-06-guardrails.md](phase-06-guardrails.md). Phase 07 adds a target-ready
human-review Lambda definition documented in
[phase-07-review-task.md](phase-07-review-task.md). Deployments and real service
invocations are separate from local tests and may incur AWS charges.
