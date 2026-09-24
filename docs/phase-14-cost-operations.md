# Phase 14 — cost envelope and operations gate

## Status and authority

This is a planning artifact for the approved authenticated public beta
architecture. It does not authorize AWS calls, resource creation, real-model
inference, or promotion to `prod`. The beta accepts only fictional/public
documents and pre-provisioned authenticated users.

No numeric spend ceiling is claimed by this document. A numeric ceiling,
retention period, request budget, and teardown target must be approved before
AWS deployment, ingestion, inference, or the real-model holdout. Local
implementation and provider-double tests remain permitted.

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
  AWS Budget alert. The operations candidate does not create an SNS topic or
  dashboard. The Budget is account-wide so it does not silently omit untaggable
  Bedrock/Marketplace spend. Alarm and log retention charges are still possible.
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
ingestion starts, 100 Harness calls, and 500 Gateway calls by default. The
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
on-call destination exists; an existing SNS topic can be passed explicitly
without creating a new topic. The Budget's direct email is only an alert.

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

Stop before AWS if the inventory is incomplete, the numeric envelope is
missing, a requested resource is not covered by IaC, edge/origin settings are
broader than approved, cleanup is unbounded, or rollback/retention ownership
is unclear. Keep the verdict `NOT_READY_FOR_PROD` until every Phase 14 gate in
`docs/production-readiness.md` has attached evidence.
