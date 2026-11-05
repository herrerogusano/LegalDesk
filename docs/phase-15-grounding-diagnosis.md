# Phase 15 T9: grounding, stream and plan diagnosis

2026-11-05. Candidate based on `ea3abb4`; historical routing run d used release
`846a227`, synthetic matter `mat_phase14_b_20260928`. This is a diagnosis and
offline fix, not completion of T9 or authorization for the golden set.

## Scope and evidence provenance

Read AGENTS.md, MASTER_PLAN, PLAN_15, AWS_COST_POLICY and the requested vault
testing/grounding/failure notes. Phase 14/IDP acceptance and T8 precede the active
T9 block. Latest relevant STATUS update read: 2026-11-04; older STATUS entries
describe other branches. No shared vault edits; the supervisor owns memory sync.
Human implementation/review estimate: 6–10 hours; actual human hours unmeasured.

Primary evidence:

- `C:\Users\herre\legaldesk-t9-release\evals\results\phase15-routing-20261104-d.json`.
- `C:\Users\herre\legaldesk-t9\20261104\routing-d.log`.
- Local synthetic corpus manifest, PDFs and upload journal in
  `C:\Users\herre\legaldesk-t9\corpus-b-20261104`.
- Read-only AWS: Lambda configuration; `GetGuardrail` version 1;
  `ListDataSources`/`GetDataSource`; exact-document DynamoDB `GetItem` projections;
  CloudWatch `FilterLogEvents` for the report's bounded time window, joined by
  correlation ID. No broad document scan, retrieval, model/Harness invocation,
  ApplyGuardrail, change set, deployment or AWS mutation.

No questions, answers, arguments, plans, passages, matched entities, masked
outputs or credentials were retained in this report or new telemetry. PDF text
was measured in memory; only counts and closed language hints were emitted.
Datasets, gates, thresholds and relationship rendering remain unchanged. PR #116
owns relationship changes; this patch touches only the grounding telemetry area
of `chat.py` and does not edit `evidence.py`.

## F-38: above-threshold blocks

Live log reads reproduced 16 terminal `grounding_validate` events: 13 blocked,
3 succeeded. Above-threshold blocked events were text-02 (1/1), mix-01 (1/1),
mix-04 (0.76/1), against grounding/relevance thresholds 0.75/0.5. Historical
events contain scores but no intervening policy; their cause is still unknown.

The production adapter demonstrably blocks any top-level
`GUARDRAIL_INTERVENED`. It also converts any non-NONE assessment action,
including `ANONYMIZED`, into fail-closed contextual sentinel actions internally.
The numeric diagnostic deliberately preserves actual provider scores. Thus high
scores and a policy block are compatible; a high grounding score is not a safety
approval. This behavior is reproduced offline through the real writer adapter,
grounding adapter and chat, with AWS-shaped ANONYMIZED and BLOCKED responses.
It is **not changed** here.

Live version 1 configuration:

- NAME, ADDRESS, EMAIL, PHONE: ANONYMIZE.
- US_SOCIAL_SECURITY_NUMBER, CREDIT_DEBIT_CARD_NUMBER, AWS_ACCESS_KEY,
  AWS_SECRET_KEY, PASSWORD: BLOCK.
- One denied topic, `IndividualizedLegalAdvice` (live equality check confirms
  the expected configured name). Grounding/relevance: BLOCK, 0.75/0.5.

Ranked candidates:

| Rank | Hypothesis | Supporting evidence | Against / missing evidence | What settles it |
|---|---|---|---|---|
| 1 | Party-name/address masking triggers the global block | Active PII configuration and adapter behavior reproduce this exact high-score outcome offline | Actual policy assessments were not logged; naming a party does not prove PII detection | New event shows sensitiveInformationPolicy NAME/ADDRESS ANONYMIZED with contextual scores passing |
| 2 | Denied-topic or another safety policy intervenes | Active denied topic; topic BLOCKED shape reproduces high-score blocking | No historical policy attribution, no stored answer | New closed policy/action labels identify topicPolicy DENY BLOCKED or another class |
| 3 | Contextual policy itself intervenes despite displayed scores | Global and per-filter actions are independently inspected | Three scores exceed the logged thresholds; no evidence of a threshold/configuration change | contextualGroundingPolicy GROUNDING/RELEVANCE intervention label alongside the actual scores |

