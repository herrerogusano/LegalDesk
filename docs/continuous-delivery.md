# Continuous delivery — constrained production beta

This repository contains a fail-closed CD path for the existing
`LegalDeskPhase14PublicEdge` stack. It is an application-code release path,
not an infrastructure migration: the checked-in public-edge template must
match the deployed template before a change set is created, and the change set
may contain only `ApplicationFunction` code, the dependent
`PublicApiIntegration` URI and, when `EnableAgentChat` is `"true"`, the code of
`AgentChatWorkerFunction` (see [Agent chat worker](#agent-chat-worker-adr-021)),
all without replacement.

The separately reviewed IAM bootstrap has been applied as stack
`LegalDeskProductionCD`; its change set contained only the OIDC deployment role
and the constrained CloudFormation execution role. No application, data-plane,
frontend, or production code mutation was part of that bootstrap.

## Verified release — 2026-10-05

Promotion PR #49 triggered [production run 37310326393](https://github.com/herrerogusano/LegalDesk/actions/runs/37310326393)
for commit `4c0b6dbb5ce51cc00a937e8fd4ac6f3b90875d03`. All quality gates
and deployment jobs succeeded. The metadata artifact
`legaldesk-release-37310326393-1` contains a `PASS` release record, immutable
Lambda/frontend versions and their pre-release rollback references. The stack
finished `UPDATE_COMPLETE`; Lambda is active with a successful update and the
expected code SHA-256. All eight bounded HTTP checks and the five served asset
hashes passed, after CloudFront invalidation completed.

The first two runs (37306453976 and 37308886816) remain failed evidence, not
successful releases. CloudFormation rolled back their changes because its
resource/output read handlers needed narrowly scoped IAM-role and CloudFront
metadata permissions. Those reads were added without application IAM writes
or data-plane access before the successful retry. This release did not run
provider inference or renew the application's semantic/holdout attestation.

## Deployment boundary

`.github/workflows/production-cd.yml` runs reusable offline CI and pinned
release tooling/lint (`cfn-lint==1.57.2`) before the only job with
`id-token: write`. Production
deployments are serialized with `cancel-in-progress: false`; pushes to `prod`
are the normal trigger, while manual dispatch fails closed unless its input is
exactly `DEPLOY_PROD`.

GitHub OIDC reuses the existing account provider. The bootstrap trust policy
accepts only the verified immutable subject
`repo:herrerogusano@112490238/LegalDesk@1362771339:ref:refs/heads/prod` and the
`sts.amazonaws.com` audience. The workflow obtains the token without printing
it or temporary credentials.

`infra/cloudformation/cd-iam.yaml` creates separate, narrowly scoped release
and CloudFormation service roles. The release role can upload only the
immutable `phase-14/<40-hex-commit>/` Lambda prefix, update the five frontend
keys, create/read one CloudFront invalidation, inspect this existing stack, and
pass the exact CloudFormation role. The service role can read that immutable
artifact, read the unchanged Lambda execution-role metadata/policies required
by CloudFormation, read only the existing CloudFront distribution metadata
needed to evaluate its outputs, update Lambda code, and patch the one API
integration. The three CloudFront reads (`GetDistribution`,
`GetDistributionConfig`, and `ListTagsForResource`) are scoped to the exact
existing distribution ARN.
Those IAM actions are read-only and scoped to the exact existing application
role. Neither role
can invoke Lambda, access application S3/document keys, read or write
DynamoDB/Bedrock/Gateway/Memory, mutate application IAM policies, create or
delete stacks, or modify business resources. `ValidateTemplate` is the only
intentionally unscoped action because CloudFormation does not support a
resource scope for that API. The bootstrap is now complete; do not repeat it or
broaden its roles without a new inventory and supervisor review.

On 2026-10-05 the owner authorized making the repository public, after a
full Git-history secret scan and review of its synthetic fixtures. GitHub now
enforces protection on `developer` and `prod`: pull requests, an up-to-date
branch and the successful `verify` check from GitHub Actions are required;
conversations must be resolved, force pushes and deletion are prohibited, and
administrators are subject to the rules. The sole-owner workflow does not
require an independent approving review. External contributors' workflow runs
require approval; fork PR CI has no deployment credentials or OIDC permission.
Public read/fork access does not grant merge or deployment authority. The exact
prod/SHA and OIDC guards remain additional controls, not substitutes for branch
protection. `main` remains the unused legacy branch.

The local `scripts/deploy_release.py` command is dry-run by default. Production
execution requires `--execute` plus
`I_UNDERSTAND_LEGALDESK_PROD_CODE_ONLY`. It validates the manifest and strict
five-file archive, rejects immutable conflicts, preserves every unchanged
parameter (including `NoEcho`) with `UsePreviousValue`, uses
`UsePreviousTemplate`, validates the change set, verifies Lambda's base64
`CodeSha256`, records rollback versions before the first mutation, uploads only
the five frontend files with `no-cache`, waits for the six-path invalidation,
and checks `/` (200), `/logout` (302), `/api/me` (403), and all five CDN asset
hashes. It never invokes chat, ingestion, retrieval, Harness, Gateway,
Bedrock, or business-data smoke operations.

Retries are idempotent at the immutable code-parameter boundary. If the
existing stack already has exactly the requested artifact bucket, key, and
version, the command records `NO_OP`, skips change-set creation (including a
CloudFormation no-diff/failed change set), and still verifies Lambda's code
hash plus the frontend/CDN checks. A stable `UPDATE_ROLLBACK_COMPLETE` stack
is inventoryable and updateable, but the poller fences prior stable
`UPDATE_COMPLETE`/rollback statuses until it observes the requested code
parameters after execution; in-progress or failed states remain fail-closed.

On failure after mutation, the metadata-only record is marked `FAILED` and
retains the pre-release Lambda/frontend rollback references. Rollback is a
separate reviewed operation: restore those immutable versions, invalidate the
same six paths, and repeat the bounded unauthenticated checks. Never delete
the stack, buckets, documents, or shared data-plane resources to recover a
release. The only expected costs are existing-beta S3 versioned-storage/
request, CloudFormation/Lambda update, and one CloudFront invalidation costs;
no inference, ingestion, retrieval, or new capacity is part of CD.

## Agent chat worker (ADR-021)

Decision of the owner, 2026-11-02: the asynchronous agentic-chat worker lives in the
`LegalDeskPhase14PublicEdge` stack and runs the **same package** as
`ApplicationFunction`, so the code-only CD releases both in one change set. API and
worker share the job contract and never run different releases.

What changed in the command (`scripts/deploy_release.py`):

- The allowed change set is `ApplicationFunction.Code`, `PublicApiIntegration.IntegrationUri`
  and `AgentChatWorkerFunction.Code`, each `Modify` without replacement and with no other
  property. Any other resource of the agent chat stack (queue, DLQ, role, event source
  mapping, alarms, log group) is a `change_set_scope_violation`.
- The worker is **required** in the change set when the deployed stack has
  `EnableAgentChat=true` (`change_set_worker_missing` otherwise) and **forbidden** when it
  does not. With `EnableAgentChat` not `"true"` the worker does not exist and the command
  behaves exactly as before.
- After the stack update the command compares the base64 `CodeSha256` of **both** Lambdas
  with the release artifact before touching the frontend. A worker output
  (`AgentChatWorkerFunctionName`) that is missing on an enabled stack, a read failure or a
  different hash fail closed (`agent_chat_worker_output_missing`,
  `agent_chat_worker_verification_failed`, `agent_chat_worker_code_hash_mismatch`). A
  `NO_OP` retry verifies both as well.
- The metadata record gains `agentChatWorker` (`VERIFIED`, `DISABLED` or `NOT_CHECKED`).
  The rollback reference is unchanged: both functions take their code from the same
  `ApplicationCodeKey`/`ApplicationCodeVersion` parameters, so `previousLambda` restores both.

IAM (`infra/cloudformation/cd-iam.yaml`), added for that one function and nothing else:

| Role | Statement | Grants | Resource |
|---|---|---|---|
| Release (OIDC) | `VerifyOnlyTheAgentChatWorker` | `lambda:GetFunction`, `lambda:GetFunctionConfiguration` | the worker function ARN |
| CloudFormation service | `ReadAndUpdateOnlyAgentChatWorkerCode` | the same two plus `lambda:UpdateFunctionCode` | the worker function ARN |
| CloudFormation service | `ReadUnchangedAgentChatWorkerRolePolicy` | `iam:GetRole`, `iam:ListRolePolicies`, `iam:ListAttachedRolePolicies`, `iam:GetRolePolicy` | the worker execution role ARN |
| CloudFormation service | `PassUnchangedAgentChatWorkerRoleIfRequired` | `iam:PassRole` to `lambda.amazonaws.com` | the worker execution role ARN |

The last two mirror the statements that the application function needed in the verified
release (the failed runs of 2026-10-05 were IAM read handlers). They give no way to write
IAM. Two new required parameters, `AgentChatWorkerFunctionArn` and
`AgentChatWorkerExecutionRoleArn`, have no default and the function ARN pattern admits only
`LegalDeskPhase14PublicEdge-agent-chat-worker`, so the template cannot be pointed at another function.

**This role is deployed by an operator in T9, not by CD**, after the Public Edge change set
that creates the worker (the ARNs do not exist before) and before the next promotion to
`prod`. Until then a promotion fails closed with `deployed_template_scope_changed`, because the
checked-in Public Edge template differs from the deployed one. See
[`phase-15-live-runbook.md`](phase-15-live-runbook.md) for the order, the change-set review and
the `iam simulate-principal-policy` check of the result.

## Operator changes to the Public Edge stack outside the code-only scope

The `LegalDeskPhase14PublicEdge` stack is bound to `LegalDeskProductionCloudFormation`, the code-only role of this document. CloudFormation uses the stack's stored role for **every** update, including
an operator's: a change set that changes the function environment or creates resources fails on `lambda:UpdateFunctionConfiguration` (found in the T9 live block of 2026-11-03), and so does its
rollback. The code-only role is not widened. An operator change outside the code-only scope passes a **temporary, stack-scoped service role** with `--role-arn`, as in the Phase 14 trusted-edge rotation
(D-054): for Phase 15, `infra/cloudformation/phase-15-edge-agentchat-bootstrap.yaml` (stage `code-and-quota`, then `agent-chat`), derived from the deployed stack by
`scripts/phase15_changeset_preview.py bootstrap-params`. The stack then stores the temporary role; **the next code-only CD release passes the code-only role explicitly and restores it** (the workflow
does this on every run), after which the temporary stack is deleted, with `describe-stacks` as proof. Until then the temporary role stays and is recorded as an open item.

Order for the agentic chat (`docs/phase-15-live-runbook.md`, step 8): operator change with the agent off (new code, quota) → promotion (the CD publishes the new frontend and works with `EnableAgentChat=false`:
`agentChatWorker: DISABLED`, no worker permission needed) → operator change that switches the agent on → `cd-iam.yaml` (step 9) → releases with `agentChatWorker: VERIFIED`. Do not promote between the
switch-on and `cd-iam.yaml`: the change set would include the worker's code, which the code-only role cannot update yet.
