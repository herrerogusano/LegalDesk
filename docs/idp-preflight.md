# IDP extension — preflight, 2026-10-09

Status: **Service-principal gate approved on 2026-10-09; implementation in progress**.
Authoritative requirements: `PHASE_14_IDP_PLAN.md`. This extends the existing
LegalDesk application; it does not replace `PLAN_14_PUBLIC_BETA.md` or rename
historical release evidence. Base: developer `789af7ec58813bc8ebc88c5e3d6534a302ce81f4`.
At preflight completion no IDP application code, infrastructure or model calls
had been implemented. Subsequent milestone evidence is recorded separately.

## Approved gate resolution — 2026-10-09

After clarification that this is permanent product functionality, not a test
identity, the owner explicitly selected option 1. Implement a dedicated Cognito
M2M client/scope through the existing Gateway for automatic IDP review creation
only. Preserve all human routes and forbid impersonation, direct Lambda bypass,
general machine tool access and machine approval/correction. ADR-020 records
the approved boundary. Token issuance and deployment must remain bounded;
approval does not imply unlimited fan-out or an unbounded evaluation budget.

## Reuse and additions

| Component | Verified existing seam | Planned addition / impact |
| --- | --- | --- |
| Upload | `documents.py:487` verifies server-owned object; public uploads remain quarantined | Do not enqueue paid IDP from browser confirmation |
| Clean promotion | `malware_scan.py:143-192` verifies clean verdict/content, copies canonical object, commits UPLOADED | Durable job/outbox at trusted clean promotion; recover a commit-to-enqueue failure |
| Storage | `documents.py:978`, `docs/data-model.md`; composite pk/sk and bounded Query | Separate IDP run/correction items and slim document pointer; no Scan, GSI or destructive migration proposed |
| RAG | `ingestion.py`, `retrieval.py`, `chat.py` | Keep indexing independent; selected-document structured lookup with cited RAG fallback |
| Explicit tools | `gateway_client.py:170-269`, ADR-016 | Gateway remains mandatory; dedicated IDP identity approved, not deployed |
| Review | `http_app.py:975-1108`, `review_tasks.py:618-839`, ADR-017 | Add IDP snapshot kind and per-field correction records without changing existing answer-review semantics |
| Release | `.github/workflows/production-cd.yml`, `scripts/deploy_release.py` | Existing CD is code-only; IDP infrastructure needs a separately reviewed bootstrap/diff, not broadened code-release credentials |

Paths above are under `backend/src/legaldesk/` unless specified otherwise.

## Physical Option B fit (proposed, not implemented)

Keep `pk=TENANT#{tenantId}#MATTER#{matterId}` and existing `DOCUMENT#{documentId}`.
Add `IDP#RUN#{documentId}#{runId}` and
`IDP#CORRECTION#{documentId}#{correctionId}` records. Point-read the authorized
document/latest pointer; bound history Query by the exact document prefix.
Resolve current values with applicable human-confirmed corrections taking
precedence over newer unreviewed runs. Keep large text/evidence artifacts in
private encrypted S3, outside Knowledge Base inclusion prefixes. The current
source bucket is not versioned: immutable IDP snapshots/content digests must
bind provenance; do not pretend existing VersionId values are guaranteed.

## Actual AWS configuration checked (read-only)

- Existing public-edge stack: UPDATE_COMPLETE; application Lambda Active,
  LastUpdateStatus Successful.
- Resolver and writer use the existing EU inference profile for Claude Sonnet
  4.6. AWS documents structured output through bedrock-runtime; local strict
  schema/evidence validation is still mandatory. Context capacity is not an IDP
  budget. Existing resolver/writer prompt and token settings are not IDP settings.
- Existing AgentCore Gateway: READY, CUSTOM_JWT, Cognito discovery, two allowed
  app clients, scope `legaldesk/use`. No IDP machine principal was observed or added.
- Existing upload/read allowance is 10 MiB, PDF and text. Proposed IDP cap remains
  configurable 20 MB / 100 pages, without silently widening upload permissions:
  the effective public path still has the lower upload cap. Text can remain
  usable by RAG while IDP skips it. This distinction must be explicit in tests.
- Textract supports async text detection and SNS/SQS continuation in Ireland;
  its PDF async limits exceed the proposed IDP limits. Password-protected PDFs
  are unsupported. English/Spanish text detection does not imply Spanish
  handwriting support.

No stack mutation, inference, OCR, ingestion, business-data access or paid cost
API call was made. Metadata management reads were used for this preflight.

