# Phase 11 acceptance — deploy and observability

Status: local implementation, AWS deployment, and synthetic smoke complete in
`eu-west-1`. Phase 12 has not started.

## Acceptance matrix

| Criterion | Evidence | Result |
|---|---|---|
| Repeatable IaC/deploy | `infra/cloudformation/phase-11-observability.yaml` and `infra/phase-11-commands.md`; stack `LegalDeskPhase11Observability` is `UPDATE_COMPLETE` | PASS |
| Minimal metrics | Seven `AWS::Logs::MetricFilter` resources in namespace `LegalDesk/Phase11`; the update added only `GatewayRequestMetric` | PASS |
| Application correlation | Local Chat flow carries one server-derived `correlationId` through agent/retrieval/final events and `ChatResponse`; live Gateway→MCP uses a separate server-derived UUID | PASS (two scoped flows) |
| Live structured evidence | Post-deploy synthetic interceptor/MCP call returned HTTP 200 and `Synthetic notice.pdf`; correlation `92e0a92e-3afa-4ba6-9fc8-d7532174e9ee` appeared in both log groups with JSON `agent/tool started → succeeded` events | PASS |
| Application telemetry redaction | LegalDesk-owned Lambda events are pure JSON from an explicit allowlist; no bodies, prompts, answers, passages, tokens, secrets, or auth headers are serialized | PASS |
| Managed runtime content logging | Phase 01 Harness v7 sets `DISABLE_ADOT_OBSERVABILITY=true` plus the content opt-out; the one post-update synthetic smoke completed successfully and the new question/answer marker appeared in zero provider events | PASS (bounded smoke; provider trace internals remain unavailable) |
| Teardown safety | Inventory and dry-run procedure documented; the Phase 11 stack owns metric filters only and was not deleted because later phases depend on retained resources | PASS (dry-run) |
| No orphaned Phase 11 resources | Stack resource inventory contains only metric filters over existing log groups; no new log group, database, queue, dashboard, or AgentCore runtime | PASS |

## AWS change evidence

Account `344774635844`, region `eu-west-1`:

- `LegalDeskPhase11Observability`: `UPDATE_COMPLETE`; the final update added
  one metric filter and preserved the existing three log-group parameters.
- `legaldesk-phase-01`: `UPDATE_COMPLETE` after change set
  `phase11-disable-adot-20260918`; only `MinimalHarness.EnvironmentVariables`
  was modified (`Replacement=False`). Harness ARN, Runtime ARN, execution role,
  Gateway attachment, and Memory attachment were preserved. Harness is `READY`
  at version 7 and reports both `DISABLE_ADOT_OBSERVABILITY=true` and
  `AWS_GENAI_CONTENT_EXTRACTION_OPT_OUT=true`.
- `LegalDeskPhase08Gateway`: `UPDATE_COMPLETE`; change set modified only the
  existing execution role, interceptor Lambda, Gateway, and MCP Lambda, all
  with `Replacement=False`. Gateway ARN was preserved.
- `LegalDeskPhase07ReviewTask`: `UPDATE_COMPLETE`; change set modified only
  the existing Review Lambda with `Replacement=False`.
- Fresh source artifacts were uploaded as versioned objects in the retained
  Phase 08 artifact bucket: interceptor
  `oUh1JxG7aoFpBtj4QNRRhzPxZlEC1aZE`, MCP
  `HAKJxn._UC55DY4baVUna16LvThLKFM5`, and review
  `GZ9H1Mt44JnOebsxE75iRdhxvW_DIFkL`.

The smoke used only fictional seeded metadata and an unsigned synthetic token
at the directly invoked interceptor boundary; no model inference or legal data
was used. A subject/matter not present in the authorization store was denied.
The direct Lambda invocation is a component smoke, not a claim that it replaces
the Gateway's production JWT signature enforcement.

The AWS-managed Harness→Gateway trace identifier is provider-owned. No
supported application-to-managed-trace injection was identified, and raw
payload logging/Transaction Search remain intentionally disabled. A provider
trace was visible in the managed runtime log metadata for this direct Harness
smoke (`6aad0abf11cb459f081ce31a56ee6211`), with request/session metadata. The
smoke message did not request a tool, so it did not exercise a Harness→Gateway
hop. Read-only X-Ray `GetTraceSummaries` returned no traces in `eu-west-1` for
the window, and no supported cross-system UUID join is claimed.

## Cost and gaps

The implementation adds only metric filters and structured events over retained
Lambda log groups. CloudWatch log ingestion, retained storage, and custom metric
datapoints may incur charges. Existing log retention remains short; no raw
AgentCore `APPLICATION_LOGS`, Transaction Search, dashboard, or durable audit
database was enabled by this phase. The managed ADOT detail is intentionally disabled in Harness v7 after the
content-only opt-out was ineffective. The bounded post-update smoke completed
successfully and found zero occurrences of its unique question/answer marker in
provider events, so the no-content logging criterion is PASS for that smoke.
The internal managed trace is no longer available for detailed inspection; the
allowlisted application telemetry and native metrics remain the operational
trace surfaces. Retrieval not-found/session counts remain queryable from
allowlisted events rather than adding a separate metrics service. Teardown
commands and special-resource warnings are in
[`infra/phase-11-commands.md`](../infra/phase-11-commands.md).

No Phase 12 evaluation, demo, or model-cost workload was implemented or run.

The environment controls are based on the AWS AgentCore observability guidance,
the AWS-maintained ADOT Python instrumentation v0.17.1 changelog, and the
CloudFormation Harness `EnvironmentVariables` property:
[`ADOT v0.17.1 changelog`](https://github.com/aws-observability/aws-otel-python-instrumentation/blob/main/CHANGELOG.md),
[`AgentCore observability configuration`](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-configure.html),
[`AWS::BedrockAgentCore::Harness`](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-bedrockagentcore-harness.html).
