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
| The temporary role **lives only for the block**: it is closed in step 9b, right after step 9, in the same session; if it cannot be deleted the block is blocked | steps 8, 9b, 14 |

Findings of the first live attempt (2026-11-03: steps 3 to 7 done, step 8 failed and was restored) and where this runbook now answers them:

| Finding | Answer |
|---|---|
| The Public Edge stack is bound to `LegalDeskProductionCloudFormation` (the code-only role): a change set that changes the function **environment** or creates the AgentChat resources fails on `lambda:UpdateFunctionConfiguration`, and so does its rollback | step 8: a **temporary service role**, passed with `--role-arn`, as in the Phase 14 secret rotation (D-054); never widen the CD role |
| The new frontend is published only by the CD when `developer` is promoted to `prod`; with the agent on and the old frontend, the free chat box would break | step 8 is split: **8a** (code and quota, agent off) → **promotion** (CD publishes the frontend) → **8b** (agent on) → step 9 |
| `check-change-set` stopped on changes that are not edits (inherited `RoleArn`, `InterceptorConfigurations`, `ExecutionRoleArn`) and on `Memory` | fixed in the tool: inherited dynamic changes and `Replacement: Conditional` through the createOnly list (step 2, step 4) |
| `Out-File -Encoding utf8` of Windows PowerShell 5.1 writes a BOM and broke `json.load`; `SHA256SUMS.txt` hashed itself | every file is written with `Save-Text` (no BOM); the tools also read BOMs; the sums are computed before the file exists |
| The ADOT probe looks only at the Harness runtime log groups | step 5 states what it does and does not prove |

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

**Runtime platform review from 2026-11-12:** the [V2 impact assessment](agentcore-runtime-v2-impact.md)
verified the existing managed runtime on V1; the AWS Health notice changes the default for new runtimes.
Harness currently has no platform pin. Before an approved Harness update, STOP for Add/Remove, actual
replacement, unresolved `Conditional` replacement or stack recreation until the supervisor reviews
platform choice. Resolve conditionals against the live create-only paths (step 2); compare Harness ID,
runtime ID and explicit platform before/after, and STOP on an unexpected change. Use a current SDK:
the installed CLI omitted the platform field. This is an operator review note, not a new automated gate.

| | |
|---|---|
| Region / account | `eu-west-1` / `344774635844` |
| Stacks touched | `legaldesk-phase-01` (Harness), `LegalDeskPhase08Gateway`, `LegalDeskPhase14PublicEdge`, `LegalDeskProductionCD`; created and deleted in this block: `LegalDeskPhase15EdgeBootstrap` (the temporary role of step 8) |
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
# Files are written WITHOUT a byte-order mark: Out-File -Encoding utf8 of PowerShell 5.1 writes one and breaks json.load.
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Save-Text($Path, $Text) { [IO.File]::WriteAllText($Path, (@($Text) -join [Environment]::NewLine), $Utf8NoBom) }
function Assert-Aws($What) { if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE): stop" } }
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
  $out = aws cloudformation describe-stacks --stack-name $s;                                            Assert-Aws "describe-stacks $s"
  Save-Text "$BK\$s.describe-stacks.json" $out
  $out = aws cloudformation get-template --stack-name $s --template-stage Original --query TemplateBody --output text; Assert-Aws "get-template $s"
  Save-Text "$BK\$s.template.yaml" $out
  aws cloudformation detect-stack-drift --stack-name $s | Out-Null; Assert-Aws "detect-stack-drift $s"   # optional; wait and read describe-stack-drift-detection-status
}
# The ids of the Public Edge resources: the temporary role of step 8 is parameterised from them, never typed
$out = aws cloudformation describe-stack-resources --stack-name LegalDeskPhase14PublicEdge;             Assert-Aws "describe-stack-resources"
Save-Text "$BK\LegalDeskPhase14PublicEdge.resources.json" $out
# Harness, runtime id and the current counters, for the "before" picture
$harnessId = ((Get-Content "$BK\legaldesk-phase-01.describe-stacks.json" -Raw | ConvertFrom-Json).Stacks[0].Outputs | Where-Object OutputKey -eq HarnessId).OutputValue
$out = aws bedrock-agentcore-control get-harness --harness-id $harnessId;                               Assert-Aws "get-harness"
Save-Text "$BK\harness.json" $out
# Sums of every backup, computed BEFORE the sums file exists (otherwise it hashes itself) and without a BOM
$sums = Get-FileHash "$BK\*" -Algorithm SHA256 | Where-Object { $_.Path -notlike "*SHA256SUMS.txt" } | ForEach-Object { "$($_.Hash.ToLower())  $(Split-Path $_.Path -Leaf)" }
Save-Text "$BK\SHA256SUMS.txt" $sums
# What the code-only CD sees today: expected FALSE, the repository template is ahead of the deployed one
python $Repo\scripts\phase15_changeset_preview.py cd-template-match --deployed "$BK\LegalDeskPhase14PublicEdge.template.yaml"
```

**After.** Four stacks in `CREATE_COMPLETE`/`UPDATE_COMPLETE` (`UPDATE_ROLLBACK_COMPLETE` is stable but ask why; it is what a restored step 8 leaves);
the Public Edge `RoleARN` is `LegalDeskProductionCloudFormation` (the code-only role), which is why step 8 needs the temporary role;
`Parameters` of the Public Edge stack include `EnableAgentChat` **absent**; the Harness is `READY` with Phase 09
memory attached (that is what step 4 removes); note `HarnessArn`, `RuntimeArn`, `QuotaHarnessPerMonth` (probably
100) and the current `ApplicationCodeKey/Version`. The `cd-template-match` answer is `false`: this is the
`deployed_template_scope_changed` the next promotion would hit, and step 8a removes it. Check the sums: `SHA256SUMS.txt` lists the backups and not itself.
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
# Public Edge 8a: new application code and quota 50, agent chat still off
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$BK\LegalDeskPhase14PublicEdge.template.yaml" --local "$cf\phase-14-public-edge.yaml" `
  --param EnableAgentChat=false --param ApplicationCodeVersion=new --param QuotaHarnessPerMonth=50 --deployed-param ApplicationCodeVersion=old --deployed-param QuotaHarnessPerMonth=100 --profile public-edge-code-and-quota
# Public Edge 8b: the agent chat on (14 new AgentChat* resources, application function environment and role), on top of 8a
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$cf\phase-14-public-edge.yaml" --local "$cf\phase-14-public-edge.yaml" `
  --deployed-param EnableAgentChat=false --param EnableAgentChat=true --profile public-edge-agent-chat-on
