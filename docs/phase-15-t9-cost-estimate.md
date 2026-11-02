# Phase 15 · T9 live block — cost estimate

Status: **estimate to approve, not an approval.** Nothing in this document calls AWS. It
is the breakdown that `PLAN_15_AGENTIC_CHAT.md` promised "before asking for approval"
(`AWS_COST_POLICY.md`). The arithmetic is reproducible and tested:

```powershell
python scripts/phase15_t9_cost_model.py          # tables below
python scripts/phase15_t9_cost_model.py --json   # same, machine readable
```

## Answer first

| | USD |
|---|---|
| **Block, typical case** (46 Harness invocations, ~460 k input / ~32 k output tokens) | **≈ 2.3** |
| **Block, upper bound** (inputs ×1.6, outputs ×2 up to each call's token cap, bigger microVM) | **≈ 4.1** |
| Ceiling proposed for approval | **5.00** |
| Fixed monthly cost *while `EnableAgentChat=true`* (alarms + custom metrics, upper bound) | ≈ 3.0 typical · ≤ 4.8 upper |

PLAN_15 estimated "1–3 USD". The typical case lands inside that range; the upper bound
does not, so the ceiling asked for is 5 USD. Taxes and the EUR/USD rate are not included.
Bedrock tokens are 85–90 % of the block; everything else together is about 0.2 USD typical and 0.6 USD at the upper bound.

**Fixed call budget** (a hard stop, not a forecast), no automatic retries anywhere:

| Limit | Value |
|---|---|
| Harness invocations in the whole block | **50** (46 planned + 4 reserve for one repeat of a failed check) |
| Retries of an invocation | **0** (the worker's single retry only exists for transient SQS jobs; the eval and checks have none) |
| Eval real | exactly 30 (one per case), abort after 3 consecutive errors (T8) |
| Per invocation | `maxIterations` 3, `timeoutSeconds` 90, `maxTokens` 2048 per iteration (`AGENT_CHAT_*` in `agent/.../client.py`) |
| Concurrency | `AgentChatMaxConcurrency` 2 for jobs; the eval and checks run one at a time |
| Eval stop rule | stop if the sum of the report's `estimatedLoopCostUsd` passes **2.20 USD** (upper bound of the Harness loop for the eval, 1.75, plus 25 %) |
| Quota | `QuotaHarnessPerMonth` 300, `QuotaChatsPerMonth` 300: a runaway loop cannot go past it through the worker |

## Assumptions

**Questions.** 30 of the eval, ~10 of the browser smoke (one of them forced into the direct-RAG
fallback), 1 ADOT probe and the 5 calls of `scripts/phase15_harness_checks.py` (1 allowed-tools pre-check, 2 session-check, 2 loop-cut). The eval dataset is
`evals/phase15_routing_dataset.json` (frozen); the model classes its 30 cases fall in:

| Class | Cases in eval / smoke | Model calls (Harness iteration **H**, resolver **R**, writer **W**) | Tokens in / out, typical | USD / question, typical | upper |
|---|---|---|---|---|---|
| A no tool (out of scope, refusals) | 5 / 1 | H | 2.7 k / 0.15 k | 0.011 | 0.019 |
| B legal text (`search_documents`) | 9 / 4 | H, R, W, H (the 4 LLM calls of ADR-021) | 12.8 k / 0.92 k | 0.057 | 0.097 |
| C inventory (`list_matter_documents`; also cross-matter denials) | 5 / 2 | H, H | 6.1 k / 0.42 k | 0.027 | 0.046 |
| D data (`list` + `get_extracted_fields`) | 7 / 2 | H, H, H | 10.8 k / 0.84 k | 0.050 | 0.085 |
| E mixed (data + text; reaches `maxIterations` 3) | 4 / 1 | H, R, W, H, H | 17.8 k / 1.44 k | 0.083 | 0.141 |

How the token figures were built (all editable in the script):

- Agent prompt `prompts/legaldesk-agent.md` is 5.9 kB ≈ 1.6 k tokens; three tool definitions
  from the Gateway ≈ 1 k; question and history ≈ 0.1 k → **≈ 2.7 k input for the first Harness
  iteration**. Each later iteration adds the previous output and the tool result
  (`search_documents` ≈ 0.8 k, `list` ≈ 0.6 k, `get_extracted_fields` ≈ 1.2 k).
- Resolver and writer: the system prompt `legaldesk-system.md` is 6.1 kB ≈ 1.7 k tokens plus five
  retrieved passages (`DEFAULT_NUMBER_OF_RESULTS = 5`, ≈ 350 tokens each). Output is capped at
  256 (resolver) and 512 (writer) tokens.
- **No prompt caching is assumed.** If Harness caches the agent prompt, the real figure is lower.
- Smoke questions carry history (up to 6 pairs): +15 % input on every Harness call.
- Characters per token ≈ 3.7 (English/Spanish prose and JSON). Sonnet 4.6 uses the older tokenizer.

**Not exercised in the eval:** the eval real mode runs `AgentChatWorker.resolve_loop` directly, so
it makes no input Guardrail call, no quota writes, no memory write and no SQS message. The smoke
does all of them.

**Models.** Every model in the templates is `eu.anthropic.claude-sonnet-4-6` (Harness `ModelId`;
resolver and writer ARNs of the Public Edge stack; IDP profile). No other model is used.

## Prices and where they come from

Read on **2026-11-02** from the public pages. The AWS pricing pages render their rate tables with
JavaScript, so what a plain fetch returns is incomplete; where a number could not be read from the
page it is marked and bounded instead of remembered.

| Item | Price used | Source | Status |
|---|---|---|---|
| Sonnet 4.6 base list price | $3 input / $15 output / $0.30 cache read / $3.75 cache write (5 min) per M tokens | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) | read |
| Regional endpoint premium on Bedrock | **+10 %** over the global endpoint, for Sonnet 4.5 and every later model; `eu.` is a regional (geographic) inference profile | [Claude on Amazon Bedrock](https://platform.claude.com/docs/en/build-with-claude/claude-on-amazon-bedrock-legacy#global-versus-regional-endpoints) and [Claude in Amazon Bedrock](https://platform.claude.com/docs/en/build-with-claude/claude-in-amazon-bedrock#regions) | read; the AWS page for the same table is not readable → confirm in the bill |
| **Sonnet 4.6 via `eu.` (used)** | **$3.30 input / $16.50 output / $0.33 cache read / $4.125 cache write** per M | the two rows above | derived |
| Guardrails | content filters $0.15, denied topics $0.15, contextual grounding $0.10 per 1,000 text units | [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) | read; the PII (sensitive-information) filter is **not** on the readable part of the page → priced at $0.15 as an upper bound |
| Knowledge Base Retrieve | $1.00 per 1,000 calls (managed KB, standard retrieval) | [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) | read; the query embedding is a few tokens and is ignored |
| AgentCore Runtime (what Harness runs on) | v1 $0.0895 per vCPU-h + $0.00945 per GB-h; v2 consumption $0.1276 + $0.0169; per second | [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) | read; **Harness has no price of its own on that page** and its microVM size is not published → 1 vCPU / 2 GB typical, 2 vCPU / 4 GB at v2 rates for the upper bound |
| AgentCore Gateway | $0.005 per 1,000 invocations (list, invoke, ping); search $0.025 per 1,000 (not used) | [AgentCore pricing](https://aws.amazon.com/bedrock/agentcore/pricing/) | read |
| AgentCore Identity | $0.010 per 1,000 token requests; included when used through Runtime or Gateway | same | read |
| AgentCore Memory | $0.25 per 1,000 events (to 2026-10-05), then $1.00 per GB ingested, 12 KB minimum per event | same | read; the block writes ≈ 10 events: < $0.001 |
| AgentCore Observability / ADOT | billed as CloudWatch (spans, logs, metrics), no separate rate | same | read |
| CloudWatch | custom metric $0.30 / month; alarm metric $0.10 / month (a metric-math alarm bills each metric in its expression); Logs ingestion $0.50 / GB; free tier 5 GB of logs, 10 custom metrics, 10 alarm metrics; span ingestion $0.35 / GB; X-Ray $0.000005 per trace recorded | [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) | read, **US East (N. Virginia)** values; eu-west-1 is not shown on the page. Ireland is usually slightly higher, immaterial at these volumes |
| Lambda x86 | $0.0000166667 per GB-s, $0.20 per M requests; free tier 1 M requests and 400,000 GB-s | [Lambda pricing](https://aws.amazon.com/lambda/pricing/) | read, US East |
| DynamoDB on demand | $0.625 per M writes, $0.125 per M reads, $0.25 per GB-month | [DynamoDB on-demand](https://aws.amazon.com/dynamodb/pricing/on-demand/) | read, US East. A job is ≈ 25 writes + 40 reads: 10 jobs ≪ $0.001 |
| SQS | 1 M requests free per month; each 64 KB is one request | [SQS pricing](https://aws.amazon.com/sqs/pricing/) | free tier read; **the per-request rate is not on the readable part** → bounded: < 1,000 requests in the block, free |
| X-Ray / Transaction Search | not enabled by the plan | [X-Ray](https://aws.amazon.com/xray/pricing/) | the page does not carry the rates; spans of one probe invocation are < 1 MB |

### The price table in the code was wrong — corrected

`backend/src/legaldesk/agent_chat_telemetry.py` (`pricingVersion` `estimate-2026-11`) priced the
Harness loop at the **global** list price, $3 / $15 per M tokens. The Harness runs
`eu.anthropic.claude-sonnet-4-6`, a regional profile, so the list price plus 10 % applies.
The table is now derived from the base price and the premium and `pricingVersion` is
`estimate-2026-11-eu-regional`:

| Token kind | Before | Now |
|---|---|---|
| input | 3.00 | **3.30** |
| output | 15.00 | **16.50** |
| cache read | 0.30 | **0.33** |
| cache write (5 min) | 3.75 | **4.125** |

A test pins the values. The premium comes from Anthropic's pages, not from the AWS page that
sells the model, so treat 3.30 / 16.50 as the conservative reading: if the first bill shows no
premium, the estimate was 10 % high, not low. `estimatedLoopCostUsd` still covers only the Harness
iterations (not the resolver and writer inside `search_documents` nor the direct fallback):
**expect it to be about half of the Bedrock cost of a text question and about 70 % of the eval's** (eval: ≈ 1.0 USD typical,
1.75 USD upper bound, against ≈ 1.4 / 2.4 USD of Bedrock in total).

## Breakdown (USD, one-off, whole block)

| Component | Typical | Upper bound | Basis |
|---|---|---|---|
| Bedrock · eval real (30 questions) | 1.39 | 2.36 | tokens by class × counts |
| Bedrock · smoke (10 questions + 1 fallback) | 0.56 | 0.94 | same, +15 % history |
| Bedrock · ADOT probe and the Harness checks script (6 invocations) | 0.10 | 0.17 | no tools for the probe and the session check; tools on for the pre-check; search for the loop-cut check |
| AgentCore Runtime (Harness sessions) | 0.04 | 0.22 | 15–55 s per question (upper: up to the 90 s timeout), bigger microVM |
| AgentCore Gateway + Identity | < 0.01 | < 0.01 | 2–16 operations and 1 token per invocation |
| Knowledge Base Retrieve | 0.02 | 0.02 | one call per `search_documents` and per fallback |
| Guardrail | 0.15 | 0.31 | 2 units for the input check (smoke only) + 12–25 units per grounded answer, four policies |
| Lambda (worker, search, MCP, interceptor) | 0.01 | 0.02 | GB-seconds and requests; inside the free tier |
| SQS, DynamoDB, S3 | ≈ 0 | 0.01 | a few hundred requests |
| CloudWatch logs and spans | 0.01 | 0.03 | < 50 MB; free tier is 5 GB |
| **Total, one-off** | **≈ 2.3** | **≈ 4.1** | |

Not billable but part of the block: CloudFormation change sets, `ValidateTemplate`, IAM simulation,
and the read-only inventory calls.

### Fixed monthly cost while the agent chat stack exists (CloudWatch only)

| Item | Count | USD / month |
|---|---|---|
| Alarms: DLQ, duration, failures, queue age (1 metric each) and fallback rate (2 in the expression) | 6 alarm metrics | 0.60 |
| Custom metrics created from the job log line (duration, completed, failures, failures by code, fallbacks, fallbacks by reason) | 8 typical · 14 upper (each distinct dimension value is a metric) | 2.40 · 4.20 |
| **Total** | | **≈ 3.0 · ≤ 4.8** |

These are charged even if nobody asks a question, and the metrics created during the block are
charged for the month they receive data even if the stack is switched off right afterwards
(whether a metric with no new data keeps billing in later months is not stated on the readable part
of the page; assume it does not, and check the next bill). The 10 alarm-metric free tier is
account-wide and probably used by the Phase 14 alarms, so no discount is assumed.

Idle polling: an SQS event source mapping polls its queue continuously. With 5 pollers and 20-second
long polls that is up to ≈ 0.65 M `ReceiveMessage` requests a month — inside the 1 M free tier if SQS
has no other use, and at most about 0.3 USD above it (rate **not confirmed** on the readable page).
`AWS_COST_POLICY.md` says never to leave continuous polling on by default; see the decision at the end.

## ADOT

The probe is one Harness invocation with managed ADOT on (`docs/phase-15-adot-probe.md`): ≤ 81
`FilterLogEvents` reads (no charge for the API), spans of one call (< 1 MB: < 0.001 USD at
$0.35/GB). The cost is not the issue; **content in logs is**, which is why ADOT is switched off
again right after the probe unless the owner accepts the residual risk of ADR-021 D9. Transaction
Search (account-wide span indexing) is not enabled by this plan.

## What would change the numbers

| If… | Effect |
|---|---|
| Harness caches the agent prompt | input cost of H calls falls by up to 90 % on the cached part (cache read $0.33/M) |
| The `eu.` premium is not billed | −9 % on Bedrock |
| The agent calls `search_documents` more than once per question | +≈ 0.03 USD each (R + W) |
| History in the smoke is at its 24 k-character cap | ≈ +6 k input tokens on each of the ≈ 22 smoke Harness calls: +0.45 USD |
| A check is repeated | +1 invocation each, inside the 4-invocation reserve |
| Harness microVM is larger than assumed | Runtime is < 0.25 USD even at the upper bound; it does not move the total |

## Verifying the real cost after the block

Use the Billing console (month-to-date, filtered to Bedrock, AgentCore, CloudWatch, Lambda) the day
after: charges appear with a delay of hours. Do not use the Cost Explorer API without the owner's
explicit single-use confirmation (`AWS_COST_POLICY.md`, USD 0.01 per request). Record the observed
figure next to this estimate in `docs/phase-15-acceptance.md`; if the real Bedrock cost is outside
the 2.3–4.1 range by more than 25 %, re-read the assumptions above before any new live run.
