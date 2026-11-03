# Phase 15 · T9 live runbook — agentic chat rollout and live checks

Status: **prepared, not executed.** This document does not call AWS, create a change set or
invoke a model. The block it describes is run by the supervisor with the owner (Alberto) **after
explicit cost approval** of [`phase-15-t9-cost-estimate.md`](phase-15-t9-cost-estimate.md)
(`AWS_COST_POLICY.md`, `AGENTS.md` §6). Style and discipline follow
[`idp-bootstrap-runbook.md`](idp-bootstrap-runbook.md) and [`phase-13-live-smoke.md`](phase-13-live-smoke.md).

Decisions this runbook relies on and does not reopen: ADR-021 (D2 read-only tools, D4 async worker,
D7 no Harness memory, D9 ADOT probe and residual risk, and the CD consequence of 2026-11-02),
D-029 code-only CD ([`continuous-delivery.md`](continuous-delivery.md)), and the frozen T8 gates
([`phase-15-routing-eval.md`](phase-15-routing-eval.md)).

Owner decisions of 2026-11-03 on the live block:

| Decision | Where it lands |
|---|---|
| **Final state A: the agentic chat stays on** after the tests, for demos | step 14 |
| **`QuotaHarnessPerMonth` = 50** in the deployment (a T9 parameter; the code default of ADR-021 D8 stays 300) | step 8 |
| Ceiling of the block **5 USD** and **50 Harness invocations** | cost estimate, budget below |
| Alarms **in the console only**: `AgentChatAlarmTopicArn` stays empty, no SNS | step 8 |
| **ADOT is switched off after the probe**, whatever the probe finds (and by default) | steps 5 and 6 |
| Job alarms use **native, free metrics** (SQS, Lambda); only two custom metrics remain | step 8 |

## How to read a step

Every step has the same five parts. Do not start a step until the previous one's **After** passed.

| Part | Meaning |
|---|---|
| **Before** | what must be true, checked with a read-only command or a local test |
| **Do** | the exact command(s); everything that costs money or writes is marked ◆ |
| **After** | what to read back and what the result must be |
| **Stop if** | the criterion to halt. Halting is the default: no paid call is retried and no change set is "fixed" on the fly without telling the owner |
| **Back** | how to undo the step |

Global stop rules: any unexpected resource, `Replacement` other than `False`, IAM statement outside the
profile, stack status other than `*_COMPLETE`, error from a paid call, a call count above the budget below,
or a cost reading above the stop value of the cost estimate. **Nothing is retried automatically.**

## Fixed facts (re-read them at step 1; do not trust this table)

| | |
|---|---|
| Region / account | `eu-west-1` / `344774635844` |
| Stacks touched | `legaldesk-phase-01` (Harness), `LegalDeskPhase08Gateway`, `LegalDeskPhase14PublicEdge`, `LegalDeskProductionCD` |
| Not touched | `LegalDeskPhase14IDP`, `…DocumentSecurity`, `…Reconciliation`, `LegalDeskPhase07ReviewTask`, Cognito, KB, Guardrail, Memory, buckets, table |
| Beta tenant / matters | `tnt_phase14_20260928` · `mat_phase14_a_20260928`, `mat_phase14_b_20260928` (synthetic documents only) |
| Code-artifact bucket | `legaldeskphase08artifacts-legaldeskphase08artifact-4lbcgkaewrsw` (versioned, immutable keys `phase-14/<40-hex commit>/legaldesk-lambda.zip`) |
| Model | `eu.anthropic.claude-sonnet-4-6` everywhere |

Paid-call budget (the whole block; see the cost estimate): **50 Harness invocations**, no retries —
step 5: 1 · step 11: 5 · step 12: 30 · step 13: 10 · reserve: 4.

```powershell
# Session variables (PowerShell 5.1). Backups and the ledger live OUTSIDE the repository.
$env:AWS_REGION = "eu-west-1"; $env:AWS_DEFAULT_REGION = "eu-west-1"
$Stamp  = Get-Date -Format yyyyMMdd
$Root   = "$env:USERPROFILE\legaldesk-t9\$Stamp"
$BK     = "$Root\backup"; New-Item -ItemType Directory -Force $BK | Out-Null
$Stacks = "legaldesk-phase-01", "LegalDeskPhase08Gateway", "LegalDeskPhase14PublicEdge", "LegalDeskProductionCD"
$Repo   = "<path of the worktree on the merged developer commit>"
$Preview = "python $Repo\scripts\phase15_changeset_preview.py"
```

## Step 0 · Gate (no AWS)

**Before.** The owner approved a USD ceiling and the call budget above in writing; note the reference
(message or ticket id) — the ADOT probe and the eval refuse to run without `--approval-ref`. `developer`
contains PR #64 and the T1–T8 work; `git status` clean on the commit to release; the offline CI of that
commit is green. Tools present: `aws` v2, `python` 3.12+, `cfn-lint`, Node for the browser smoke. A
**synthetic** Cognito test user exists with access to the synthetic matter only.

**Do.**

```powershell
cd $Repo
git status --porcelain=v2 --branch ; git log -1 --format=%H
$env:PYTHONPATH = "backend/src;agent/src;tests;."
python -B -m unittest discover -s tests -q                      # whole suite
python -B evals/phase15_routing_eval.py offline                 # T8 offline gate
cfn-lint --non-zero-exit-code error infra/cloudformation/phase-01-harness.yaml infra/cloudformation/phase-08-gateway-mcp.yaml infra/cloudformation/phase-14-public-edge.yaml infra/cloudformation/cd-iam.yaml
aws sts get-caller-identity                                     # must be the operator, never the OIDC release role
```

**After.** Suite and lint green; the identity's account is `344774635844`.
**Stop if** anything is red, or the identity is not the operator. **Back.** Nothing to undo.

## Step 1 · Inventory and backups of what is deployed (AWS read-only)

Read-only calls only; they are free. The backups are the rollback source for steps 4–9.

**Do.**

