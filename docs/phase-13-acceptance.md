# Phase 13 — integration acceptance ledger

Status: **integration gate and bounded AWS smoke complete; NOT_READY_FOR_PROD**.
The final authorized browser smoke passed the complete fixed journey, including
direct Gateway→MCP metadata and Gateway→Lambda review dispatch, cross-matter
denial, actor/correlation preservation, audit and logout. Temporary resources
were removed and shared stacks restored after every attempt. No prod promotion.

A third separately authorized smoke passed Cognito, upload, ingestion and both
RAG cases, including a factual citation. It stopped at the first explicit MCP
action because Harness returned without a structured tool call. AWS's Harness
contract makes tool execution model-selected; `allowedTools` is an allowlist,
not deterministic dispatch. ADR-016 now routes explicit UI commands through a
direct backend→Gateway/MCP call; Harness remains reserved for agentic flows.

## Evidence classes

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| Unit tests | Parsing, state transitions, authorization and fail-closed invariants | Provider behavior |
| Component tests | Adapter contracts and interactions under controlled doubles | An executable browser/application journey |
| Local HTTP integration | The application entry point composes identity and business components | Cognito/AWS interoperability or semantic quality of a real model |
| Deterministic evaluations | Regression against synthetic contracts and expected outcomes | General semantic entailment or legal quality |
| Historical real-model evaluations | The specific bounded calls recorded in immutable Phase 12 reports | One final integrated E2E run, statistical reliability or the independent holdout |
| Bounded AWS smoke | The approved cases executed through the integrated application and real AWS boundaries | Production certification, broad legal accuracy or the independent holdout |

## Baseline and reproducibility

- Developer baseline: `eedc520abf4044f453f1b251e3fe66cbe0230639`.
- Reviewed local runtime: Python 3.13.13, boto3/botocore 1.43.97 and PyJWT
  2.14.0. SDK schema checks are offline; they require the AWS optional dependency.
- Before changes: 315 tests and 24/24 deterministic evaluations passed.
- Initial identity transport changes: 324 tests passed locally.
- Git's Windows checkout converted the system prompt to CRLF, invalidating
  its exact-byte hash without a Git content change. `.gitattributes` now pins
  `prompts/*.md` to LF. Restoring LF restored the approved 1.3.0 artifact hash;
  prompt semantics, version and historical report expectations were unchanged.
- The initial transport tests are component evidence, not the final signed-JWT
  HTTP integration gate. Subsequent review findings require regression tests.
- Reviewed identity/grounding/Harness stream checkpoint: 351 tests passed,
  deterministic evaluation 24/24 with zero AWS calls. Stream fixtures and the
  invocation request are validated against the installed botocore schema.
  This is an intermediate result, not the final HTTP/UI acceptance count.

Final local verification on 2026-09-22:

- Complete Python suite after local release hardening: **413 tests passed**,
  versus the 315-test baseline.
- Concrete application journey: one integrated happy-flow test plus 20 HTTP
  scenario/security regressions; two additional HTTP boundary tests and three
  factory tests. Other Phase 13 tests cover identity transport, provider stream
  schemas, productive-grounding contracts, frozen holdout and scoped CORS.
- Deterministic evaluations: **24/24**, **zero AWS calls**.
- Python compile checks and Node syntax checks for both frontend scripts pass.
- Offline Edge/Chromium browser acceptance passes login, selection, presigned
  PUT/indexing, factual answer, citation passage, MCP, review, history, audit,
  operational UI error, no-evidence answer and logout. Desktop 1365px and mobile
  390px screenshots inspected; no horizontal overflow or JavaScript errors.
- No configured linter/type-check suite exists. Local IaC regression tests pass;
  no CloudFormation service validation, synth deployment or live diff was run.
- The browser and Python fixtures use synthetic data only. Their scripted
  provider responses are not real-model evaluation results.

## Productive grounding boundary

The selected strategy is Bedrock Guardrails contextual grounding for a bounded,
self-contained documentary question, selected source passages and candidate
answer. It is not a general conversation-quality evaluator. AWS documents
aggregate evaluation of source blocks, so a passing score does not attest
each citation individually. Exact citation IDs and access checks remain
deterministic backend responsibilities.

Provider limits are 1,000 query characters, 100,000 combined source characters
and 5,000 response characters. Integration must reject unsupported sizes
locally, not truncate material evidence silently. Scoped conversation history
must not become documentary evidence. Broad multi-turn conversational grounding
is outside this provider's documented supported use cases.

