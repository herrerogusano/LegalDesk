# Phase 15 observability of the agentic chat job (T7)

Everything here is metadata. `AGENTS.md` §7: no questions, answers, passages,
prompts, tokens or document text in telemetry. Whatever the ADOT probe says, the
worker's own line is the supported operational surface
([ADR-021 D9](../ARCHITECTURE_DECISIONS.md)).

## One line per message

The worker Lambda prints one JSON line per processed message
(`legaldesk.agent_chat_lambda._log`). Its content is decided in one place,
`legaldesk.agent_chat_telemetry`: an allowlist of keys **and** a closed shape for
each value (identifier-like strings, numbers, booleans; tool names that are not
LegalDesk's are reported as `other`; nulls are dropped).

| Group | Attributes |
|---|---|
| Identity | `jobId`, `correlationId`, `agentPromptVersion`, `agentPromptSha256` |
| State | `action` (`completed`, `failed`, `retry`, `ignored`, `error`), `claim`, `failure` (closed code), `attempt`, `quotaReleased` |
| Loop | `loopOutcome`, `stopReason`, `iterations`, `tools`, `toolCalls`, `toolFailures`, `invalidToolResults` |
| Output | `outputPath` (`grounded`, `data`, `inventory`, `mixed`, `tool_result`, `rag_fallback`), `fallbackReason`, `planError`, `omittedBlocks`, `blockCount`, `citationCount`, `memoryWritten` |
| History | `historyMessages`, `historyTrimmed`, `historyUnavailable` |
| Time | `latencyMs`, `queueWaitMs`, `segmentsMs` (`session`, `bind`, `history`, `agent`, `resolve`, `fallback`, `publish`) |
| Usage | `usageAvailable`, `usageComplete`, `modelCalls`, `costBasis`, `tokens`, `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `estimatedLoopCostUsd`, `pricingVersion` |

`estimatedLoopCostUsd` covers **only the Harness loop** and uses the price table
of `agent_chat_telemetry.PRICE_PER_MILLION_TOKENS` (`pricingVersion` says which).
The Bedrock calls made inside `search_documents` (resolver, writer) and by the
direct RAG fallback are not in the Harness usage, so the figure is a lower
bound for the whole question. The table is an estimate: since `estimate-2026-11-eu-regional` it is the Sonnet 4.6 list price plus the 10 % premium of the `eu.`
regional profile (sources and the gaps in them: [`phase-15-t9-cost-estimate.md`](phase-15-t9-cost-estimate.md)); confirm it against the bill
before quoting cost in a report.

## Metrics and alarms (only with `EnableAgentChat=true`)

Six alarms, **in the console only** (`AgentChatAlarmTopicArn` stays empty by decision of 2026-11-03: no SNS, nothing is notified). They read **native, free CloudWatch
metrics** wherever one exists; a metric is custom only when CloudWatch publishes nothing equivalent.

| Alarm | Metric | Kind | Threshold |
|---|---|---|---|
| `AgentChatDeadLetterQueueAlarm` | SQS `ApproximateNumberOfMessagesVisible` of the DLQ | native | ≥ 1 |
| `AgentChatQueueAgeAlarm` | SQS `ApproximateAgeOfOldestMessage` of the work queue | native | > 120 s for 2 consecutive minutes |
| `AgentChatJobDurationAlarm` | Lambda `Duration` of the worker, p95 | native | > 90 s in 2 consecutive 15-min periods |
| `AgentChatWorkerErrorsAlarm` | Lambda `Errors` of the worker (uncaught exception or timeout) | native | ≥ 3 in 15 min |
| `AgentChatJobFailureAlarm` | `AgentChatJobFailures` | **custom** | ≥ 3 in 15 min |
| `AgentChatFallbackRateAlarm` | `AgentChatFallbacks` over Lambda `Invocations` of the worker | **custom** + native | fallbacks ≥ 50 % of invocations in 1 h, with at least 5 invocations |

Why each custom metric stays (namespace `LegalDesk/Phase15AgentChat`, one metric filter each on the worker log group, **no dimensions**):

- `AgentChatFallbacks`: an answer that did not come from the agent's plan (a reused tool result or the direct RAG path). To Lambda that is a *successful* invocation, so no native
  metric can see it.
- `AgentChatJobFailures`: a job the handler marked failed (deadline exceeded, attempts exhausted, agent failed, internal error), **excluding `session_closed`**, which is the user
  logging out with a question in flight and not a fault. The handler returns normally and the message is consumed, so neither Lambda `Errors` nor the DLQ ever sees it.
  An unclassified `error` is redelivered once and then lands in the DLQ, which has its own alarm.

What was removed, and where the information is now: the job duration and the job count are the native Lambda `Duration` and `Invocations` of the worker (BatchSize 1: one invocation
per job message, so the count also includes redeliveries and the fallback rate can only read lower, never raise a false alarm); the split of failures by code and of fallbacks by
reason were metrics with a dimension (one billable metric per value) and are now the Logs Insights queries below, over the same fields of the job line. Fixed cost: 7 alarm metrics
and 2 custom metrics, 1.30 USD a month ([`phase-15-t9-cost-estimate.md`](phase-15-t9-cost-estimate.md)).

The API's p95 ≤ 3 s alarm does not apply: a job is asynchronous and takes 15–60 s.
Thresholds are fixed in `phase-14-public-edge.yaml`; tune them with data after T9.

The template change needs the operator to deploy the Public Edge template
(T9.1), as with any change to it.

## Logs Insights

```
fields @timestamp, jobId, action, outputPath, fallbackReason, failure, iterations, latencyMs, queueWaitMs, estimatedLoopCostUsd
| filter event = "agent_chat_job"
| sort @timestamp desc
```

```
filter event = "agent_chat_job" and action = "completed"
| stats count() as jobs, avg(latencyMs) as avgMs, pct(latencyMs, 95) as p95Ms, sum(estimatedLoopCostUsd) as loopUsd by outputPath
```

Failures by code (what `AgentChatJobFailuresByCode` used to show):

```
filter event = "agent_chat_job" and action = "failed"
| stats count() as jobs by failure
| sort jobs desc
```

Fallbacks by reason (what `AgentChatFallbacksByReason` used to show):

```
filter event = "agent_chat_job" and action = "completed" and ispresent(fallbackReason)
| stats count() as jobs by fallbackReason
| sort jobs desc
```

## Grounding diagnostics (F-05, offline candidate 2026-11-04)

The terminal `grounding_validate` stage event includes optional numeric
`grounding_score`, `relevance_score`, `grounding_threshold` and
`relevance_threshold` fields. All four are finite numbers in [0, 1]. They come
from the real `ApplyGuardrail` response's
`assessments[].contextualGroundingPolicy.filters[]` entries (`GROUNDING` and
`RELEVANCE`, `score` / `threshold`), including policy-blocked responses. Missing
or malformed measurements are omitted, never represented by invented zeros.
The threshold fields are the provider's configured thresholds; the unchanged
adapter also enforces floors of 0.75 grounding (constructor default) and 0.5
relevance. No question, answer, document, tool argument or token text is added.

Diagnostics travel with the request-local result or blocked exception; the
strict three-key grounding result mapping remains unchanged. Low scores with
provider action `NONE` retain the existing `error` / `model_failed` stage
outcome; provider intervention retains `blocked`. Accepted outcomes retain
`succeeded`. Phase 11 metric filters and their input fields are unchanged; no
new event or metric is emitted.

Code trace for the relevance hypothesis: `search_documents_lambda` receives the
agent's tool `question` argument, builds `ChatRequest` from it and passes that
through `answer_question` to `GroundingRequest.question`; the adapter tags it
`query`. Thus the search tool measures relevance against the model-written
search question, not independently against the original user's question.
The direct RAG path uses its own chat question. This is code-level evidence,
not a diagnosis of text-02's intermittent live block.

Validation: `$env:PYTHONPATH = 'tests;backend/src;agent/src'`, then
`python -m unittest test_grounding_scores test_phase13_grounding test_guardrails test_chat test_evidence test_model_output_reasons test_phase_11_observability test_phase15_search_documents test_phase15_agent_chat_telemetry`:
**Ran 134 tests, OK**. `python evals/phase15_routing_eval.py offline`:
30/30, no failed gates. `python evals/phase15_golden_eval.py offline`:
80 cases (77 graded, three expected known-limit failures) plus 29/29 async
scenarios, no failed gates. `git diff --check`: PASS. These tests exercise the
real Converse writer, grounding adapter and chat with provider clients faked;
they do not verify real Bedrock values, CloudWatch delivery, IAM, deployment,
or explain text-02. No AWS operations or model/Harness calls were made.
After release, search Lambda code still needs a reviewed **Gateway change set**;
Public Edge code-only CD does not update it. No templates, prompts, frozen
sets, gates or thresholds changed.