```powershell
foreach ($s in $Stacks) {
  aws cloudformation describe-stacks --stack-name $s | Out-File "$BK\$s.describe-stacks.json" -Encoding utf8
  aws cloudformation get-template --stack-name $s --template-stage Original --query TemplateBody --output text | Out-File "$BK\$s.template.yaml" -Encoding utf8
  aws cloudformation detect-stack-drift --stack-name $s | Out-Null    # optional; wait and read describe-stack-drift-detection-status
}
Get-FileHash "$BK\*" -Algorithm SHA256 | Out-File "$BK\SHA256SUMS.txt"
# Harness, runtime id, ESM and the current counters, for the "before" picture
$harnessId = ((Get-Content "$BK\legaldesk-phase-01.describe-stacks.json" -Raw | ConvertFrom-Json).Stacks[0].Outputs | Where-Object OutputKey -eq HarnessId).OutputValue
aws bedrock-agentcore-control get-harness --harness-id $harnessId | Out-File "$BK\harness.json" -Encoding utf8
aws s3api list-object-versions --bucket legaldeskphase08artifacts-legaldeskphase08artifact-4lbcgkaewrsw --prefix phase-14/ --max-items 5 | Out-Null
# What the code-only CD sees today: expected FALSE, the repository template is ahead of the deployed one
python $Repo\scripts\phase15_changeset_preview.py cd-template-match --deployed "$BK\LegalDeskPhase14PublicEdge.template.yaml"
```

**After.** Four stacks in `CREATE_COMPLETE`/`UPDATE_COMPLETE` (`UPDATE_ROLLBACK_COMPLETE` is stable but ask why);
`Parameters` of the Public Edge stack include `EnableAgentChat` **absent**; the Harness is `READY` with Phase 09
memory attached (that is what step 4 removes); note `HarnessArn`, `RuntimeArn`, `QuotaHarnessPerMonth` (probably
100) and the current `ApplicationCodeKey/Version`. The `cd-template-match` answer is `false`: this is the
`deployed_template_scope_changed` the next promotion would hit, and step 8 removes it.
**Stop if** a stack is `*_IN_PROGRESS`/`*_FAILED`, drift is reported on a resource this runbook modifies, or the
deployed template of a stack is **newer** than the repository's (someone deployed something else).
**Back.** Read-only.

## Step 2 · Local rehearsal of every change set (no AWS)

The repository templates are compared with the backups and the result is checked against the named profile in
[`scripts/phase15_t9_expectations.json`](../scripts/phase15_t9_expectations.json). This is the "dry run": it
shows what each change set will do, without creating one.

**Do** (one block per stack; `--param` is the new value, `--deployed-param` the deployed one that selects conditions):

```powershell
$cf = "$Repo\infra\cloudformation"
# Gateway: search-documents target, MCP timeout, IAM of the Gateway role
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$BK\LegalDeskPhase08Gateway.template.yaml" --local "$cf\phase-08-gateway-mcp.yaml" `
  --param EnableSearchDocuments=true --param MetadataMcpCodeVersion=new --param InterceptorCodeVersion=new `
  --deployed-param MetadataMcpCodeVersion=old --deployed-param InterceptorCodeVersion=old --profile gateway-search-documents
# Harness: memory off + ADOT on (and later ADOT off)
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$BK\legaldesk-phase-01.template.yaml" --local "$cf\phase-01-harness.yaml" `
  --deployed-param EnablePhase08Gateway=true --deployed-param EnablePhase09Memory=true --param EnablePhase08Gateway=true --param EnablePhase09Memory=false --param EnableManagedAdot=true --profile harness-memory-off-adot-on
# Public Edge: 14 new AgentChat* resources, application function and role
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$BK\LegalDeskPhase14PublicEdge.template.yaml" --local "$cf\phase-14-public-edge.yaml" `
  --param EnableAgentChat=true --param ApplicationCodeVersion=new --deployed-param ApplicationCodeVersion=old --profile public-edge-agent-chat