New `grounding_validate.grounding_diagnostics` contains a closed top action,
deduplicated `policy:type:action` intervention labels, query/answer/source
character counts, source count and maximum source size, and en/es/mixed/unknown
language hints. Only fixed enums escape; unknown PII/filter types become OTHER.
Regexes become REGEX, custom words CUSTOM; names, matches, regex patterns and
outputs are never copied. Missing diagnostics are absent, not evidence that no
policy intervened. Tests reject extra keys, arbitrary labels and bad numeric
types at the telemetry boundary. Language labels are a conservative stopword
heuristic, not a detector, and never affect acceptance.

AWS documents that masking-only intervention returns modified content; a
blocking intervention returns a configured refusal; no intervention returns an
empty outputs array. See [ApplyGuardrail behavior and examples](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-use-independent-api.html).
That makes these owner decisions possible, none implemented:

1. Keep all interventions blocked. Safest continuity, but factual party names
   can make useful legal-document answers unavailable.
2. Permit strictly masking-only interventions and publish only the correctly
   mapped masked answer. Never return the original. This requires validating
   every policy, output shape and answer position, rejecting unknown/mixed
   interventions, and checking that the modified answer still satisfies its
   evidence/relationship contract. Simply ignoring the global action would
   publish content that the service meant to redact.
3. Review NAME/ADDRESS treatment for already authorized documentary facts.
   Party identity is often the fact being requested, so anonymizing it can
   remove the answer's meaning without adding matter isolation. It still
   protects against unintended personal disclosure; changing it needs an
   explicit privacy/security decision and dedicated adversarial coverage.

Passwords, AWS keys, card numbers and SSN must retain their BLOCK protection
under any masking-only alternative. Topic/content/contextual blocks and unknown
assessments must also remain fail closed. Guardrails do not replace authorization.

For earlier matter-A text-02, the owner's evidence is first search blocked,
second passed, in all three repetitions. The search tool accepts a model-written
`question`; direct fallback uses the original job question. They can differ,
as can retrieval selection and writer output. Old logs contain none of those
texts or closed comparisons, so **the reason for the first/second difference
cannot be reconstructed**. Repetition makes a systematic path/query difference
plausible; it does not prove the PII hypothesis. The new stream projection
records same/different/unavailable against the original user question for each
search call, without retaining arguments. Missing/incomplete/duplicate-key
argument JSON stays unavailable. It does not compare search 1 directly to
search 2, nor prove semantic equivalence.

## Low grounding: input contract and ranked hypotheses

Verified from production code:

- Retrieval returns up to five authorized raw Bedrock text chunks. The resolver
  sees all five. Both the writer and grounding receive only the resolver's
  selected citation passages. Grounding does not reload whole PDFs or whole
  pages. Page metadata identifies provenance, not the extent of a text chunk.
- One query block is the search tool's model-written question (or the original
  question on direct fallback); selected passages use `grounding_source`; the
  final answer is unqualified. The adapter bounds query/answer/combined sources
  to 1,000/5,000/100,000 characters and sends `source=OUTPUT`, `outputScope=FULL`.
- Grounding runs **after** current relationship rendering replaces writer prose.
  It therefore evaluates that rendered sentence if a relationship survives.
  Required/advisory relationship handling is untouched in this PR.
- Partial-evidence/ambiguity wording from the writer is evaluated. The canonical
  no-support abstention skips the writer and contextual grounding. The public
  disclaimer flag/UI wording is outside the answer passed to this validator.
- Live ingestion configuration confirms FIXED_SIZE, 800 tokens, 15% overlap,
  with no configured OCR/parser override. Successful INDEXED status is not proof
  every page is searchable.

AWS evaluates the tagged source set together, not per citation. Unsupported
statements lower grounding; relevance measures the query/answer relationship.
[Contextual grounding](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-contextual-grounding-check.html)
and [qualifier semantics](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_GuardrailTextBlock.html)
distinguish the source/query roles from the guarded answer. Under current AWS
documentation, these source/query-only blocks are excluded from other safety
policies; the unqualified answer is checked by them.

`FULL` asks for detected **and non-detected assessment entries**; it does not
choose different evidence or document a different scoring algorithm. Changing
it to INTERVENTIONS could omit the non-intervening filters our strict adapter
requires. Returned masked/refusal text is not fed back into this evaluation;
the adapter blocks first. No scope/scoring A/B was run. See the
[ApplyGuardrail API contract](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ApplyGuardrail.html).

