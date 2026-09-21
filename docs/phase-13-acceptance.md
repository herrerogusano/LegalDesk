# Phase 13 — integration acceptance ledger

Status: **in progress; NOT_READY_FOR_PROD**. No Phase 13 AWS deployment,
inference, ingestion or live smoke has been executed. No prod promotion.

## Evidence classes

| Evidence | What it establishes | What it does not establish |
|---|---|---|
| Unit tests | Parsing, state transitions, authorization and fail-closed invariants | Provider behavior |
| Component tests | Adapter contracts and interactions under controlled doubles | An executable browser/application journey |
| Local HTTP integration | The application entry point composes identity and business components | Cognito/AWS interoperability or semantic quality of a real model |
| Deterministic evaluations | Regression against synthetic contracts and expected outcomes | General semantic entailment or legal quality |
| Historical real-model evaluations | The specific bounded calls recorded in immutable Phase 12 reports | One final integrated E2E run, statistical reliability or the independent holdout |
| Future AWS smoke | Only the approved cases actually executed through the integrated application | Production certification or broad legal accuracy |

## Baseline and reproducibility

- Developer baseline: `eedc520abf4044f453f1b251e3fe66cbe0230639`.
- Before changes: 315 tests and 24/24 deterministic evaluations passed.
- Initial identity transport changes: 324 tests passed locally.
- Git's Windows checkout converted the system prompt to CRLF, invalidating
  its exact-byte hash without a Git content change. `.gitattributes` now pins
  `prompts/*.md` to LF. Restoring LF restored the approved 1.3.0 artifact hash;
  prompt semantics, version and historical report expectations were unchanged.
- The initial transport tests are component evidence, not the final signed-JWT
  HTTP integration gate. Subsequent review findings require regression tests.

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

- [ ] One executable application and connected minimal browser UI.
- [ ] Signed-JWT login and scope preservation through Harness/Gateway/tools.
- [ ] A/A allow, A/B deny, B/B allow; forged matter/document/session denied.
- [ ] Actual user attributed as review creator; expired token denied.
- [ ] Upload confirmation checks storage, then bounded indexing reaches INDEXED.
- [ ] Processing is distinguishable from missing documentary evidence.
- [ ] Factual, partial, absent, conflicting and injection-adjacent evidence cases.
- [ ] Concrete productive grounding, independent frozen holdout and honest limits.
- [ ] Technical failures never become documentary not_found.
- [ ] Authorized citation passage inspection without internal locations.
- [ ] Scoped short-term history; long-term Memory remains disabled.
- [ ] Correlated stage/tool outcomes and effective prompt metadata, no content logs.
- [ ] Complete regression, deterministic evaluation and UI verification.
- [ ] Reconciled real-smoke request limits, resources, budget and teardown.

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