Sources: [contextual grounding behavior and limits](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html),
[supported languages](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-supported-languages.html).
Local doubles verify parsing and rejection behavior, not the provider's
semantic accuracy; the independent holdout remains unexecuted against AWS.

The holdout contains 14 cases, including a role-reversal contradiction and an
invented citation. Review corrected an unsupported time anchor in the draft
paraphrase before any prompt modification or provider evaluation. The frozen
artifact SHA-256 is
`3f66bf0d43c5667a19f8465300571933ffd7d358cb83c37285126c31bd5fe5cf`;
`tests/test_phase13_holdout.py` checks its exact bytes. This checksum test is
not a semantic evaluation result.

## Local release gate

### HTTP acceptance scenario map

`test_phase13_integration.py` uses the concrete application factory and a real
loopback HTTP server with RS256 JWT verification. Only provider/SDK boundaries
are doubled. The presigned upload performs a separate HTTP PUT; no test changes
document state manually between upload, confirmation, ingestion and chat.
`test_phase13_journey_security.py` extends the same composition:

| Required scenario | Local evidence |
|---|---|
| Factual grounded response | upload → HEAD confirmation → real sync workflow → Retrieve → Resolver → Writer → Guardrail adapter → response |
| Partial evidence | scripted provider coverage partial; backend retains valid citation and accepted history |
| No evidence | empty authorized retrieval, canonical response, no Writer call |
| Conflict | provider conflict signal, backend ambiguous status with citation |
| Injection adjacent to fact | untrusted passage stays data; retrieval remains scoped and no tool is invoked |
| Cross-matter | A/A and B/B allowed; A/B, guessed document and foreign session/citation denied |
| Processing | UPLOADED document blocks chat before paid provider calls; status is not documentary absence |
| Model failure | safe operational error, no rejected text or not_found audit |
| MCP | actual interceptor/grant/MCP handler through an HTTP-shaped local Gateway transport |
| Review | actual interceptor/grant/Review Lambda handler, creator is signed-in Alice |
| Expired token | signed token expired beyond configured clock leeway; login and existing session reject |
| Citation inspection | exact uploaded passage, opaque handle, expiry and membership reauthorization |

Additional regressions reject invented citation IDs, grounding intervention,
malformed/error Gateway results and forged operation correlations.
Every question has a distinct correlation; its tool/audit events retain the
server-owned question correlation and all three prompt versions/hashes.
This validates integration contracts, **not** model semantic accuracy or live
Gateway JWT/IAM behavior. Scripted test-provider answers never enter product code.

- [x] One executable application and connected minimal browser UI.
- [x] Signed-JWT login and scope preservation through the direct Gateway/tools
  path locally; Harness remains separately scoped for agentic workflows.
- [x] A/A allow, A/B deny, B/B allow; forged matter/document/session denied.
- [x] Actual user attributed as review creator; expired token denied.
- [x] Upload confirmation checks storage, then bounded indexing reaches INDEXED.
- [x] Processing is distinguishable from missing documentary evidence.
- [x] Factual, partial, absent, conflicting and injection-adjacent provider contracts.
- [x] Concrete productive grounding, independent frozen holdout and honest limits.
- [x] Technical failures never become documentary not_found.
- [x] Authorized citation passage inspection without internal locations.
- [x] Scoped short-term history; long-term Memory remains disabled.
- [x] Correlated stage/tool outcomes and effective prompt metadata, no content logs.
- [x] Complete regression, deterministic evaluation and UI verification.
- [x] Proposed real-smoke request limits, historical resources, budget and teardown.

## Remaining gates and production gaps

The actionable deployment gate is maintained in `production-readiness.md`.
The hardening pass added fail-closed HTTPS endpoint validation, browser response
headers and provider-response bounds without making AWS calls or creating
resources. These controls reduce local risk but do not replace the hosting,
durable-state, operational and semantic gates below.

The final bounded smoke verified the reviewed change sets, restricted
application session, Cognito, ingestion, RAG/Guardrail and direct-Gateway
interoperability for its exact synthetic cases. This is not production
certification. The 14-case independent semantic holdout is unexecuted; local
tests must not be reported as proof that a real model accepts all paraphrases or
rejects all contradictions. The proposed smoke is smaller than that holdout.

