# Phase 15 golden set — cost of the real run

Status: **estimate to approve, not an approval.** Nothing in this document calls AWS or a model. It is the breakdown
`AWS_COST_POLICY.md` asks for before a paid run. The arithmetic is reproducible and tested:

```powershell
python scripts/phase15_golden_cost_model.py          # tables below
python scripts/phase15_golden_cost_model.py --json   # same, machine readable
```

It **reuses the T9 model** ([`phase-15-t9-cost-estimate.md`](phase-15-t9-cost-estimate.md), `scripts/phase15_t9_cost_model.py`): same price table
(Sonnet 4.6 through the `eu.` profile, USD 3.30 / 16.50 per million tokens, AgentCore, Knowledge Base, Guardrails, Lambda), same token
profile per kind of question, same upper-bound factors. A test pins that the prices are the ones the worker reports cost with.

## Answer first

| | USD |
|---|---|
| **Real run, typical case** (80 cases) | **≈ 6.6** |
| **Real run, upper bound** (inputs ×1.6, outputs ×2 up to each call's cap, bigger microVM, the Guardrail lets every "blocked" question through) | **≈ 12.0** |
| One-off preparation of the corpus in the matter (IDP for the 9 generated documents, Textract for their scanned pages, KB embeddings) | 0.2 typical · 0.8 upper |
| **Real run plus corpus preparation** | **≈ 6.8 typical · ≈ 12.8 upper** |
| Average per case | 0.082 typical · 0.150 upper |
| **Of which the real async subset** (8 cases through the deployed queue and worker, plus 2 async scenarios) | **0.58 typical · 1.03 upper** |

The owner's range for D-060 was roughly 5–15 USD: both ends of this estimate fall inside it. Bedrock tokens are about 90 % of the
total. Taxes and the EUR/USD rate are not included. **No prompt caching is assumed**: if Harness caches the agent prompt the real
figure is lower.

## Fixed call budget (a hard stop, not a forecast)

Enforced by the runner (`real_plan`, `DeployedGoldenClient._spend`) and checked in `tests/test_phase15_golden_eval.py`; the model and the
runner share the numbers through `tests/test_phase15_golden_cost_model.py`.

| Limit | Value |
|---|---|
| Input Guardrail calls | **80** input Guardrail calls (one per case) |
| Harness invocations | at most **89** (80 direct + the 8 async cases + 1 duplicate-delivery scenario); a case the input Guardrail blocks makes none |
| In-process pipeline probes (resolver + writer + KB + grounding Guardrail) | at most **80**; **58** cases have a RAG expectation, the rest only probe when the agent ended in the direct-RAG fallback |
| Async jobs through the deployed queue (each consumes quota) | **10** (8 cases + 2 scenarios) |
| Retries, anywhere | **0** (and `total_max_attempts=1` on every SDK client; a test pins it) |
| Abort | after 3 consecutive errors; the cases not run count as failed, so an aborted or interrupted run cannot pass |
| Loop-cost stop | stop if the sum of the report's `estimatedLoopCostUsd` passes **5.9** USD (upper bound of the Harness loops, 4.65, plus 25 %). It never trips in the typical case (2.6) |
| Per invocation | `maxIterations` 3, `timeoutSeconds` 90, `maxTokens` 2048 per iteration (`AGENT_CHAT_*`) |

## Quota (this is the constraint on the async part)

The deployed `QuotaHarnessPerMonth` is **50** and `QuotaChatsPerMonth` 300. Only the deployed chain consumes quota: the 8 async cases and the
2 async scenarios use **10 units** of the month; the 72 direct cases and the pipeline probes use none (they never create a job). The T9 browser smoke
uses about 10 more, so the whole block stays at about 20 of 50. Quota counters are monthly and are **not refundable**: a job that fails after
reserving gives its unit back, a job that succeeds does not. Doing the async part for all 80 cases would need 80 units, more than the month holds; that
is why it is a subset (decision for the owner below).

## Assumptions

| Class (T9) | Used for | Cases (typical) | USD per case, typical | upper |
|---|---|---|---|---|
| `A_no_tool` | out of scope, injection the agent ignores (plus the direct-RAG fallback as probe) | 6 | 0.026 | 0.044 |
| `B_text` | `search_documents` (legal text, unanswerable, ambiguous) | 36 | 0.099 | 0.175 |
| `C_inventory` | `list_matter_documents` | 6 | 0.029 | 0.052 |
| `D_data` | list + `get_extracted_fields` | 16 | 0.052 | 0.093 |
| `E_mixed` | data + text (and "lookup then search") | 10 | 0.124 | 0.220 |

Figures include the input Guardrail call, the Harness loop and its tools, and, when the case has a RAG expectation, the probe (one resolver call and,
unless the resolver finds no support, one writer call, one Knowledge Base retrieve and the grounding Guardrail units). Cases the Guardrail is expected to
block (6) stop after the input check in the typical case and run as ordinary questions in the upper bound (which is why the class counts differ).
The direct runs do not go through the worker, the queue or the job store; the async subset is costed with the full T9 job cost (history, worker Lambda,
job state). The probe is a second, independent sample of a stochastic pipeline (see the design document); it is the price of per-stage evidence without
touching production code.

## Breakdown (USD, one-off)

| Component | Typical | Upper bound |
|---|---|---|
| Input Guardrail (80 calls) | 0.09 | 0.18 |
| Harness loops and tools (direct runs) | 4.35 | 7.99 |
| In-process pipeline probes | 1.54 | 2.80 |
| Async subset: 8 case jobs | 0.55 | 0.97 |
| Async scenarios (expired session: no model call; duplicate delivery: one job) | 0.03 | 0.06 |
| CloudWatch logs and state | 0.01 | 0.04 |
| **Total** | **6.56** | **12.04** |
| Corpus preparation (separate, one-off) | 0.2 | 0.8 |

## What would change the numbers

| If… | Effect |
|---|---|
| The probe only runs for cases whose end-to-end answer failed (cheaper; no recall metric for passing cases) | about −1.2 typical, 0 in the upper bound |
| The dataset is cut to 60 cases (mostly the second case of each document kind) | 5.6 typical · 9.9 upper |
| The real async subset is dropped (only the 2 scenarios stay) | 6.0 typical · 11.1 upper, and 2 quota units instead of 10 |
| Harness caches the agent prompt | input cost of the Harness calls falls by up to 90 % on the cached part |
| The `eu.` premium is not billed | −9 % on Bedrock |
| The agent calls `search_documents` more than once per question | +≈ 0.03 each (resolver + writer) |
| A second run (a new approval and a new file name) | the same again; reruns of a stochastic agent are how consistency is measured, at full price |

## Documents the real run needs (preparation, cleanup)

The real run reads a **dedicated synthetic matter** that holds exactly the corpus (the inventory cases depend on the exact list). Nothing is uploaded by the
runner and nothing in this branch has been uploaded.

1. `python evals/phase15_golden_eval.py prepare-corpus --output-dir <new empty folder>` writes the PDFs of the 8 documents the IDP fixtures lack, a truncated
   file for the failed-upload document, and `corpus-manifest.json` with the SHA-256 of all 27 (the 18 fixture PDFs stay in `tests/fixtures/idp/pdfs`).
   It needs `reportlab` and `Pillow` (the dev dependencies of `build_idp_fixtures.py`); it makes no network call.
2. The operator uploads them (synthetic data only, ADR-014), waits for indexing and IDP, and writes the **document map** `{dataset id: real id}`
   (`--document-map`): the beta assigns its own ids, and the runner translates every id at the edge. A map that misses a document, names an unknown one or is not
   one-to-one stops the run before any spend.
3. The failed-upload document must really end in status `FAILED`; that is **not verified** (it is a hypothesis about how ingestion treats a truncated
   PDF). Four inventory cases depend on it; if it cannot be produced they are excluded explicitly, not silently.
4. The "other matter" document (`mat_glacier`) goes to a different tenant/matter the test user cannot open.

**Cleanup** (designed before the first write, as the guides ask): delete the synthetic matter with the matter-delete operation of
[`phase-14-data-governance-runbook.md`](phase-14-data-governance-runbook.md), which also removes the chat jobs of the matter; delete the conversation
bindings listed in the report's `residualState` (step 14 of [`phase-15-live-runbook.md`](phase-15-live-runbook.md), same as the routing eval); the session
records the async transport writes are deleted by the transport itself; the job records expire by TTL (1 h). The monthly quota units are not refundable.
AgentCore Memory events written by the deployed worker for the async subset are residual state to review before declaring the cleanup complete.

## Verifying the real cost afterwards

Use the Billing console the day after (month-to-date, filtered to Bedrock, AgentCore, CloudWatch, Lambda). Do not use the Cost Explorer API without the
owner's single-use confirmation (`AWS_COST_POLICY.md`). If the real Bedrock cost is outside the typical–upper range by more than 25 %, re-read the assumptions
before any new live run. Record the observed figure next to this estimate.

> **Correction (2026-11-04).** The 5.9 USD loop-cost stop reads `estimatedLoopCostUsd`, which before this date counted only loops with a single model call (so it could not trip). It now sums the model calls of each loop and prices a call that reported no usage at an upper bound; an unknown cost can trip the stop. See `docs/phase-15-t9-cost-estimate.md` (usage accounting correction and the CloudWatch cross-check).