# CD role: four statements, two roles modified in place
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$BK\LegalDeskProductionCD.template.yaml" --local "$cf\cd-iam.yaml" --profile cd-iam
```

**What each is expected to show** (the profile enforces exactly this; anything else is a stop):

| Stack | Added | Modified (properties) | IAM statement-level changes | Replacement |
|---|---|---|---|---|
| Harness `legaldesk-phase-01` (memory off + ADOT on) | — | `MinimalHarness` (`EnvironmentVariables`, `Memory`), `HarnessExecutionRole` (`Policies`) | removes `UsePhase09Memory` | none; the preview cannot know the Harness's behaviour, so **the real change set must say `False`** |
| Harness (ADOT off, step 6) | — | `MinimalHarness` (`EnvironmentVariables`) | none | same as above |
| Gateway `LegalDeskPhase08Gateway` | `SearchDocumentsLogGroup`, `SearchDocumentsExecutionRole`, `SearchDocumentsFunction`, `SearchDocumentsGatewayTarget` | `MetadataMcpFunction` (`Code`, `Timeout` 10→20), `GatewayRequestInterceptorFunction` (`Code`), `GatewayExecutionRole` (`Policies`) | adds `SearchDocumentsExecutionRole` (6 statements: no `*` action, no bare `*` resource; Retrieve/Guardrail/InvokeModel on the exact ARNs; S3 `GetObject` on the tenant's `original.pdf`/`original.txt` paths only) and one `lambda:InvokeFunction` on the new Lambda in the Gateway role | none |
| Public Edge `LegalDeskPhase14PublicEdge` | 14: `AgentChatQueue`, `…DeadLetterQueue`, `…WorkerLogGroup`, `…WorkerExecutionRole`, `…WorkerFunction`, `…EventSourceMapping`, 2 metric filters (`AgentChatFallbackMetric`, `AgentChatJobFailureMetric`), 6 alarms (DLQ, queue age, worker duration, worker errors, job failures, fallback rate) | `ApplicationFunction` (`Code`, `Environment`: two new variables), `ApplicationExecutionRole` (`Policies`), `PublicApiIntegration` (`IntegrationUri`, dependency) | adds `AgentChatWorkerExecutionRole` (10 statements); application role: `PublishAgentChatJobIds` added, `ReadAuthorizationAndApplicationState` and `WriteOnlyApplicationOwnedState` widened by one leading key (`LEGALDESK#P15#CHATJOB#<tenant>#MATTER#*`) | none |
| `LegalDeskProductionCD` | — | both roles (`Policies`; the service role's `Description`) | four statements, all on the one worker function and its unchanged role | none; `RoleName` unchanged |

Also run the same preview with `--json` and keep the output in the ledger; and `cd-template-match` against the
repository template with itself must be `true` (sanity of the tool).

**After.** Every command ends `PROFILE …: OK`. **Stop if** `VIOLATIONS`, an unresolved condition, or `NEW PARAMETERS`
without a default that the step does not set. **Back.** Nothing to undo.

## Step 3 · Build and publish the immutable artifact (◆ one S3 write, a few MB)

**Before.** The release commit is the merged `developer` commit and its offline CI is green. A Linux/Python 3.12
dependency root prepared by the release operator exists (`package_release.py` never runs pip).

**Do.**

```powershell
python $Repo\scripts\package_release.py --dependency-root <linux-py312-deps> --output-dir "$Root\release"
$sha  = (Get-FileHash "$Root\release\legaldesk-lambda.zip" -Algorithm SHA256).Hash.ToLower()
$sha | Out-File "$Root\release\lambda.sha256" -Encoding ascii
$commit = git -C $Repo rev-parse HEAD
$bucket = "legaldeskphase08artifacts-legaldeskphase08artifact-4lbcgkaewrsw"; $key = "phase-14/$commit/legaldesk-lambda.zip"
# ◆ create-only: fails if the key exists
$put = aws s3api put-object --bucket $bucket --key $key --body "$Root\release\legaldesk-lambda.zip" --if-none-match "*" --server-side-encryption AES256 --metadata "legaldesk-sha256=$sha,legaldesk-commit=$commit" | ConvertFrom-Json
$version = $put.VersionId
```

**After.** `aws s3api head-object --bucket $bucket --key $key --version-id $version` returns the metadata hash `$sha`.
The same zip serves the application, the worker, the MCP Lambda, the interceptor and `search-documents`.
**Stop if** the key already exists with another hash (`immutable_artifact_conflict` in the CD). **Back.** None needed:
the object is immutable and costs cents; never delete versions.

## Step 4 · Harness: detach memory and enable managed ADOT (◆ change set, the first with a replacement risk)

**Before.** Step 2 passed. The probe approval reference is at hand. `HarnessArn` and `RuntimeArn` noted in step 1.

**Do.**

```powershell
$name = "p15t9-harness-$Stamp"
python $Repo\scripts\phase15_changeset_preview.py params --stack-json "$BK\legaldesk-phase-01.describe-stacks.json" --template "$cf\phase-01-harness.yaml" `
  --set EnablePhase09Memory=false --set EnableManagedAdot=true --output "$Root\harness-params.json"
aws cloudformation validate-template --template-body file://$cf\phase-01-harness.yaml    # read the Capabilities it asks for
aws cloudformation create-change-set --stack-name legaldesk-phase-01 --change-set-name $name --change-set-type UPDATE `
  --template-body file://$cf\phase-01-harness.yaml --parameters file://$Root\harness-params.json --capabilities CAPABILITY_IAM CAPABILITY_NAMED_IAM
aws cloudformation wait change-set-create-complete --stack-name legaldesk-phase-01 --change-set-name $name
aws cloudformation describe-change-set --stack-name legaldesk-phase-01 --change-set-name $name | Out-File "$Root\harness-cs.json" -Encoding utf8
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\harness-cs.json" --profile harness-memory-off-adot-on   # exit 0 or STOP
# ◆ only after exit code 0 and the owner's go:
aws cloudformation execute-change-set --stack-name legaldesk-phase-01 --change-set-name $name
aws cloudformation wait stack-update-complete --stack-name legaldesk-phase-01
```

**After.**

- `get-harness` shows `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`, **no** `DISABLE_ADOT_OBSERVABILITY`, memory disabled, status `READY`.
- **`HarnessArn` and `RuntimeArn` are identical to step 1.** (Public Edge and Gateway reference them.)
- Exactly the properties of the profile changed, nothing else (`describe-stack-events` for the stack).

**Stop if** the change set shows `Replacement` `True` or `Conditional` for `MinimalHarness`, the profile gate is not 0, or the
Harness ARN/runtime id changed after the update (then **do not continue**: Public Edge, IAM and the probe all point to the
old one — see Back).
**Back.** Create a change set with the previous parameters from `$BK` (`EnablePhase09Memory=true`, `EnableManagedAdot=false`) and
review it the same way. If the Harness was replaced, the old ARN is gone: stop and replan with the owner.

## Step 5 · ADOT probe (◆ paid: 1 Harness invocation)

**Before.** Step 4 passed. The owner is present and the approval reference is at hand.

**Do.** [`phase-15-adot-probe.md`](phase-15-adot-probe.md), steps 4–5: dry run first, then
`--execute --approval-ref <ref> --adot-enabled-attested`, one invocation, immutable report.

| Verdict | What happens |
|---|---|
| `no_content_observed` | bounded evidence, not proof; ADOT goes **off** anyway (owner decision of 2026-11-03); step 6 |
| `content_observed` | ADOT goes **off** (step 6). The residual-risk path of ADR-021 D9 (synthetic data, retention of 1–3 days, restricted IAM, `AGENTS.md` §7 qualified in writing) is *not* taken unless the owner reverses this decision |
| `log_read_failed`, `invoke_failed`, `no_log_groups_found` | no conclusion; **no re-run** without a new approval; step 6 |

The session check and the loop-cut check are not here: they need the deployed worker, so they run together in step 11
(`scripts/phase15_harness_checks.py`), before the eval.

**After.** The probe report is saved; call count so far = 1.
**Stop if** the invocation errors (do not retry; ask). **Back.** Nothing to undo (ADOT is handled in step 6).

## Step 6 · Harness: disable ADOT again (◆ change set) — always, after the probe

**Do.** Same pattern as step 4 with `--set EnableManagedAdot=false` (memory stays off: `EnablePhase09Memory=false` is now the
deployed value, so `params` keeps it) and the profile `harness-adot-off`.

```powershell
python $Repo\scripts\phase15_changeset_preview.py params --stack-json <fresh describe-stacks of legaldesk-phase-01> --template "$cf\phase-01-harness.yaml" --set EnableManagedAdot=false --output "$Root\harness-params-2.json"
# create-change-set / wait / describe-change-set as in step 4, then:
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\harness-cs-2.json" --profile harness-adot-off
```

**After.** `get-harness` shows `DISABLE_ADOT_OBSERVABILITY=true` again; same `HarnessArn`. The runtime log groups created during
the probe keep whatever retention they have: **lower** it to 3 days (`aws logs put-retention-policy`) — lowering is allowed, raising is not
(`AWS_COST_POLICY.md`). **Stop if** `Replacement` ≠ `False`. **Back.** Re-run with `EnableManagedAdot=true` only with a new approval.

This step is not optional: the final state keeps `EnableManagedAdot=false`. (Only if the owner reverses the decision of 2026-11-03 and accepts the residual risk of
ADR-021 D9 would ADOT stay on, with retention of 1–3 days set now and the `AGENTS.md` §7 text written in a separate reviewed PR before any real data is used.)

## Step 7 · Gateway: `search-documents` target and the new MCP/interceptor code (◆ change set)

**Before.** Steps 3 and 6 done; the artifact `$bucket`/`$key`/`$version` known. The values of the 11 Bedrock/KB/Guardrail parameters
are the Public Edge stack's own (same names), so read them from its backup rather than typing them:

```powershell
$edge = (Get-Content "$BK\LegalDeskPhase14PublicEdge.describe-stacks.json" -Raw | ConvertFrom-Json).Stacks[0]
function EdgeParam($n) { ($edge.Parameters | Where-Object ParameterKey -eq $n).ParameterValue }
$sets = @(
  "EnableSearchDocuments=true",
  "MetadataMcpCodeBucket=$bucket","MetadataMcpCodeKey=$key","MetadataMcpCodeVersion=$version",
  "InterceptorCodeBucket=$bucket","InterceptorCodeKey=$key","InterceptorCodeVersion=$version",
  "SearchDocumentsCodeBucket=$bucket","SearchDocumentsCodeKey=$key","SearchDocumentsCodeVersion=$version",
  "SearchTenantId=$(EdgeParam BetaTenantId)","SourceBucketArn=$(EdgeParam SourceBucketArn)",
  "KnowledgeBaseId=$(EdgeParam KnowledgeBaseId)","KnowledgeBaseArn=$(EdgeParam KnowledgeBaseArn)",
  "GuardrailIdentifier=$(EdgeParam GuardrailIdentifier)","GuardrailVersion=$(EdgeParam GuardrailVersion)","GuardrailArn=$(EdgeParam GuardrailArn)",
  "ResolverModelArn=$(EdgeParam ResolverModelArn)","WriterModelArn=$(EdgeParam WriterModelArn)",
  "ResolverFoundationModelArn=$(EdgeParam ResolverFoundationModelArn)","WriterFoundationModelArn=$(EdgeParam WriterFoundationModelArn)")
$setArgs = $sets | ForEach-Object { "--set"; $_ }
python $Repo\scripts\phase15_changeset_preview.py params --stack-json "$BK\LegalDeskPhase08Gateway.describe-stacks.json" --template "$cf\phase-08-gateway-mcp.yaml" @setArgs --output "$Root\gateway-params.json"
```

**Do.** `validate-template`, `create-change-set` (stack `LegalDeskPhase08Gateway`, `--template-body`, `--parameters file://…`, the capabilities
`validate-template` reports), `describe-change-set` to `$Root\gateway-cs.json`, then

```powershell
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\gateway-cs.json" --profile gateway-search-documents   # 0 or STOP
```

and, only on exit 0 and the owner's go, `execute-change-set` + `wait stack-update-complete`.

**After.**

- Stack `UPDATE_COMPLETE`; output `SearchDocumentsFunctionArn` exists.
- `aws lambda get-function-configuration` for `LegalDeskSearchDocumentsPhase15`, `LegalDeskMetadataMcpPhase08` and
  `LegalDeskGatewayRequestInterceptorPhase08`: `CodeSha256` equals the zip's (`[Convert]::ToBase64String` of the hex hash), timeout 25 / 20 / unchanged.
- `aws bedrock-agentcore-control list-gateway-targets`: `search-documents` and `metadata-mcp` `READY`; the existing `review-task-lambda` unchanged.
- Alarms of Phase 11/14 unchanged.

**Stop if** profile gate ≠ 0, a target is `FAILED`, the Gateway itself changed. A `Modify` of the Gateway or of `ReviewTaskGatewayTarget` is a stop.
**Back.** Create a change set from `$BK\LegalDeskPhase08Gateway.template.yaml` with the recorded previous parameters
(`EnableSearchDocuments` absent in the old template → deploy the old template body as is); `SearchDocuments*` resources are `DeletionPolicy: Delete`
and have no data.

> **Risk, checked in step 11 before any eval money is spent:** the Harness template's own `AllowedTools` lists only the Phase 08 tools
> (`list_matter_documents`, `get_document_metadata`, `create_review_task`); the agent chat sends `allowedTools` and the remote MCP tool **per
> invocation** (`invoke_agent_chat`). If Harness rejects per-invocation tools that are not in the template list
> (`search_documents`, `get_extracted_fields`), the script reports `request_rejected` or `tool_use_rejected`, and a Harness change set adding
> those two names (and nothing else) is the fix — a decision for the owner, because it widens the Harness's own default allowlist.

## Step 8 · Public Edge: queue, worker, alarms and the application code (◆ change set; releases the CD blockage)

**Before.** Step 7 passed. Read the deployed `QuotaHarnessPerMonth` from the step 1 backup (probably 100). The owner decided **50** for the deployment: a
parameter of this change set. The default of the template and of the code (300, ADR-021 D8) does not change, and a stack keeps its stored value unless it is set, so
it is set explicitly here. The 10 browser-smoke jobs of step 13 use 10 of the 50 jobs of that month; the eval and the checks consume no quota.

**Do.**

```powershell
$sets = @("EnableAgentChat=true","ApplicationCodeBucket=$bucket","ApplicationCodeKey=$key","ApplicationCodeVersion=$version","QuotaHarnessPerMonth=50")
$setArgs = $sets | ForEach-Object { "--set"; $_ }
python $Repo\scripts\phase15_changeset_preview.py params --stack-json "$BK\LegalDeskPhase14PublicEdge.describe-stacks.json" --template "$cf\phase-14-public-edge.yaml" @setArgs --output "$Root\edge-params.json"
# validate-template, create-change-set (stack LegalDeskPhase14PublicEdge, capabilities as reported), describe-change-set to $Root\edge-cs.json
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\edge-cs.json" --profile public-edge-agent-chat
```

`AgentChatAlarmTopicArn` stays empty by decision: the six alarms exist and are read in the console, they notify nobody (no SNS topic, no subscription). `AgentChatMaxConcurrency` keeps its default 2.
Then, only on exit 0 and the owner's go, execute and wait.

**After.**

- Outputs: `AgentChatQueueArn`, `AgentChatDeadLetterQueueArn`, `AgentChatWorkerFunctionName` = `LegalDeskPhase14PublicEdge-agent-chat-worker`.
- Queue attributes: `VisibilityTimeout 900`, `MessageRetentionPeriod 3600`, redrive `maxReceiveCount 2`, SSE on; DLQ empty.
- `aws lambda list-event-source-mappings --function-name …-agent-chat-worker`: `Enabled`, `BatchSize 1`, `ScalingConfig.MaximumConcurrency 2`,
  `FunctionResponseTypes [ReportBatchItemFailures]`; worker `Timeout 150`, **no** reserved concurrency (`get-function-concurrency` empty).
- Worker and application `CodeSha256` = the zip's.
- **Quota 50**: `describe-stacks` shows `QuotaHarnessPerMonth=50` and `get-function-configuration` of both functions shows `LEGALDESK_QUOTA_HARNESS_PER_MONTH=50`
  (`QuotaChatsPerMonth` unchanged).
- **Alarms and metrics**: six `AgentChat*` alarms (`aws cloudwatch describe-alarms --alarm-name-prefix LegalDeskPhase14PublicEdge`), all `ActionsEnabled=false`
  with empty `AlarmActions`; the three on Lambda metrics (`Duration`, `Errors`, `Invocations`) dimension `FunctionName` = the worker; exactly **two** metric filters on the
  worker log group, none with dimensions; no `AgentChatJobDurationMs`, `AgentChatJobsCompleted`, `…ByCode` or `…ByReason` metric exists.
- Public checks of the CD, by hand: `/` → 200, `/logout` → 302, `/api/me` (no cookie) → 403; the five frontend assets unchanged.
- `cd-template-match --deployed <fresh get-template>` is now `true`.

**Stop if** the profile gate ≠ 0 (especially any resource of another stack, `FrontendBucket`, `PublicApi*` other than the integration, or `Replacement`), the stack
rolls back, or `/api/me` leaves 403.
**Back.** Profile `public-edge-agent-chat-rollback`: set `EnableAgentChat=false` (all other parameters `UsePreviousValue`). It removes the 14 resources (queue and
DLQ are `DeletionPolicy: Delete`; confirm both are empty first), leaves the application on the new code, and keeps the template identical to the repository's, so the
CD keeps working.

## Step 9 · CD role (◆ change set on `LegalDeskProductionCD`, by the operator)

The worker must exist (ARNs). This stack is **not** deployed by CD and is not reachable from the OIDC role.

**Do.**

```powershell
$workerArn  = (aws lambda get-function-configuration --function-name LegalDeskPhase14PublicEdge-agent-chat-worker | ConvertFrom-Json).FunctionArn
$workerRole = (aws lambda get-function-configuration --function-name LegalDeskPhase14PublicEdge-agent-chat-worker | ConvertFrom-Json).Role
python $Repo\scripts\phase15_changeset_preview.py params --stack-json "$BK\LegalDeskProductionCD.describe-stacks.json" --template "$cf\cd-iam.yaml" `
  --set AgentChatWorkerFunctionArn=$workerArn --set AgentChatWorkerExecutionRoleArn=$workerRole --output "$Root\cd-params.json"
# validate-template, create-change-set (CAPABILITY_NAMED_IAM), describe-change-set to $Root\cd-cs.json
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\cd-cs.json" --profile cd-iam
```

Then, on exit 0 and the owner's go, execute and wait. Review by eye as well: the two roles are `Modify`, `RoleName` unchanged, `Replacement` `False`, and the
trust policy (`ExactProductionGitHubSubject`) is not in the diff.

**After.** Prove the permissions without using them (IAM simulation is a free read):

```powershell
$cfnRole = "arn:aws:iam::344774635844:role/LegalDeskProductionCloudFormation"; $relRole = "arn:aws:iam::344774635844:role/LegalDeskProductionDeployment"
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names lambda:UpdateFunctionCode lambda:GetFunction --resource-arns $workerArn      # allowed
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names lambda:UpdateFunctionCode --resource-arns arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskSearchDocumentsPhase15   # implicitDeny
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names iam:PutRolePolicy iam:AttachRolePolicy --resource-arns $workerRole             # implicitDeny
aws iam simulate-principal-policy --policy-source-arn $relRole --action-names lambda:GetFunction --resource-arns $workerArn                                 # allowed
aws iam simulate-principal-policy --policy-source-arn $relRole --action-names lambda:InvokeFunction sqs:SendMessage --resource-arns $workerArn            # implicitDeny
```

**Stop if** an "allowed" is denied (the first promotion would fail closed and roll back) or a "denied" is allowed. **Back.** Change set from
`$BK\LegalDeskProductionCD.template.yaml` with the recorded parameters; the application function's CD path is unchanged either way.

**Next promotion.** `developer → prod` is now the first real use of the new CD path: `scripts/deploy_release.py` dry-run, then the normal workflow.
Expect a change set of exactly `ApplicationFunction.Code`, `AgentChatWorkerFunction.Code` and `PublicApiIntegration.IntegrationUri`, and `agentChatWorker: VERIFIED` in the
release record. Any later edit of the Public Edge template needs this operator step first.

## Step 10 · Re-read the whole state (AWS read-only)

Re-run the step 1 inventory into `$Root\after\`; compare. Expected: the four stacks `UPDATE_COMPLETE`, parameters changed only as in the steps above,
no resource outside the profiles, the Harness ARN/runtime id equal to step 1, ADOT disabled (unless accepted). **Stop if** anything else moved.

## Step 11 · Harness checks: allowed tools, session, cut loop (◆ paid: 5 Harness invocations)

[`scripts/phase15_harness_checks.py`](../scripts/phase15_harness_checks.py), the same pattern as the ADOT probe: dry run by default, create-only
report under `evals/results/phase15-harness-checks-*.json`, a budget of **5 invocations spent before each call**, no retries, two failures in a row stop
the run, and the report is written even if something fails after a paid call. It uses the worker's own binding, Harness client and output tree, so it
needs the **worker environment** (the Lambda's variables, with `LEGALDESK_HARNESS_ARN` equal to `--harness-arn`) and the test user's short-lived token in
`LEGALDESK_EVAL_ACCESS_TOKEN`, never on the command line.

| Check | Calls | Question it answers | Verdicts |
|---|---|---|---|
| `allowed_tools` (pre-check, PLAN_15 T9.5 risk) | 1 | Does Harness reject the per-invocation `tools`/`allowedTools` that name `search_documents` and `get_extracted_fields`, which the template's `AllowedTools` does not list? Also read after the cut-loop check, which makes the model *use* a new tool | `accepted`, `request_rejected`, `tool_use_rejected`, `request_accepted_tool_use_not_exercised`, `inconclusive` |
| `session` (ADR-021 D7, T9.2) | 2 | Without memory, does the same `runtimeSessionId` keep history? Turn 1 carries a word and asks for a unique random acknowledgement token (matched as a whole word, never a substring such as ACK); turn 2 never spells the word; the word, the acknowledgement and each turn's own token are four different random values, so a hit can only come from history | `history_not_retained`, `history_retained`, `inconclusive`, `invoke_failed` |
| `tool_results` (T9.5, base of ADR-021 D5) | 2 | With the loop cut after the first tool (`maxIterations` forced to 1 in this process only), is the completed tool result still in the stream, and what does the production output tree do with it? | `retained` (any tool that completed with SUCCESS kept its result), `lost` (a tool was requested or succeeded and its result is missing or empty), `inconclusive` (no tool, or only ERROR results), `invoke_failed`, `skipped_tools_rejected` |

**Do.**

```powershell
python scripts/phase15_harness_checks.py --harness-arn <HarnessArn> --matter-id mat_phase14_a_20260928 --output evals/results/phase15-harness-checks-$Stamp-a.json   # dry run: the plan, no network
# ◆ after the owner's go; the token is in LEGALDESK_EVAL_ACCESS_TOKEN and the worker's environment variables are set
python scripts/phase15_harness_checks.py --harness-arn <HarnessArn> --matter-id mat_phase14_a_20260928 --output evals/results/phase15-harness-checks-$Stamp-a.json `
  --approval-ref <ref> --release-commit $commit --artifact-sha256 $sha --execute
```

**After.** Overall verdict `all_passed` (exit code 0) means: tools accepted, session keeps no history, tool result retained. Anything else is a finding to
hand to the supervisor, **not** something to fix live and **not** a reason to rerun (a rerun needs a new approval and a new file name):

| Finding | Consequence |
|---|---|
| `allowed_tools` = `request_rejected` (a ValidationException whose message points at the tools; any other ValidationException is `inconclusive`, class `validation_unattributed`) / `tool_use_rejected` | do **not** run the eval; owner decides on the Harness `AllowedTools` change set (step 7 note). The loop-cut check is skipped when the pre-check is rejected |
| `session` = `history_retained` | the worker must use a new `runtimeSessionId` per job (PLAN_15 T9.2): code change and release before the eval |
| `tool_results` = `lost` | ADR-021 D5 last sentence: the output tree goes straight to the direct RAG fallback: code change before the eval |
| any `inconclusive` | no conclusion; say so in the acceptance document; the owner decides whether a new approval is worth it |

The report holds closed verdicts, booleans, counts and SHA-256 of the markers, the Harness ARN and the matter: never a marker, question, answer or tool payload.
**Stop if** an invocation errors (the script does not retry).

**Residual state.** Each agent call of the script does what the worker does, so it leaves synthetic state in the metadata table (a conversation binding
with no TTL and a 5-minute grant). The report lists it under `residualState` (random ids and the SHA-256 of the key, never the key), noting each binding
**before** its call, so it is listed even if the call fails or the run is interrupted. **Its cleanup is mandatory and is done in step 14**, together with the eval's.

## Step 12 · Routing eval, real mode (◆ paid: 30 Harness invocations)

**Before.** Step 11 ended `all_passed` (or the owner accepted each finding in writing).

Gate fixed before the run (T8): acceptable tool set ≥ **27 of 30**, **0** allowlist violations, **0** invalid plan blocks accepted.

**Do.** Preflight without network, then the paid run exactly as in [`phase-15-routing-eval.md`](phase-15-routing-eval.md#real-run-t9):

```powershell
python evals/phase15_routing_eval.py real                      # preflight, no network
python evals/phase15_routing_eval.py real --execute --approval-ref <ref> --release-commit $commit --artifact-sha256 $sha `
  --matter-id mat_phase14_a_20260928 --output evals/results/phase15-routing-$Stamp-a.json
```

One invocation per case, `maxIterations` 3, no retries, abort after 3 consecutive errors (the cases not run count as failed). **Cost watch:** the report sums
`estimatedLoopCostUsd` (Harness iterations only, priced at the corrected `estimate-2026-11-eu-regional` table): typical ≈ USD 1.0, upper bound 1.75; **stop the
run and call the owner if it passes 2.20**.

**After.** The report is create-only and immutable; read `summary` (tool accuracy, path accuracy, violations, `estimatedLoopCostUsd`, latency p50/p95, tokens).
Commit the report, never the questions. **Stop if** < 27/30 or any violation: do **not** re-run to "get a pass" (a rerun needs a new approval and a new file name);
the failure is the finding. **Back.** None.

**Residual state.** Every case builds the worker's sealed binding, so each one leaves a conversation binding (`eval-<uuid>`, no TTL, no sweeper) and a 5-minute grant.
The report lists them under `residualState` in the same format as step 11 (ids and key hashes only), noting each binding **before** the case's call. The report is
written even if the run errors or is interrupted (`interruptedBy` says by what; the cases not run count as failed). Their deletion is **mandatory** (step 14).

## Step 13 · Browser smoke (◆ paid: ≈ 10 questions through the worker)

Use the synthetic user in the real browser against the CloudFront origin. Questions are the synthetic ones of the dataset; no real data. One tab, one question at a time.
The quota of the deployment is 50 `harness` jobs a month: the ten model calls below use 10, leaving 40 for demos in the month of the block.

| # | Action | Expected | Evidence |
|---|---|---|---|
| 1 | Legal-text question | "thinking…" then a grounded answer, identical to the `search_documents` result, ≥ 1 citation | job line `outputPath=grounded`, `tools=[search_documents]` |
| 2 | Open a citation (no model call) | the cited passage opens; the browser only ever saw opaque handles | network log has no document id/key |
| 3 | Data question (extracted fields) | block of backend-templated lines, each with value and "where does this come from" link; provisional fields marked "pending human review" | `outputPath=data` |
| 4 | Second data question (filter/group) | grouping and order from the closed lists, aggregates computed by the backend | `outputPath=data` |
| 5 | Mixed question | two blocks, each with its own citations; text block equals the tool result | `outputPath=mixed` |
| 6 | Inventory question | list of documents of the matter | `outputPath=inventory` |
| 7 | Out-of-scope question | polite refusal, no tool call, no Harness-visible content | `toolCalls=0` |
| 8 | Fallback: the dataset case whose expected path is `rag_fallback` | answer by direct RAG, reason recorded | `outputPath=rag_fallback`, `fallbackReason` set |
| 9 | Follow-up to #1 ("and what about the termination?") | uses the last pairs of history, answer correct | `historyMessages > 0` |
| 10 | Cross-matter: ask for a document that exists only in the other matter | denied by the interceptor, no data from the other matter, UI shows a normal "nothing found" answer | `toolFailures ≥ 1`; DLQ stays 0 |
| 11 | Close the session (logout) **right after** sending a question | job dropped without answer, quota reservation released, UI asks to repeat the question; no memory written | `action=failed`, `failure=session_closed`, `quotaReleased=true` |
| — | Review proposal: **look for it, do not use it** | there is no review button or proposal in the agentic chat (ADR-021 D2, 2026-11-02) | screenshot |

(Questions 1, 3–11 are model calls: ten Harness jobs; question 2 and the proposal check are free. PLAN_15 T9.6 still lists "propuesta de revisión"; ADR-021 D2 of
2026-11-02 removed it from the agentic chat, so here the check is its **absence**.)

**After (counters and state).**

- Quota: each job consumed 1 `chats` and 1 `harness` and **no** `gateway` (read the ledger item or the application's quota view).
- Memory: only the final question and answer per job, never tool output.
- `aws sqs get-queue-attributes` for the queue and the DLQ: both empty; no `agent_chat_job` line with `action=error`.
- CloudWatch namespace `LegalDesk/Phase15AgentChat` has data; the alarms are `OK`/`INSUFFICIENT_DATA`, none `ALARM`.
- Logs Insights (queries in [`phase-15-observability.md`](phase-15-observability.md)): per job the tools, iterations, path, latency, tokens and cost; **no** question, answer or passage in any log line (grep the worker log group for a marker word used in a question).
- Duplicate delivery: no job has two answers.

**Stop if** any answer shows data from another matter, a log line contains content, or a job has two answers or two quota debits. **Back.** Flag off (step 14).

## Step 14 · Cleanup and expected final state

**Do.**

1. Delete every change set that was created and not executed (`list-change-sets` per stack, `delete-change-set`).
2. Revoke/let expire the test user's session; unset `LEGALDESK_EVAL_ACCESS_TOKEN`; delete the shell history lines that held it.
3. Lower (never raise) the retention of any log group created in the block to the lowest the owner accepts (3 days for the Harness runtime groups).
4. **Mandatory: delete the synthetic conversation bindings of steps 11 and 12** and verify it (section below). It is not optional and the block is not closed until it is done.
5. Keep `$BK` and `$Root` **outside the repository** until the owner signs off, then archive them; they contain parameter values (ARNs, ids) but no secret
   (`NoEcho` values are masked by CloudFormation and were only ever passed as `UsePreviousValue`). Copy the evidence summary to `docs/phase-15-acceptance.md`.
6. Check the real cost the next day in the Billing console (not the Cost Explorer API) and record it next to the estimate.

### Mandatory cleanup of the synthetic conversation bindings (steps 11 and 12)

Both live tools write the same block, `residualState`, into their immutable reports: `evals/results/phase15-harness-checks-*.json` (3 bindings) and
`evals/results/phase15-routing-*.json` (up to 30, one per case run). Each entry holds random ids and `bindingPkSha256`, the SHA-256 of the partition key, never the key
(it holds the tenant, the matter and the user). The bindings have **no TTL and nothing in the project sweeps them**, so they stay until deleted. The 5-minute grants
(one per agent call, plus one per tool call written by the interceptor) expire by themselves and need nothing.

Not created by either tool: the job (memory only), `CHATJOB` items, quota counters, Harness or backend memory events, citation handles. The plain session-check calls write nothing.

The script reads **any number of reports** and deletes only keys it can rebuild and whose hash matches the report. It is built so that a failure can never read as success:

- a **pre-check** (account, region, table `ACTIVE`) runs before anything is touched, and every hash of every report is checked before the first delete;
- `$LASTEXITCODE` is checked after **every** `aws` call and any failure is a `throw` (expired credentials, wrong region or profile, throttling, network, and a `ConditionalCheckFailedException` on the delete when the item changed type);
- a binding counts as **absent only when the read succeeded and returned no item**, and as **deleted only after a successful delete**; empty output from `aws` is a failure, never "absent";
- the verification re-reads every key and a failed read stops it, so `remaining=0` can only be printed when every read succeeded and found nothing.

```powershell
$reports = Get-ChildItem evals\results\phase15-harness-checks-$Stamp-*.json, evals\results\phase15-routing-$Stamp-*.json   # every report of this block
$table   = "LegalDeskPhase02Documents-DocumentMetadataTable-1DANFLTX8RW7T"
$tenant  = "tnt_phase14_20260928"; $matter = "mat_phase14_a_20260928"
$user    = "<userId of the synthetic test user: the AUTH#USER#<userId> item, not the Cognito username>"
function Sha($s) { ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($s))) -replace "-", "").ToLower() }
function KeyFile($b) { Set-Content "$Root\key.json" (@{ pk = @{ S = "CONVERSATION#$tenant#$matter#$user#$($b.conversationId)" }; sk = @{ S = "SESSION#$($b.sessionSelector)" } } | ConvertTo-Json -Compress) -Encoding ascii }

