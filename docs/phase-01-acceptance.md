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
| Agent responds to real invocation | Final smoke call returned the exact required response; temporary Marketplace policy was removed and verified absent | Complete |

Phase 01 is complete: one real smoke invocation passed and temporary IAM
teardown behavior was confirmed. The intentionally retained Harness/Runtime
resource is documented below because it is the phase output.

## Verification completed

```text
python -m unittest discover -s tests -v
Ran 12 tests — OK

aws cloudformation validate-template ...
Valid; requires CAPABILITY_NAMED_IAM
```

## AWS deployment on 2026-09-14

- Stack `legaldesk-phase-01`: `CREATE_COMPLETE` in `eu-west-1`.
- Harness `LegalDeskPhase01-7EMjvNs1PC`: `READY`, version 3 after the approved
  Phase 08 Gateway attachment.
- Underlying Runtime: `harness_LegalDeskPhase01-Kh25KQHkM9`.
- Memory remains disabled. The original Phase 01 deployment had no tools; the
  current version 3 has the Phase 08 Gateway and an exact three-tool allowlist.
- Execution role: `LegalDeskBedrockAgentCoreHarnessPhase01`.

On 2026-09-18, the existing Harness was updated in place to version 7 with
`DISABLE_ADOT_OBSERVABILITY=true` and
`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` (ADOT Python instrumentation
>=0.17.1 required for the content opt-out). The change set modified only the
Harness environment variables and preserved the ARN, Runtime, role, Gateway,
and Memory. A bounded synthetic smoke completed successfully and its unique
question/answer markers appeared in zero provider events; managed internal
trace detail remains intentionally unavailable.

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

With explicit user authorization, the execution role received one temporary
inline policy named `LegalDeskMarketplaceActivationTemporary`, granting:

- `aws-marketplace:ViewSubscriptions` on `*`;
- `aws-marketplace:Subscribe` on `*`, conditioned to product
  `prod-ffvjxvh4ltq64` (Claude Sonnet 4.6);
- `aws-marketplace:Unsubscribe` on `*`, as required by the documented Bedrock
  model-access prerequisite.

AWS Marketplace does not support resource ARNs for these actions. The policy
was added once, used for the activation invocation, then deleted in a
`finally` block. A subsequent `ListRolePolicies` check confirmed the policy is
absent; the model subscription remains active.

## Final smoke invocation on 2026-09-15

The one authorized final smoke invocation was executed through
`agent/scripts/invoke_harness.py` using the deployed Harness ARN and the
prompt `Reply with exactly: LegalDesk Phase 01 ready`.

- Session ID: `554241d7-4c55-4944-9124-adfec00fbeb3`.
- Process exit code: `0`.
- Response: `LegalDesk Phase 01 ready`.
- Invocation count in this operation: exactly one; no retry was performed.
- Temporary policy: added and read back successfully; deleted successfully;
  verified absent afterward.
- No other model was invoked during that Phase 01 smoke operation. A later
  Phase 08 acceptance smoke invoked the evolved version 3 Harness separately.

## Resources and potential cost

The IAM role and managed Harness/underlying Runtime remain deployed as the
Phase 01 output. The final successful invocation and prior failed attempts may
produce Runtime/session/log usage and Bedrock model charges. Harness itself has
no separate charge, but underlying AgentCore capabilities and Bedrock usage
may be billed. The temporary IAM policy is not retained.