The loopback server has process-local sessions/audit/citation handles and no
production TLS hosting or distributed controls. Membership is reread per
request (audit once per distinct matter); JWT verification is not an IdP
revocation lookup. Whole-data-source ingestion requires a bounded synthetic
source. Abandoned uploads and expired grant records need manual reconciliation;
authorization expiry does not delete DynamoDB rows. Long-term Memory stays off.

The original local acceptance run initiated no AWS calls. The subsequently
authorized attempt deployed prerequisites, then stopped before login because
the default verifier allowed ID tokens without configuring their audience.
The application factory now explicitly accepts access tokens only, matching
its PKCE exchange. 394 local tests pass after the fix; the factory regression
uses the real no-ID-audience configuration. A separately authorized second
attempt passed factory construction but stopped during browser login without
an observed callback response. A later read-only reproduction established that
the hosted page contained hidden and visible duplicate forms: the old selector
waited on the first hidden username and timed out. The runner now enumerates
matches and selects the actually visible control, covered locally and checked
against the public hosted page without submitting credentials.
396 local tests passed before this attempt. Both attempt reports are preserved.
An additional, unexercised JSHandle comparison defect in the browser's indexing
check was found locally; it does not explain the earlier login failure.
See `phase-13-live-smoke.md` for execution and verified cleanup. No billed spend
has been queried; zero inference does not imply zero infrastructure/request cost.
The smoke plan proposes an
incremental estimate under USD 2 and a conservative USD 5 approval envelope,
not a guaranteed account billing cap. Verify inventory, observability of request
ceilings and teardown targets before any separately authorized execution.

Attempt 3 consumed one bounded real execution: 2 Retrieve, 3 Converse,
3 Guardrail and 1 Harness call; 4,216 input and 192 output tokens. It did not
retry. Cleanup and shared-stack restoration were verified. See the immutable
sanitized attempt report and `phase-13-live-smoke.md` for exact evidence.

At that point the post-attempt ADR-016 implementation was local-only and had
not yet been validated against a live Gateway. A later smoke required fresh
authorization; that documentation change itself did not authorize AWS usage.

Attempt 4 used that fresh authorization but stopped before its browser process
started: the launcher did not forward the workspace Playwright module path, so
the Node process produced no JSON report and the Python wrapper recorded an
`IndexError`. It made four synthetic fixture writes and zero upload, ingestion,
inference, Harness or direct-Gateway calls. The attempt was not retried. The
runner now performs a browser/module launch preflight before any AWS write and
parses empty/malformed child output as a closed diagnostic. Cleanup and exact
shared-stack restoration were independently verified. The live direct-Gateway
gate therefore remained pending at that point.

Attempts 5–7 resolved the remaining gate. Attempt 5 passed login, upload,
ingestion and RAG but showed that the request interceptor rewrote the qualified
`target___tool` name before AgentCore could route it. The interceptor now
preserves the qualified name; Gateway performs target routing and presents the
local name to the downstream MCP target. Attempt 6 demonstrated metadata via
MCP, review via Lambda, cross-matter denial and audit, then exposed a browser
runner race that tried to reread the logout JSON body while the UI navigated.
The runner now validates logout status without consuming that body. Attempt 7
passed every stage. It recorded 2 Retrieve, 3 Converse, 3 Guardrail, 2 direct
Gateway calls, zero Harness calls, 2,848 input and 74 output tokens, with zero
automatic retries. Cleanup removed the synthetic fixtures and temporary stacks,
restored all shared templates/artifact versions and original no-CORS state, and
removed all three attempt artifacts. Billing APIs were not queried.

## Developer to prod checklist — do not execute

1. Review the final Phase 13 diff and record the exact tested commit.
2. Require every local release gate above and resolve all critical findings.
3. Obtain explicit authorization for the bounded AWS resource changes/smoke.
4. Verify regional availability, current inventory, scoped deployment/application
   IAM, disabled content logging, public Cognito callback and zero SDK retries.
5. Execute only the approved smoke; preserve metadata-only results and actual
   counters. Stop on failure or budget overrun; no automatic retry/tuning loop.
6. Reconcile real results with the independent holdout's still-unproven semantic
   expectations; do not substitute canned outcomes or the lexical oracle.
7. Record remaining production gaps and approved teardown status.
8. Obtain separate release authorization, then open/review developer→prod PR.
   Neither local tests nor the smoke plan authorizes that merge.
