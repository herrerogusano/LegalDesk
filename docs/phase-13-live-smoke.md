# Phase 13 — authorized live-smoke execution ledger

## Authorization and current gate

The user explicitly approved the proposed smoke on 2026-09-21: an incremental
USD 5 envelope, the fixed synthetic cases and request ceilings, no promotion
to prod. This supersedes the earlier NOT AUTHORIZED heading in the design
document for this one run; it does not authorize repeated inference/tuning.

Current status: **STOPPED BEFORE LOGIN — LOCAL FIX VERIFIED, TEARDOWN COMPLETE**.
One authorized attempt consumed; no inference/ingestion and no live rerun.
Local code baseline: `3be1a6d`, PR #18 against developer. The 378-test local
acceptance and 24/24 deterministic evaluations are not live-smoke results.

## Initial read-only inventory (before deployment)

Verified account `344774635844`, region `eu-west-1`. Sixteen AWS read-only
requests performed with one total SDK attempt; one bucket-CORS lookup reported
NoSuchCORSConfiguration. No Cost Explorer calls, writes, ingestion or inference.

| Dependency | Actual inventory result |
|---|---|
| Source / metadata | `LegalDeskPhase02Documents` CREATE_COMPLETE; bucket `legaldeskphase02documents-documentbucket-ojlu4kvhlied`; table `LegalDeskPhase02Documents-DocumentMetadataTable-1DANFLTX8RW7T` |
| Browser upload | Existing source bucket has no CORS configuration |
| Cognito | Pool `eu-west-1_tHvFPpktv`; public client `101hke40t7n5easmh7i3g9265o`, code flow, localhost:8000 callback, openid + legaldesk/use |
| Gateway | `legaldeskgatewayphase08-f17ddi2woq` READY; JWT allows the public client and legaldesk/use |
| Harness | `LegalDeskPhase01-7EMjvNs1PC` READY; Sonnet `eu.anthropic.claude-sonnet-4-6`; maxIterations 3; timeout 120s; managed ADOT/content extraction disabled |
| Runtime | `harness_LegalDeskPhase01-Kh25KQHkM9` (CloudFormation output, not invoked) |
| Memory | `LegalDeskPhase09-NKV8SZFz5U` ACTIVE |
| Lambda artifacts | Gateway stack still references Phase 11 artifacts; interceptor retry environment setting is absent |
| Knowledge Base | No LegalDesk KB found; an unrelated `aws-document-rag-dev` KB exists and must not be reused or modified |
| Guardrail | None listed in this region |
| Operator identity | Existing IAM user has AdministratorAccess; unsuitable as application credentials |

The earlier historical assumption that the source bucket was absent was wrong:
the live Phase 02 stack exists. Preserve this stack/table and its existing data.

## Initial preparation checklist (resolved or superseded below)

1. Instrument and test SDK attempt/token counters. The current adapter discards
   Harness numeric usage. SDK stream metadata exposes usage/latency but no
   authoritative count of internal model attempts or downstream Gateway calls.
   Do not claim a post-call counter is a pre-call billing guarantee.
2. Establish observable/bounded execution of the aggregate smoke ceilings;
   unresolved managed-service accounting remains a stop condition.
3. Prepare an exact-prefix synthetic KB source, vectors and Guardrail, preserving
   the existing table and unrelated KB. Do not ingest the whole existing bucket.
4. Review change sets for CORS and current Lambda artifacts/retry settings;
   preserve existing ARNs and disabled content logging.
5. Run the app with temporary least-privilege credentials, not the admin user.
6. Record new synthetic users/membership/object/version/resource IDs and exact
   cleanup targets before execution. Preserve all pre-existing data/resources.

The fixed PDF has been generated locally and visually inspected: one page,
2,003 bytes, SHA-256
`03de0479544250393d9709a1eb4511a1e24332871f06252b8cb4513398d0c17a`.
It contains only the fictional parties/payment sentence from the approved plan.
No file has been uploaded to AWS.

## Managed-service accounting gate

The original approved design made unobservable/unbounded aggregate ceilings a stop
condition. The initial preflight could not establish all those ceilings, so it
paused until the explicit residual-risk acceptance recorded below. This was not a failed
model evaluation, an IAM denial or evidence that the application is broken.

- Local SDK wrappers can reserve application calls before dispatch. They do
  not see requests issued inside Harness or deployed Lambda functions, nor
  the browser's presigned PUT. The initial utility was not yet wired into the
  factory. It was subsequently connected through an explicit optional smoke
  budget and scoped boto3 session, before the one live launcher attempt.