# CD role: four statements, two roles modified in place
& python $Repo\scripts\phase15_changeset_preview.py preview --deployed "$BK\LegalDeskProductionCD.template.yaml" --local "$cf\cd-iam.yaml" --profile cd-iam
```

**What each is expected to show** (the profile enforces exactly this; anything else is a stop):

| Stack | Added | Modified (properties) | IAM statement-level changes | Replacement |
|---|---|---|---|---|
| Harness `legaldesk-phase-01` (memory off + ADOT on) | — | `MinimalHarness` (`EnvironmentVariables`, `Memory`), `HarnessExecutionRole` (`Policies`) | removes `UsePhase09Memory` | `Memory` is **conditional**: the only createOnly paths of the Harness are `HarnessName` and `Memory/ManagedMemoryConfiguration/EncryptionKeyArn` (`describe-type`), and this change touches neither; the profile accepts it explicitly and the real change set is checked against the same list |
| Harness (ADOT off, step 6) | — | `MinimalHarness` (`EnvironmentVariables`) | none | none |
| Gateway `LegalDeskPhase08Gateway` | `SearchDocumentsLogGroup`, `SearchDocumentsExecutionRole`, `SearchDocumentsFunction`, `SearchDocumentsGatewayTarget` | `MetadataMcpFunction` (`Code`, `Timeout` 10→20), `GatewayRequestInterceptorFunction` (`Code`), `GatewayExecutionRole` (`Policies`); `LegalDeskGateway` (`RoleArn`, `InterceptorConfigurations`) changes only because the role and the interceptor Lambda of the same change set change (neither is replaced): **inherited**, accepted by rule | adds `SearchDocumentsExecutionRole` (6 statements: no `*` action, no bare `*` resource; Retrieve/Guardrail/InvokeModel on the exact ARNs; S3 `GetObject` on the tenant's `original.pdf`/`original.txt` paths only) and one `lambda:InvokeFunction` on the new Lambda in the Gateway role | none |
| Public Edge **8a** | — | `ApplicationFunction` (`Code`, `Environment`: the two new variables and the quota), `PublicApiIntegration` (inherited from the function ARN, accepted by rule) | none | none |
| Public Edge **8b** | 14: `AgentChatQueue`, `…DeadLetterQueue`, `…WorkerLogGroup`, `…WorkerExecutionRole`, `…WorkerFunction`, `…EventSourceMapping`, 2 metric filters (`AgentChatFallbackMetric`, `AgentChatJobFailureMetric`), 6 alarms (DLQ, queue age, worker duration, worker errors, job failures, fallback rate) | `ApplicationFunction` (`Environment` only: the code is already deployed by 8a), `ApplicationExecutionRole` (`Policies`) | adds `AgentChatWorkerExecutionRole` (10 statements); application role: `PublishAgentChatJobIds` added, `ReadAuthorizationAndApplicationState` and `WriteOnlyApplicationOwnedState` widened by one leading key (`LEGALDESK#P15#CHATJOB#<tenant>#MATTER#*`) | none |
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
$out = aws cloudformation describe-change-set --stack-name legaldesk-phase-01 --change-set-name $name; Assert-Aws "describe-change-set"
Save-Text "$Root\harness-cs.json" $out
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\harness-cs.json" --profile harness-memory-off-adot-on   # exit 0 or STOP; read acceptedAsInherited / acceptedAsConditional
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

**What the probe covers, and what it does not** (the recorded result, `no_content_observed`, is not changed by this): it counts markers in the **CloudWatch log groups of the Harness runtime**
(`/aws/bedrock-agentcore/runtimes/<runtime id>*`; one group was found on 2026-11-03). It does **not** read spans (X-Ray / CloudWatch `aws/spans`, Transaction Search is not enabled) or any other
destination of the managed ADOT layer. And when `answerTokenWritten` is `false` (the model did not write the answer token), the answer side has **no conclusion**: only the question side was
tested. The evidence is bounded, which is why ADOT goes off whatever the verdict.

The session check and the loop-cut check are not here: they need the deployed worker, so they run together in step 11
(`scripts/phase15_harness_checks.py`), before the eval.

**After.** The probe report is saved; call count so far = 1.
**Stop if** the invocation errors (do not retry; ask). **Back.** Nothing to undo (ADOT is handled in step 6).

## Step 6 · Harness: disable ADOT again (◆ change set) — always, after the probe

**Do.** Same pattern as step 4 with `--set EnableManagedAdot=false` (memory stays off: `EnablePhase09Memory=false` is now the
deployed value, so `params` keeps it) and the profile `harness-adot-off`.

