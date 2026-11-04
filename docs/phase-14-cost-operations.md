# Phase 14 — cost envelope and operations gate

## Status and authority

This began as a planning artifact for the approved authenticated public beta
architecture. The Phase 14 stacks and bounded tests were subsequently executed
under operation-specific user approvals. This document does not authorize any
additional AWS call, resource change, real-model inference, destructive
cleanup, or promotion to `prod`. The beta accepts only fictional/public
documents and pre-provisioned authenticated users.

No general numeric spend ceiling is claimed by this document. Earlier
operation-specific approvals do not establish a reusable production budget.
A numeric ceiling, retention period, request budget, and teardown target must
be approved before further ingestion/inference, the final real-model holdout,
or production promotion. Local implementation and provider-double tests remain
permitted.

## Resource inventory and cost classes

### Existing resources to inventory and reuse

- Cognito User Pool, public PKCE client, issuer/domain and user memberships.
- Private source S3 bucket and metadata/auth/review DynamoDB table.
- Bedrock Knowledge Base, S3 Vectors index/data source and embedding model.
- Guardrail/version and resolver/writer model or inference profile.
- AgentCore Gateway, interceptor, MCP metadata Lambda and Review Lambda.
- AgentCore Harness and short-term Memory.
- Existing Lambda log groups, metric filters and versioned artifacts.

Their retained storage, requests, model/embedding/retrieval, AgentCore,
Lambda, and CloudWatch usage may continue to incur charges. Historical smoke
teardown is not a live inventory or a guarantee that an account has no
retained resources.

The deployed Phase 02 source bucket remains unversioned. For the fictional/public
beta, recovery is therefore re-upload-only: only quarantine uploads expire by S3
lifecycle (canonical originals do not: see the data-governance runbook), and the
application does not claim object-version backup coverage.
Enabling versioning would add storage cost, require version-aware deletion and
least-privilege `ListBucketVersions`/`DeleteObjectVersion` permissions, and is
deferred until that separate design and approval exist.

### New or changed resources in the target topology

- CloudFront distribution, HTTPS custom domain and ACM certificate; Route 53
  DNS is optional but may add hosted-zone/query charges.
- Dedicated private S3 frontend bucket and CloudFront Origin Access Control.
- API Gateway HTTP API and Lambda application adapter.
- DynamoDB state items and possibly a state GSI; a separate state table is an
  alternative with additional storage/request cost.
- Reconciliation scheduler/worker (EventBridge + Lambda, or an approved
  equivalent).
- CloudWatch log ingestion, alarms and custom metrics.
- Twelve standard CloudWatch alarms over the public API, application,
  malware/reconciliation Lambdas and their DLQs, plus one direct-email monthly
  AWS Budget alert. The operations candidate creates no alarm topic by default;
  it can either reuse an explicitly supplied existing topic or create one
  project-owned SNS topic and one email subscription when explicitly enabled.
  SNS request/delivery charges and the subscription confirmation/teardown are
  operator responsibilities. The Budget is account-wide so it does not
  silently omit untaggable Bedrock/Marketplace spend. Alarm and log retention
  charges are still possible.
- Optional WAF WebACL/rules and request charges.
- Optional ECR image storage/scanning if Lambda is delivered as a container.
- Real-model holdout inference, retrieval, Guardrail calls, and any required
  KB re-ingestion/embedding.

An ECS/Fargate + ALB alternative would add load-balancer hourly/LU charges and
always-on vCPU/memory charges; it is not the approved default topology.

## Local-first boundary

Before any paid operation, complete locally:

- IaC/template validation and change-set review preparation;
- Lambda/API adapter tests and durable-state repository tests;
- restart/multi-instance, cross-matter, replay, expiry and cleanup tests;
- endpoint/CSP/CORS/cookie/security-header tests;
- synthetic malware/content/quarantine and retention/deletion/export tests;
- deterministic evaluations and the holdout runner preflight.

