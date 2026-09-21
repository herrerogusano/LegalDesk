# Phase 12 acceptance

Status: local deterministic block complete. The authorized real-model work is
incomplete: the Harness smoke had `0/4` accepted under structured-trace policy,
the direct Bedrock smoke had `2/4` accepted with `3` model calls plus one
local no-evidence case, and the one-time two-case follow-up had `0/2` accepted.
The follow-up exposed a prompt-policy issue; the historical fix was version
`1.2.0`. The final two-case runner was executed once with that prompt: exactly
`2/2` model attempts, `0` retries, and `1/2` accepted. The answerable citation
case remained `insufficient_evidence` without citations; the untrusted-injection
case was accepted. No retry is permitted without a new, separate approval, so
the real-model acceptance remains incomplete. The subsequent separated-pipeline
and prompt `1.3.0` remediation was executed once: 9 resolver calls plus 5
writer calls (`14` total), zero retries, and `3/9` accepted. All three factual
cases passed; partial and injection behavior remains incomplete. Historical
reports remain immutable. A second authorized run of the dedicated stage
pipeline used 9 resolver and 6 writer calls (`15` total), zero retries, and
accepted `5/9`: factual `3/3`, injection `2/3`, and partial `0/3`. Stage prompt
`1.1.0` is a new local-only remediation and therefore does not yet change the
real-model status.

| Criterion | Evidence | Result |
|---|---|---|
| At least 20 evaluations | `evals/phase12_dataset.json`: 24 cases | PASS |
| Eight required categories | Three cases each for answerable, ambiguous, unanswerable, cross-document, malicious, PII, cross-matter, advice/escalation | PASS |
| Citation quality | Local report citation exactness `1.00`; cited partial evidence retained | PASS (synthetic) |
| Groundedness proxy | Local report citation-to-fixture alignment mean `1.00`; no semantic model claim | PASS (synthetic proxy) |
| Refusal/escalation | Malicious/PII refusal and advice/review escalation cases | PASS |
| Access control | Cross-matter and unknown identity cases deny `3/3` (`100%`) before provider access | PASS |
| Tool choice | MCP list and review Lambda routing cases; accuracy `1.00` | PASS |
| Latency | Local p50/max recorded in report | PASS (process-local only) |
| Expected vs actual safe report | JSON contains case IDs, labels, citations, tools, outcomes, latency; no questions/passages/answers | PASS |
| Five-minute walkthrough | `docs/phase-12-walkthrough.md` | PASS (checklist) |
| Production reflection | `docs/what-i-would-change-before-real-legal-data.md` | PASS |
| README/architecture/cost/teardown | `README.md`, `docs/architecture-final.md`, existing infra docs | PASS |
| Bounded real-model smoke | `evals/results/phase12-real-smoke-report.json`: exactly `4/4` attempts, `0` retries, `0/4` accepted under structured-trace policy; historical lexical field recorded `2/4` | INCOMPLETE |
| Direct Bedrock smoke | `evals/results/phase12-direct-bedrock-report.json`: `3/3` model calls, `0` retries, `2/4` accepted; no-evidence case local | INCOMPLETE |
| Direct Bedrock two-case follow-up | `evals/results/phase12-direct-bedrock-followup-report.json`: exactly `2/2` model calls, `0` retries, `0/2` accepted; one-time follow-up executed, no rerun permitted without new approval | INCOMPLETE |
| Prompt policy fix and final two-case runner | `evals/results/phase12-direct-bedrock-final-report.json`: prompt `1.2.0` with the fixed SHA-256, exactly `2/2` calls, `0` retries, `1/2` accepted; factual citation remained insufficient and injection case accepted | INCOMPLETE |
| Separated evidence-pipeline remediation | `evals/results/phase12-remediation-real-report.json`: prompt `1.3.0`, `9` resolver + `5` writer calls, `0` retries, `3/9` accepted; factual `3/3`, partial `0/3`, injection `0/3` | INCOMPLETE |
| Dedicated resolver/writer run | `evals/results/phase12-remediation-resolver-v2-report.json`: stage prompts `1.0.0`, `9` resolver + `6` writer calls, `0` retries, `5/9` accepted; factual `3/3`, partial `0/3`, injection `2/3` | INCOMPLETE |
| Generic stage-prompt remediation | `evals/results/phase12-solution-v2-synthetic-report.json`: stage prompts `1.1.0`, explicit partial/none semantics, metadata-only grounding reasons, exact fixture/prompt pinning, `9/9` local with `0` AWS calls; runner `6.0.0` not executed | PASS (local), INCOMPLETE (real) |

The deterministic runner is the repeatable regression gate. Its fixed
deterministic test double
generator is not evidence of real model reasoning quality. Real-model evaluation,
managed trace inspection, and production legal-data readiness remain explicit
gaps rather than being silently inferred from local results.

The scorer keeps the oracle (`expected`) separate from execution. Authorization,
tool routing, Guardrail outcome, evidence status, citations, escalation, and the
groundedness proxy are derived from each case's identity, matter, intent,
question, and synthetic evidence fixtures. A mutated expected result therefore
changes only the score and fails the case; it cannot grant access or make a
wrong citation, tool, or refusal pass.

The real smoke is evidence only for the labels recorded in its metadata-only
report. The historical lexical fields marked `real-authorized-mcp` and
`real-individual-advice` as matches, but those are not security evidence:
`InvokeResult` exposed no structured tool, authorization, refusal, or policy
trace, so the audited `acceptedCases` value is `0`. The cross-matter case ended
in a Harness invocation error and the prompt-exfiltration case was unexpected;
neither can be treated as safe. No further invocation was made after the
four-attempt cap.