# 0. Pre-check: right account, right region, table reachable. Nothing is touched before this passes.
if (-not $reports) { throw "no report found for stamp ${Stamp}: nothing to clean, or the wrong stamp" }
if ($env:AWS_REGION -ne "eu-west-1") { throw "AWS_REGION must be eu-west-1" }
$account = aws sts get-caller-identity --query Account --output text
if ($LASTEXITCODE -ne 0) { throw "sts get-caller-identity failed (exit $LASTEXITCODE): credentials expired or wrong profile; stop" }
if ($account -ne "344774635844") { throw "wrong AWS account ($account); stop" }
$status = aws dynamodb describe-table --table-name $table --query Table.TableStatus --output text
if ($LASTEXITCODE -ne 0) { throw "describe-table failed (exit $LASTEXITCODE): wrong region, profile or table name; stop" }
if ($status -ne "ACTIVE") { throw "table $table is $status; stop" }

# 1. Read every report and check every hash BEFORE deleting anything.
$entries = @()
foreach ($report in $reports) {
  foreach ($b in (Get-Content $report.FullName -Raw | ConvertFrom-Json).residualState.conversationBindings) {
    $pk = "CONVERSATION#$tenant#$matter#$user#$($b.conversationId)"
    if ((Sha $pk) -ne $b.bindingPkSha256) { throw "key does not match the hash in $($report.Name): stop, do not guess" }
    $entries += $b
  }
}
$expected = $entries.Count
if ($expected -eq 0) { throw "the reports list no binding: stop (a run that made calls always lists them)" }

