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

That result exposed a prompt-policy issue. The server prompt is now version
`1.2.0` with hash
`d87c5f6469de95979800097858b27f0eb66d96e8618f8808cffc4c2430bdb2e2` and
preserves the exact JSON/refusal contract while classifying explicitly
supported facts as `answerable`. The prepared `direct_bedrock_final.py` runner
requires that prompt, is capped at exactly two calls with zero retries, and
uses the distinct report path
`evals/results/phase12-direct-bedrock-final-report.json`. It has not been
executed; no real-model acceptance is inferred.