Do not assume English-only support: current AWS documentation lists English,
French and Spanish for Classic contextual grounding. Standard supports more
languages and requires cross-Region configuration. Live configuration has no
explicit contextual tier, which the documentation defines as Classic. These
facts **do not establish quality for mixed-language source/answer pairs**.
[Supported languages](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-supported-languages.html)
and [tier behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails-tiers.html).

Local PDF extraction, without printing text:

| Relevant synthetic document | Type / fixture form | Pages | Extractable characters | Textless pages | Language hint |
|---|---|---:|---:|---:|---|
| contract-01 | contract, digital | 1 | 647 | 0 | en |
| contract-03 NDA | contract, mixed | 2 | 401 | 1 | en |
| contract-04 | contract, digital | 1 | 415 | 0 | es |
| contract-05 | contract, mixed fixture | 2 | 720 | 0 | en |
| ext-msa-01 | contract, long digital | 9 | 4198 | 0 | en |
| ext-lease-01 | contract, mixed long | 7 | 1070 | 3 | es |
| ext-contract-06 | contract, table | 1 | 334 | 0 | unknown |
| ext-contract-07 | contract, annex | 2 | 626 | 0 | es |

These are measurements of local corpus copies, not reconstructed retrieved
chunks or OCR results. Document type is the fixture designation, not accepted
IDP classification. Exact live metadata reads confirm 19 INDEXED/7 FAILED;
six fully scanned PDFs and the intentionally broken upload are FAILED. The NDA,
MSA and lease are INDEXED/IDP_REVIEW_REQUIRED. IDP review status does not certify
retrieval page coverage; `judgment-03` remains INDEXED/IDP_FAILED (existing F-33).
No unexpected new lifecycle state was found.

| Rank | Hypothesis | Evidence for | Evidence against / limitation | Evidence needed |
|---|---|---|---|---|
| 1 | Evaluated phrasing introduces claims absent from sources, including current relationship sentence and partial-evidence language | Grounding runs after rendering; low grounding with high relevance is consistent with an on-topic unsupported claim; F-19 has offline reproductions | Historical answers not retained; 15/16 writer successes dropped advisory relationship data, so emitted relationships do not imply 15 rendered answers | Correlate new sizes/languages/policy labels after #116; any further content-preserving comparison requires separately authorized synthetic testing |
| 2 | Selected evidence is incomplete: resolver selection, missing scanned pages or chunk boundaries | NDA has one textless page; long lease three; text-04 lacks an exact dependency in the corpus; 800-token boundaries can split context | Grounding sees selected support, not all retrieved text; short digital contract-01 also had a high-score block | Closed source count/size vs retrieval count; supervisor-controlled synthetic known-source probe to distinguish source omission from wording |
| 3 | Rewritten tool question changes retrieval and relevance | Search question is generated by Harness; direct fallback restores the user's question; mix-01 relevance varied from 0.12 to 1 | Most failures have relevance 1, so query mismatch alone is not a strong explanation for low grounding | New per-call same/different/unavailable comparison plus query size/language and scores |
| 4 | Translation/mixed languages depress score | Spanish questions and English/Spanish corpus coexist | Both languages are documented as supported; no historical answer/source language pairs | New language hints identify correlation; a controlled cross-language pair would be needed for causality, beyond the proposed run |
| 5 | outputScope/response parsing creates low scores | Adapter requires exact filter shape | Numeric scores are copied from provider, not recomputed; FULL is documented as reporting scope | No evidence supports a behavior change; provider A/B only with separate approval if contradictory evidence appears |

No hypothesis is established by an offline stub returning a chosen low score.
New telemetry can isolate paths/correlations; it cannot prove semantic support
without a separately governed synthetic comparison. Historical source selection,
chunk boundaries, answer language and rewritten query remain unknown.

## F-36: stream bound and partial loops

The two historical diagnostic records match data-02 and mix-02 by correlation:
`ValueError`, `events_received=256`. The old combined invalid/oversized message
was classified by the substring `invalid`; that was our limit, not bad arguments.

Every SDK mapping counted: message starts/stops, content block boundaries,
incremental text, tool argument/result fragments and usage metadata. A large
inventory may stream many fragments in one tool call. There is no one-event-per-
token guarantee. Four model iterations at 2048 output tokens and up to 16 tool
results do not justify a 256-frame cap. The existing 64-message, block-index,
16-result and 256 KiB per-agent-result checks remain in place.

