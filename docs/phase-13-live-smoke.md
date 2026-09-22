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

## Attempt 2 — 2026-09-22 (`20260922-02`)

Fresh user approval: "dale", following the proposal of one corrected smoke,
expected below USD 2 with USD 5 planning margin (not a guaranteed billing cap).
Same bounded cases, no automatic retries, stop on first failure, no prod promotion.
The original report/sentinel is preserved. New report:
`build/phase13-smoke/live-result-20260922-02.json`.

Preflight: exact factory wiring constructed offline with access-only tokens,
audience unset, scoped provider doubles and stubbed JWKS; zero provider calls.
Three preflight regression tests passed. Existing shared stacks were verified
UPDATE_COMPLETE and all three original template backups matched live templates.
The fictional tenant's S3 prefix was empty; the unrelated KB is excluded.

Temporary dependencies are being recreated through reviewed changesets. Artifact
prefix: `phase-13/smoke-20260922-02/`; exact version IDs for cleanup:

- review-task.zip: `eKf2HCWse64RV8Ena25rpRPjiodZCaUd`
- interceptor.zip: `BDawpXkfdnDQ7GbYme9rvUQH2XZ_GIHg`
- metadata-mcp.zip: `Pyy3wFKSl31XVjBJqKf1psPcviGaBGGL`

### Result and teardown

The single execution stopped during `login`, category `smoke_failed`, before
any observed callback HTTP response. Its original diagnostic lacked the failing
substep and exception category. A subsequent non-mutating reproduction established
the runner cause: Cognito's hosted page rendered two username/password/submit
forms, with the first matching controls hidden and the second visible. The old
compound `waitForSelector(..., state=visible)` selected the first hidden username
and timed out; the exact old selector reproduced `TimeoutError` with visibility
`[false, true]`. This is a browser-runner defect, not an authentication, model,
retrieval or grounding failure.
The factory successfully constructed; the previous audience error did not recur.
Do not attribute this result to the model, retrieval, grounding or the separate
browser index-check defect identified below.

Application counters: 4 fixture DynamoDB writes; zero S3 operations, ingestion,
Retrieve, Converse, Guardrail, Harness or Memory operations. Browser login did
not pass. Cleanup made 9 requests without errors. Subsequent reads verified
2 synthetic users and 4 rows absent, and an empty fictional source prefix.

Both temporary stacks reached DELETE_COMPLETE. All three shared stacks returned
to UPDATE_COMPLETE using original templates and Phase 11 artifact versions.
Original no-CORS configuration verified. Three exact new artifact versions
deleted after restoration, then HEAD checks confirmed absence. No provider
billing API used; actual spend remains unknown. No live retry, merge or prod
promotion. Independent holdout remains unexecuted.

Local review also found an unexercised browser-test defect: Playwright
`waitForFunction` returns a JSHandle, not its underlying string. Comparing it
directly with `indexed` would falsely reject successful indexing. Fix and
regression tests are local-only; they do not turn this attempt into a pass.
396 local tests passed before execution. Post-attempt checks passed: four
Python preflight tests, browser helper checks for indexed/error/failed-value
handles and primitive rejection, browser syntax and git diff checks. Login
failures now report an allowlisted exception type and a closed substep, without
exception messages, tokens, URLs or response bodies. No production application
behavior, IAM or prompts were changed by these post-attempt corrections.
Login selection now enumerates all matching controls, waits for any actually
visible element, and fills/clicks that specific locator. A selector-only check
against the public hosted page selected the visible username, password and submit
controls without submitting credentials. This correction has not had a new live
authenticated smoke, so the E2E result remains unproven.
Sanitized result:
`evals/results/phase13-live-smoke-20260922-02-report.json`.

## Attempt 3 — 2026-09-22 (`20260922-03`)

Fresh user authorization: one execution with the same request/token/time caps,
expected below USD 2 and USD 5 planning margin (not a billing guarantee), zero
automatic retries, stop on first failure and mandatory teardown. No prod promotion.
The first two reports and sentinels remain immutable; the new report path is
`build/phase13-smoke/live-result-20260922-03.json`.

Pre-provisioning checks passed: clean Git worktree at commit `76b21e8`, exact
factory built offline with access-only tokens and no audience, selector/helper
tests passed, three original template backups matched the deployed templates,
shared stacks were UPDATE_COMPLETE, synthetic prefix/users/matter seeds were
absent, and the only existing KB was the unrelated project resource.

### Result and teardown

