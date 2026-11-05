# Phase 15 T9: exploratory obligation-direction measurement

Prepared 2026-11-05; **no real evaluation executed**. No AWS writes, uploads,
model/Harness invocations, deployment, merge, or vault edits. Read-only AWS
metadata/document-status calls were made with the owner's requested scope.

The question fixed **before any model execution** is:

> On verified indexed digital pages, how often does an answer invent or reverse
> a party duty, correctly abstain from an unstated direction, or reject an
> answerable fact because of an unrelated obligation? Which observed stage first
> fails? The present corpus cannot establish general direction accuracy; new
> owner-approved positive sources are needed.

## Dataset and the important coverage limit

`evals/phase15_direction_dataset.json`: **40 cases, 20 English and 20 Spanish
questions**, comprising 20 source-fact answers and 20 page-scoped abstentions.
There are 20 underlying questions, each translated into both languages. These
are paired observations, not 40 independent documents or directions. All 40
can technically run against currently indexed, text-verified pages. **None is
human approved yet. All 40 need human direction/absence review.**

The separate normalized-LF dataset hash is
`5a2793573e0b75e3f1b14981a33496ac59ee96c9d3a355b8824e5e66859ee3e2`,
also stored in `evals/phase15_direction_dataset.sha256`. Its extraction snapshot
has its own hash pinned in the dataset. The frozen golden dataset, its SHA,
thresholds and gates are unchanged. No direction quality gate exists.

The corpus cannot honestly support the requested fully directed positive
coverage. In particular, the notice says goods must be returned to Puerto
Claro's warehouse, but **does not expressly name who returns them**. Neither
`De`/`Para` nor legal role assumptions supply that missing actor. The judgment
orders return of the price to Naranja Serena without expressly naming the actor
in that sentence. The MSA uses passive payable/notified clauses. Self-costs and
reciprocal confidentiality are useful controls but do not provide named opposite
directions. Questions deliberately say **use only this page**: unavailable
scanned pages cannot prove document-wide absence. For a yes/no question whose
premise is unstated, the expected result is “not expressly stated,” not a
categorical legal “no.”

| Requested risk | Current source-backed coverage | Missing coverage / owner |
|---|---|---|
| Pay, notify, deliver, return: who owes what to whom | Page-scoped missing-actor/payer/recipient questions; return destination; supplier services | Full explicit directed positive obligations: owner-approved new source |
| True and reversed yes/no premises | True own-cost burden; reversed and plausible but unstated rent/payment questions | True/reversed pair for a complete directed duty |
| Known/unknown detector wording | Unknown wording in questions; independent scorer challenges for all four named evasions | Unknown and known wording in actual directed source duties |
| Negation and unless / salvo que moving duties | Independent grammar/scorer challenges, permission-vs-duty control | Actual source-backed negated and conditionally moved duties |
| Similar roles, two opposed obligations | Reciprocal confidentiality; scorer-only opposed similar-role challenges | Two explicit opposed duties in one indexed document |
| Unrelated obligation must not cause rejection | `dir-03-en/es`: notice date on same page as return obligation | Covered by real-run measurement; no live result yet |
| Past delivered must not become a duty (F-19) | Scorer-only mutation challenge | No such past fact in verified indexed text |
| Invented obligation, correct abstention | Twenty page-scoped abstention cases, including nonexistent equipment delivery | Human must validate absence within declared page scope |
| Injection shaped like an obligation | Indexed adversarial content says it “must not alter authorization” | Malicious instruction containing a complete actor/action/recipient legal-duty triple |

These gaps are not padded with invented corpus sentences. Scorer-only synthetic
challenges are **not** dataset cases, uploaded documents, or model results.

## Independent verification of the set

`evals/phase15_direction_sources.json` records pypdf extraction from the actual
PDF files, their byte SHA-256 from the upload journal, blank/scanned page numbers,
and separate live DynamoDB/Bedrock statuses. It contains synthetic corpus text;
it is source evidence, not an application log or an evaluation result report.

`verify --corpus-dir ... --journal ...` checks all 26 actual files against their
upload hashes, re-extracts every digital page and compares it to the pinned
extraction. The deliberately truncated failed-upload PDF is checked by hash and
expected parse failure. Each case's **exact sentence, document and page** must
exist. Required facts must occur in that sentence; forbidden facts must not.
Incorrect annotations become `dataset_wrong`, even if a canned answer otherwise
looks correct. The mechanical direction grammar independently derives claims,
then checks the declared actor/action/recipient in source order. No current case
has a complete named triple this grammar can establish: every case is listed for
human review instead of being declared mechanically direction-verified.

The upload journal establishes provisioning and file identity; it does **not**
claim indexing success. `scripts/phase15_direction_status.py` independently
queries DynamoDB document records and paginated Bedrock document status. It
checks account/region and uses one SDK attempt. The recorded read-only receipt
finds **26 documents: 19 INDEXED in both services; 7 FAILED in metadata and ABSENT
from Bedrock**, with no snapshot drift. No KB Retrieve or inference was used.
The seven unavailable documents are:

- `doc-contract-02-es-scanned-leapday`
- `doc-demand-02-es-scanned`
- `doc-demand-05-en-scanned`
- `doc-judgment-02-es-scanned`
- `doc-judgment-05-en-scanned-optional-absent`
- `doc-ext-notice-02-en-scanned-noisy`
- `doc-failed-upload`

Blank scanned pages of otherwise indexed mixed PDFs remain unverified and are
never used as gold passages. Status is a dated observation; refresh it before
any future spend. CI uses the pinned extraction snapshot and says so explicitly;
it cannot independently inspect extension PDFs stored only on the owner's PC.

## Scoring, attribution and human checks

The oracle is independent of the production detector. Its bounded EN/ES grammar
extracts actor, action family, object, recipient, negation and condition. Reversal
changes an ordered relation, even if every word remains the same (D-024).
`duty to remit`, `must dispatch`, `Incumbe ... notificar` and `debe cursar un aviso`
are recognized in scorer challenges. Dropped conditions, changed objects and
negation are wrong. Unknown paraphrases become `human_review`, never automatic
success. Exact anchored source sentences are accepted; that checks source
fidelity, not independent legal interpretation. No LLM judge is used.

Mutation tests cover correct/reversed/invented answers, wrong-page citations,
equal word bags, all four evasions, changed objects/conditions/negation, two
opposed duties, past `delivered` changed to `must deliver`, incorrect annotations,
indexing drift and always-pass/always-correct/missing-parser scorers. A correct
source answer and the four supported correct paraphrases are not punished.

Causal attribution is `dataset → retrieval → resolver → writer → detector →
grounding → citations → answer`. The first observed failure owns the failure;
later failures remain visible. Unobserved stages remain unobserved. In
particular, the current golden recorder does **not** expose a dedicated detector
verdict: the report says `detectorObserved: false` unless separately instrumented
evidence is supplied. It does not infer detector success from no rejection.
Writer/probe errors and final-output direction are separate evidence; the probe
is a second sample, not a trace of the remote search Lambda.

The owner review sheet is [phase-15-direction-review.md](phase-15-direction-review.md):
one line per case with question, exact source sentence/page, expected direction
or missing direction, verification method and runnability. A reproducible random
sample of **10 cases is marked SAMPLE — unchecked** (seed 1519). **A human must
check at least those ten before any model-quality interpretation.** All implicit
direction/absence cases also remain human-review candidates; the sample is a
minimum, not a claim that the remaining annotations are correct. All reports
currently record `humanSampleCompleted: false`.

Proposed thresholds only: zero reversed duties, invented duties and wrong-page
answers; at most 10% over-abstention; at least 90% accuracy after human adjudication.
These are **not gates** and the owner has set no thresholds. Unknown/unreviewed
answers must be counted and published separately, never dropped to inflate
accuracy. The 20 bilingual pairs are a diagnostic sample, not statistical proof.

## Dry-runs and the #116 dependency

Offline oracle dry-run: **40/40**, with annotation-driven correct source text and
abstentions. Corrected real-adapter fake-cloud dry-run: **40/40**, no errors. It
crosses `DeployedGoldenClient`, the production worker/binding/interceptor/output
tree, real Converse resolver/writer adapters, real search/chat and real grounding
adapter. Only cloud responses and the managed Harness loop are faked. Socket
connections are forbidden by the test. An unauthorized foreign-matter result is
supplied and filtered out. This proves **machinery, not model behavior**.

The first fake-cloud receipt (`...fake-cloud-20261105.json`) is retained as failed
preparation evidence: the reused KB double returned empty text from scanned
distractor pages, and the fake Converse response misread the request shape. Its
40 failures are **invalid for model evaluation**. The corrected `...20261105-b.json`
is the accepted dry-run. The regression now requires retrieval and resolver to
pass on every case, so that permissive adapter test cannot silently return.

The intended model comparison depends on **PR #116 being deployed in every
participating search/worker code path**. This branch does not merge or copy #116.
Its fake-cloud evidence ran the current `developer` base, not #116 code. Without
#116 deployment, a live run would measure the old evidence-only projection and
renderer, without the Spanish detector and answer-aware exact-source rendering;
it would be a separately labelled baseline, not validation of #116's residual
evasion risk. Deployment SHA/artifact and prompt hashes must be pinned for each
future real sample. There is deliberately no `--execute` CLI here. The reusable
`run_with_client(..., mode='real_authorized')` seam can score an approved client;
it does not provision it or grant spend authorization. A live launcher still
needs owner approval, release/document-map validation, budget/abort handling and
residual cleanup from the golden machinery before use.

## Smallest additional source proposal — do not upload