Set `_MAX_STREAM_EVENTS = _MAX_STREAM_BYTES // 2 = 524288`, preserving the
1,048,576 serialized-UTF-8-byte cap. Every accepted mapping serializes to at least
two bytes (`{}`), so the event bound is derived from the byte budget: ordinary
provider frames reach the byte cap first; even empty mappings remain bounded.
This is a serialized payload bound, not a 1 MiB Python heap bound: container
overhead still exists. It is not permission for larger tool payloads or loops.

Both stream consumers (`HarnessInvoker.invoke`, `invoke_agent_chat`) enforce the
same constants. Local event/byte exhaustion raises typed `StreamLimitExceeded`;
agent results report `stream_limit_exceeded`, selected by exception type rather
than message keywords. Other errors retain their existing classifications.

On a cut after at least one accepted event, the agent returns a failed loop,
complete valid tool results and whatever numeric usage arrived. The output tree
skips the final plan, salvages a valid completed search result if available, or
uses direct RAG fallback. Incomplete tool results cannot become accepted facts.
No automatic restart of that partially received loop is added. Before any event,
the exception propagates through the existing worker failure handling. The
simple invoker continues to raise its wrapped invocation error.

Tests cross `invoke_agent_chat`: more than 256 real-shaped deltas complete;
real-shaped streams at the actual byte cap complete and one frame over fail
with the closed limit code while preserving a completed tool result. Counter
boundary tests inject a small event cap to isolate that check; at the production
event count real-shaped frames necessarily exceed the byte cap already. The
legacy simple-invoker boundary test now injects its cap rather than freezing 256
as production behavior. This is adapter/component evidence, not live verification.

## F-37 and F-39: plan and writer limits

All four historical unreadable plans (text-05, data-03, mix-04, inj-05) have
`stopReason=end_turn`, not max_tokens. The first three failed JSON decoding;
inj-05 failed schema shape validation. No plan length/envelope was retained.

New worker telemetry and routing reports carry `planChars`,
`planHasJsonStart`, `planHasJsonEnd`, `planParseStage` and a closed stop reason.
Stages are not_attempted/json_decode/size/schema/accepted, derived from the
**actual parser's error**, without a permissive second parser. Bracket booleans
only mean characters were found; they do not prove balanced/valid JSON. Unknown
stop reasons become `other` in diagnostics, without changing loop classification.
The existing parser still accepts exactly one enclosing Markdown fence and
rejects prose, duplicate keys, unknown shapes and oversized plans.

Ranked plan hypotheses:

1. Malformed JSON or surrounding prose: consistent with three invalid_json
   outcomes at clean end_turn; paired delimiter flags/lengths will distinguish
   missing tails from some envelopes, but cannot identify prose exactly.
2. Contract-shape error: established category for inj-05; details require a
   separately designed synthetic reproduction, not guessing from its case ID.
3. Output truncation at MaxTokens: plausible with a larger inventory, but all
   affected stop reasons argue against provider-reported truncation in run d.
   A future max_tokens + missing end + large plan would support it. No prompt,
   MaxTokens or parser relaxation is justified by the existing plan evidence.

Writer `max_tokens` is **512**, resolver **256**; Harness final-plan cap is
**2048 per iteration**. The writer's actual production builder uses its 512
default; it is separate from Harness. Live logs show 19 writer starts, 16
successes and three `model_output_truncated` errors. Of 16 successes, 13 record
`writer_relationship_unsupported`, one action-preposition and one action-length
drop, with one no-drop event. That establishes **15 advisory relationship drops**,
not 15 authoritative relationship renderings. Emitting rejected structured data
can still consume output budget before the backend discards it.

Truncation justifies reviewing the writer budget after #116, not raising it
blindly here. More tokens cost money/latency and may merely generate more
unnecessary relationship data. Measure post-#116 truncation before deciding
whether 768/1024 is warranted; no value change in this PR.

## Proposed targeted run — supervisor only, not executed

Written question: **Across two repetitions, do high-score blocks identify a
specific safety policy, do low-score cases show different input sizes/languages
or rewritten queries, and do larger streams/plans finish under the preserved
byte/iteration/token limits?**

- Frozen case IDs: text-02, text-03, text-05, data-02; repeat 2: **8 Harness
  invocations**, sequential, no automatic retries. Separate diagnostic-subset
  report with no gate verdict; no dataset/threshold changes and no golden set.
