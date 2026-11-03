# Phase 15 · T9 live block: evidence (2026-11-03)

Operator: `arn:aws:iam::344774635844:user/aws-cloud-ai-web-cli` (`aws login` with MFA), region `eu-west-1`.
Approval: owner, 2026-11-03, in session. Ceiling USD 5 and 50 Harness invocations; steps 3–9, then 10–12 automatically if nothing fails.
Runbook: [`phase-15-live-runbook.md`](phase-15-live-runbook.md). Raw evidence (backups, change sets, parameters) is kept outside the repository in
`%USERPROFILE%\legaldesk-t9\20261103`.

## Steps 0–9

| Step | Result |
|---|---|
| 0 · Gate | Release commit `a011b58` (same tree as the CI-verified `925dcfe`); suite 1453 tests with one known Windows-only failure (CRLF fixture); offline eval 30/30; `cfn-lint` clean; operator identity verified |
| 1 · Inventory and backups | 4 stacks stable. Drift on `FrontendBucket` was a false positive (`DETECTION_FAILED`), verified by reading the bucket directly. Harness `READY` with memory attached and ADOT off. Public Edge quota `harness` = 20 |
| 2 · Local rehearsal | The 4 change-set profiles passed against the deployed templates |
| 3 · Artifact | `phase-14/a011b58…/legaldesk-lambda.zip`, SHA-256 `3fb93d69…d7d4`, create-only |
| 4 · Harness: memory off, ADOT on | The gate stopped on `Replacement: Conditional` (Memory) and a dynamic `ExecutionRoleArn`. The resource schema shows only `HarnessName` and `Memory/ManagedMemoryConfiguration/EncryptionKeyArn` are createOnly. Executed with the owner's go; **same Harness and Runtime ARNs** |
| 5 · ADOT probe | `no_content_observed`, 1 invocation. Bounded evidence: 1 runtime log group, no X-Ray spans, and the answer side is inconclusive (`answerTokenWritten=false`) |
| 6 · ADOT off | Gate OK; Harness `READY`, memory `disabled`, `DISABLE_ADOT_OBSERVABILITY=true` |
| 7 · Gateway | The gate stopped on inherited dynamic changes of the Gateway (`RoleArn`, `InterceptorConfigurations`). Executed with the owner's go: Gateway ARN, URL and id unchanged; targets `review-task-lambda`, `metadata-mcp` and `search-documents` `READY`; the 3 Lambdas carry the artifact |
| 8 (first attempt) | **Failed**: Public Edge has the stack service role `LegalDeskProductionCloudFormation` (code-only), which cannot update `Environment`. `UPDATE_ROLLBACK_FAILED`; recovered with `continue-update-rollback --resources-to-skip ApplicationFunction` after proving the function never changed. Template identical to the step 1 backup. Fixed by PR #68 (temporary CloudFormation role, D-059) |
| 8.0 · Temporary role | `LegalDeskPhase15EdgeBootstrap`, stage `code-and-quota`; IAM simulation as expected |
| 8a · Code and quota 50, agent chat off | Gate OK; app carries the artifact, `QuotaHarnessPerMonth=50`, `cd-template-match=true`, public checks 200/302/403 |
| Promotion (#69) | CD green: new frontend published, stack `RoleARN` restored to the code-only role |
| 8b (first attempt) | **Failed**: CloudFormation truncates **both** the stack name and the logical id of generated role names (`LegalDeskPhase14PublicEdg-AgentChatWorkerExecutionR-…`). The temporary role pattern did not match. Clean rollback with no orphans. Fixed by PR #70 (naming model checked against the real names; `PassRole` simulated with `iam:PassedToService`) |
| 8b · Agent chat on | Gate OK; IAM simulated with the exact generated names. 14 resources created; queue 900 s / 1 h / `maxReceiveCount` 2 / SSE; ESM `MaximumConcurrency` 2; worker timeout 150 s and no reserved concurrency; worker and app run the same code; inline policies read back; 6 alarms with no actions; 2 metric filters; `cd-template-match=true` |
| 9 · CD role | Gate OK; the code-only role may update only the worker code; the release role may only read the worker |

Spend so far: 1 Harness invocation and one S3 upload (cents).

## Pending

9b (this promotion restores the code-only role, then the temporary stack is deleted), 10 (re-read the state), 11 (Harness checks), 12 (real routing eval),
13 (browser smoke with the owner) and 14 (cleanup and final state).
