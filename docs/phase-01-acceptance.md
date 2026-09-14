# Phase 01 acceptance record

## Local preparation

Validated on 2026-09-10 with Python 3.13.13, AWS CLI 2.35.20, and the
CloudFormation `ValidateTemplate` API in `eu-west-1`.

| Criterion | Evidence | Status |
|---|---|---|
| Minimal agent/config | CloudFormation Harness and Python invocation adapter | Ready locally |
| Reproducible deployment | `infra/cloudformation/phase-01-harness.yaml` | Ready locally |
| Different session IDs | UUID unit tests | Ready locally |
| No S3/KB/Gateway/Memory dependency | Template and policy regression test | Ready locally |
| IAM avoids unnecessary wildcard permissions | Scoped model/log ARNs and documented unavoidable wildcards | Ready locally |
| Harness vs Runtime documented | `docs/agentcore-harness-runtime.md` | Complete |
| Agent responds to real invocation | AWS rejected inference until Anthropic use-case details are submitted | Blocked externally |

Phase 01 is not complete until one real smoke invocation passes and teardown
behavior is confirmed or the intentionally retained resource is documented.

## Verification completed

```text
python -m unittest discover -s tests -v
Ran 11 tests — OK

aws cloudformation validate-template ...
Valid; requires CAPABILITY_NAMED_IAM
```

## AWS deployment on 2026-09-14

- Stack `legaldesk-phase-01`: `CREATE_COMPLETE` in `eu-west-1`.
- Harness `LegalDeskPhase01-7EMjvNs1PC`: `READY`, version 2.
- Underlying Runtime: `harness_LegalDeskPhase01-Kh25KQHkM9`.
- Memory disabled; tools empty; `allowedTools = [phase01_no_tools]`.
- Execution role: `LegalDeskBedrockAgentCoreHarnessPhase01`.

Two authorized smoke attempts were consumed:

1. AWS rejected `temperature` plus `top_p` for Sonnet 4.6. `TopP` was removed
   and the stack updated successfully.
2. AWS rejected inference because Anthropic use-case details have not been
   submitted for this account.

No third inference was attempted. The remaining acceptance blocker is an AWS
account prerequisite, not an application or infrastructure failure.

## Resources and potential cost

The IAM role and managed Harness/underlying Runtime remain deployed so the
single pending smoke call can be retried after account enablement. The two
failed attempts may have produced short Runtime session/log usage; no successful
model output tokens were generated. Harness itself has no separate charge, but
underlying AgentCore capabilities and Bedrock usage may be billed.
