# Phase 14 operations and release runbook

## Status

This stack is **deployed in `eu-west-1`**. The 2026-09-28 release exercise
confirmed all twelve alarms in `OK` state, alongside the monthly Budget and a
confirmed project SNS subscription. The transient `MalwareScanErrorsAlarm`
was traced to delayed events from the synthetic reconciliation fixture; the
document-security route and handler were corrected before the alarm returned
to `OK`. The operations stack is
`infra/cloudformation/phase-14-operations.yaml`; it reuses resource
names supplied as parameters and creates twelve standard CloudWatch alarms and
one monthly AWS Budget. It creates no alarm topic by default. An operator may
explicitly choose one of two mutually exclusive notification modes:
`ExistingAlarmTopicArn` reuses an already-owned topic, or
`CreateAlarmTopic=true` creates one project-owned topic plus one email
subscription using the existing `BudgetEmail` NoEcho parameter. No topic is
created when both modes are empty, and the template rejects both modes being
selected at once. No additional AWS call is authorized by this document.

## DLQ email deployment candidate — 2026-11-03

This candidate is **offline only; not deployed**. Read-only inspection found
five DLQs: malware scan and reconciliation already notify the Operations
topic; agent chat has an alarm with actions disabled; IDP work and OCR have
no alarms in the live stack **or the previous templates**, including conditions.
The two IDP alarms stay active independently of `EnableIDPProcessing`, since
retained messages still need attention when processing is stopped. They require
`ExistingAlarmTopicArn`; an empty destination is rejected. This reuses the
existing SNS action pattern without creating another topic/subscription.

Keep the chat alarm in Public Edge, using its existing
`AgentChatAlarmTopicArn`. Moving it into Operations would create a duplicate
alarm during migration and add queue-name wiring. Supplying that parameter
enables **all six** existing chat alarms, including the DLQ (not just one).
The other two DLQs keep their current alarms and actions in Operations.

