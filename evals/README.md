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
