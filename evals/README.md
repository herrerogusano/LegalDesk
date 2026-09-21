# Phase 12 local evaluation

`phase12_dataset.json` contains 24 synthetic cases across the eight required
categories. `run_evals.py` invokes `runner.py`, which uses local fakes around
the authorization, retrieval/chat, Guardrails, review-task, and tool-routing
boundaries.

```powershell
python evals/run_evals.py --output evals/results/phase12-local-report.json
```

The runner makes zero AWS calls and zero model-inference calls. The report
contains only case IDs, categories, bounded expected/actual outcomes, citation
IDs, tool names, and local latency. `expected` is an oracle read only by the
scorer; `actual` is derived from the case input and repository seams, so
mutating the oracle cannot alter execution. The groundedness number is only a
bounded citation-to-fixture alignment signal, not semantic model groundedness.
The artifact is safe to review as a regression report, but it is not a claim
of production legal quality.

The separately authorized real smoke is run with:

```powershell
python -m evals.real_smoke --harness-arn <existing-harness-arn> --region eu-west-1
```

It is hard-capped at four calls with no retries. The checked-in report stores
only bounded labels and latency; it does not store prompts, responses, tokens,
or secrets. A failed or unexpected case leaves the real-model acceptance
incomplete.

## Direct Bedrock smoke (executed once; acceptance incomplete)

`direct_bedrock_smoke.py` is a separate four-case alternative: three cases send
the versioned system prompt and synthetic passages directly to
`eu.anthropic.claude-sonnet-4-6` through `bedrock-runtime` in `eu-west-1`. It
reuses the backend JSON/citation validator, fixes `maxTokens` to `256` and
temperature to `0`, and has no retry path. The fourth no-evidence case uses the
backend's deterministic canonical response without a model call, so the model
cap is exactly three calls (`maxModelInvocations=3`). It was executed once with
exactly three model calls and zero retries; report:
`evals/results/phase12-direct-bedrock-report.json`. The run accepted `2/4`
cases. Any rerun requires new approval for model cost, logs, retention, and
data scope. The report contains only prompt metadata, evidence status, citation
IDs, validation/error codes, safety booleans, and latency.
The injection gate uses a documented synthetic canary plus fail-closed literal
prompt-overlap checks; it does not prove resistance to every semantic
paraphrase.

The exact two-case follow-up in `direct_bedrock_followup.py` was executed once
with runner `1.0.0`, exactly two model calls, and zero
retries. It accepted `0/2` cases; the metadata-only report is
`evals/results/phase12-direct-bedrock-followup-report.json`. The previous
direct-smoke report remains preserved as historical `1.0.0` evidence, and any
rerun requires new explicit approval. The follow-up helper rejects this
historical path even when supplied as an absolute path; its CLI default is a
distinct rerun filename, so the one-time report cannot be overwritten.

That result exposed a prompt-policy issue. The historical
`direct_bedrock_final.py` runner remains frozen to prompt `1.2.0`, SHA-256
`d87c5f6469de95979800097858b27f0eb66d96e8618f8808cffc4c2430bdb2e2`, is
capped at exactly two calls with zero retries, and uses the distinct report path
`evals/results/phase12-direct-bedrock-final-report.json`. It was executed once
with exactly `2/2` attempts and `0` retries; the metadata-only report records
`acceptedCases=1/2`. Phase 12 remains incomplete and no retry is permitted
without new explicit authorization.

The current local remediation prompt is version `1.3.0`, SHA-256
`de28c6e7d7b3a9284cfac505e4f4d099e8854da9ce8ecebb7911c0adefe8af56`.
It clarifies that authorized document content remains evidence while document
instructions remain untrusted, and adds the separated writer contract.

## Remediation runner

`EVAL_DEBUG_SYNTHETIC=true` enables `synthetic_debug.py`, a fixture-only report
with synthetic questions, passages, resolver/writer raw and normalized
outputs, final results, citation IDs, grounding scores, prompt metadata, and
validation/error codes. It makes zero AWS and inference calls and stores no
CoT, secrets, or tokens. It cannot read or write
outside the local `evals/` fixture area and does not alter historical reports.
`phase12_remediation_runner.py` preflights and can execute the approved real
subset: three factual, three partial, and three injection samples. The
separated pipeline permits at most 9 resolver + 9 writer calls, one total HTTP
attempt per stage and zero retries. It was executed once with prompt `1.3.0`:
9 resolver calls and 5 writer calls (`14` total), `0` retries, and `3/9`
accepted. All factual cases passed. One partial resolver returned no support,
two partial writers failed the deterministic grounding oracle, and all three
injection resolvers returned no support instead of the expected cited fact.
The metadata-only report is
`evals/results/phase12-remediation-real-report.json`. It is immutable; no rerun
is permitted without new explicit authorization.

The resolver schema is exposed locally as a Bedrock Converse
`outputConfig.textFormat` JSON Schema fragment. Server-side citation validation
remains mandatory. Native provider citations are not enabled because this
design uses its own `supportingCitationIds` contract.

After the `3/9` real result, runner `5.1.0` isolates the resolver and writer
with dedicated prompt contracts instead of sending both stages the general
conversation prompt. It also replaces exact-sentence grounding with a
fixture-owned typed-claim oracle that verifies claims against cited passage
text and rejects invented typed or lexical claims, spec/evidence drift, missing
uncertainty, invalid citations, and declared injection echoes even when
stopwords are omitted. It is not a general semantic injection detector. Preflight pins the
exact fixture IDs plus both stage-prompt hashes; the general `1.3.0` prompt is
recorded only as product-artifact provenance and is not sent to either stage.
The original local report
`evals/results/phase12-solution-synthetic-report.json` passes `9/9` with zero
AWS calls. Runner `5.1.0` was then executed once. Its immutable report
`evals/results/phase12-remediation-resolver-v2-report.json` records `5/9`
accepted after 9 resolver and 6 writer calls (`15` total), zero retries.

The next remediation versions both stage prompts as `1.1.0`, clarifies
the generic `partial`/`none` boundary, preserves supported facts beside
embedded directives, and adds closed metadata-only grounding reason codes
without changing the strict grounding-result schema. The local report
`evals/results/phase12-solution-v2-synthetic-report.json` passes `9/9` with zero
AWS calls. Runner `6.0.0` was executed once and its immutable
`evals/results/phase12-remediation-resolver-v3-report.json` records `6/9` after
18 calls with zero retries. Resolver output matched all nine expected
resolutions; two partial writer answers and one injection-adjacent factual
answer failed the bounded oracle.

Writer `1.2.0` preserves explicit requested values and units and constrains
partial explanations. The oracle now accepts bounded omission and count-order
paraphrases but retains rejection of missing facts, invented claims, invalid
citations, and directive echoes. The new local report
`evals/results/phase12-solution-v3-synthetic-report.json` passes `9/9` with zero
AWS calls. `evals.phase12_writer_followup` pins the resolver-v3 report by hash
and can revalidate all nine writer outputs with at most 9 calls, zero retries,
and the new metadata-only path
`evals/results/phase12-remediation-writer-v1-report.json`. It requires new
explicit authorization.
