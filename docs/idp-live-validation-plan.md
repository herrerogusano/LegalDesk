# Phase 14 IDP live-validation plan

Status: read-only execution plan. This document does not bootstrap resources,
write AWS state, invoke Bedrock/Textract, or deploy a change set. Live
validation remains a separately approved, cost-bearing operation.

The authoritative functional requirements are in
[`PHASE_14_IDP_PLAN.md`](../PHASE_14_IDP_PLAN.md), especially Sections 7–12 and
Milestone 14.5. The current repository evidence is offline/provider-neutral;
it is not an AWS E2E result.

## 1. Inventory boundary

The following are repository-recorded dependencies, not fresh live claims. The
operator must re-read stack outputs and resource inventories immediately before
any change set; this plan deliberately does not invent ARNs, URLs, bucket
names, client IDs, or table names.

| Existing dependency | Repository evidence | Required read-only proof |
| --- | --- | --- |
| Public application edge | `infra/cloudformation/phase-14-public-edge.yaml`, `docs/phase-14-public-edge.md` | `LegalDeskPhase14PublicEdge` status, Lambda code hash/version, API integration and rollback version |
| Malware/verified-clean boundary | `infra/cloudformation/phase-14-document-security.yaml` | `LegalDeskPhase14DocumentSecurity` status, exact clean-event route, queue URL/ARN and DLQ |
| Metadata/source data plane | Phase 02 parameters and `backend/src/legaldesk/documents.py` | exact existing Dynamo table ARN/key schema and source-bucket ARN; no replacement table/bucket |
| Gateway, Cognito and MCP | `infra/cloudformation/phase-08-gateway-mcp.yaml`, `infra/cloudformation/phase-10-identity.yaml`, `docs/idp-review-contract.md` | existing Gateway URL/ARN, allowed client/scope configuration, resource-server scope and target tool contract |
| Harness/Memory/KB/Guardrail | `docs/phase-14-public-edge.md`, `infra/cloudformation/phase-14-public-edge.yaml` | exact retained ARNs/IDs only where the smoke uses them; no new Harness, Memory, KB or Guardrail |
| Release/CD boundary | `infra/cloudformation/cd-iam.yaml`, `docs/continuous-delivery.md` | current CD role remains code-only; it cannot bootstrap Phase 14 IDP resources or secrets |

The candidate IDP stack is
[`infra/cloudformation/phase-14-idp.yaml`](../infra/cloudformation/phase-14-idp.yaml).
Its relevant logical resources are `IDPWorkQueue`/DLQ,
`IDPOCRCompletionQueue`/DLQ, `TextractCompletionTopic`, the worker and OCR
continuation Lambdas, their execution roles, event-source mappings, and the
purpose-specific resource server/client. Conditions default processing and
review dispatch off. The candidate reconciliation stack is
`infra/cloudformation/phase-14-reconciliation.yaml`; its schedule and IDP
recovery parameters require a separate inventory/change-set review.

The source bucket is also the default IDP artifact location in the candidate
template, under the isolated `idp-artifacts/` prefix. Before enabling this
choice, verify encryption, versioning, lifecycle, and IAM prefix conditions;
otherwise supply an already-approved artifact bucket. A blank artifact-bucket
configuration is not evidence that a bucket exists.

## 2. Mandatory preconditions and STOP gates

Stop before any paid call or state-changing change set if any condition is
false:

1. The exact metadata table, canonical source bucket, existing Gateway and
   Cognito pool/client/resource-server records are not identified from read-only
   inventory. Do not substitute guessed values.
2. The packaged worker/OCR artifacts, SHA-256 manifest, dependency versions and
   prompt bundle hashes are available in the approved immutable artifact bucket.
   The existing code-only CD workflow is insufficient for this bootstrap.
3. If review dispatch is being enabled, the dedicated IDP Cognito M2M client
   must already exist. Only then may the operator transfer its secret
   programmatically, directly into the SSM `SecureString` named by
   `M2MSecretParameterName`. The secret value must never appear in
   CloudFormation parameters, logs, reports, shell history, or test output.
4. `EnableIDPProcessing` and `EnableIDPReviewDispatch` are false in the initial
   change set, and no event-source mapping, schedule or reserved concurrency is
   active before the explicit enablement review. A new reconciliation schedule
   requires its own recurring-cost and scope review; an already deployed,
   separately approved schedule is inventoried and bounded rather than treated
   as an unapproved new resource.
5. The exact EU Sonnet 4.6 inference profile, its provider-controlled routing
   destinations, Textract Ireland support, SNS topic/subscription identity and
   Lambda execution-role policies are read and match the reviewed template.
6. The account billing alert/credit state and an owner-approved incremental
   budget are visible. Existing retained-resource charges are not silently
   attributed to this smoke.

These are plan gates, not requests to broaden the CD role. Missing resource
bootstrap, missing machine-secret bootstrap, or an enabled unbounded scheduler
is a mandatory STOP under the Phase 14 security and recurring-cost gates.
Pricing uncertainty must still be reported and bounded against the approved
ceiling; it is not by itself a new approval gate when the already-approved
small-cost envelope covers the conservative estimate.

