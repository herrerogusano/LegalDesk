# LegalDesk operations — Phase 11

Phase 11 adds repeatable, low-volume operational telemetry. Application code
emits one-line JSON events containing only `event_type`, `correlation_id`,
`outcome`, timestamps, closed-vocabulary operation/error codes, latency and
bounded counts. Questions, answers, prompts, retrieved passages,
documents, tokens, secrets, and arbitrary request payloads are excluded.

## Trace and audit pointer

Application `correlation_id` is generated or validated at each authorized
application boundary and is copied through the components in that flow:
retrieval, guardrails, model/agent steps, tools, and final response. It is not
claimed to be a provider-wide trace identifier. `InMemoryTelemetrySink` is the
local audit view used by tests. Production logs are the audit pointer: query
allowlisted events in the relevant Lambda/runtime log group with CloudWatch
Logs Insights. The pointer contains metadata only; it is not a document or
conversation archive.

AgentCore Harness/Gateway also have a managed provider trace/request context
(for example X-Ray or AgentCore trace identifiers). This repository does not
assert that AWS exposes a supported way to inject the application UUID into
that managed trace, and no payload logging or Transaction Search is enabled
to manufacture such a join. Therefore acceptance demonstrates application
correlation locally for Chat and live Gateway→MCP separately; a managed
Harness→Gateway trace must be inspected in the AWS provider tooling when
available.

The Phase 11 IaC does not enable AgentCore `APPLICATION_LOGS` or Transaction
Search. The existing managed Harness runtime previously emitted provider OTEL
`gen_ai` events with content. The content-only remediation in Harness version 6
(`AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`) was ineffective, so version 7
also sets the official `DISABLE_ADOT_OBSERVABILITY=true` control. The one
post-update synthetic smoke completed successfully and its unique question and
answer markers appeared in zero provider events. This supports PASS for the
bounded no-content logging criterion; the internal managed trace is deliberately
unavailable, and the application allowlist plus native metrics are the supported
operational trace surfaces. Do not query or treat any residual managed payload
events as audit evidence. Transaction Search also adds account-wide
span-ingestion cost.

References: [AgentCore observability configuration](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html),
[AWS ADOT Python instrumentation changelog](https://github.com/aws-observability/aws-otel-python-instrumentation/blob/main/CHANGELOG.md),
and [CloudFormation Harness environment variables](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-harness.html).

## Metrics

`infra/cloudformation/phase-11-observability.yaml` attaches a small set of
metric filters to the existing Phase 07/08 Lambda log groups. It creates no
new database, queue, dashboard, log group, or runtime. Metrics cover errors,
tool errors, and latency where structured events provide it; retrieval
not-found and request/session counts remain available through the structured
event stream and local sink. CloudWatch custom metrics and log ingestion can
be billed, so the filters and 14-day existing log retention are intentionally
small.

## Deploy / inspect / teardown

Use the commands in [`infra/phase-11-commands.md`](../infra/phase-11-commands.md).
Deployment must use a change set and explicit existing log-group parameters.
The stack owns metric filters only. Teardown removes those filters but leaves
the Phase 07/08 log groups and the Phase 01 Harness/Gateway/Memory resources
because later phases depend on them. Lambda-managed log groups and the
AgentCore runtime log group require separate inventory/review before removal.

Known gap: production ingestion does not yet attach a durable audit database;
the CloudWatch pointer is intentionally short-retention and metadata-only.
Phase 12 evaluation/demo must not treat it as a source of legal evidence.