The existing Operations topic has one confirmed email subscription; its
resource policy already permits CloudWatch alarms in this account/region.
No topic-policy or IAM change is needed in the application stacks. Read only
subscription protocol and confirmation status; never print `Endpoint`.
The cost gate is recorded in
[cost operations](phase-14-cost-operations.md#dlq-email-cost-check--2026-11-03).

### Deployment path and exact expected changes

Re-read live templates, parameters, stack roles and alarm configuration before
the supervisor prepares change sets. Preserve **every** existing parameter with
`UsePreviousValue`, including NoEcho values, immutable code versions, quotas,
`EnableAgentChat=true`, `EnableIDPProcessing=true` and review-dispatch settings.
Do not replace deployed code with the branch's latest artifact as part of this
alarm update. The 2026-11-03 original-template comparison found only the two
new IDP resources and the chat DLQ description delta below.

| Stack | Parameter delta | Expected resource changes | Replacement / IAM |
|---|---|---|---|
| `LegalDeskPhase14IDP` | New `ExistingAlarmTopicArn` = Operations `EffectiveAlarmTopicArn` output | Add `IDPWorkDeadLetterQueueAlarm`, `IDPOCRDeadLetterQueueAlarm`; no other resources | Add has no Replacement field; no existing resource replaced; IAM unchanged |
| `LegalDeskPhase14PublicEdge` | Existing `AgentChatAlarmTopicArn` = same output | Modify `AgentChatDeadLetterQueueAlarm` (description, actions), `AgentChatQueueAgeAlarm`, `AgentChatJobDurationAlarm`, `AgentChatWorkerErrorsAlarm`, `AgentChatFallbackRateAlarm`, `AgentChatJobFailureAlarm` (actions only) | Every Modify `Replacement=False`; IAM unchanged |
| `LegalDeskPhase14Operations` | None; preserve managed-topic mode and Budget | No changes / no deployment | Existing topic, policy, subscription and Budget unchanged |
| DocumentSecurity / Reconciliation | None | No changes / no deployment | IAM unchanged |

**Public Edge template and the CD.** `scripts/deploy_release.py` refuses to deploy (`deployed_template_scope_changed`) unless `infra/cloudformation/phase-14-public-edge.yaml` equals the deployed template exactly, comments aside. So any edit to that file, even a description, must ship in the same change as its stack update: do not merge it earlier. The new description of `AgentChatDeadLetterQueueAlarm` (the pointer to `#dlq-email-triage-and-redrive`) and of `AgentChatAlarmTopicArn` is therefore NOT in the template yet; add both in the PR that deploys this email change, and that change set then also shows the description as modified.

Both IDP alarms use native `AWS/SQS` maximum visible-message count **> 0**,
one five-minute period and `notBreaching` for missing data. This adds two
standard alarm metrics, zero custom metrics. No OK/insufficient-data emails
are added. An alarm sends on transition into ALARM, not once per message;
it is detection, not an email-delivery guarantee or a repeating reminder
([AWS alarm behavior](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Alarms.html)).

IDP has **no stored service role** at inspection time. Use the operator's
existing IAM-user session, with no `--role-arn` and no IAM expansion.
Public Edge **does** store `LegalDeskProductionCloudFormation`, whose
permissions are code-only. An operator login alone does not bypass that role.
Use the existing temporary-role procedure in
[live runbook step 8.0 / 8b](phase-15-live-runbook.md#step-8--public-edge-in-three-moves-8a-code-and-quota--promotion--8b-agent-chat--change-sets-with-a-temporary-role),
stage `agent-chat`, and explicitly bind its role ARN to the alarm change set.
The unchanged bootstrap template already permits `cloudwatch:PutMetricAlarm`
on these alarms; **do not widen either role**. Recreating that temporary
bootstrap stack is a separate reviewed Add of its one IAM role, not a claim
that the whole operator procedure has no IAM resources. Do not replay the
original code/quota/enablement steps or change CD IAM (step 9 is already done).
Close the temporary-role window using step 9b: restore the stored code-only
role through the reviewed code-only CD path, verify it with `describe-stacks`,
then remove only the temporary stack. No temporary role may remain at close.

Use `scripts/phase15_changeset_preview.py params` to generate parameter files
from private, fresh `describe-stacks` backups (not committed):

```powershell
$topic = aws cloudformation describe-stacks --stack-name LegalDeskPhase14Operations --region eu-west-1 --query "Stacks[0].Outputs[?OutputKey=='EffectiveAlarmTopicArn'].OutputValue" --output text
if ($LASTEXITCODE -ne 0 -or $topic -notmatch '^arn:aws:sns:eu-west-1:[0-9]{12}:[A-Za-z0-9_-]+$') { throw 'Alarm topic lookup failed' }
python scripts/phase15_changeset_preview.py params --stack-json "$BK\LegalDeskPhase14IDP.describe-stacks.json" --template infra/cloudformation/phase-14-idp.yaml --set "ExistingAlarmTopicArn=$topic" --output "$Root\idp-email-params.json"
if ($LASTEXITCODE -ne 0) { throw 'IDP parameter generation failed' }
python scripts/phase15_changeset_preview.py params --stack-json "$BK\LegalDeskPhase14PublicEdge.describe-stacks.json" --template infra/cloudformation/phase-14-public-edge.yaml --set "AgentChatAlarmTopicArn=$topic" --output "$Root\edge-email-params.json"
if ($LASTEXITCODE -ne 0) { throw 'Edge parameter generation failed' }
```

`$BK` and `$Root` are private evidence directories from the live runbook. The
supervisor creates UPDATE change sets with the respective repository templates
and these files; Public Edge uses `--role-arn $tempRole`, IDP omits it.
Use the existing template transport/validation procedure; `CAPABILITY_IAM`
acknowledges existing template IAM resources, not permission to change them.
Inspect `describe-change-set` against the **exact allowlist above**; the old
code/quota preview profiles do not authorize alarm updates. Stop on any code,
queue, mapping, policy, Budget or unrelated resource delta, replacement or
unresolved difference. No change set was created by this worker.

After supervisor-approved execution, independently verify the two added alarms
and six updated chat alarms have `ActionsEnabled=true` and the exact topic in
`AlarmActions`; verify all five DLQ queue dimensions and the existing two
Operations alarms too. Confirm the email subscription remains confirmed,
the stack roles are closed correctly and no topic/subscription was duplicated.
If a queue was already nonempty, check alarm history: enabling actions on an
already-ALARM alarm may not produce a new transition/email. A notification
delivery exercise requires its own owner-approved synthetic test; do not
inject or replay messages just to prove the deployment. Rollback through a
reviewed template/parameter change set, preserving queue contents and code;
disabling email again leaves a known monitoring gap that must be reported.

## DLQ email triage and redrive

1. Start with the alarm's `QueueName`, alarm history, and
   `sqs get-queue-attributes` for `QueueArn`,
   `ApproximateNumberOfMessages`, `ApproximateNumberOfMessagesNotVisible`,
   `ApproximateNumberOfMessagesDelayed`, `MessageRetentionPeriod` and
   `RedrivePolicy` on its source. These calls do not retrieve bodies. Check the
   verified queue ARN/account/region before proceeding. A zero visible count
   can coexist with in-flight messages; missing metrics do not prove emptiness.
2. Correlate approved job/event IDs with **existing redacted** worker logs,
   error codes, Lambda Errors/Throttles and event-source mapping health.
   Work DLQ: extraction failure, retry exhaustion or poison input. OCR DLQ:
   callback validation/continuation failure, expired job or duplicate completion;
   verify the Textract status and persisted job stage before replaying.
   Chat: inspect the failed/deadline-expired job and worker error category.
   EventBridge malware/reconciliation: inspect target delivery permissions,
   retry exhaustion and worker failures. Fix that cause before recovery.
3. Do not use console message-body viewers, CLI `--debug`, unfiltered
   `receive-message`, full log exports or body dumps in reports. If message
   metadata is essential, an approved operator may receive **one** message and
   project only safe fields, for example:

   ```powershell
   aws sqs receive-message --queue-url $dlqUrl --region eu-west-1 --max-number-of-messages 1 --visibility-timeout 30 --wait-time-seconds 0 --message-system-attribute-names ApproximateReceiveCount SentTimestamp --query 'Messages[].{MessageId:MessageId,ReceiveCount:Attributes.ApproximateReceiveCount,SentTimestamp:Attributes.SentTimestamp}' --output json
   ```

   This suppresses bodies/receipt handles from output but **receives the body
   in memory and changes visibility/receive count**; it is not a passive
   metadata API. No receive/delete/purge/redrive is authorized by this PR.
4. After the owner approves the request/inference budget, source, destination
   and message scope, native SQS redrive can use
   `start-message-move-task --source-arn <verified-DLQ-ARN> --destination-arn
   <verified-source-queue-ARN> --max-number-of-messages-per-second 1` for the
   chat/work/OCR DLQs, which have SQS source queues. Monitor with
   `list-message-move-tasks`; cancel the task if errors recur. This API moves
   the queue's available messages, **not one selected message**, and can
   restart billable model/OCR work. For a one-message recovery use an approved
   bounded application replay procedure instead; never copy bodies into shell
   arguments. Inspect persisted job state/idempotency first: a terminal chat
   job may be acknowledged without rerunning, and an OCR callback must not
   start Textract again. Reconcile application state separately if needed.
5. Malware/reconciliation DLQs originate at **EventBridge targets**, not SQS
   source queues. Do not use native SQS redrive or send their bodies directly
   to a Lambda. Use the owning runbook's explicitly approved bounded replay or
   synthetic reconciliation run after fixing delivery. Preserve evidence and
   remove only specifically approved recovered messages; never purge a DLQ.
   Verify source processing/job outcomes, visible/in-flight counts and alarm
   recovery independently. Retention expiry is not successful recovery.

See AWS [DLQ redrive](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-configure-dead-letter-queue-redrive.html)
and [StartMessageMoveTask](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/APIReference/API_StartMessageMoveTask.html).

## Completed release evidence — 2026-09-28

- Final public smoke report `phase14-public-smoke-20260928-08.json` passed with
  one `POST /api/chat`, cross-matter `403`, and zero cleanup errors.
- A DynamoDB PITR restore into an isolated table contained 62 items and was
  deleted after verification.
- Rollback to the prior immutable artifact from commit `50dd36f`, followed by
  forward recovery to final commit
  `fe53da68b1cb696de0741e94ff1ece99ecc3d710` (Lambda artifact SHA-256
  `2632928ac6e20e3ca23ae2e3e6a241e1aa456a50c6303b28e45d1ec46f8723be`), reached
  `UPDATE_COMPLETE` in both directions and returned HTTP 200.
- The synthetic reconciliation exercise passed with one invocation and
  `examined=6`, `changed=1`, `skipped=5`, `failed=0`; cleanup was verified.

The alarms are visible-only when both topic modes are empty. If either valid
mode is selected, all twelve alarms use the effective topic as their action.
For the managed mode, SNS sends a confirmation email; alarm delivery is not
considered active until the recipient confirms it. Do not reuse an unrelated
SNS topic. The managed topic/subscription is stack-owned and must be removed
only through an owner-approved teardown; SNS request/email delivery charges
may apply. Its topic policy grants only `cloudwatch.amazonaws.com` the
`sns:Publish` action, restricted by both the current account and the exact
regional CloudWatch alarm ARN pattern. This source-account/source-ARN binding
is the confused-deputy control; it is not applied to an existing topic because
that topic's owner controls its policy. The effective topic ARN is exposed in
the `EffectiveAlarmTopicArn` stack output for release evidence. The Budget
uses the supplied email directly and its 80%/100%
notifications are alerts, not a billing hard cap. It is deliberately
account-wide because some
Bedrock/Marketplace charges cannot be reliably attributed by project tag; set
the threshold with the account's existing non-LegalDesk spend in mind.
Since 2026-11-03 the Budget excludes credits and refunds so they cannot hide
resource usage. See [gross-usage budget accounting](phase-14-cost-operations.md#monthly-budget-measures-usage-before-credits)
for credit-balance lookup, pricing, and the deferred forecast alert.

## Beta SLOs and alert intent

These are release-candidate targets for the small authenticated fictional/public
beta, not a promise of legal-service availability:

| Signal | Beta target / alarm | Response |
| --- | --- | --- |
| HTTP API 5xx | zero expected; alarm on >=1 in 5 minutes | inspect API access log request ID and Lambda logs; rollback if persistent |
| HTTP API latency | p95 <=3 seconds for two 5-minute periods | inspect route and downstream latency; stop traffic if sustained |
| Application Lambda errors/throttles | zero errors and throttles in 5 minutes | check deployment, reserved/account concurrency and dependencies |
| Application Lambda duration | p95 <25 seconds for two periods (29-second API integration limit) | investigate slow provider/retrieval calls and disable traffic if needed |
| Malware Lambda errors/throttles | zero in 5 minutes | keep new documents quarantined; inspect retry/DLQ state |
| Reconciliation errors/throttles | zero in 5 minutes | keep cleanup disabled only after bounded operator review |
| Reconciliation duration | p95 <100 seconds for two periods (120-second timeout) | reduce scope/limit and investigate provider latency |
| Malware/reconciliation DLQ | zero visible messages | inspect original event and replay only with an idempotency review |
| Monthly spend | email at 80% and 100% of configured limit | stop optional AWS operations; budget is not a hard stop |

Missing CloudWatch data is `notBreaching` so an idle beta does not page as a
failure. This means the operational check must also confirm that the expected
traffic/heartbeat exists; an alarm in `INSUFFICIENT_DATA` is not evidence of
health.

## Local synthetic-fault exercise

Run these exercises without AWS or real-model calls:

1. Render the operations template with a local CloudFormation parser and
   assert all twelve alarms use native namespaces, bounded dimensions,
   `TreatMissingData: notBreaching`, and the effective topic action. Verify
   that the local default creates no topic, that `ExistingAlarmTopicArn` and
   `CreateAlarmTopic=true` are mutually exclusive, and that the managed mode
   requires email confirmation.
2. In the API/application provider doubles, inject one 5xx, a timeout-shaped
   latency, and a Lambda error/throttle result. Confirm the local incident
   report maps each signal to the runbook response and never prints tokens,
   prompts, document bodies, or secrets.
3. Inject malware/reconciliation failures into the existing Lambda fakes and
   assert the retry boundary, DLQ-visible state and quarantine/failed
   lifecycle remain idempotent.
4. Exercise the budget parser with an 80% and 100% notification event. Confirm
   the close procedure stops optional inference/ingestion and records the
   release commit; it must not pretend that an alert prevented billing.

Provider-side exercise, only after a separate numeric cost approval, uses one
fictional/public document and one pre-provisioned beta user. Trigger a bounded
synthetic error in a disposable stage, verify the alarm and runbook evidence,
then delete only the recorded fixture. Do not use a real legal document or
deliberately corrupt the production data path.

## Release procedure

1. Record the commit SHA, template SHA-256 values, exact parameter fingerprint,
   artifact SHA-256/manifest and intended rollback artifact. Exclude tokens,
   cookies, prompts, answers, source documents and secrets.
2. Build artifacts offline with `scripts/package_release.py`; verify the
   emitted manifest and Lambda zip before upload. The Lambda code is immutable
   in the deployment parameters through S3 object version.
3. Run `sam validate --lint` for every Phase 14 template and the focused local
   tests. Review the synthesized CloudFormation and IAM changes.
4. Create a CloudFormation change set for the edge, document-security,
   reconciliation and operations stacks. Do not execute it until the Phase 14
   cost gate has an approved account/region, numeric ceiling, retention and
   teardown target.
5. After execution, verify outputs, alarm dimensions, budget email state,
   exact public origin/CSP/CORS, and the authenticated synthetic journey. The
   first release must remain limited to the beta tenant and pre-provisioned
   users.

## Rollback

Prefer a CloudFormation change-set rollback or stack update to the previous
known-good template and immutable Lambda S3 object version. Stop public traffic
and new uploads first; leave existing documents quarantined unless the
incident procedure authorizes a safe transition. Preserve the metadata table,
source bucket, Cognito pool, Gateway, Memory and Knowledge Base. A rollback is
not complete until a synthetic read/upload/reconciliation check is recorded
and no new 5xx or DLQ messages are observed during the bounded observation
window.

If a change-set update is stuck, use CloudFormation continue-update-rollback
only after identifying the exact failed resource. Never delete a shared data
resource to recover a failed application deployment.

The bounded release exercise completed rollback to the prior `50dd36f`
artifact and forward recovery to `fe53da68...`; both stack updates were
`UPDATE_COMPLETE` and the synthetic HTTP check returned 200.

## Backup and recovery ownership

The operations stack does not silently enable a new backup service. The Phase
02 table template enables DynamoDB point-in-time recovery; deployment evidence
must verify it is active and the data owner must record the permitted recovery
window and owner-approved restore test. The deployed Phase 02 source bucket is
unversioned, so the fictional/public beta has re-upload-only recovery and no
object-version backup claim. Its encryption, lifecycle and retained-object
policy must be verified from the deployed resource; local template validation
is not proof of backup coverage. Enabling versioning is deferred because it
would be a one-way CloudFormation decision and would first require
version-aware deletion, delete-marker handling, additional least-privilege
permissions and a restore drill. The isolated DynamoDB PITR restore/delete
  drill is complete, but source-object versioning, retention ownership and the
  broader recovery design remain open; production promotion is blocked.

## Teardown and retained ownership

Teardown is a recorded, owner-approved operation—not part of a failed test.
Stop traffic and inference, disable the reconciliation schedule, and remove
only synthetic/public beta objects, sidecars, vectors, state records, alarms,
budget and temporary artifacts. Verify quarantine and source prefixes, DLQs,
log groups and CloudFormation events independently. If versioning is ever
approved, teardown must additionally list and delete object versions and delete
markers with dedicated, exact-scope controls; the current beta has no such
version-management path.

The candidate templates mark frontend bucket, log groups, malware/reconciliation
DLQs and other durable resources with `DeletionPolicy: Retain` where they own
retained evidence. Before deleting a retained resource, record its owner,
retention period, legal hold status, export destination and recovery procedure.
The shared source bucket, metadata table, Cognito, Gateway, Memory and
Knowledge Base are never deleted by the Phase 14 teardown by default.

Ownership must be explicit before deployment: a named beta operator owns
alarm review and budget close; an application owner owns rollback; a data
owner owns retention/deletion/export approval. No production promotion is
allowed while any owner or retained-resource inventory is unknown.

### Alert delivery ownership and retention

The alarm topic is not a delivery guarantee: CloudWatch actions can publish to
the topic, but the email endpoint must confirm the SNS subscription and remain
owned by the named beta operator. Record the confirmation and the topic ARN in
the release evidence without recording the email address in source control.
For teardown, disable alarm actions, remove the managed subscription and topic
through the stack, and retain only the minimum release/incident evidence. Do
not delete or modify an existing topic supplied through
`ExistingAlarmTopicArn`; its owner controls its lifecycle.

## Final4 release evidence — 2026-09-29

Current status is **CONSTRAINED_PROD_BETA_DEPLOYED** for pre-provisioned
authenticated users and fictional/public documents only. This does not certify
real legal data or strict TLS/custom domains.

- App commit `0c8bb718e58811afff2085114dad7821cf573e3f`; artifact SHA-256
  `99d074e793335f2867266b07ae091cb110a4747b8ad51e9180b031a2091f4f31`.
- Deployment stack `UPDATE_COMPLETE`; only Lambda Code and dynamic API
  integration changed, with no replacement. Lambda S3 object version is
  `.VmAi0D4.baSxBpc61lKMBFkXobwoMyf`.
- Smoke `phase14-public-smoke-20260929-final4-02.json` passed with one chat,
  two citations, cross-matter `403`, audit `17`, logout `200`, and no cleanup
  errors. Report SHA-256:
  `1431503bd4a336bf552853af4cb8eb7b87a8cc488a44e59b7542e9624281d153`.
- Twelve alarms are `OK` and actions are enabled. Governance ownership is the
  AWS/repository owner role for this solo project.
- No custom domain is owned; the default CloudFront TLS residual is accepted
  and documented, without claiming strict TLS. Anonymous signup and real
  legal/client data remain prohibited.