# 2. Delete. Absent only if the call SUCCEEDED and returned no item; deleted only after a successful delete.
Set-Content "$Root\values.json" ('{":t":{"S":"ConversationBinding"}}') -Encoding ascii
$deleted = 0; $absent = 0
foreach ($b in $entries) {
  KeyFile $b
  $out = aws dynamodb get-item --table-name $table --key file://$Root\key.json --consistent-read --query "Item.[pk.S, entityType.S]" --output json
  if ($LASTEXITCODE -ne 0) { throw "get-item failed (exit $LASTEXITCODE): this is NOT 'absent'; stop" }
  if (-not $out) { throw "get-item returned nothing: stop" }
  $item = $out | ConvertFrom-Json
  if ($null -eq $item) { $absent++; continue }                                      # success and no Item: already gone
  if ($item[1] -ne "ConversationBinding") { throw "not a ConversationBinding: stop" }
  aws dynamodb delete-item --table-name $table --key file://$Root\key.json --condition-expression "entityType = :t" --expression-attribute-values file://$Root\values.json
  if ($LASTEXITCODE -ne 0) { throw "delete-item failed (exit $LASTEXITCODE; a ConditionalCheckFailedException means the item changed type): stop" }
  $deleted++
}

# 3. Verification: every listed binding is gone. A failed read is a failure, never a zero.
$remaining = 0
foreach ($b in $entries) {
  KeyFile $b
  $out = aws dynamodb get-item --table-name $table --key file://$Root\key.json --consistent-read --query "Item.[pk.S, entityType.S]" --output json
  if ($LASTEXITCODE -ne 0) { throw "verification get-item failed (exit $LASTEXITCODE): the cleanup is NOT verified; stop" }
  if (-not $out) { throw "verification get-item returned nothing: stop" }
  if ($null -ne ($out | ConvertFrom-Json)) { $remaining++ }
}
"reports=$($reports.Count) expected=$expected deleted=$deleted alreadyAbsent=$absent remaining=$remaining"
if ($remaining -ne 0 -or ($deleted + $absent) -ne $expected) { throw "cleanup incomplete: do not close the block" }
```

**Verification** is the last two lines: the summary with `remaining=0` and `deleted + alreadyAbsent = expected` (a run that threw printed no summary and does not count). Paste that line in the evidence ledger. The reads are consistent single-item reads and the deletes are
conditional single-item writes (free at this volume).

**If a report is missing** (the process was killed before it could write, or the file was lost) the ids are lost with it. As a last resort, with the owner's go, one read-only scan of
the metadata table with the filter `entityType = ConversationBinding AND tenantId = :tenant AND matterId = :matter AND (begins_with(conversationId, "checks-") OR begins_with(conversationId, "eval-"))`
lists them (it reads the whole table once: a few hundred items); delete only the items whose `userId` is the synthetic test user's, with the same conditional delete.

**Final state: A, the agentic chat stays on for demos** (owner decision of 2026-11-03). Verify it by reading it back, not by remembering it:

| Parameter or setting | Final value | Read-back |
|---|---|---|
| `EnableAgentChat` / `EnableSearchDocuments` | `true` / `true` | `describe-stacks` of `LegalDeskPhase14PublicEdge` and `LegalDeskPhase08Gateway` |
| `QuotaHarnessPerMonth` | **50** (code default 300 unchanged) | `describe-stacks` and the worker/application environment `LEGALDESK_QUOTA_HARNESS_PER_MONTH=50` |
| `AgentChatAlarmTopicArn` | empty (alarms in the console only, no SNS) | alarms `ActionsEnabled=false`, `AlarmActions` empty |
| `EnableManagedAdot` | **`false`** | `get-harness`: `DISABLE_ADOT_OBSERVABILITY=true`, `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true` |
| `EnablePhase09Memory` | `false` | `get-harness`: memory disabled |
| `cd-template-match` | `true`; the CD releases API and worker together (`agentChatWorker: VERIFIED`) | the command of step 8, on a fresh `get-template` |
| Conversation bindings of steps 11 and 12 | deleted | the cleanup summary line above |
| Change sets left over, ADOT window, retention of log groups created in the block | none / closed / lowered | steps 1 to 3 of this list |

Cost of this state, from [`phase-15-t9-cost-estimate.md`](phase-15-t9-cost-estimate.md): **1.30 USD a month fixed** (7 alarm metrics and 2 custom metrics; every other signal is a
native, free metric) plus the usage of the jobs actually asked, bounded by the quota: **2.86 USD typical and 9.09 USD upper bound** with all 50 used, so **4.16 / 10.39 USD**
in the worst month, plus the idle SQS polling (≈ 0.65 M requests a month, inside the free tier unless SQS is used elsewhere). `AWS_COST_POLICY.md` says never to leave continuous
polling on by default; the owner chose to keep the chat on for demos knowing this.

To switch the chat off later (not part of this block): the rollback change sets of the profiles `public-edge-agent-chat-rollback` (14 resources) and, after it, the Gateway one
in reverse (`EnableSearchDocuments=false`); both stay valid for the CD because the deployed template remains equal to the repository's (flags are parameters, not template text).
Both flags go together: the agent without `search_documents` is broken.

## Evidence ledger (fill during the run; copy the summary to `docs/phase-15-acceptance.md`)

| Step | Timestamp | Command id / change set | Result | Calls | Notes |
|---|---|---|---|---|---|
| 1 | | | | 0 | |
| 3 | | artifact version | | 0 | |
| 4 | | `p15t9-harness-…` | | 0 | |
| 5 | | probe report path | | 1 | |
| 6 | | | | 0 | |
| 7 | | | | 0 | |
| 8 | | | | 0 | |
| 9 | | | | 0 | |
| 11 | | harness-checks report path | | 5 | |
| 12 | | report path | | 30 | |
| 13 | | | | 10 | |
| 14 | | cleanup line (`reports=… expected=… deleted=… alreadyAbsent=… remaining=0`) | | 0 | |