## Mandatory gate: automatic review cannot use the human binding

`gateway_client.py:205-225` requires a sealed binding with a bearer token.
`http_app.py:1047-1066` requires an accepted chat-answer snapshot to create a
review. ADR-016/017 bind that path to a verified user and reauthorization. The
IDP worker has neither a human token nor an accepted chat answer. Forging either
or invoking the Review Lambda directly is prohibited.

Options requiring an explicit decision:

1. **Recommended: purpose-specific Cognito M2M principal through the existing
   Gateway.** Dedicated client/scope, short-lived tokens, tightly allowlisted IDP
   review creation only, persisted run/field/evidence lookup and service-actor
   audit. Interceptor and target must reauthorize the job scope; machine grants
   must not be convertible to human sessions, general tools or field approval.
   Reuse business logic via a versioned IDP snapshot adapter, never a fabricated
   accepted-answer snapshot. Update ADR only after approval. Adds token-request
   and secure credential-management costs; regional costing and bounded
   deployment/smoke envelope must be established before activation.
2. **Deferred human initiation.** Persist REVIEW_REQUIRED and let an authorized
   human request an IDP review through Gateway. No machine auth client, but
   automatic task creation from the plan is not met and the scope change needs
   approval. It still needs the additive IDP snapshot/correction contract.

Per-field human approve/correct/reject is an additive extension of the existing
review workflow, not proof that today's resolution-note contract already does
it. Preserve old clients, immutable correction audit and human JWT authorization.
If this cannot remain backward-compatible, invoke the separate product-contract
STOP gate before implementation.

## Proposed file/dependency map after approval

1. `backend/src/legaldesk/idp/`: registry, runs/jobs, repository, evidence,
   page-text adapter, bounded classifier/extractor, derived rules.
2. New isolated IDP IaC: SQS/DLQs, worker/continuation Lambda, Textract SNS
   completion, exact-resource policies, logs/alarms; optional M2M client/scope
   only with approval. Variable requests/storage/compute/AI costs, not free.
3. Clean-promotion job/outbox producer, independent idempotency/checkpoints and
   bounded delivery recovery; never rely on a successful DynamoDB commit to
   imply SQS delivery.
4. Additive metadata/IDP query and human correction adapters; existing Gateway
   tools and RAG response/citation contracts remain intact.
5. Versioned IDP prompts, synthetic dataset, local/security/regression tests,
   frozen real-model evaluation and bounded AWS E2E with measured usage.

Preflight validation used source inspection and AWS configuration reads only.
The requested phase is **not complete**.

## Local implementation checkpoint — 2026-10-09

Provider-neutral foundations now exist under `backend/src/legaldesk/idp/`:
versioned schemas, separate job/run state, scoped DynamoDB repository, delivery
recovery seams, conditional claims, page-aware PDF acquisition, asynchronous OCR
contracts, strict output/evidence validation and provisional calendar rules.
IDP prompts are separate versioned artifacts and explicitly included in the
release package. The existing public upload limit is unchanged.

The evaluation corpus contains 15 synthetic supported documents and three
additional UNKNOWN/adversarial/negative documents, including English/Spanish,
scanned and mixed-page examples. Expectations were authored separately from
the extractor; the supervisor reviewed selected values, anchors and rendered
pages. This is not legal-expert or human ground-truth certification.

| Validation | Actual result | Evidence boundary |
| --- | --- | --- |
| IDP foundation, processing, dataset and independent adversarial suites | 49 passed | Local deterministic/fake-provider tests, including an offline real-SDK serialization capture |
| Corpus presentation | 22 rendered pages inspected | Readability only; no OCR quality claim |
| AWS IDP inference/OCR/deployment | Not executed | No real-model or AWS E2E acceptance evidence |

Remaining: production S3/clean-promotion delivery integration, durable stage/OCR
adapters, bounded Bedrock/Textract calls, SQS/Lambda/DLQ infrastructure, approved
M2M Gateway review path, human corrections, query integration and real evaluation.
The current modules are not yet an activated production IDP feature. In
particular, local queue/OCR interfaces do not prove durable cloud execution.

## Static infrastructure review checkpoint — 2026-10-09

`infra/cloudformation/phase-14-idp.yaml` defines the isolated asynchronous
resources and purpose-specific Cognito machine client, with processing disabled
by default. The supervisor independently ran the eight parsed infrastructure
contract tests and offline cfn-lint successfully. A read-only
`GetInferenceProfile` confirmed the existing EU Sonnet profile is ACTIVE and its
six exact foundation-model destinations match the proposed IAM list.