```powershell
python $Repo\scripts\phase15_changeset_preview.py params --stack-json <fresh describe-stacks of legaldesk-phase-01> --template "$cf\phase-01-harness.yaml" --set EnableManagedAdot=false --output "$Root\harness-params-2.json"
# create-change-set / wait / describe-change-set (Assert-Aws, Save-Text) as in step 4, then:
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
> (`search_documents`, `get_extracted_fields`), the script reports `request_rejected` or `tool_use_rejected` (a `client_parser_rejected` is our parser, not this risk; see the findings table), and a Harness change set adding
> those two names (and nothing else) is the fix — a decision for the owner, because it widens the Harness's own default allowlist.

## Step 8 · Public Edge, in three moves: 8a code and quota → promotion → 8b agent chat (◆ change sets with a temporary role)

**Why the order.** The browser code of the agentic chat is published by the CD when `developer` is promoted to `prod`. With the agent on and the old frontend, the free chat box would break. So the
application code and the quota go first with the agent **off** (8a), the CD then publishes the new frontend, and only then is the agent switched on (8b). The beta never loses a function.

**Why a temporary role.** The Public Edge stack is bound to `LegalDeskProductionCloudFormation`, the code-only CD role. CloudFormation uses it whenever the stack is updated, also by the operator. It can only
`UpdateFunctionCode`: any change set that changes the function environment (`QuotaHarnessPerMonth`, the agent chat variables) or creates the AgentChat resources fails on `lambda:UpdateFunctionConfiguration`,
and so does the rollback. The CD role is not widened. As in the Phase 14 trusted-edge rotation (D-054, [`idp-bootstrap-runbook.md`](idp-bootstrap-runbook.md)), a **temporary service role** with exactly the
permissions of these change sets is passed with `--role-arn`, and deleted once the code-only role is back.

### 8.0 · The temporary role (◆ stack `LegalDeskPhase15EdgeBootstrap`)

[`infra/cloudformation/phase-15-edge-agentchat-bootstrap.yaml`](../infra/cloudformation/phase-15-edge-agentchat-bootstrap.yaml), one role, two stages:

| Stage | Used by | Grants (all by exact ARN or name pattern; nothing is `*` except two regional read-only listings) |
|---|---|---|
| `code-and-quota` | 8a | what the code-only role has (read and `UpdateFunctionCode` on the application function, read of the immutable artifact, `PassRole` and read of the application role, read of the distribution and its dependencies, `GET`/`PATCH` of the one API integration) **plus** `lambda:UpdateFunctionConfiguration` and `lambda:ListTags` on that function. Creates and deletes nothing |
| `agent-chat` | 8b | the above plus create, update and delete of the AgentChat resources, each by pattern: queues `LegalDeskPhase14PublicEdge-agent-chat*`, the worker log group and its metric filters, the worker function, the mapping of that function, the worker role (`LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-*`, the name CloudFormation really generates, see below) with `PassRole` to Lambda only, the alarms `LegalDeskPhase14PublicEd*-AgentChat*`, and `iam:PutRolePolicy` on the application role only |

What it cannot do: attach or detach **managed** policies, create users, access keys or policies, change a trust policy or a permissions boundary, touch S3 objects other than reading the artifact, DynamoDB, Bedrock, AgentCore, Cognito,
any other role or any other Lambda. What it **can** do, and is the sensitive part: **write inline policies** (`iam:PutRolePolicy`, and in the stage `agent-chat` `iam:CreateRole` / `iam:DeleteRole` / `iam:DeleteRolePolicy`) in exactly
two places: the worker execution role it creates (`LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-*`) and the application role of the Public Edge stack. Those actions exist only in the stage `agent-chat`, never in `code-and-quota`, and only on those
ARNs (the tests pin it: `tests/test_phase15_edge_bootstrap_iac.py`, `tests/test_phase15_edge_bootstrap_access.py`). Whatever policy a template carries is therefore applied by this role, which is why the change set is gated by the **exact IAM statements**
of the profile (`public-edge-agent-chat-on` lists the 10 statements and the trust policy of the worker role and the 3 statement changes of the application role; any other statement, any wildcard or `NotAction`, stops the run).

**Who can use it.** CloudFormation assumes the role only if the caller of the change set passes it (`--role-arn`), which needs `iam:PassRole` on it. The holders today are the account administrators, who can already do everything. It is **not** the OIDC release
role of the CD (`LegalDeskProductionDeployment`): it can pass only `LegalDeskProductionCloudFormation`, and its `CreateChangeSet` is conditioned on `cloudformation:RoleArn` being that role. Nor is it the code-only CloudFormation role, whose `PassRole` reaches
only the application and worker roles, towards Lambda. The temporary role itself cannot pass itself, and nothing but CloudFormation can assume it. `tests/test_phase15_edge_bootstrap_access.py` checks every one of these statements against `cd-iam.yaml` and the bootstrap
template, so a later change that lets the CD (or any role of these templates) pass the temporary role fails the suite.

**Why the window is short.** The role exists only for the block: created in 8.0, closed in step 9b right after step 9 (a docs-only or closing promotion restores the code-only role, which is proven with `describe-stacks`, and the temporary stack is deleted in the
same session). A role that cannot be deleted blocks the close of T9; it is not an open item.

**Why no permissions boundary** (assessed, not adopted). A boundary on the worker role would need a managed policy created and kept before the change set, a `PermissionsBoundary` property in the Public Edge template (a change to a deployed template that the CD compares for equality), and a
condition on `iam:PermissionsBoundary` in `CreateRole`. It would not cover the second sensitive write, `PutRolePolicy` on the **existing** application role, which already exists and cannot be bounded retroactively without touching its policy. And a boundary that is slightly wrong denies a
needed action of the worker at run time, in a live block, where it is hard to tell from a missing permission. What bounds the risk instead: only administrators can pass the role (above), it lives for hours, the exact IAM statements are enforced by the profile before anything is executed, the inline
policies are read back after 8b (below), and the change sets are reviewed by a person. Revisit it if the role ever has to live longer or be usable by a non-administrator.

**Names that CloudFormation generates (and why the first pattern failed).** Resources with an explicit name (the two queues, the worker function, the worker log group) are matched by that name. The ones CloudFormation names itself are
`<stack>-<logical id>-<12 random characters>`, and **both** the stack name and the logical id are cut to fit the limit of the resource type (64 for an IAM role). The rule below is **inferred from two names observed on 2026-11-03**, it is not AWS
documentation:

| Role | Name | Parts |
|---|---|---|
| Application role (deployed) | `LegalDeskPhase14PublicEdge-ApplicationExecutionRole-X3hgVtmlHWXr` | stack 26 + logical id 24 + 12 = 64: nothing cut |
| Worker role (created by 8b) | `LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-bzFuZq7zur6f` | stack 26 -> 25, logical id 28 -> 25, + 12 = 64: **both cut** |

Rule: the first two parts share `limit - 14` characters; if they do not fit, each is cut to half of that, and a part shorter than half keeps its length while the other takes the rest. The first live attempt of 8b assumed that only the logical id is cut (pattern
`LegalDeskPhase14PublicEdge-AgentChatWorker*`), so `iam:CreateRole` was denied ("permissions error performing a tagging operation … not authorized to perform: iam:CreateRole") and the rollback failed on `iam:DeleteRolePolicy`; it ended
`UPDATE_ROLLBACK_COMPLETE` with the role absent. The model lives in `scripts/phase15_changeset_preview.py` (`cfn_generated_name`, command `generated-name`), is tested against both names, and the tests check **every** `AgentChat*` resource: queues, function and log group by
their explicit names (within their limits), the two metric filters by their log group, the mapping by its function plus tag-only access, the six alarms (limit 255: nothing cut, and the pattern `LegalDeskPhase14PublicEd*-AgentChat*` also holds if they were cut like a role),
the worker role as above.

**Do** (stage `code-and-quota` first; the parameters are derived from the deployed stack):

```powershell
$Boot = "LegalDeskPhase15EdgeBootstrap"; $bootTemplate = "$cf\phase-15-edge-agentchat-bootstrap.yaml"
python $Repo\scripts\phase15_changeset_preview.py bootstrap-params --stack-json "$BK\LegalDeskPhase14PublicEdge.describe-stacks.json" --resources-json "$BK\LegalDeskPhase14PublicEdge.resources.json" --stage code-and-quota --output "$Root\bootstrap-params.json"
aws cloudformation validate-template --template-body file://$bootTemplate | Out-Null;                                   Assert-Aws "validate-template"
aws cloudformation create-change-set --stack-name $Boot --change-set-name p15t9-bootstrap-1 --change-set-type CREATE `
  --template-body file://$bootTemplate --parameters file://$Root\bootstrap-params.json --capabilities CAPABILITY_IAM;     Assert-Aws "create-change-set"
aws cloudformation wait change-set-create-complete --stack-name $Boot --change-set-name p15t9-bootstrap-1;              Assert-Aws "wait change set"
$out = aws cloudformation describe-change-set --stack-name $Boot --change-set-name p15t9-bootstrap-1;                  Assert-Aws "describe-change-set"
Save-Text "$Root\bootstrap-cs-1.json" $out
# By eye: exactly ONE change, Add of AWS::IAM::Role EdgeAgentChatCloudFormationRole. Then, on the owner's go:
aws cloudformation execute-change-set --stack-name $Boot --change-set-name p15t9-bootstrap-1;                          Assert-Aws "execute-change-set"
aws cloudformation wait stack-create-complete --stack-name $Boot;                                                       Assert-Aws "wait stack-create-complete"
$tempRole = aws cloudformation describe-stacks --stack-name $Boot --query "Stacks[0].Outputs[?OutputKey=='EdgeAgentChatCloudFormationRoleArn'].OutputValue" --output text; Assert-Aws "describe-stacks"
```

**After.** Prove the stage with IAM simulation (a free read; the role may need ~10 s to propagate). `$fn` is the application function ARN, `$other` any other function of the account:

```powershell
aws iam simulate-principal-policy --policy-source-arn $tempRole --action-names lambda:UpdateFunctionConfiguration lambda:UpdateFunctionCode --resource-arns $fn   # allowed
aws iam simulate-principal-policy --policy-source-arn $tempRole --action-names lambda:UpdateFunctionConfiguration --resource-arns $other                          # implicitDeny
aws iam simulate-principal-policy --policy-source-arn $tempRole --action-names sqs:CreateQueue iam:CreateRole iam:PutRolePolicy lambda:CreateFunction --resource-arns "*"   # all implicitDeny at this stage
```

**Stop if** the change set has any other change, an "allowed" is denied, or a "denied" is allowed. **Back.** Delete the stack (nothing else uses it yet).

### 8a · New application code and quota 50, agent chat still off (◆ change set on `LegalDeskPhase14PublicEdge`)

**Before.** 8.0 done. Step 7 passed. The step 1 backup is the rollback source. Read the deployed `QuotaHarnessPerMonth` (probably 100): the owner decided **50** for the deployment; the template and code default
(300, ADR-021 D8) does not change, and a stack keeps its stored value unless it is set. The 10 browser-smoke jobs use 10 of the 50 jobs of that month; the eval and the checks consume no quota.

**Do.**

```powershell
$sets = @("EnableAgentChat=false","ApplicationCodeBucket=$bucket","ApplicationCodeKey=$key","ApplicationCodeVersion=$version","QuotaHarnessPerMonth=50")
$setArgs = $sets | ForEach-Object { "--set"; $_ }
python $Repo\scripts\phase15_changeset_preview.py params --stack-json "$BK\LegalDeskPhase14PublicEdge.describe-stacks.json" --template "$cf\phase-14-public-edge.yaml" @setArgs --output "$Root\edge-params-8a.json"
aws cloudformation validate-template --template-body file://$cf\phase-14-public-edge.yaml | Out-Null;                    Assert-Aws "validate-template"
aws cloudformation create-change-set --stack-name LegalDeskPhase14PublicEdge --change-set-name p15t9-edge-8a --change-set-type UPDATE `
  --template-body file://$cf\phase-14-public-edge.yaml --parameters file://$Root\edge-params-8a.json --capabilities CAPABILITY_IAM `
  --role-arn $tempRole;                                                                                                  Assert-Aws "create-change-set"      # <- the temporary role, explicitly
