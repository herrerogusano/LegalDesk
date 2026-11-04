# F-04 Gateway observability ? evidence and decision

Prepared 2026-11-04, read-only in eu-west-1 / account `344774635844` as `aws-cloud-ai-web-cli` (profile `legaldesk`). **No template, preview profile, resource, permission, target, alarm, metric, model or Harness invocation was changed/created. No activation is authorized.** Safe configuration/volume/price inputs are in [the evidence snapshot](phase-15-gateway-observability-evidence.json).

Recommendation: **not viable for activation under the current no-content rule**. Unfiltered application logs contain tool request/response bodies. A metadata-only whitelist loses the status/error/latency needed for F-04. Service-generated traces have the desired attributes, but the delivery requires the entire trace, without a nested attribute exclusion control or an established exhaustive no-content contract. The supervisor accepted non-activation. This does not establish the cause of F-04.

## Required diagnostic fields versus application logs

[AWS Gateway observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-gateway-metrics.html) distinguishes application logs from spans. The exact status/error code from 2026-11-03 cannot be reconstructed retrospectively by enabling a delivery now.

| Needed value | Documented location | Application-log metadata whitelist | Verified in this account? |
|---|---|---|---|
| Request ID | Log `request_id`; span attribute `aws.request.id` | Kept | Schema/documentation only; no Gateway delivery |
| HTTP status | Span attribute `http.response.status_code` | Not a selectable outer application-log field | No |
| Error type | Span attribute `error_type` | Not a selectable outer application-log field; human error message can be in `body.log` | No |
| JSON-RPC code | Span attribute `jsonrpc.error.code` | Not a selectable outer application-log field; response/error body may contain it | No |
| Latency | Span attribute `latency_ms` (also `overhead_latency_ms`) | Not a selectable outer application-log field; subtracting timestamps is not proof of Gateway latency | No |

The application-log example has `body.requestBody` with arguments and `body.responseBody` with tool result text. Question text can be a search argument; results can contain document passages or IDP values. Documentation mentions malformed/missing authorization headers but does **not** guarantee that raw headers/tokens or sensitive strings never occur in errors. Those are unverified risks, not claims that actual tokens were observed. `ExceptionLevel=DEBUG` is not proposed.

Live `DescribeConfigurationTemplates(service=bedrock-agentcore, resourceTypes=gateway, logTypes=APPLICATION_LOGS)` lists CWL/S3/FH support. For CWL only `resource_arn` and `event_timestamp` are mandatory. Its complete selectable-field list is below. This was a **rejected candidate** whitelist, not a configuration change:

| Field | Kept/excluded | Why | Question/document/argument/result/token/free-text risk |
|---|---|---|---|
| timestamp | Excluded | Prefer event_timestamp | Time only |
| resource_arn | Kept | Mandatory exact resource identity | ARN metadata; no content expected |
| gateway_id | Excluded | Redundant identity | Identifier only |
| event_timestamp | Kept | Mandatory event time | Time only |
| body | Excluded | Contains requestBody, responseBody, log/error text | **Yes:** question, arguments/results/document text; token/header leakage in error text not ruled out |
| account_id | Kept | Scope verification | Account metadata only |
| request_id | Kept | Request correlation | Opaque metadata |
| trace_id | Kept | Trace correlation | Opaque metadata |
| span_id | Kept | Span correlation | Opaque metadata |
| resource | Excluded | Structured open metadata | Unknown nested fields; no no-content guarantee |
| attributes | Excluded | Structured open metadata | Unknown nested fields; content/error/headers not ruled out |
| timeUnixNano | Excluded | Duplicate timestamp | Time only |
| severityNumber | Kept | Numeric severity | Number only |
| severityText | Excluded | Avoid unnecessary string | Severity string expected, closed vocabulary not established |
| traceId | Excluded | Duplicate trace identifier | Opaque metadata |
| spanId | Excluded | Duplicate span identifier | Opaque metadata |

[Delivery.RecordFields](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-logs-delivery.html) can select outer fields. It cannot retain a safe error code from inside an excluded body. Neither field selection nor absence of prohibited content was tested against delivered records, because activation is prohibited. Masking after ingestion is not exclusion from storage.

## Trace-only route and account prerequisites