- Preserve correlation IDs and closed Lambda diagnostics for each search in
  order, including failures. Compare the first and fallback search paths within
  a case; retain no text. This samples F-38 high-score, low-score, F-37 and F-36
  paths; it does not measure a cross-language causal effect or settle inj-05.
- Run d's same four cases cost $0.184887 in Harness-loop estimates; doubled
  baseline $0.369774 excludes resolver/writer and Guardrail costs, and data-02
  was cut. Planning estimate **$0.60–$1.20 total**, with **$2 requested allowance**
  if the owner authorizes it. This is not a measured bill or a guaranteed upper
  bound. Larger tool results/extra searches can increase it. Up to 32 Harness
  model turns, plus two tool-internal model calls per completed search; 8
  invocations does not mean 8 billable model calls. Use existing stop rules and
  current remaining T9 dollar/invocation allowance; do not enlarge either here.
- Prerequisites: supervisor releases and deploys **both the search Lambda and
  chat worker**, reconciles #116 separately, confirms exact release/configuration,
  owner approves the run/cost, and obtains the authorized synthetic session.
  Stop on unexpected security/configuration state; do not repeat on a guess.

## Validation and handoff

Final validation on Windows, Python 3.13.13, all exit codes zero; no skipped
tests in the selected suite:

```powershell
$env:PYTHONPATH='backend/src;agent/src;tests'
python -B -m unittest test_phase15_grounding_diagnosis test_grounding_scores test_guardrails test_phase13_grounding test_model_output_reasons test_chat test_evidence test_retrieval test_harness_stream_diagnostics test_harness_trace_evidence test_phase13_harness_stream test_phase15_agent_chat_client test_phase15_agent_chat_worker test_phase15_agent_chat_telemetry test_phase15_agent_contracts test_phase15_agent_output test_phase15_routing_eval test_phase15_golden_eval test_phase15_targeted_runs test_phase15_usage_accounting test_phase15_search_documents test_phase15_agent_chat_packaging test_phase15_runbook_contract -q
python -B evals/phase15_routing_eval.py offline
python -B evals/phase15_golden_eval.py offline
git diff --check
```

Results: **499 tests ran, OK**, including **9 new diagnostic tests**. Routing:
**30/30**, tool/path accuracy 1.0, zero allowlist violations/invalid blocks
accepted. Golden: **80 cases + 29 async scenarios**, passed verdict. These offline
evals verify machinery, not model quality. Final logs:
`%TEMP%\legaldesk-diagnosis-suites-final.log`,
`%TEMP%\legaldesk-diagnosis-routing-final.log`,
`%TEMP%\legaldesk-diagnosis-golden-final.log`.
Whitespace check passed. No configured Python lint/type-check command was found;
the selected tests import and execute changed paths. Full repository Windows
suite and Linux/Python 3.12 CI were not claimed as locally verified. An initial
selected-suite run found the old test expecting 257 frames to fail; its isolated
counter test was updated, then all 499 passed. No known failure was suppressed.

No IaC changed, so no synth/change set was needed. AWS resources modified: **none**. Read-only
logs/API reads may use ordinary service allowances; no paid Guardrail/model call
was made. Existing CD may run offline CI on publication; this PR does not deploy.
Delivery uses a draft PR: the repository automatically reviews ordinary PRs,
and the pinned PullBeacon revision explicitly skips drafts before model use.
No review request/comment or ready-for-review transition is authorized here.

Checklist before calling anything proven:

- Seen live: historical failure counts/scores/stop reasons; current policy,
  ingestion configuration and exact inventory status. New behavior: **not seen
  live**, candidate only.
- Real code crossed: writer/grounding/chat, agent stream adapter, strict plan
  parser, worker metadata, routing report. Only provider/network is doubled.
  IAM/provider score quality/masked-output mapping in a changed acceptance path
  remain unverified; no such changed acceptance path is implemented.
- Corpus variety measured early: digital, mixed, scanned, long, table and broken
  synthetic files. Text extraction is not a visual/layout or OCR-fidelity audit.
- Unexpected states: existing IDP_FAILED judgment acknowledged; no new unexplained
  state ignored. Errors remain closed and visible. Estimates are estimates.

F-36 is fixed offline, unverified live. F-37/F-38 have new diagnostics, causes
partly or wholly open. F-39 is a confirmed writer-limit symptom pending #116
measurement. Supervisor owns security decisions, live verification and vault
updates; do not mark these failures resolved, trigger reviews, merge, deploy or
start the golden set from this delivery.
