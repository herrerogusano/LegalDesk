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
| Usage | `usageAvailable`, `tokens`, `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `estimatedLoopCostUsd`, `pricingVersion` |

`estimatedLoopCostUsd` covers **only the Harness loop** and uses the price table
of `agent_chat_telemetry.PRICE_PER_MILLION_TOKENS` (`pricingVersion` says which).
The Bedrock calls made inside `search_documents` (resolver, writer) and by the
direct RAG fallback are not in the Harness usage, so the figure is a lower
bound for the whole question. The table is an estimate to check against the
Bedrock price list before quoting cost in a report.

## Metrics and alarms (`LegalDesk/Phase15AgentChat`, only with `EnableAgentChat=true`)

| Metric (filter on the job line) | Alarm |
|---|---|
| `AgentChatJobDurationMs` (completed jobs) | p95 > 90 s in 2 consecutive 15-min periods |
| `AgentChatJobFailures`, `AgentChatJobFailuresByCode` (dimension `FailureCode`) | ≥ 3 failures in 15 min |
| `AgentChatFallbacks`, `AgentChatFallbacksByReason` (dimension `FallbackReason`), `AgentChatJobsCompleted` | fallbacks ≥ 50 % of completed jobs in 1 h, with at least 5 jobs |
| SQS `ApproximateAgeOfOldestMessage` (work queue) | > 120 s for 2 consecutive minutes |
| SQS `ApproximateNumberOfMessagesVisible` (DLQ, existing) | ≥ 1 |

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