No CI test or deterministic evaluation may call AWS or a real model. Real
deployment, ingestion, retrieval, inference, or holdout execution requires a
separate approval that names the account/region, resource IDs, request caps,
model/profile, prompt hashes, data retention, and numeric cost ceiling.

Budgets and alarms are detection mechanisms, not billing hard caps. Request
ceilings, bounded retries, one-operation idempotency, and a scheduled close
procedure are the primary controls. Missing/ambiguous provider usage is not
treated as zero cost.

The public application also has a server-side monthly quota ledger before each
potentially billable operation. It keys one DynamoDB counter item by the
authorized tenant and UTC month (using the existing metadata table, never a
scan) and reserves with one conditional `UpdateItem`: 50 uploads/500 MiB, 300 chats, 50
ingestion starts, 300 Harness calls (raised from 100 in Phase 15 so it matches `chats`, ADR-021 D8), and 500 Gateway calls by default. The
limits are server-side environment configuration and are strictly validated;
the Lambda template exposes bounded parameters for them. Uploads reserve both
count and bytes. Ingestion reservations use the request idempotency key so a
retry does not double-charge. Quota exhaustion or an unavailable quota store
returns a generic HTTP 429 before the provider is called. Reads, status
lookups, and authorization failures do not reserve quota. Ingestion retry keys
are hashed together with the verified subject, tenant, and matter before being
added to the counter item's token set; there are no separate idempotency items
or non-atomic claim/update sequence. Loopback tests use an explicit disabled
ledger, while public compositions require an explicitly injected ledger and
the AWS public composition injects DynamoDB. The token set is bounded by the
configured monthly operation ceilings; unusually high retry-key cardinality
should still be monitored for DynamoDB item-size pressure.

The local operational candidate and response procedure are in
[`phase-14-operations-runbook.md`](phase-14-operations-runbook.md). It uses
native AWS metrics, five-minute beta evaluation periods by default, and
`notBreaching` for missing data. No alarm action is enabled until an approved
on-call destination exists. An existing SNS topic can be passed explicitly, or
the stack can create a project-owned topic only when `CreateAlarmTopic=true`
using the existing NoEcho `BudgetEmail` confirmation endpoint; both modes
cannot be selected together. The Budget's direct email is only an alert.

## Monthly budget measures usage before credits

The operations template explicitly sets `CostTypes.IncludeCredit=false` and
`IncludeRefund=false`. Credits previously offset the account's usage to zero,
so the alerts could stay silent while AWS resources consumed credit. This
gross-usage measure excludes those offsets; all other cost-type settings,
including discounts, remain unchanged. It is account-wide, not LegalDesk-only.
The deployed limit remains **25 USD/month**, with direct-email ACTUAL alerts
above 80% and 100%, using the same subscriber.
Budget figures can take about 24 hours to recompute and are not a billing cap.