The single execution passed real Cognito login, Matter A selection, presigned
upload, S3 confirmation, KB ingestion, factual RAG (`answerable` with one
citation), citation inspection and the absent-fact path
(`insufficient_evidence`, zero citations). It then stopped at `metadata_tool`:
the `/api/mcp` response was non-successful after one successful Harness
invocation. Review, cross-matter denial and final audit were therefore not run.

Metadata-only application counters: 1 ingestion start, 2 ingestion polls,
2 Retrieve, 3 Converse, 3 Guardrail, 1 Harness, 5 S3, 102 DynamoDB,
4 Memory writes and 3 Memory reads; 4,216 model input tokens and 192 output
tokens. No automatic retry was made.

The evidence isolates an architectural mismatch rather than an IAM/Gateway
failure. The application created its scoped Harness invocation binding, but no
Gateway grant or correlated interceptor/target Lambda telemetry appeared.
Harness completed without a structured tool result, so the backend failed
closed. AWS documents that Harness tools run only as a result of model reasoning
and `allowedTools` restricts selection rather than forcing dispatch; InvokeHarness
has no tool-choice field. An explicit UI command therefore cannot rely on the
model to call exactly one tool deterministically.

Relevant current AWS references:

- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-tools.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html
- https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeHarness.html

The recommended architecture decision is to route explicit metadata
and review commands from the trusted backend directly to Gateway/MCP with the
verified user's bearer token and existing server-bound matter/correlation data.
Keep Gateway interceptor/target reauthorization and least privilege. Reserve
Harness for genuinely agentic, model-selected workflows. Prompting Harness more
strongly would remain probabilistic and is not the recommended production fix.

ADR-016 implements this recommendation locally: explicit metadata/review
actions use one bounded direct MCP `tools/call`, the existing sealed binding,
Gateway interceptor and target reauthorization. This local change has not been
validated against a live Gateway and does not authorize another smoke.

Cleanup made 25 bounded requests without reported errors. Subsequent reads
verified 2 synthetic users and 7 owned rows absent. The two temporary stacks
reached DELETE_COMPLETE. All three shared stacks returned to UPDATE_COMPLETE
using their original templates and Phase 11 artifact versions. The original
no-CORS state, empty fictional source prefix and absence of all three new
artifact versions were verified. Billing APIs were not used; actual spend is
unknown. No merge or prod promotion occurred.

Sanitized durable result:
`evals/results/phase13-live-smoke-20260922-03-report.json`.

## Attempt 4 — 2026-09-22 (`20260922-04`)

The user authorized one further bounded test run after ADR-016, under the same
under-USD-2 estimate, USD-5 planning margin and residual-risk conditions. Local
preflight used commit `d4a28f1`; 403 tests, 24/24 deterministic evaluations and
the offline desktop/mobile browser journey were green. Shared stack templates
matched their saved originals, the synthetic prefix/users/matter seeds were
absent and the previous reports remained immutable.

Temporary KB/vector and Guardrail stacks were created from the reviewed Phase
03/06 templates. Phase 02 received only the no-replacement CORS update; Phase
07/08 received the current versioned Lambda artifact with one SDK attempt.
Gateway was `READY` and all three Lambdas were Active/Successful before the
runner was invoked exactly once.

The run stopped before browser startup. The launcher created its two synthetic
users and four conditional authorization records, then its Node child exited
before writing the required metadata-only JSON line. The Python launcher tried
to index the empty stdout list and recorded `IndexError`. Reproduction without
AWS established the underlying runner defect: Playwright is provided through
the workspace dependency bundle, but the live launcher neither required nor
forwarded `PLAYWRIGHT_MODULE`; the browser script imports Playwright at module
load, outside its safe error handler. This is not a Cognito, Gateway, IAM, RAG,
model or grounding result.

Application counters were four fixture DynamoDB writes and zero S3, ingestion,
Retrieve, Converse, Guardrail, Harness, direct Gateway or Memory operations.
There was no upload, model input/output or browser login. The attempt was not
retried.

Internal cleanup reported nine requests and no errors. Independent verification
found both synthetic users and all four exact rows absent, the source prefix
empty, both temporary stacks deleted, all three shared stacks restored to their
exact saved templates/Phase 11 artifact versions, original no-CORS state restored
and all three attempt artifact versions absent. No billing API was called and
actual billed cost is unknown. The live direct-Gateway path therefore remains
unproven; no prod promotion occurred.

Sanitized report:
`evals/results/phase13-live-smoke-20260922-04-report.json`.