- InvokeHarness supplies numeric usage after execution, but its documentation
  does not define how multiple usage blocks within a stream should be combined.
  No undocumented aggregation is accepted as an authoritative billed total.
- `maxIterations=3` bounds agent-loop iterations, not a documented count of
  all internal retries, Gateway discovery calls or Memory requests. The API
  reference describes `maxTokens` as generated tokens per iteration; the
  operations guide calls it a per-invocation budget. Neither establishes a
  pre-call bound on total input/history/tool-schema tokens.
- Gateway publishes invocation metrics in one-minute batches. Those can aid
  subsequent reconciliation but cannot stop the 41st request in-flight.
  They also need attribution to this smoke rather than unrelated shared use.
- Enabling full traces to investigate this is not an acceptable shortcut:
  provider traces/logs may contain payloads. Existing content/ADOT logging
  remains disabled. No new observability feature has been enabled.

Sources checked 2026-09-21:
- [InvokeHarness API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeHarness.html)
- [Harness cost controls](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)
- [Gateway metrics](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.html)

## Decision needed before AWS execution

**Decision received 2026-09-21:** the user explicitly accepted the alternative
below for exactly one execution: strict application request/iteration/time
limits, post-call managed usage, and no guaranteed USD 5 billing cap. This
resolves the managed-accounting stop condition only. Resource scoping,
least-privilege credentials, local verification and cleanup gates still apply.

The existing USD 5 approval remains recorded; repeating the same approval
would not resolve this gate. Proceeding requires a specific change to the
execution conditions: accept local request caps plus provider iteration/time/
output limits and post-call numeric accounting, with managed internal counts
treated as estimates rather than guaranteed aggregate limits. This retains
the fixed cases, zero application/SDK retries, no tuning loops, no payload logs,
and no prod promotion. USD 5 remains an estimate/stop envelope, not a hard
account billing cap; a single in-flight provider invocation can exceed a
post-call threshold. Do not silently treat this alternative as approved.

Alternatively retain the original strict ceilings and keep execution blocked
until a compliant control has been designed and verified. No additional AWS
resources should be created merely while waiting for that decision.

At that initial pause no cleanup was necessary. Resources subsequently created
for the accepted attempt and their final cleanup are recorded below.

## Authorized deployment preparation (not execution evidence)

Dedicated tenant: `tnt_phase13_20260921`; matters
`mat_phase13_a_20260921` and `mat_phase13_b_20260921`.
Verify source prefix `tenants/tnt_phase13_20260921/` is empty before using it.
Provision authorization records only with conditional inserts, never overwrite.

Planned changes:
- CREATE `LegalDeskPhase13SmokeKnowledgeBase` from the Phase 03 template with
  that exact tenant prefix; new KB/data source/vector bucket/index and scoped
  KB execution role. Delete this new stack after the smoke.
- CREATE `LegalDeskPhase13SmokeGuardrail` from the unchanged Phase 06 template;
  new Guardrail/version. Delete this new stack after the smoke.
- UPDATE `LegalDeskPhase02Documents`: reviewed CORS-only bucket change, no
  replacements, new bucket or new table. Restore the original template at teardown.
- UPDATE existing Phase 07 Review and Phase 08 Gateway stacks with current
  versioned Lambda artifacts and zero-retry environment variables. Preserve
  resource identity and existing JWT clients/scope. At teardown restore the
  prior templates/artifact versions before deleting the new smoke artifacts.
- Temporary least-privilege STS application session; never use administrator
  credentials for the application. No persistent new application IAM role.
- Two suppressed-notification synthetic Cognito users; remove only their exact
  newly created usernames and conditional authorization records after the run.

Control-plane operator ceilings for this one attempt: CloudFormation up to eight
CreateChangeSet, eight ExecuteChangeSet, sixty Describe calls, three GetTemplate,
two DeleteStack and eight DeleteChangeSet; IAM two GetRole; STS two federation requests; Cognito
two each CreateUser/SetPassword/DescribeUser/DeleteUser. Each request has one
SDK attempt. Failed deployment is not automatically retried. S3 artifact puts
(three maximum), prefix existence check and cleanup count within the 30 S3
  operator/application slots; managed KB reads remain provider-owned estimates.
All new resource IDs/object versions must be recorded before cleanup. Never
delete the retained source/table, unrelated KB or pre-existing identities.

New artifact versions are removed only after restoring the original stack
references. Original artifacts/resources remain unchanged. Thus this smoke
does not establish a permanently deployed portfolio environment.

## Execution events