[AWS Budgets pricing](https://aws.amazon.com/aws-cost-management/aws-budgets/pricing/)
(verified 2026-11-03) makes budget monitoring and notifications free, including
forecast notifications. This change adds no Budget Actions, Budget Reports,
SNS subscriptions or other paid features. Existing CloudWatch/SNS costs remain.

Read remaining credits separately from usage, using the IAM-user login:

```powershell
aws freetier get-account-plan-state --region us-east-1
```

Deploy only through the operations CloudFormation template and a reviewed change
set, preserving every live parameter with `UsePreviousValue` (in particular the
25 USD limit and NoEcho email). The resource schema from `describe-type` permits
`CostTypes` updates in place, but marks `NotificationsWithSubscribers` as
create-only. A 2026-11-03 change set adding a free FORECASTED 100% email alert
listed only `MonthlyBudget` Modify, with conditional replacement. On execution,
the replacement failed because the existing budget had the same name and a
different internal ID; CloudFormation rolled back successfully. The forecast
alert is therefore deferred: the final template preserves both existing ACTUAL
notifications and changes only `CostTypes` in place. Adding the forecast alert
needs a separately agreed CloudFormation migration; do not rename/delete the
budget or edit it outside IaC to work around this.

Stop if any resource besides `MonthlyBudget` changes; never edit the stack-owned
budget in the console or expand IAM to deploy it.

The console-created `My Zero-Spend Budget` and the other project's
`honda-mapit-mcp-prod-account-wide-monthly` budget are outside this change.

Deployment evidence (2026-11-03): `budget-gross-usage-only-20261103` contained
only `MonthlyBudget` Modify (conditional replacement in the preview), changed
only `Budget.CostTypes`, and completed in place with the same budget name
(only update events, no replacement). AWS advanced `TimePeriod.Start` from
September 1 to the current month's November 1; the template leaves that start
implicit. The end date remained unchanged. The stack reached `UPDATE_COMPLETE`
with no service role attached;
the operator used an IAM-user `aws login` session, without IAM changes.
`describe-budget` confirmed both exclusions and the 25 USD limit; calculated
spend immediately read **0.007 USD actual / 0.38 USD forecast**, pending the next
cost-data refresh. Both ACTUAL subscribers and the other two budget
configurations were verified unchanged. The reviewed change-set JSON and
before/after snapshots are outside the repo under
`%USERPROFILE%\legaldesk-t9\20261103\budget\` (UTF-8 without BOM).

## DLQ email cost check — 2026-11-03

Read-only account-wide inspection, before implementation, scanned all **18
enabled regions** returned by `ec2 describe-regions`. Only `eu-west-1` has
alarms: **22 metric-alarm objects representing 23 standard-resolution alarm
metrics**, with no high-resolution, composite or Metrics Insights alarms.
LegalDesk uses 20 metrics (19 alarms), Honda MAPIT two and AWS Remote MCP one.
The chat fallback-rate expression lists two metrics and therefore counts as
two, even though only one result is evaluated. Disabled alarm actions do not
stop alarm-metric billing. Other 17 enabled regions have zero alarms; disabled
opt-in regions cannot host active account resources and were not queried.

| Standard alarm metrics | Now | This change adds | After deployment |
|---|---:|---:|---:|
| Account total across projects/regions | 23 | 2 IDP DLQs | 25 |
| Monthly free allowance | 10 | 0 | 10 |
| Remaining free allowance | 0 | 0 | 0 |
| Above allowance | 13 | 2 | 15 |
| Full-month gross alarm cost, USD | 1.30 | **0.20** | **1.50** |

The existing chat DLQ alarm only gains email actions; no extra alarm is created
for chat. Setting its existing topic parameter enables all six chat alarms;
those alarm metrics already exist. The increment **0.20 USD/month** is within
the owner's 0.30 USD/month change ceiling; the **account total is not** below
that ceiling. These are steady-state, pre-tax, before-credit estimates, not a
November invoice: actual alarm charges are prorated by usage time.

[CloudWatch pricing](https://aws.amazon.com/cloudwatch/pricing/) confirms ten
eligible standard-resolution alarm metrics free monthly and billing per listed
metric, including metric math. It also lists ten custom/detailed-monitoring
metrics free. AWS Price List `GetProducts` independently confirmed the Ireland
standard alarm price **0.10 USD per metric-month**. The pricing page did not
explicitly state account/region aggregation for these CloudWatch allowances;
the account's Free Tier API verified the alarm allowance as **global** and
`Always Free`, limit 10. Do not multiply it by projects or regions.

`freetier get-account-plan-state --region us-east-1` returned **PAID / ACTIVE**,
with credits remaining. `get-free-tier-usage` **succeeded**, rather than the
anticipated billing-access denial. Its global `CW:AlarmMonitorUsage` entry was
1.10416649 actual / 11.0416649 forecast, limit 10; these are time-weighted usage
figures, not the current deployed count of 23. No Cost Explorer call was used.
The paid plan therefore does not remove this account's reported always-free
alarm allowance; remaining credits must not be described as zero gross cost.

Custom-metric headroom is **unverified**, not ten free slots assumed available.
`list-metrics` found 153 recently active non-`AWS/` metric identities in
Ireland (151 `bedrock-agentcore`, two `LegalDesk/Phase11`), zero elsewhere.
`DocumentIntegrityFindings` was not listed. Listing only recently active
identities cannot establish monthly billable usage, and the Free Tier response
had no custom/detailed-metric usage entry. Provider namespace billing treatment,
hourly activity, expired identities and any detailed monitoring are additional
limits on that estimate. The existing DocumentIntegrity metric may consume a
custom slot when emitted; do not promise that it is free. First-tier custom
metric list price is 0.30 USD/metric-month, prorated. **This DLQ change creates
zero custom metrics**: queue-visible metrics are native `AWS/SQS`.

[SNS pricing](https://aws.amazon.com/sns/pricing/) lists the first **1,000
email/email-JSON notifications per month free**, aggregating free usage across
regions. The [AWS SNS FAQ](https://aws.amazon.com/sns/faqs/) and Ireland Price
List entry confirm **2 USD per 100,000 deliveries thereafter** (0.00002 each),
and one million Standard API requests free monthly, then 0.50 USD/million.
One existing, confirmed email subscription is reused, with no new subscription,
topic, SMS, KMS key or custom-metric charge from this change. At low alarm
volume email cost is expected to be zero **if the account's shared allowance
remains available**; Free Tier returned no SNS usage entry, so remaining SNS
headroom is unverified. For example, 100 additional deliveries cost at most
0.002 USD at the stated delivery rate even if that allowance is exhausted,
plus any metered requests/transfer. Notifications are transition-based; this
is not an unlimited-volume cost guarantee. Keep bodies out of notifications.

The implementation and exact per-stack deployment/role expectations are in the
[DLQ email candidate](phase-14-operations-runbook.md#dlq-email-deployment-candidate--2026-11-03).
No AWS write, change-set creation, deployment, message receive or redrive was
performed for this candidate. Existing costs outside these alarm changes were
not approved or modified by this worker.

## Deployment and rollback expectations

1. Inventory the existing stacks/resources and ownership; do not recreate the
   shared metadata table, Cognito pool, Gateway, Memory, or source bucket by
   default.
2. Review synthesized IaC and an explicit CloudFormation change set. Confirm
   IAM resources are scoped to the exact table, key prefixes, bucket prefixes,
   model/Guardrail IDs, Gateway ARN and Memory ARN.
3. Deploy the smallest synthetic/public beta fixture only after the cost gate.
   Validate HTTPS, OAuth, exact origin, API throttling, durable state and
   cleanup before enabling broader traffic.
4. Roll back through the previous immutable Lambda/image and CloudFormation
   change set. Preserve the metadata table and shared identity/resources unless
   deletion is explicitly approved.
5. Keep a release record containing commit, template hashes, resource IDs,
   configuration fingerprints, alarms, budgets, retention and rollback target;
   exclude tokens, documents, prompts, answers and secrets.

## Teardown and recovery

- Stop application traffic/inference before deleting temporary fixtures.
- Remove only recorded synthetic objects, sidecars, vectors, state records,
  temporary schedules, alarms and deployment artifacts.
- Reconcile `PENDING_UPLOAD`, `PENDING_INGESTION`, expired grants and stale
  operation records with bounded authorized queries; TTL alone is insufficient.
- Verify final state independently; a delete request or stack deletion is not
  proof that dependent S3 versions, vector entries, log groups, schedules or
  artifacts are gone.
- Preserve shared Cognito/Gateway/Memory/table resources unless an explicit
  owner-approved teardown says otherwise.

## Release stop conditions

Stop before any additional AWS mutation or billable operation if the inventory
is incomplete, the numeric envelope is missing, a requested resource is not
covered by IaC, edge/origin settings are broader than approved, cleanup is
unbounded, or rollback/retention ownership is unclear. Keep the verdict
`NOT_READY_FOR_PROD` until every Phase 14 gate in
`docs/production-readiness.md` has attached evidence.