## 3. Minimal approved change-set order

No step below is executed by this document.

1. Capture a read-only inventory and immutable rollback references for the
   existing stacks and dependencies in Section 1. Save IDs, statuses, hashes
   and parameter fingerprints, never credentials or document bodies.
2. Build the Python 3.12 worker/OCR packages offline with
   `scripts/package_release.py`; verify the manifest, prompt files and import
   set. Uploading those packages is a separate approved artifact operation.
3. Deploy the IDP infrastructure candidate with processing and review dispatch
   disabled, using only verified existing dependency values. This creates the
   dedicated resource server/client while mappings and invocation remain off.
   Confirm no event source mapping is enabled and no Lambda reserved
   concurrency is consumed.
4. After the dedicated client exists, transfer its secret programmatically,
   directly into the exact SSM `SecureString` parameter (no command output or
   logs). Configure the existing Gateway with the exact client, scope, purpose
   and target, then verify metadata only; never call `GetParameter` with
   decryption for a report or log.
5. If reconciliation is needed, use a separately reviewed change set with a
   bounded, server-owned tenant/matter allowlist and a disabled schedule until
   the recovery path is explicitly approved. Do not let the existing code-only
   CD path imply that this resource bootstrap occurred.
6. Perform the read-only proofs in Section 5. Only after those proofs and an
   explicit cost approval may an operator enable IDP processing. Review dispatch
   remains a second opt-in.
7. Run the smoke envelope in Section 4. Preserve metadata-only results and
   immutable rollback references. Do not retry an ambiguous paid call.

## 4. Bounded evaluation and smoke envelopes

The manifest currently contains 18 synthetic PDFs: five contracts, five
demands, five judgments, three UNKNOWN/adversarial/negative cases, 22 total
pages, and eight scanned pages. The corpus is fictional and must remain the
only live input. `tests/fixtures/idp/manifest.json` is the independent oracle;
its source text is never used as production OCR evidence.

### Offline gate (no AWS)

Run the full 18-fixture corpus through local/fake-provider tests first. This
has no model, OCR, S3, DynamoDB or Gateway cost and is the release prerequisite
for any live envelope.

### Real-model evaluation ceiling

| Counter | Hard ceiling | Enforcement |
| --- | ---: | --- |
| Fixtures | 18 | manifest allowlist; reject unknown IDs/duplicates |
| Bedrock calls | 36 total; 2 per fixture | classifier + extractor only; no automatic paid retry |
| Input tokens | 200,000 total | reserve before each call; stop before the next call |
| Output tokens | 46,080 total | current runtime request caps are 512 classifier + 2,048 extractor per fixture; the aggregate is a plan-only runner guard and must be enforced by the runner, not assumed from provider defaults |
| OCR source PDFs/pages | 8 PDFs / 11 pages minimum | scanned and mixed PDFs are sent as whole PDFs to Textract; count actual manifest PDF pages, not only pages labelled scanned |
| OCR pages hard cap | 22 total for this corpus | all manifest pages are the conservative upper bound when image/logo heuristics require extra OCR; stop on extra coverage |
| Textract page API calls | 32 maximum | four per callback/job; no Lambda polling |
| SDK/provider retries | 0 | `total_max_attempts=1`; ambiguous outcome is terminal/recoverable, not replayed |
| SQS receive attempts | at most 3 per queue message | candidate redrive policy; any deterministic DLQ case stops the run |
| Observation polls | 20 maximum per bounded status wait | 15-second spacing, no production wait loop |
| Wall time | 60 minutes for the entire evaluation | harness deadline; abort and preserve partial metadata |

The token ceiling is an execution guard, not a claim about provider metering;
actual input/output usage must be recorded from provider metadata. No prompt,
document text, secret, or raw model response enters the evaluation report.

### Minimal AWS smoke ceiling

Use at most six synthetic cases: one digital contract, one scanned demand, one
mixed judgment with a review-required interpretive field, one UNKNOWN, one
oversize/corrupt skip/failure case, and one duplicate/replay of an already
processed case. The last two must not make a paid model call.

| Counter | Hard ceiling |
| --- | ---: |
| Bedrock calls | 8 total (four eligible documents × classifier/extractor) |
| Input/output tokens | 80,000 / 10,240 total |
| OCR | 4 pages and 16 `GetDocumentTextDetection` observations maximum |
| RAG fallback retrievals | 2 total; no IDP write-back |
| Selected-document structured queries | 2 total |
| Review task creation | 1 successful machine-created task; no human JWT fabrication |
| Human decisions/history | 2 decisions; history read covers at least 3 chronological runs |
| Cross-matter probes | 2 denied requests; neither may reach S3, OCR or Bedrock |
| SQS receives | 3 per message, with partial-batch failure reporting |
| Status observations | 20 per job, 15 seconds apart maximum |
| Total wall time | 30 minutes; individual Lambda limits remain 360 seconds |

