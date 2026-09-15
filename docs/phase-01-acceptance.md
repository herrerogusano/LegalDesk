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
| Agent responds to real invocation | Agreement now available; final smoke call still required | Pending approval |

Phase 01 is not complete until one real smoke invocation passes and teardown
behavior is confirmed or the intentionally retained resource is documented.

## Verification completed

```text
python -m unittest discover -s tests -v
Ran 12 tests — OK

aws cloudformation validate-template ...
Valid; requires CAPABILITY_NAMED_IAM
```

## AWS deployment on 2026-09-14

- Stack `legaldesk-phase-01`: `CREATE_COMPLETE` in `eu-west-1`.
- Harness `LegalDeskPhase01-7EMjvNs1PC`: `READY`, version 2.
- Underlying Runtime: `harness_LegalDeskPhase01-Kh25KQHkM9`.
- Memory disabled; tools empty; `allowedTools = [phase01_no_tools]`.
- Execution role: `LegalDeskBedrockAgentCoreHarnessPhase01`.

Four authorized smoke attempts were consumed across three approval rounds:

1. AWS rejected `temperature` plus `top_p` for Sonnet 4.6. `TopP` was removed
   and the stack updated successfully.
2. AWS rejected inference because Anthropic use-case details have not been
   submitted for this account.
3. After the FTU form became readable through `GetUseCaseForModelAccess`, AWS
   still rejected inference with the same message. The model reports
   `agreementAvailability = NOT_AVAILABLE`; no retry was made.
4. After the agreement became `AVAILABLE`, AWS rejected the first subscription
   because the Harness execution role lacks `aws-marketplace:ViewSubscriptions`
   and `aws-marketplace:Subscribe`; no retry was made.

At the time of the third attempt, the remaining blocker was the Anthropic
Marketplace agreement rather than an application or infrastructure failure.

## Anthropic agreement on 2026-09-15

With explicit user authorization, the account administrator accepted the single
available agreement offer for `anthropic.claude-sonnet-4-6`. The operation was
performed once and did not invoke a model. A supervisor read-back confirmed:

- agreement: `AVAILABLE`;
- authorization: `AUTHORIZED`;
- entitlement: `AVAILABLE`;
- region: `AVAILABLE`.

No permanent `aws-marketplace` permissions were added to the Harness execution
role. AWS requires the first invoking principal to complete the Marketplace
subscription even after the account-level agreement becomes available.

The proposed remediation is a separately approved, temporary policy granting:

- `aws-marketplace:ViewSubscriptions` on `*`;
- `aws-marketplace:Subscribe` on `*`, conditioned to product
  `prod-ffvjxvh4ltq64` (Claude Sonnet 4.6);
- `aws-marketplace:Unsubscribe` on `*`, as required by the documented Bedrock
  model-access prerequisite.

AWS Marketplace does not support resource ARNs for these actions. After one
successful activation/invocation, all three permissions must be removed from
the execution role without cancelling the model subscription. The only
remaining criterion is one successful, separately authorized smoke invocation.

## Resources and potential cost

The IAM role and managed Harness/underlying Runtime remain deployed for the
pending final smoke call. The failed attempts may have produced short Runtime
session/log usage; no successful model output tokens were generated. Harness
itself has no separate charge, but underlying AgentCore capabilities and
Bedrock usage may be billed.