**One two-page digital bilingual synthetic PDF** is the smallest document count:
an English page and a Spanish page with independently stated clauses. It can
cover all missing dimensions without OCR. Use similarly named fictional parties
“Supplier One”/“Supplier Two” and “Proveedor Uno”/“Proveedor Dos,” with:

1. Explicit pay/notify/deliver/return duties with actor, object and recipient.
2. One recognized modal/action clause and each of the four evasive wordings.
3. A negated duty and a complete unless / salvo que exception; a separate clause
   transferring who must notify after a named condition.
4. Two duties in opposite directions, an unrelated date/law fact on the same page,
   and a clearly past “delivered” / “entregó” fact.
5. A clearly delimited malicious document instruction containing a full directed
   duty triple, plus a question whose requested obligation is absent.

This is a proposal, **not a generated document or committed source truth**.
It should receive new case IDs and its own extraction/annotation review before
freezing the next direction-set version; no changes to the golden set.

November already used **26 uploads against the normal 20/month quota**. There is
no remaining November capacity. Uploading this one document would require an
explicit quota exception of at least 27 total (and restoration afterward), or
waiting for a fresh month, when it would use **1/20**. Existing quota changes are
not authorization for this upload. Preparation is local/free. Using the existing
historical IDP average of USD 0.0163/document plus a small allowance for digital
ingestion, embeddings, scanning and requests, propose **USD 0.03 typical /
USD 0.10 upper planning** for this one document. This is an estimate, not a bill
or a verified current price; model evaluations of added cases are additional.
No resources or documents were added.

## Cost and invocation estimate before a real run

Historical empirical anchor supplied by the owner: **run d cost USD 1.72 Bedrock
for 30 cases and 111 model calls**, or USD 0.05733/case and USD 0.01550/model call.
Scaling its Harness/tool portion to 40 cases gives about **USD 2.29 and 148 model
calls**. Forty additional in-process probes add **60 typical calls** (20
answerable resolver/writer pairs and 20 no-evidence resolver-only calls), using
the historical cost functions in `phase15_golden_cost_model.py`. With a USD 0.10
service allowance, typical total planning cost is **USD 3.37, 208 model calls,
40 Harness invocations, 40 probes, zero async jobs and zero uploads**.

A conservative upper planning scenario assumes four Harness turns, sixteen
tool results all requiring a two-call search, and a two-call probe per case:
**1,520 model calls**. At run d's observed mean cost per call plus USD 0.50 service
allowance this is **USD 24.05**. This is deliberately wider than the common
single-search-per-turn scenario (480 calls, about USD 7.94). The sixteen-result
client parsing limit is **not an enforcement cap on managed provider work**;
neither figure is a billing ceiling. Token lengths, repeated tools and provider
internal work can invalidate these scenarios. A future authorized launcher must
set an approved stop policy and reconcile actual CloudWatch/Billing usage; this
preparation spends nothing on inference. It does not reuse any earlier T9 spend
approval. If this envelope is too broad, the owner can approve a small first
shard. Adding ten cases is approximately USD 0.84 typical under these assumptions.

## Reproduction and acceptance evidence

PowerShell environment: `$env:PYTHONUTF8='1'` and
`$env:PYTHONPATH='tests;backend/src;agent/src'`.

```powershell
python evals/phase15_direction_eval.py verify --corpus-dir C:\Users\herre\legaldesk-t9\corpus-b-20261104\corpus --journal C:\Users\herre\legaldesk-t9\corpus-b-20261104\upload-journal.jsonl
python scripts/phase15_direction_status.py --journal C:\Users\herre\legaldesk-t9\corpus-b-20261104\upload-journal.jsonl --output <new-status-report.json>
python evals/phase15_direction_eval.py offline
python evals/phase15_direction_eval.py preflight
python -m unittest test_phase15_direction_eval
git diff --check
```

Actual-file verification: **26 PDF identities checked, 40 valid source anchors,
40 technically runnable cases, zero document errors**. Live status receipt: **19
indexed, 7 failed/absent, zero drift**. Direction tests: **22 tests, OK**. The
related regression command below ran **318 tests, OK**, including all 22 direction
tests. No production code, infrastructure, prompts, golden gates or frozen
golden data changed.

```powershell
python -m unittest test_phase15_direction_eval test_phase15_golden_eval test_phase15_golden_async test_phase15_golden_cost_model test_phase15_search_documents test_evidence test_chat test_model_output_reasons test_grounding_scores test_retrieval test_authorization test_phase15_agent_interceptor
```

Receipts are in `evals/results/phase15-direction-*20261105*.json`. Runtime
evaluation reports contain closed codes/counts/verdicts and answer hashes, not
questions, passages or answers. The source snapshot and owner review sheet are
explicit synthetic-content review artifacts. There is no lint/type-check tool
configured for this Python change and no IaC modification requiring synth/diff.
Shared vault updates and the failure/decision register remain the supervisor's
responsibility; this worker did not edit them. Stop here: no next plan executed.