The live Gateway TRACES configuration template has **only XRAY** as destination, mandatory record field `trace`, optional `timestamp`/`resourceArn`, no advertised delivery-source configuration or attribute filter. [PutDeliverySource](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutDeliverySource.html) supports TRACES separately from APPLICATION_LOGS. [PutDeliveryDestination](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutDeliveryDestination.html) supports XRAY without a destination resource ARN. Thus service-generated Gateway traces can be delivered independently of application logs; no application-log destination is required. This is separate from Runtime/Harness ADOT instrumentation.

But [X-Ray trace delivery](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AWS-logs-infrastructure-V2-XRayTraces.html) requires **Transaction Search**. Merely sending to the current classic X-Ray destination is not a documented alternative that skips this prerequisite. [Enable Transaction Search](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/Enable-TransactionSearch.html) changes the account/region destination to CloudWatchLogs and requires a resource policy permitting `xray.amazonaws.com` to PutLogEvents into `aws/spans` and `/aws/application-signals/data`, constrained by account/source ARN. X-Ray also needs a policy permitting `delivery.logs.amazonaws.com` to PutTraceSegments from the delivery source. AWS automatically creates one policy per AgentCore resource type, covering **all gateway/* in the account/region**, not just this Gateway; that breadth needs review. There is no such policy now. No broad policy is proposed by this PR.

| Live read | Observed value | Implication |
|---|---|---|
| GetTraceSegmentDestination | Destination=XRay, Status=ACTIVE | Transaction Search not enabled for CloudWatchLogs |
| GetIndexingRules | Default DesiredSamplingPercentage=0 | Current setting; not a Gateway head-sampling guarantee |
| GetSamplingRules | Default FixedRate=0.05, reservoir=1/s | Existing classic X-Ray rule; not proof of Gateway service emission or sampling behavior |
| DescribeLogGroups | No `aws/spans` or `/aws/application-signals/data` | No existing Transaction Search storage group |
| DescribeResourcePolicies | No X-Ray-to-Logs policy | Prerequisite missing |
| X-Ray ListResourcePolicies | Empty | Trace delivery policy missing |
| DescribeDeliverySources | Empty | Gateway application logs and traces both have no delivery source |
| Harness stack EnableManagedAdot | false | Preserve earlier no-content decision |

Head sampling controls whether spans are produced; indexing controls which already ingested spans get X-Ray trace summaries. 1% indexing is **not** 1% ingestion and can miss the rare failure in the trace-summary view. Transaction Search can search all ingested spans. AWS documents [head sampling versus indexing](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search-ingesting-spans.html), but this worker has not established which incoming sampled flag/service decision controls a failed Gateway discovery span. No incoming tracing settings were changed.

`describe-type` in **eu-west-1** successfully returned schemas for `AWS::Logs::DeliverySource`, `AWS::Logs::DeliveryDestination`, `AWS::Logs::Delivery`, `AWS::Logs::LogGroup`, `AWS::XRay::ResourcePolicy`, `AWS::XRay::SamplingRule` and `AWS::XRay::TransactionSearchConfig`. This verifies regional schema registration, **not** a successful deployment. Delivery source name and destination name/type/format/ARN are create-only; Delivery source/destination references are create-only. [TransactionSearchConfig](https://docs.aws.amazon.com/AWSCloudFormation/latest/TemplateReference/aws-resource-xray-transactionsearchconfig.html) is account-scoped; IndexingPercentage updates without interruption, CloudWatchLogsConfiguration is create-only/replacement. A review must consider ownership by other stacks/services. No change set or synth claiming safe deployment was created.

Gateway stack RoleARN is absent (caller credentials), unlike Public Edge's code-only `LegalDeskProductionCloudFormation`. Read-only IAM simulations of the operator returned allowed for delivery create/get/delete/update/tag, log-group create/retention/policy, `bedrock-agentcore:AllowVendedLogDeliveryForResource`, X-Ray destination/index changes and Put/DeleteResourcePolicy. The Public Edge code-only role returned implicitDeny for the tested provisioning actions; it is not appropriate. Simulations used Resource=* to detect grants and are **not** a complete exact-resource/condition/SCP validation. Remaining setup dependencies such as Application Signals discovery/service-linked-role/CloudTrail channel permissions and exact trace policy conditions were not fully simulated. No permissions were widened; an activation implementation would need a new concrete least-privilege review.

## Published span attributes and content boundary

This is the **complete published Gateway operation attribute list**, not a proven exhaustive list of all fields a delivered OTEL/X-Ray span may contain. List Tools has the first 16 attributes below. Call Tool and Search Tools also have `tool.name`. Tools/call has SERVER and CLIENT spans; other MCP methods have a SERVER span. The service does not document an allowlist/closed vocabulary for every string, absence of other attributes, or exclusion of status messages/events/exception details. No actual Gateway span was available for inspection.

| Attribute | Use | Can carry content? / confidence | Excludable individually in delivery? |
|---|---|---|---|
| aws.operation.name | Operation | Operation metadata expected | No |
| aws.resource.arn | Exact Gateway | ARN metadata expected | No |
| aws.request.id | Required request ID | Opaque metadata expected | No |
| aws.account.id | Account | Account metadata | No |
| gateway.id | Gateway identity | Identifier metadata | No |
| aws.xray.origin | Service origin | Service string expected | No |
| aws.resource.type | Resource type | Service string expected | No |
| aws.region | Region | Region metadata | No |
| latency_ms | Required latency | Numeric duration expected | No |
| error_type | Required error type | **Unverified:** no documented closed vocabulary or no-free-text guarantee; error strings could embed request content | No |
| jsonrpc.error.code | Required JSON-RPC code | Numeric code expected; not error message | No |
| http.method | Method | Method metadata expected | No |
| http.response.status_code | Required HTTP status | Numeric code expected | No |
| gateway.name | Gateway name | Operator-defined string; no user content expected here | No |
| url.path | Route | Path string, normally /mcp; content-free behavior on malformed input unverified | No |
| overhead_latency_ms | Gateway overhead | Numeric duration expected | No |
| tool.name | Call/Search only | Tool name, possibly externally configured; no strict no-content contract | No |

Span envelope normally adds trace/span IDs, kind, start/end/duration, resources, status and potentially events/links. The Gateway page does not enumerate that full emitted schema. Questions, arguments, results, document text, authorization headers and tokens are **not listed** in the published Gateway attribute table; that is not proof they or free-text exceptions never appear elsewhere. `error_type` is not this project's closed #96 enum. The TRACES schema requires the full `trace` field; no nested allowlist exists. [CloudWatch span limitations](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch-Transaction-Search-ingesting-span-log-groups.html) explicitly exclude span transformation. A downstream subscription/filter cannot undo the original ingestion. Therefore safe exclusion is not established; no trace activation is recommended.

A failed tools/list during Harness discovery is **documented as an eligible operation**, with error attributes, but **actual span emission for that failure path is unverified**. The documentation does not promise every pre-target authorization/discovery/timeout error emits a sampled completed span. No delivery existed on 2026-11-03, so absence of records proves nothing about emission. ADOT being off must not be silently reversed to fill this gap.

## Measured volume and exact regional unit prices

CloudWatch `AWS/Bedrock-AgentCore`, UTC 2026-11-03 00:00 through 2026-11-04 00:00, Sum of Invocations for this exact Gateway ARN, Operation=InvokeGateway, Protocol=MCP:

| Method | Requests measured |
|---|---:|
| initialize | 129 |
| notifications/initialized | 129 |
| tools/list | 258 |
| tools/call | 123 |
| **Total** | **639** |

The aggregate series also reads 639. Do **not** add it or per-tool series to the method totals. The 129 Runtime invocations used for normalization are from the supervisor's F-04 investigation, not equivalent to 129 billable successful chat jobs. Estimated Gateway spans, assuming every request emits once and every tools/call adds one CLIENT span: 639+123=**762**. No Gateway bytes/span count was measured because delivery is off.

Rates below were downloaded from the [official EU (Ireland) regional price list](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonCloudWatch/current/eu-west-1/index.json); SKU/version/publication date are preserved in the JSON snapshot. [CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) explains the span bundle and includes an example assuming approximately 1kB per span. We use **1 KiB=1024 bytes/span as an estimate**, not observed Gateway size. The upper-size sensitivity is **8 KiB/span**, a planning assumption with no claimed provider maximum. Full bodies can be substantially larger; there is no honest exact total or hard upper cost without measured event sizes/counts.

| Item | Exact first-tier eu-west-1 rate, USD | Usage type |
|---|---:|---|
| Application/vended logs, Standard | 0.57 / GB ingested | EU-VendedLog-Bytes |
| Custom Standard logs (comparison only) | 0.57 / GB | EU-DataProcessing-Bytes |
| Transaction Search/Application Signals span bundle | 0.35 / GB ingested (first 10 TiB); 0.20 next 20 TiB; 0.15 above | EU-Application-Signals-Bytes |
| Additional indexed spans beyond included first 1% | 0.75 / million indexed spans | EU-XRay-Spans-Indexed |
| Standard stored logs/spans | 0.03 / GB-month stored (compressed bytes) | EU-TimedStorage-ByteHrs |
| Logs/trace query scan | 0.005 / GB scanned | EU-Omni-DataScanned-Bytes |

Do not charge both 0.57 log ingestion and the 0.35 span bundle for the same spans. Account free allowances, trials, query allowances, credits and other regional traffic were **not verified**, so none are subtracted. The published first 1% included span indexing is a bundle property; it is not the account's unverified free tier. No Cost Explorer calls were made.

For quota **50/month**, typical request counts normalize the measured day, round tools/call up to 48 and assume 50 initialize +50 initialized +100 list = **248 requests/296 spans**. Upper planning sensitivity assumes 16 calls/job and the same discovery overhead: **1000 requests/1800 spans**, at 8 KiB/span. The client parser's 16-result cap is not a Gateway request quota; hidden retries, failed discovery and unrelated callers can exceed this sensitivity. Harness evals/checks bypass the application monthly quota, so 50 is not an account-wide Gateway hard cap.

Formula: GiB=spans*bytes/2^30; ingestion=GiB*0.35; storage=GiB*0.03*retentionDays/30 (conservative uncompressed storage); 100%-index extra=spans*0.99*0.75/1e6. At <=1% indexing, additional indexing charge=0. Three-day retention is a **hypothetical explicit choice**, not an activated or verified default. The upper column uses 30-day storage and 100% indexing to expose sensitivity.

| Scenario | Estimated spans/bytes each | Span ingestion USD | Storage USD (3 days / 30 days) | Extra indexing USD (1% / 100%) | Total USD (3d,1% / 30d,100%) |
|---|---|---:|---:|---:|---:|
| Measured day's traffic, typical size | 762 / 1 KiB | 0.000254345 | 0.000002180 / 0.000021801 | 0 / 0.000565785 | **0.000256525 / 0.000841931** |
| Same day, upper-size sensitivity | 762 / 8 KiB | 0.002034760 | 0.000017441 / 0.000174408 | 0 / 0.000565785 | **0.002052200 / 0.002774952** |
| Quota-50 typical month | 296 / 1 KiB | 0.000098801 | 0.000000847 / 0.000008469 | 0 / 0.000219780 | **0.000099648 / 0.000327049** |
| Quota-50 upper planning month | 1800 / 8 KiB | 0.004806519 | 0.000041199 / 0.000411987 | 0 / 0.001336500 | **0.004847717 / 0.006555006** |

These tiny **Gateway-only estimates are not a quote/cap for account-wide Transaction Search**. Other producers/EMF/APM and existing trace sources can be affected by the account change; their future bytes are unknown. Storage persists after disabling a delivery/Transaction Search until explicit retention/deletion; deleting configuration does not refund ingestion or remove local exports/trace summaries. A customer-managed KMS key would add key/request charges and a policy review; it is not proposed. Logs/spans are service-encrypted by default. No new alarms, metric filters, custom metrics, ADOT or Application Signals discovery is added by this evidence PR: incremental resources/cost **zero**. Native Gateway metrics remain available. Re-enabling ADOT/EMF or account-wide discovery would require a fresh content and metric-budget review; no credit from the previously estimated custom-metric allowance is assumed here.

## Verification required after any separately authorized activation

This is a future acceptance procedure, **not permission to activate or call anything**. With a separately reviewed delivery and written synthetic-call approval, the supervisor can make **one direct synthetic tools/list call**, without Harness/model invocation, recording UTC start/end and the provider request ID privately. Use a unique harmless marker in an allowed synthetic request field; never use a real secret as a canary. A successful list proves only that path, not failure emission or that free-text errors cannot leak credentials.

Read the entire delivered event set for that request/window into private memory/storage, including paginated events, not a console projection that hides body. For a log whitelist, recursively parse each raw message, assert its full top-level key set is a subset of the chosen whitelist, assert body/attributes/resource/requestBody/responseBody and request/response/header/token/content subtrees are absent, and search every value for the unique synthetic marker. Compare event times/request IDs; require at least one matched complete request. Print only booleans, counts, key names and closed numeric diagnostics. Absence of a marker alone is insufficient; unknown fields, no matched events, delivery delay or incomplete pagination fail the check. This does not prove every possible future error path safe. For TRACES, inspect the complete trace envelope/resources/events/status/attributes similarly; no configuration currently can enforce the nested exclusions, so a successful canary is not a lasting control.

If a later approved investigation collects a cut stream, read a narrow UTC time window and join by `aws.request.id`/trace ID; require all four diagnostic fields plus request ID/latency in the raw matched record. Do not claim JSON-RPC code from the HTTP status or client SDK status. A query projection cannot remove already stored content. This PR supplies **no executable change-set steps/profile** because the activation was explicitly rejected; creating a change set now would not answer the approved question safely. Future activation needs a new reviewed proposal, exact-resource IAM review, no Gateway replacement/target changes, budget and written exception if content cannot be excluded.

## Time-boxed unfiltered synthetic log window ? separate owner exception only

This describes scope without recommending or preparing it. It would require an **explicit written owner exception to the no-content rule**, a time/request/byte budget and a separate activation/cleanup approval. Exact resources: `AWS::Logs::LogGroup` in Standard class under `/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/<gateway-id>`, `AWS::Logs::DeliverySource` for this Gateway ARN with APPLICATION_LOGS, `AWS::Logs::DeliveryDestination` for that group with JSON format, and `AWS::Logs::Delivery` joining them with unfiltered default record fields; delivery's log-group resource policy must be reviewed as well. No Gateway replacement/target change, ADOT, Transaction Search, new metric or alarm is needed. Service encryption at rest would avoid a new KMS key; encryption does not remove content risk. **[One day is the shortest nonzero CloudWatch retention](https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html)**; expired events normally take up to 72 more hours to be physically deleted, sometimes longer. Retention is not instant erasure.

After a tightly bounded synthetic-only window, stop/remove the delivery first, verify delivery stopped, delete the window's entire log group/streams (CloudWatch has no per-event delete), then delete source/destination and remove only policy permissions introduced for this window. Check all extra destinations/exports/subscriptions and remove private raw copies under the approved procedure; provider-managed retention/backups are not independently verifiable. Do not claim immediate physical erasure from retention alone. Any actual token/header in errors would remain sensitive despite synthetic questions/documents; the written decision must explicitly address that unverified risk and whether it prohibits the window.

Cost is bytes-based at **0.57/GB ingestion +0.03/GB-month stored**; no fixed group/delivery fee is added here. For illustration only, **20 requests**, 2 events/request and 1 KiB/event give 40 KiB: ingestion **$0.000021744**, one-day uncompressed storage **$0.000000038**, total **$0.000021782**. A larger sensitivity, 20 requests*4 events*256 KiB=20 MiB, gives ingestion **$0.011132813**, one-day storage **$0.000019531**, total **$0.011152344**. These event counts/sizes are assumptions, **not measured or upper bounds**; unfiltered payloads can exceed them. A full scan of those respective volumes would add ~$0.000000191 / $0.000097656 before any unverified query allowance. Direct Gateway request/Lambda charges are separate existing-service usage and depend on actual approved calls; no inference/Harness calls are implied. No such window is approved or executed.

## What remains for F-04

The closed client diagnostic from [PR #96](https://github.com/herrerogusano/LegalDesk/pull/96) is what remains: closed exception class/AWS code, numeric SDK status, event count and correlation ID, without messages/headers/payloads. Its SDK status does not establish the Gateway's external HTTP status or JSON-RPC code. Deployment/current live behavior of that PR was not reverified in this worker.

**AWS Support is not an available fallback as previously written.** The supervisor supplied `aws support describe-severity-levels -> SubscriptionRequiredException`: this account has no Premium Support plan. This result was not independently rerun. Buying a paid plan or posting a public re:Post question are **separate owner decisions; neither has been taken**. No case or public post was created. If the owner ever chooses either route, prepare a sanitized evidence packet: region, Gateway ARN, UTC 2026-11-03 windows 22:15:09.855?22:15:57.130 and 22:18:33.324?22:19:14.427, two tools/list UserErrors at 22:15Z/22:19Z with ~40070/40039 ms latency, no throttles/5xx, relevant release/configuration and safe #96 diagnostics. Request IDs should come from an observed diagnostic; they are not currently joined across the service. Exclude tokens, cookies, user IDs/subjects, questions, documents, raw headers and private endpoints/identifiers from any public post. Historical minute-level correlation remains evidence, not a proven causal join.

Validation: regional schemas/configurations/metrics read only; JSON snapshot parses and method totals reconcile; cost arithmetic recalculated offline; `git diff --check`. No template changed, so cfn-lint/change-set preview is not applicable. Supervisor maintains the failure register; no shared vault note was edited.