This is not deployment evidence. Producer/reconciliation integration, real
handler composition, the Gateway machine-review adapter, human correction,
query/UI integration and live evaluation remain in progress. Component tests
initially missed production composition defects in source-hash lookup,
checkpoint transitions and OCR continuation; independent runtime-path tests
are being added before acceptance. No IDP resource or paid inference/OCR call
has been activated by this checkpoint.

## Production-composition review checkpoint — 2026-10-09

The local production composition now connects clean promotion, durable dispatch,
the existing bounded reconciler, canonical S3 reads, the Bedrock adapters, async
Textract continuation and immutable extraction persistence. Independent tests
exercise the real handlers/processor against fake transports rather than a
processor stub that simply returns success.

Supervisor verification: 14 runtime-contract tests, eight generation/recovery
tests, and the broader `test_phase14_idp*.py` discovery (99 tests) passed locally.
These counts overlap and must not be added together. Coverage includes changed
or deleted source content after extraction, corrupt PDFs, duplicate review-required
jobs, prompt drift, crash-after-save recovery and cross-method generation fences.
Both classifier and extractor Converse requests also passed botocore input-shape
validation offline using their default repository prompt discovery. This checks
SDK request structure, not model availability or service acceptance of a schema.

This remains offline evidence. Timeout/lease coordination and the approved M2M
Gateway integration are under review; human correction, structured query/UI,
real model evaluation and deployed AWS E2E are not yet accepted. No IDP deployment
or paid inference/OCR has occurred at this checkpoint.

## Disabled implementation checkpoint — 2026-10-09

Supervisor full offline discovery passed: **734 tests, one platform-specific
skip**. The machine contract suite includes actual interceptor/Review target and
scheduled reconciler composition, canonical bytes with empty S3 metadata,
cross-tenant/matter denial, strict machine/human separation, duplicate creation,
ambiguous dispatch recovery and no resend after `SENT`. Repository-name-based
authorization exceptions are prohibited; missing deployment scope fails closed.

The M2M route uses the existing `create_review_task` tool with additive opaque
invocation references and separate short-lived machine grants. Infrastructure
gates remain disabled by default. Existing human review behavior is preserved.
Human field-decision/query helpers are still contracts, not connected UI/API
functionality. Chronological history/effective-run resolution, including recovery
across more than two reruns of one document, needs the next integration review.
This checkpoint does not satisfy real model quality or deployed E2E acceptance.

## M2M infrastructure review checkpoint — 2026-10-09

The optional Gateway client/scope and existing Review target now have matching
machine-creation configuration. New canonical source reads are restricted to
the configured tenant and one or two matters, and only `original.pdf`/`original.txt`.
The bucket ARN is derived from its configured name. Incomplete configuration
does not add a machine client to the Gateway authorizer; runtime checks also deny.
Worker invocation writes and exact SSM secret reads are separately opt-in, and
do not grant human capabilities or direct target invocation.

The previous cfn-lint 1.39.1 catalog did not recognize valid AgentCore resource
types/actions. Release tooling is now pinned to 1.57.2. Supervisor strict lint
passed for the Review, Gateway, IDP, reconciliation, public-edge and CD-IAM
templates in `eu-west-1`, without resource/action waivers. Focused independent
checks passed: four M2M IaC tests, one tooling-consistency test, and four release
packaging tests (one Windows symlink-capability skip). Packaging checks include
both IDP entry points and byte-identical versioned prompt artifacts.

These are local validation results, not a CloudFormation change set or deployed
authorization proof. No IDP AWS resources were deployed and no paid model/OCR
calls were made. Human integration/history/query and real evaluation remain open.

## Primary references checked

- [Sonnet 4.6 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-sonnet-4-6.html)
- [Bedrock structured output](https://docs.aws.amazon.com/bedrock/latest/userguide/structured-output.html)
- [Textract async operations](https://docs.aws.amazon.com/textract/latest/dg/api-async.html)
- [Textract document limits](https://docs.aws.amazon.com/textract/latest/dg/limits-document.html)
- [Textract regions](https://docs.aws.amazon.com/general/latest/gr/textract.html)
- [Lambda SQS failure handling](https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html)
- [Cognito M2M pricing](https://aws.amazon.com/cognito/pricing/)
- [M2M app-client fixed charge removal](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-cognito-removes-machine-machine-app-client-price-dimension/)