The smoke must prove: verified-clean promotion triggers one durable job; digital
and OCR paths preserve the same source hash; UNKNOWN and skip/failure preserve
RAG; duplicate delivery reuses committed artifacts without a paid replay;
changed/deleted source fails closed; review dispatch uses the existing Gateway
and exact purpose-bound machine client; cross-matter access is denied before
target access. It must additionally include: a failed/skip IDP case with a
working existing RAG fallback; one authorized human review task with
approve/correct decisions and chronological history covering at least three
runs; and a selected-document IDP-first query followed by the selected-
document RAG fallback. A successful local composition is not AWS E2E evidence.

## 5. Read-only API proof before enablement

The operator should collect only bounded metadata through read-only APIs:

- CloudFormation: `DescribeStacks`, `ListStackResources` and `GetTemplate` for
  stack status, parameters with NoEcho values redacted, outputs, resource
  lists and template comparison. Do not start a drift-detection operation in
  this read-only preflight.
- Lambda: function configuration, code SHA/version, timeout, concurrency,
  environment **names and safe non-secret values only**, event-source mappings,
  and log-group retention. Never dump environment values containing tokens.
- SQS/SNS: queue attributes, visibility/redrive values, topic/subscription
  confirmation and exact source ARN policies. Do not publish test messages yet.
- Cognito/Gateway: user-pool/client/resource-server metadata, exact allowed
  client/scope and token-use contract, Gateway target/tool schema and URL. Do
  not mint a token in preflight and do not invoke the Gateway.
- IAM/SSM: role policy simulation or policy-document inspection limited to the
  reviewed principal/resource set; `DescribeParameters` only for the M2M
  parameter. Do not decrypt or print the secret.
- Bedrock/Textract: inference-profile/model metadata and region/API support
  only. Do not call Converse, StartDocumentTextDetection or GetDocumentTextDetection.

The proof record must contain request type, timestamp, region, resource ID
digest/ARN suffix where safe, and pass/fail reason. It must not contain raw
legal text, credentials, tokens, model prompts or response bodies.

## 6. Cost envelope (USD, before tax)

The current reviewed Sonnet 4.6 Standard assumption is **$3.30 per million
input tokens and $16.50 per million output tokens** for the approved EU geo
profile. Recheck the regional/provider terms immediately before execution:
[AWS Bedrock pricing](https://aws.amazon.com/bedrock/pricing/) and the
[Sonnet 4.6 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html).

Under the real-model ceiling, the model-only upper estimate is:

`200,000 × $3.30 / 1,000,000 + 46,080 × $16.50 / 1,000,000 = $1.42032`.

For the six-case smoke it is at most:

`80,000 × $3.30 / 1,000,000 + 10,240 × $16.50 / 1,000,000 = $0.43392`.

Textract is page-priced. The verified public Price List snapshot for
`eu-west-1` (published 2026-09-11) identifies SKU
`HV3WZRSJSH5TPZZ`, `EU-AsyncTextPagesProcessed`, at **$0.0015/page** for the
first million pages. The source is the regional Price List endpoint
[`AmazonTextract/current/eu-west-1/index.json`](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonTextract/current/eu-west-1/index.json);
recheck it immediately before execution. Thus the conservative whole-corpus
OCR allowance is `22 × $0.0015 = $0.033`, and the honest totals are
`$1.45332 + metered data-plane requests` for evaluation and
`$0.43992 + metered data-plane requests` for the six-case smoke (using the
four-page smoke cap). A missing or changed regional rate is reported as
uncertainty, not zero; it is not an automatic STOP when the owner-approved
small-cost ceiling covers the conservative allowance. Stop before execution if
the plausible rate/spend could exceed that ceiling or materially change the
approval. See [Amazon Textract pricing](https://aws.amazon.com/textract/pricing/).

S3, DynamoDB, SQS, SNS, Lambda, CloudWatch, Cognito and Gateway requests,
retention and existing stacks are variable or already-retained costs and are
not silently folded into the model estimate. Obtain the owner-approved total
ceiling and billing alarm before execution; this document does not authorize
paid inference, OCR or new infrastructure.

## 7. Rollback and evidence

- Before enablement, revert the IDP change set or leave the disabled stack in
  place; do not delete shared source, metadata, RAG or Gateway resources.
- During an incident, disable the IDP event-source mappings and review dispatch,
  stop the bounded reconciliation schedule, and preserve queues/DLQs for
  diagnosis. Do not replay ambiguous model calls or delete canonical uploads.
- Application-code rollback uses the existing immutable Lambda artifact/CD
  procedure and its recorded prior code hash. IDP infrastructure rollback is a
  separate reviewed CloudFormation change set; the code-only CD role must not
  be broadened to perform it.
- Preserve metadata-only smoke/evaluation reports, observed token/OCR counts,
  status/reason codes, stack change-set IDs, artifact hashes and rollback
  references. Raw documents, page text, prompts, tokens and secrets never enter
  reports.

The live result must explicitly distinguish: read-only inventory, static IaC
validation, local/fake-provider integration, real Bedrock evaluation, real
Textract continuation, and deployed AWS E2E. None may be inferred from another.