- Initial local regression: 390 tests, one loopback WinError 10053; isolated
  HTTP tests passed, then the complete 390-test run passed with buffered output.
  No product change was used to mask that transient transport error.
- Prefix existence check: zero objects in `tenants/tnt_phase13_20260921/`.
- AWS ValidateTemplate passed for Phase 02, 03 and 06 templates.
- Three original shared-stack templates backed up under ignored
  `build/phase13-smoke/`; these contain resource configuration, not credentials.
- Reviewed three change sets: KB adds five resources, Guardrail adds two;
  storage has no replacements/table changes. A direct original-template diff
  confirms only the CORS parameter/property changed; bucket-policy/role entries
  in the change set are dependent-reference evaluations, not new IAM statements.
- Executed those three change sets once. Completion is not yet asserted.
- All three completed: KB `40R8OKAZOR`, data source `A53UDNNEMP`, vector bucket
  `legaldeskphase13smokeknowled-legaldeskvectorbucket-tffjtjitmpmh`, index
  `legaldesk-phase03`; Guardrail `qin0b7t7vmtd`, version `1`; existing source
  stack UPDATE_COMPLETE with unchanged bucket/table IDs.
- Uploaded three 92,393-byte code ZIPs (same SHA-256
  `0e9343102a3bcec80cf662fdc98ed8403314d2ef15177eb4bb41e7497635881c`)
  to the existing artifacts bucket under `phase-13/smoke-20260921/`:
  `review-task.zip` version `tT6N2MX4I6935j0RzASgBwQbqvGdLrAK`,
  `interceptor.zip` version `U5m3YufMXLoJraKVxLKD7aZV9p6ZNiwc`,
  `metadata-mcp.zip` version `wWlYi33e0dY0OwWKSHiFdUiArzI59sGZ`.
- Review/Gateway update change sets reviewed: no replacements. Original
  template diffs change only zero-retry environment values; code parameters
  select the three new versions, and dependent IAM references retain the
  same permissions/ARNs. No JWT client/scope or logging policy changes.
- Both updates completed; all three Lambda functions Active/Successful with
  AWS_MAX_ATTEMPTS=1 and AWS_RETRY_MODE=standard. No Lambda business invocation.

## Final outcome — 2026-09-22

Runner code: `885579e` (with `66996ba` preparation). Exactly one authorized
launcher invocation. Two suppressed-notification synthetic Cognito users were
created, four conditional authorization records inserted, and a restricted STS
session obtained. The application factory then raised
`ValueError: audience is required for ID tokens`, before HTTP server/browser
startup. All application counters except the four fixture DynamoDB writes were
zero: **0 uploads, 0 ingestion jobs, 0 Retrieve, 0 Converse, 0 ApplyGuardrail,
0 InvokeHarness, 0 Memory calls**. No JWT was obtained through browser login.

Cause reproduced offline: PKCE returns only access_token, but the verifier's
default allowed both access and ID tokens and required an ID-token audience.
The production factory now sets `allowed_token_use={access}` explicitly. The
regression configuration omits audience just like the real deployment. Signature,
issuer, client_id, expiry and scope checks were not relaxed. 394 local tests
passed after the fix; four factory tests also passed with the exact missing-
audience configuration. No corrected live run was attempted.

### Cleanup and verification

- Launcher cleanup: nine bounded requests, no reported errors; later reads
  verified **two users absent and four authorization rows absent**.
- Temporary KB/data source/vector bucket/index/role and Guardrail/version stacks:
  **DELETE_COMPLETE**. No documents/vectors were ingested.
- Three shared stacks restored from original templates and original Phase 11
  artifact versions: **UPDATE_COMPLETE**. Original no-CORS state verified.
- Three exact smoke artifact versions deleted only after restoration; HEAD
  checks verified absence. These code ZIPs can be regenerated locally.
- No app server/browser was started; no credentials/passwords persisted.
  STS credentials existed only in process memory and are no longer held by a
  running smoke process; no claim of provider-side early session revocation.
- Existing source bucket/table, Cognito pool/client, Gateway, Harness, Memory
  and unrelated KB preserved. No prod promotion or PR merge.

The operator/application requests can incur small costs even with zero inference.
No billing API was called; actual billed spend is unknown. This result does not
demonstrate login, ingestion, RAG quality, tools or live cross-matter isolation.
The independent 14-case holdout is still unexecuted. A new smoke needs fresh
authorization and must rebuild its removed dependencies; do not reset the
one-shot sentinel to silently repeat this attempt.

Sanitized durable result: `evals/results/phase13-live-smoke-report.json`.