aws cloudformation wait change-set-create-complete --stack-name LegalDeskPhase14PublicEdge --change-set-name p15t9-edge-8a; Assert-Aws "wait change set"
$out = aws cloudformation describe-change-set --stack-name LegalDeskPhase14PublicEdge --change-set-name p15t9-edge-8a;  Assert-Aws "describe-change-set"
Save-Text "$Root\edge-cs-8a.json" $out
python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\edge-cs-8a.json" --profile public-edge-code-and-quota
```

Expected (as on 2026-11-03): only `Modify` of `ApplicationFunction` (`Code`, `Environment`) and the dependent `PublicApiIntegration` (inherited from the function ARN; the command reports it under `acceptedAsInherited`), no
`Add`, no IAM resource, no replacement. On exit 0 and the owner's go: `execute-change-set` (the role was bound at creation) and `wait stack-update-complete`.

**After.**

- `describe-stacks`: `UPDATE_COMPLETE`, `QuotaHarnessPerMonth=50`, `EnableAgentChat=false`; the stack `RoleARN` is now the temporary role (recorded; restored below).
- Application function: `CodeSha256` = the zip's; environment `LEGALDESK_QUOTA_HARNESS_PER_MONTH=50`, `LEGALDESK_AGENT_CHAT_ENABLED=false`, `LEGALDESK_AGENT_CHAT_QUEUE_URL` empty. No `AgentChat*` resource exists.
- `cd-template-match --deployed <fresh get-template>` is now **`true`**: the checked-in template equals the deployed one, so the code-only CD can run.
- Public checks of the CD, by hand: `/` → 200, `/logout` → 302, `/api/me` (no cookie) → 403; the free chat box still works (synchronous path, old frontend).

**Stop if** the profile gate ≠ 0, the stack rolls back, or `/api/me` leaves 403.

**Back.** A change set with the **backup template and the recorded previous parameters** (old code key/version, quota 100), again with `--role-arn $tempRole`. If a failed update leaves `UPDATE_ROLLBACK_FAILED`, as on 2026-11-03:
confirm that the function was never changed (`CodeSha256`, environment unchanged) and then `aws cloudformation continue-update-rollback --stack-name LegalDeskPhase14PublicEdge --resources-to-skip ApplicationFunction` (and `--role-arn $tempRole`
if the stored role cannot do the rest), record it, and look at the stack events before any new attempt. Never skip a resource whose state you have not read.

### Promotion · `developer` → `prod` (the CD publishes the new frontend)

By pull request, as always. The CD (`production-cd.yml`) passes `LegalDeskProductionCloudFormation` explicitly, which also restores the stack's stored role.

**Before.** 8a verified; `cd-template-match` = `true`; the CI of the promoted commit is green. Dry run locally (it makes no AWS call): `python scripts/deploy_release.py --commit <sha> --release-dir <dir> --artifact-bucket … --artifact-key …`.

**After** (the record of the run and AWS read-back):

- Workflow green; release record `PASS` with `agentChatWorker: DISABLED` (the flag is off: the worker does not exist and the CD behaves exactly as before). Change set of exactly `ApplicationFunction.Code` and `PublicApiIntegration.IntegrationUri`.
- The five frontend assets served by CloudFront have the hashes of the release; `/` 200, `/logout` 302, `/api/me` 403.
- `describe-stacks` of the Public Edge stack: `RoleARN` is **`LegalDeskProductionCloudFormation`** again.
- **The CD did not need the step 9 role**: the worker does not exist yet, so `LegalDeskProductionCD` is not touched here. `cd-template-match` still `true`.
- The free chat box works with the new frontend (agent off: synchronous path).

**Stop if** the CD fails closed (`deployed_template_scope_changed`, `change_set_scope_violation`…): do not continue to 8b; read the record and fix on the repository side. **Back.** The CD's own rollback record (previous Lambda and frontend versions).

### 8b · Agent chat on (◆ stage `agent-chat`, then change set on `LegalDeskPhase14PublicEdge`)

**Before.** The promotion verified. Re-take the Public Edge backup into `$BK\after-8a\` (`describe-stacks`, `get-template`, `describe-stack-resources`): the code parameters now point to the **release the CD deployed**, and 8b
keeps them (`UsePreviousValue`), so the worker and the API run the same package. Note that commit and zip hash: steps 11 and 12 use them as `--release-commit` / `--artifact-sha256`.

1. **Widen the temporary role to stage `agent-chat`** (a reviewed update of the bootstrap stack):

   ```powershell
   python $Repo\scripts\phase15_changeset_preview.py bootstrap-params --stack-json "$BK\after-8a\LegalDeskPhase14PublicEdge.describe-stacks.json" --resources-json "$BK\after-8a\LegalDeskPhase14PublicEdge.resources.json" --stage agent-chat --output "$Root\bootstrap-params-2.json"
   # create-change-set --change-set-type UPDATE on $Boot with the same template: exactly ONE change, Modify of the role (Policies), Replacement False, then execute and wait stack-update-complete
   ```

   Simulate again, with the name CloudFormation will really generate for the worker role (computed with the same model as the tests, not an example name; the random suffix is a placeholder):

   ```powershell
   $workerRoleName = python $Repo\scripts\phase15_changeset_preview.py generated-name --stack LegalDeskPhase14PublicEdge --logical-id AgentChatWorkerExecutionRole
   $workerRoleName      # LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-XXXXXXXXXXXX : both the stack name and the logical id are cut
   $acct = (aws sts get-caller-identity --query Account --output text); Assert-Aws "sts"
   $roleArn = "arn:aws:iam::${acct}:role/$workerRoleName"
   aws iam simulate-principal-policy --policy-source-arn $tempRole --action-names iam:CreateRole iam:TagRole iam:PutRolePolicy iam:GetRole iam:DeleteRolePolicy iam:DeleteRole --resource-arns $roleArn   # all allowed
   # PassRole is conditioned on iam:PassedToService = lambda.amazonaws.com: without the context key the simulator reports implicitDeny
   aws iam simulate-principal-policy --policy-source-arn $tempRole --action-names iam:PassRole --resource-arns $roleArn --context-entries ContextKeyName=iam:PassedToService,ContextKeyValues=lambda.amazonaws.com,ContextKeyType=string   # allowed
   $appRoleLike = "arn:aws:iam::${acct}:role/" + (python $Repo\scripts\phase15_changeset_preview.py generated-name --stack LegalDeskPhase14PublicEdge --logical-id ApplicationExecutionRole)
   aws iam simulate-principal-policy --policy-source-arn $tempRole --action-names iam:CreateRole iam:DeleteRole iam:DeleteRolePolicy --resource-arns $appRoleLike   # implicitDeny: never the application role's name
   ```

   Also **allowed**: `sqs:CreateQueue` on `…:LegalDeskPhase14PublicEdge-agent-chat` and `…-agent-chat-dlq`, `lambda:CreateFunction` on `…:function:LegalDeskPhase14PublicEdge-agent-chat-worker`, `lambda:TagResource` on an event source mapping, `cloudwatch:PutMetricAlarm` on
   `…:alarm:` + `(python …\phase15_changeset_preview.py generated-name --stack LegalDeskPhase14PublicEdge --logical-id AgentChatWorkerErrorsAlarm --limit 255)` (the name with 12 `X`), and `iam:PutRolePolicy` on the application role.
   **implicitDeny**: the same actions on any other name, `iam:AttachRolePolicy`, `lambda:CreateFunction` on another function and `s3:PutObject`. If the worker-role line above is not all **allowed**, stop: the rule no longer matches what CloudFormation generates and
   the pattern must be corrected through a reviewed change, as on 2026-11-03.

2. **The change set** (only `EnableAgentChat` changes; `AgentChatAlarmTopicArn` stays empty by decision: the six alarms are read in the console, they notify nobody; `AgentChatMaxConcurrency` keeps its default 2):

   ```powershell
   python $Repo\scripts\phase15_changeset_preview.py params --stack-json "$BK\after-8a\LegalDeskPhase14PublicEdge.describe-stacks.json" --template "$cf\phase-14-public-edge.yaml" --set EnableAgentChat=true --output "$Root\edge-params-8b.json"
   # validate-template, create-change-set (--role-arn $tempRole, --capabilities CAPABILITY_IAM), wait, describe-change-set (Save-Text) to $Root\edge-cs-8b.json
   python $Repo\scripts\phase15_changeset_preview.py check-change-set --change-set-json "$Root\edge-cs-8b.json" --profile public-edge-agent-chat-on
   ```

   Then, only on exit 0 and the owner's go, execute and wait.

**After.**

- Outputs: `AgentChatQueueArn`, `AgentChatDeadLetterQueueArn`, `AgentChatWorkerFunctionName` = `LegalDeskPhase14PublicEdge-agent-chat-worker`.
- Queue attributes: `VisibilityTimeout 900`, `MessageRetentionPeriod 3600`, redrive `maxReceiveCount 2`, SSE on; DLQ empty.
- `aws lambda list-event-source-mappings --function-name …-agent-chat-worker`: `Enabled`, `BatchSize 1`, `ScalingConfig.MaximumConcurrency 2`, `FunctionResponseTypes [ReportBatchItemFailures]`; worker `Timeout 150`, **no** reserved concurrency (`get-function-concurrency` empty).
- Worker and application `CodeSha256` = the zip of the release the CD deployed.
- **The inline policies that the temporary role wrote**: `aws iam get-role-policy` of the worker role and of the application role (`LegalDeskPublicApplicationDataPlane`): the `Sid`s are exactly those of the profile `public-edge-agent-chat-on` (`iam` list), no statement without a `Sid`, no `*` action, no bare `*` resource, no `NotAction`/`NotResource`.
- **Quota 50**: `describe-stacks` shows `QuotaHarnessPerMonth=50` and both functions show `LEGALDESK_QUOTA_HARNESS_PER_MONTH=50` (`QuotaChatsPerMonth` unchanged).
- **Alarms and metrics**: six `AgentChat*` alarms (`aws cloudwatch describe-alarms --alarm-name-prefix LegalDeskPhase14PublicEdge`), all `ActionsEnabled=false` with empty `AlarmActions`; the three on Lambda metrics (`Duration`, `Errors`, `Invocations`) have dimension
  `FunctionName` = the worker; exactly **two** metric filters on the worker log group, none with dimensions; no `AgentChatJobDurationMs`, `AgentChatJobsCompleted`, `…ByCode` or `…ByReason` metric exists.
- Public checks of the CD, by hand: `/` → 200, `/logout` → 302, `/api/me` (no cookie) → 403; the five frontend assets unchanged. A free chat question now returns a `jobId` and the new frontend polls it.
- `cd-template-match --deployed <fresh get-template>` is still `true`.

**Stop if** the profile gate ≠ 0 (especially any resource of another stack, `FrontendBucket`, `PublicApi*` other than the integration, or `Replacement`), the stack rolls back, or `/api/me` leaves 403.
An `AccessDenied` on a **tagging operation of `iam:CreateRole`** (CloudFormation words it "permissions error performing a tagging operation … not authorized to perform: iam:CreateRole") is the name-pattern case of 2026-11-03: compare the role name in the event with `generated-name`, it is the first thing to check. If the rollback or the creation fails on a missing permission, read the `AccessDenied` in the stack events, add exactly that action to the bootstrap template through a reviewed change (with its test), update the bootstrap stack and repeat: never widen the role on the fly.

**Back.** Profile `public-edge-agent-chat-rollback`: set `EnableAgentChat=false` (all other parameters `UsePreviousValue`, `--role-arn $tempRole`). It removes the 14 resources (queue and DLQ are `DeletionPolicy: Delete`; confirm both are empty first), leaves the application on the
new code and keeps the template identical to the repository's, so the CD keeps working.

After 8b the stack's stored `RoleARN` is the temporary role. **Do not promote until step 9 is done**; the role is closed by the promotion of step 9b, which comes **after** step 9.

## Step 9 · CD role (◆ change set on `LegalDeskProductionCD`, by the operator)

The worker must exist (ARNs): after 8b. This stack is **not** deployed by CD and is not reachable from the OIDC role. Until the worker exists the CD does not need it (the change set has no worker). Between 8b and this step a
promotion would include `AgentChatWorkerFunction.Code`, which the code-only CloudFormation role cannot update yet: the CD would fail closed and roll back. **Do not promote between 8b and this step.**

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

**Already executed before the attribute reads existed (live 2026-11-03).** The first version of this step did not give the code-only role the reads that CloudFormation makes on the neighbours of the functions it updates, and the CD of the promotion failed with
"Unable to retrieve Arn attribute for AWS::SQS::Queue ... not authorized to perform: sqs:getqueueattributes". If the stack `LegalDeskProductionCD` was executed from the older template, run **this step again** with the current `cd-iam.yaml` (same `Do` block, but check the change set with `--profile cd-iam-attribute-reads`: it adds
the statement `ReadOnlyAgentChatQueueAttributes` on `CloudFormationExecutionRole` and nothing else) before the next promotion.

**After.** Prove the permissions without using them (IAM simulation is a free read). The real ARNs of the queues come from the deployed stack, not from an example:

```powershell
$cfnRole = "arn:aws:iam::344774635844:role/LegalDeskProductionCloudFormation"; $relRole = "arn:aws:iam::344774635844:role/LegalDeskProductionDeployment"
$edgeOutputs = (aws cloudformation describe-stacks --stack-name LegalDeskPhase14PublicEdge --query "Stacks[0].Outputs" --output json | ConvertFrom-Json)
$queueArn = ($edgeOutputs | Where-Object OutputKey -eq "AgentChatQueueArn").OutputValue
$dlqArn   = ($edgeOutputs | Where-Object OutputKey -eq "AgentChatDeadLetterQueueArn").OutputValue
$queueArn; $dlqArn     # arn:aws:sqs:eu-west-1:344774635844:LegalDeskPhase14PublicEdge-agent-chat and ...-agent-chat-dlq
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names lambda:UpdateFunctionCode lambda:GetFunction --resource-arns $workerArn      # allowed
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names lambda:UpdateFunctionCode --resource-arns arn:aws:lambda:eu-west-1:344774635844:function:LegalDeskSearchDocumentsPhase15   # implicitDeny
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names iam:PutRolePolicy iam:AttachRolePolicy --resource-arns $workerRole             # implicitDeny
# the attribute reads CloudFormation makes on the neighbours when it updates the code (!GetAtt AgentChatQueue.Arn, !Ref AgentChatQueue, role Arn):
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names sqs:GetQueueAttributes sqs:GetQueueUrl --resource-arns $queueArn $dlqArn     # allowed
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names iam:GetRole --resource-arns $workerRole                                      # allowed
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names sqs:SendMessage sqs:ReceiveMessage sqs:SetQueueAttributes sqs:PurgeQueue --resource-arns $queueArn   # implicitDeny
aws iam simulate-principal-policy --policy-source-arn $cfnRole --action-names sqs:GetQueueAttributes --resource-arns arn:aws:sqs:eu-west-1:344774635844:SomeOtherQueue          # implicitDeny
aws iam simulate-principal-policy --policy-source-arn $relRole --action-names lambda:GetFunction --resource-arns $workerArn                                 # allowed
aws iam simulate-principal-policy --policy-source-arn $relRole --action-names lambda:InvokeFunction sqs:SendMessage --resource-arns $workerArn            # implicitDeny
```

**Stop if** an "allowed" is denied (the first promotion would fail closed and roll back) or a "denied" is allowed. An `Unable to retrieve ... attribute` error in the CD (`stack_update_failed`, an `UPDATE_FAILED` of `ApplicationFunction`, `AgentChatWorkerFunction` or `PublicApiIntegration` naming an `...:GetQueueAttributes`, `iam:GetRole` or similar denial for the
code-only role) is **this case**: CloudFormation resolves the `!GetAtt`/`!Ref` of the neighbours with the service role when it updates a function's `Code`. Read the denied action and resource in the event, add **only that read** on **only that resource** to `CloudFormationExecutionRole` through a reviewed change
(the test `tests/test_cd_iam_attribute_reads.py` derives the reads from the Public Edge template and fails when a new neighbour is not covered), and repeat this step; do not widen with a wildcard. **Back.** Change set from
`$BK\LegalDeskProductionCD.template.yaml` with the recorded parameters; the application function's CD path is unchanged either way.

**Next: step 9b, immediately.** The promotion that restores the code-only role is the next one, and it comes **after** this step: the CD's change set now includes the worker's code, which the code-only CloudFormation role can update only once this step has been executed. Do not stop here.

## Step 9b · Close the temporary role: restore the code-only role and delete the temporary stack (right after step 9, same session)

The temporary role must not outlive the block. After 8b the stack's stored `RoleARN` is the temporary role; it goes back the way D-054 did it, with a code-only CD release that passes `LegalDeskProductionCloudFormation` explicitly (the workflow does this on every run). That release
is made **now**, right after step 9: a `developer` → `prod` promotion that carries only documentation (this runbook's closing changes) or the T9 closing commit. It is also the first real use of the new CD path with the worker.

**Before.** Step 9 executed and its simulation passed (the code-only role may update the worker). The promoted commit contains no code change that has not been reviewed; a docs-only commit is preferred because the release then redeploys the same behaviour.

**Do.** The promotion by pull request. Then, in the same session:

```powershell
$edgeRole = aws cloudformation describe-stacks --stack-name LegalDeskPhase14PublicEdge --query "Stacks[0].RoleARN" --output text; Assert-Aws "describe-stacks"
if ($edgeRole -notmatch '/LegalDeskProductionCloudFormation$') { throw "stored role is $edgeRole, not the code-only role: STOP, do not delete the temporary stack" }
aws cloudformation delete-stack --stack-name $Boot;                                    Assert-Aws "delete-stack"
aws cloudformation wait stack-delete-complete --stack-name $Boot;                      Assert-Aws "wait stack-delete-complete"
$getRole = aws iam get-role --role-name (Split-Path $tempRole -Leaf) 2>&1 | Out-String
if ($LASTEXITCODE -eq 0) { throw "the temporary role still exists: the block is blocked" }
if ($getRole -notmatch "NoSuchEntity") { throw "could not confirm the temporary role is gone (not NoSuchEntity): the block is blocked" }
```

**After.**

- The workflow is green; the release record is `PASS` with `agentChatWorker: VERIFIED` (both Lambdas carry the release); change set of exactly `ApplicationFunction.Code`, `AgentChatWorkerFunction.Code` and `PublicApiIntegration.IntegrationUri`.
- `describe-stacks` of the Public Edge stack: `RoleARN` is `LegalDeskProductionCloudFormation`. A no-op or an assumed restoration does not count.
- The temporary stack is gone (`describe-stacks` of it fails with "does not exist") and `iam get-role` of the temporary role returns `NoSuchEntity`.
- `cd-template-match` is still `true`.

**Stop if** the CD fails closed (do not retry blindly: read the record; the Public Edge stack keeps whatever role it had), the stored role is not the code-only role, or the temporary stack cannot be deleted. **A temporary role that is still there is a blocker, not an open item:** step 11 onwards (live questions)
and the close of T9 do not proceed until it is deleted, and the owner is told at once. **Back.** The CD's own rollback record; the temporary stack can be created again from 8.0 if a further operator change were ever needed (it would restart the window).

## Step 10 · Re-read the whole state (AWS read-only)

Re-run the step 1 inventory into `$Root\after\`; compare. Expected: the four stacks `UPDATE_COMPLETE` (`LegalDeskPhase15EdgeBootstrap` is already deleted by step 9b), parameters changed only as in the steps above,
no resource outside the profiles, the Harness ARN/runtime id equal to step 1, ADOT disabled (unless accepted). **Stop if** anything else moved.

## Step 11 · Harness checks: allowed tools, session, cut loop (◆ paid: 5 Harness invocations)

[`scripts/phase15_harness_checks.py`](../scripts/phase15_harness_checks.py), the same pattern as the ADOT probe: dry run by default, create-only
report under `evals/results/phase15-harness-checks-*.json`, a budget of **5 invocations spent before each call**, no retries, two failures in a row stop
the run, and the report is written even if something fails after a paid call. It uses the worker's own binding, Harness client and output tree, so it
needs the **worker environment** (the Lambda's variables, with `LEGALDESK_HARNESS_ARN` equal to `--harness-arn`) and the test user's short-lived token in
`LEGALDESK_EVAL_ACCESS_TOKEN`, never on the command line.

| Check | Calls | Question it answers | Verdicts |
|---|---|---|---|
| `allowed_tools` (pre-check, PLAN_15 T9.5 risk) | 1 | Does Harness reject the per-invocation `tools`/`allowedTools` that name `search_documents` and `get_extracted_fields`, which the template's `AllowedTools` does not list? Also read after the cut-loop check, which makes the model *use* a new tool | `accepted`, `request_rejected`, `tool_use_rejected`, `client_parser_rejected`, `request_accepted_tool_use_not_exercised`, `inconclusive` |
| `session` (ADR-021 D7, T9.2) | 2 | Without memory, does the same `runtimeSessionId` keep history? Turn 1 carries a word and asks for a unique random acknowledgement token (matched as a whole word, never a substring such as ACK); turn 2 never spells the word; the word, the acknowledgement and each turn's own token are four different random values, so a hit can only come from history. Short markers (an acknowledgement of 17 characters), a prompt that says it is an automated check and forbids tools; the word of turn 1 is recognised loosely (its random core, any case) because the dangerous error is a false "not retained"; the acknowledgement stays strict. An `inconclusive` carries `ackDiagnosis` (`whole_word`, `inside_longer_token`, `different_case`, `empty_answer`, `absent`) and the lengths of both answers, never their text | `history_not_retained`, `history_retained`, `inconclusive`, `invoke_failed` |
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
| `allowed_tools` = `request_rejected` (a ValidationException whose message points at the tools; any other ValidationException is `inconclusive`, class `validation_unattributed`) / `tool_use_rejected` (an error that **Harness itself** put in the stream or raised, pointing at the tools) | do **not** run the eval; owner decides on the Harness `AllowedTools` change set (step 7 note). The loop-cut check is skipped when the pre-check is rejected |
| `allowed_tools` = `client_parser_rejected` (error class `client_parser_rejected`: **our** stream parser, `ClientStreamParserError`, refused the stream) | **not** a Harness finding and **not** an `AllowedTools` change: it is a defect of our client. Live 2026-11-03: Harness ran the tools but names a Gateway tool `legaldesk_gateway_search-documents___search_documents` in the stream while `allowedTools` holds `@legaldesk_gateway/search-documents___search_documents`; the parser now accepts exactly that image of each permitted entry (`@<group>/<rest>` → `<group>_<rest>`) and normalises it to the entry. If it happens again, look at the tool name in the stream (a diagnostic invocation, with approval), do not widen the parser by hand. Do not run the eval; the cut-loop check is skipped |
| `session` = `history_retained` | Harness keeps the history of a `runtimeSessionId` even with memory disabled (found live on 2026-11-03). The worker answers it in code: **a new `runtimeSessionId` on every agent call** (every job and every retry), so the report keeps showing `history_retained` (the check reuses one id on purpose) and records `sessionPolicy`; with that policy the verdict is handled and does not stop `all_passed`. The button-flow calls (`HarnessInvoker.invoke` for a binding) follow the same rule. A test pins the policy to the adapter |
| `tool_results` = `lost` | ADR-021 D5 last sentence: the output tree goes straight to the direct RAG fallback: code change before the eval |
| `tool_results` = `inconclusive` with every call `ERROR` and `toolErrorHints` | read the closed label before anything else: `missing_required_argument` is the case of 2026-11-03 below; `access_denied` points at the interceptor (its log line carries `denyStage`, a closed reason: `raw_matter_selector`, `selector_mismatch`, `invocation_binding`, `tool_target_pair`, `tool_not_in_invocation`); `tool_not_allowed` at Harness; `no_text` and `unclassified` mean the reason could not be classified |
| any `inconclusive` | no conclusion; say so in the acceptance document; the owner decides whether a new approval is worth it |

**Why `search_documents` came back `ERROR` on 2026-11-03 (second report).** Every tool of the agent requires `matterId` (the target schemas and the interceptor, which reads it from the raw request body), but the system prompt said the model never passes a matter and nothing gave it the value, so a call carried no valid `matterId`. The symptoms fit that and nothing else found offline: the target Lambda was never invoked and the interceptor only ran for ~2 ms with no log line (its lifecycle calls, `tools/list`, return without logging; a refused `tools/call` logs a WARNING). The fix is code, no AWS change: the client appends a server-built system instruction with the session's matter (`matter_selector_instruction`) and the prompt (1.3.0) tells the model to pass it unchanged. It is a selector, not authority: another value is refused by the interceptor. **Not proven without AWS:** whether Harness, Gateway or the interceptor refused the call; the next run records `toolErrorHints` per failed call and the interceptor now logs a closed `denyStage`, which settle it. Read-only evidence that would settle it today: the Gateway application logs for the window, if log delivery is enabled on the Gateway, and the `GatewayAuthorizationGrant` writes of the metadata table (none is written for a refused call, and they expire after 300 s: only readable soon after the call).

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

### Step 12, first real run (2026-11-03): gate failed; what was found offline and what the next run will say

Report `phase15-routing-20261103-a.json`: toolAccuracy 0.667 (gate 0.9), pathAccuracy 0.367, 29 of 30 cases fell back to RAG, 0 violations, 0 errors. Not re-run.

**1. `search_documents` failed 18 times out of 18 and was reported as a success.** CloudWatch: every run ended `knowledge_base_retrieve` `retrieval_invalid_response`. That one code covered four different
checks after a *successful* `Retrieve` (normalising the results, the document record, the record's status, the object HEAD). Ruled out offline: a missing `LEGALDESK_DATA_SOURCE_ID` (only ingestion uses it, `Retrieve` does not),
`get_for_scope` needing `Query` (it is a `GetItem`, allowed), the HEAD needing more than `s3:GetObject` (it does not), and the shape of a real Knowledge Base response (a test now feeds one: float page number, `x-amz-bedrock-kb-*` metadata,
typed location). **Not pinned without AWS** which of the four it was. The codes are closed now (`retrieval_results_malformed`, `retrieval_document_lookup_failed`, `retrieval_document_missing`, `retrieval_document_not_indexed`,
`retrieval_document_not_clean`, `retrieval_object_denied`, `retrieval_object_missing`, `retrieval_object_failed`), the failing search records the pipeline's code on its own tool event, and the next run says it. The behaviour is unchanged on purpose:
one unavailable document among the results (a stale vector, a document not `INDEXED`, an unreadable object) still blocks the whole response (fail closed). **If the code turns out to be `retrieval_document_missing`/`not_indexed`
because the Knowledge Base still holds vectors of documents that are gone or not indexed, the decision to take is whether to skip such a passage instead of failing the answer, or to re-sync/clean the Knowledge Base; that is an owner decision
(ADR), not made here.** Read-only look to pin it before spending anything: the document records of the eval matter (status and `malwareScanStatus`) against the documents of the Knowledge Base data source.

**2. A failed search must be an ERROR.** Gateway reports a returned payload as a successful call whatever it says. The Gateway entry point of the search Lambda is now `search_documents_gateway_handler`, which raises `SearchToolError(<closed code>)` on
`timeout`, `service_unavailable`, `access_denied`, `invalid_request` and a pipeline error, so Gateway answers `isError` (with debugging off the model sees a generic error; the code is in the Lambda log, WARNING). `blocked` (a fixed backend sentence) is still returned.
The worker's call log also marks any result that reports its own failure (`{"error": ...}` or `operationStatus: "error"`) as `ERROR` with the hint `tool_reported_error`, so it does not depend on every target raising.

**3. Gateway change set for the fix (do not execute without the owner).** Same stack `LegalDeskPhase08Gateway`, same parameters as step 7 (`EnableSearchDocuments=true`) with the three code triples pointing at the new artifact. Expected, and checked by
`check-change-set --profile gateway-search-handler-and-code`: **Modify** `GatewayRequestInterceptorFunction` (`Code`), `MetadataMcpFunction` (`Code`) and `SearchDocumentsFunction` (`Code`, `Handler`), `Replacement` `False` on all three;
no `Add`, no `Remove`, no IAM statement change, no Gateway or target change. Anything else is a stop. After: `get-function-configuration` of `LegalDeskSearchDocumentsPhase15` shows `Handler` `legaldesk.search_documents_lambda.search_documents_gateway_handler` and the new `CodeSha256`.
Back: the previous parameters and handler (`search_documents_lambda_handler`, which returns instead of raising).

**4. The agent side, offline.** The answer-plan grammar of prompt 1.3.0 is exactly what `resolve_loop` validates (tests build every block type from the prompt with realistic Harness tool-use ids); the matter-selector instruction did not shift the format; the
`get_extracted_fields` call the prompt prescribes satisfies the tool schema (limits included). So the plan format is not the cause. What the data explains: every `text`/`mixed` plan points at a `search_documents` call that had no usable result (cause 1),
and a model that gets an error sentence as an "answer" may answer in prose (`plan_invalid`). What it does **not** explain, and needs live evidence: (i) the three inventory cases and `xm-01` (`list_matter_documents` fine, plan valid, block omitted: only
`unknown_tool_use_id` can omit an inventory block, so either the model copied the tool-use id wrongly or the listing failed its contract); (ii) nine data/mixed loops that ended `failed` after the first tool (the interceptor saw `get_extracted_fields` once in the
whole run, so the other calls never reached Gateway; `data-05` shows the path works). The next report records, per case and as closed labels: `diagnostics.sourceIdMatches` (`exact`/`similar`/`none` for each plan source id), `invalidToolResults`,
`planError`, `omittedErrors`, `stopReason`, `toolStatuses`, `toolErrorHints`, `unansweredTools` (tools the model asked for whose result never arrived) and `streamErrorHint`. No text, no ids.

**5. Root cause, pinned read-only by the owner (2026-11-03).** The document bucket had the Phase 02 lifecycle rule `ExpireFictionalOriginals` (`tenants/`, 30 days, no versioning), so S3 deleted everything uploaded
on 2026-09-28 without telling the metadata records or the Knowledge Base. Two INDEXED documents of `mat_phase14_a_20260928` (`31073a29…`, `be50cac0…`) and three FAILED records had no object, and retrieval's fail-closed
check turned that one stale vector into 18 failed searches out of 18. The fail-closed behaviour is **kept** (it is what makes a ghost safe); what changes is that a ghost can no longer appear silently or by configuration:
the expiry is removed (Phase 02), the reconciliation Lambda reports each ghost with its probable cause and alarms (including while any canonical expiry exists), a test refuses to reintroduce an expiry without the code that keeps
record and KB consistent, and an operator tool plus runbook repairs it (`docs/phase-14-data-governance-runbook.md`, "Ghost documents"). **The routing eval and the golden set do not depend on those documents**: their expectations
are tool sets, output paths and closed reasons (never a document id, name or count of the live matter), and the golden real mode has not uploaded its corpus yet. **Order matters for that upload:** deploy the Phase 02 change first,
or the new corpus would expire in 30 days too. After the cleanup the live inventory loses the removed records (the three FAILED ones show up in `list_matter_documents` today, with status Failed).

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
5. **Temporary role**: the code-only role is the stored `RoleARN` of the Public Edge stack again and `LegalDeskPhase15EdgeBootstrap` is deleted (step 9b, which ran right after step 9). Re-read both here. If either is not true the block is **blocked**, not "open": nothing else of this list counts as done until it is.
6. Keep `$BK` and `$Root` **outside the repository** until the owner signs off, then archive them; they contain parameter values (ARNs, ids) but no secret
   (`NoEcho` values are masked by CloudFormation and were only ever passed as `UsePreviousValue`). Copy the evidence summary to `docs/phase-15-acceptance.md`.
7. Check the real cost the next day in the Billing console (not the Cost Explorer API) and record it next to the estimate.

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
| `RoleARN` of the Public Edge stack; temporary stack | `LegalDeskProductionCloudFormation`; `LegalDeskPhase15EdgeBootstrap` deleted | `describe-stacks`; `describe-stacks` of the temporary stack fails; `iam get-role` of the temporary role `NoSuchEntity` |
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
