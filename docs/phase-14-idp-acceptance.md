# Phase 14 IDP acceptance

**Acceptance: ACCEPTED (bounded constrained-beta scope) — 2026-10-13**

Final delivery required by `PHASE_14_IDP_PLAN.md` sections 14 and 15. Region
`eu-west-1`, account `344774635844`. No real legal data was used; every document
is a synthetic fixture. Live evidence is under `evals/results/idp-live-*`
(run 2026-10-13); final AWS detail is in `docs/idp-deployment-evidence-2026-10-09.md`
"Final AWS acceptance evidence (2026-10-13)". Local or mocked tests are never
counted as AWS E2E (plan section 12). Scope: beta tenant, two matters,
synthetic data; known limits are in section 10.

## 1. Change summary

Independent IDP pipeline added to the existing LegalDesk application
(ADR-020), without changing upload, malware scan, indexing or RAG:

- Registry-driven schemas (CONTRACT, DEMAND, JUDGMENT, UNKNOWN, version
  `1.0.0`) in `backend/src/legaldesk/idp/registry.py`; field acceptance,
  evidence checks and the deterministic `DerivedMetadataEngine`
  (`rules.py`: `ADD_CALENDAR_MONTHS_V1`, `ADD_CALENDAR_YEARS_V1`).
- Async processing: SQS work queue and DLQ, worker Lambda, conditional durable
  claims, step checkpoints before paid calls, Textract async OCR continuation
  through SNS and an SQS completion queue.
- Dedicated Cognito M2M client and scope for machine review creation through
  the existing AgentCore Gateway; human review, correction and history remain
  JWT/matter-authorized.
- IDP-first structured answers with RAG fallback; bounded operator tools
  (`scripts/reprocess_idp.py`, `scripts/verify_idp_duplicate.py`).
- Commits `6b16c17..HEAD` (39 before this record) cover the above hardening, the live
  smoke fixes listed in section 8 and the rotation tooling.

## 2. Architecture (deployed components)

Verified against `infra/cloudformation/phase-14-idp.yaml`,
`phase-14-reconciliation.yaml`, `docs/idp-deployment-evidence-2026-10-09.md`
and the live reports of 2026-10-13. All nodes shown have live evidence.
Final releases: all 8 Lambdas on `30adeac`; Public Edge application on
`89b84ca`. The reconciliation role additionally has read-only
`bedrock:GetKnowledgeBaseDocuments`.

```mermaid
flowchart LR
  U[Verified upload + GuardDuty clean promotion<br/>Phase14DocumentSecurity] -->|conditional IDP job + send| WQ[(IDPWorkQueue / DLQ<br/>maxReceive 3)]
  REC[Reconciliation Lambda<br/>EventBridge rate 15 min] -->|recover dispatch + review| WQ
  WQ --> W[IDPWorkerFunction<br/>digital text, Bedrock classify/extract<br/>EU Sonnet 4.6 profile]
  W --> BR[Bedrock EU inference profile]
  W -->|scanned/long PDF| TX[Textract async<br/>StartDocumentTextDetection]
  TX --> SNS[TextractCompletionTopic]
  SNS --> OQ[(IDPOCRCompletionQueue / DLQ)]
  OQ --> OC[IDPOCRContinuationFunction]
  OC --> W
  W --> DDB[(DynamoDB: run, job, projection)]
  W --> S3[(S3 idp-artifacts/ page artifacts)]
  W -->|machine review, M2M token| GW[AgentCore Gateway<br/>request interceptor]
  REC -->|review dispatch recovery| GW
  GW --> RT[Review Task Lambda]
  RT --> DDB
  B[Browser] --> PE[Public Edge<br/>CloudFront / API Gateway / app Lambda]
  PE --> GW
  GW --> MCP[Metadata MCP Lambda]
  MCP --> DDB
  MCP --> S3
  PE -. fallback .-> KB[RAG Knowledge Base]
```

Not shown because not an IDP component: Cognito user pool (existing),
SSM SecureString for the M2M secret (read by worker/reconciliation only when
review dispatch is enabled). Textract continuation was proven live (recovery
report); the RAG fallback node is pre-existing and was exercised live.

## 3. Final acceptance checklist (plan section 14)

| # | Item | Status | Evidence |
|---|---|---|---|
| 1 | Supported documents classify correctly; UNKNOWN not forced | EVIDENCED | Real model: 18/18, 3/3 UNKNOWN (`idp-eval-20261013-c-summary.json`). Live: CONTRACT (`-digital-h`), JUDGMENT (`-judgment-provisional-a`), DEMAND scanned (`-scanned-demand-b`), UNKNOWN invoice (`-unknown-c`, `-insufficient-a`) |
| 2 | Fields/schema versions added without changing control flow | EVIDENCED (local tests) | Registry is data-driven; `test_registry_is_versioned_...`, `test_subtype_vocabulary_is_registry_data_not_control_flow`. The subtype vocabulary change (`4144703`) needed no pipeline change |
| 3 | Accepted fields have valid traceable evidence; fabricated anchors rejected | EVIDENCED | 139/139 anchors digest-exact, 0 auto-accepted not found (`-c-summary.json`); subtype finding closed with the real model: contracts 01/05 now `ABSENT`/`UNAVAILABLE`, NDA still `AUTO_ACCEPTED` (`idp-eval-20261013-d{,-report}.json`); fabricated-anchor tests |
| 4 | Literal / derived / provisional distinct in persistence and responses | EVIDENCED | Per-field `origin`/`acceptance` in live reports (`-digital-h`: `estimated_anniversary_date` DERIVED/PROVISIONAL); live provisional judgment field (`-judgment-provisional-a`, provisional expectation met) |
| 5 | Deterministic date rules with input provenance and tests | EVIDENCED | `rules.py` unit tests (month-end, leap); live derived provenance in smoke `h` (rule `ADD_CALENDAR_MONTHS_V1`, inputs recorded) |
| 6 | Human review safe, history preserved, reprocess never overwrites human value | EVIDENCED | Human approve via Gateway (smoke `h`); human decision precedence over a newer machine run; 3-run same-document history live (`-history-b`, count 3, EXECUTED); `docs/idp-review-contract.md` |
| 7 | Async SQS/Lambda, retries, idempotency, DLQ, Textract continuation | EVIDENCED | Duplicate replay PASS, zero paid calls (`-duplicate-b`); Textract continuation through SNS/SQS/Lambda, one DLQ message redriven, no repeated StartDocumentTextDetection (`-recovery-a`, `-scanned-demand-b`) |
| 8 | 20 MB / 100-page limits and skip without disabling RAG | EVIDENCED | `IDP_SKIPPED` live (`-skip-g`, operator read `UNSUPPORTED_MEDIA_TYPE`); RAG independence: KB per-document fix `36713c9` stops one unparseable document blocking indexing (siblings INDEXED, `-recovery-a`). Answer-level limitation in section 10 |
| 9 | IDP first, RAG fallback, insufficient evidence; provisional not certified | EVIDENCED | IDP-first (`source: IDP`, `-digital-h`); RAG fallback when the IDP field is missing (`source: RAG`, 1 citation, `-rag-fallback-a`); `insufficient_evidence`, source NONE (`-insufficient-a`) |
| 10 | Cross-tenant/matter denied on all new entrypoints; malicious documents cannot command | EVIDENCED | Cross-matter 403 in digital, judgment, unknown, scanned, history and insufficient runs; adversarial UNKNOWN fixture not followed; contract/adversarial tests. Cross-tenant by local tests only |
| 11 | 15-document dataset, tests, AWS smoke evidence, real cost/usage documented | EVIDENCED | 18 fixtures, final evaluation, live scenario families, cost in section 7 |
| 12 | No unjustified always-on infra, no unapproved contract changes, no open STOP gate | EVIDENCED | Section 7 (17 additions, no table/bucket/KMS/alarm); `docs/idp-preflight.md` gate resolution; ADR-020; no always-on cost added |

Plan section 14 scenario list (14.5), live on `eu-west-1`:

| Scenario | Result | Report |
|---|---|---|
| Digital contract | PASS | `idp-live-digital-20261013-h.json` |
| Scanned demand (Textract continuation) | PASS | `-scanned-demand-b.json`, `-recovery-a.json` |
| Judgment with provisional field | PASS | `-judgment-provisional-a.json` |
| UNKNOWN | PASS | `-unknown-c.json` |
| Skip limit | PASS for `IDP_SKIPPED` and KB independence; RAG answer limited (section 10) | `-skip-g.json`, `-skip-h.json` |
| Deliberately failed IDP, RAG working | IDP fails closed (PASS); RAG routing proven, answer blocked by guardrail (section 10) | `-failed-idp-rag-d.json`, `-e.json` |
| Repeat-processing idempotency | PASS | `-duplicate-b.json` |
| RAG fallback / insufficient evidence | PASS | `-rag-fallback-a.json`, `-insufficient-a.json` |
| Authorized review, history, cross-matter denial | PASS | `-digital-h`, `-history-b` |

## 4. Schema, rule and example output

Registry version `1.0.0` (`backend/src/legaldesk/idp/registry.py`). Review-
sensitive fields are never auto-accepted without review policy.

| Class | Fields (review-sensitive in bold) |
|---|---|
| CONTRACT | parties, effective_date, explicit_expiration_date, initial_duration_value/unit, **automatic_renewal**, **renewal_period_value/unit**, **termination_notice_value/unit**, amount, currency, **jurisdiction**, **governing_law**, subtype (closed vocabulary: NDA) |
| DEMAND | claimants, defendants, court, case_number, filing_date, **claims**, claimed_amount, currency |
| JUDGMENT | court, case_number, decision_date, parties, **operative_ruling**, **costs_statement**, **appeal_information** |
| UNKNOWN | **document_type**, general_document_evidence |

Field state model: presence (`PRESENT`, `ABSENT`, `AMBIGUOUS`, `UNKNOWN`,
`NOT_APPLICABLE`), origin (`LITERAL`, `INTERPRETIVE`, `DERIVED`), acceptance
(`AUTO_ACCEPTED`, `PROVISIONAL`, `REVIEW_REQUIRED`, `HUMAN_CONFIRMED`, `REJECTED`, `UNAVAILABLE`). Evidence is
`(page, quote offsets, quoteDigest, contentSha256)`.

Derived rules: `ADD_CALENDAR_MONTHS_V1` and `ADD_CALENDAR_YEARS_V1`
(`estimated_anniversary_date` from `effective_date` + initial duration,
calendar-month end clamping, recorded `inputs` and `parameters`). Other units
(business days) are rejected, not guessed.

Sample extraction, `contract-01-en-digital-monthend` (real-model export
`idp-eval-20261013-c.json`; values omitted, the export stores digests only;
this is the pre-`4144703` run, so `subtype` reflects the finding in section 6):

| Field | Presence | Origin | Acceptance | Evidence anchors |
|---|---|---|---|---|
| effective_date | PRESENT | LITERAL | AUTO_ACCEPTED | 1 |
| initial_duration_value / unit | PRESENT | LITERAL | AUTO_ACCEPTED | 1 each |
| parties, amount, currency | PRESENT | LITERAL | AUTO_ACCEPTED | 1 each |
| automatic_renewal | PRESENT | INTERPRETIVE | REVIEW_REQUIRED | 1 |
| renewal / termination notice value, unit | PRESENT | LITERAL | REVIEW_REQUIRED | 1 each |
| governing_law, jurisdiction | PRESENT | LITERAL | REVIEW_REQUIRED | 1 each |
| explicit_expiration_date | ABSENT | LITERAL | UNAVAILABLE | 0 |
| estimated_anniversary_date | PRESENT | DERIVED | PROVISIONAL | 0 (rule `ADD_CALENDAR_MONTHS_V1` v1.0.0, inputs `effective_date`, `initial_duration_value`, `initial_duration_unit`) |
| subtype | PRESENT | LITERAL | AUTO_ACCEPTED | 1 (finding, section 6) |

Run status `REVIEW_REQUIRED`; 2 Bedrock calls, 2,525 input / 792 output tokens,
0 OCR pages, 7.7 s.

## 5. Authorization proof summary

- Cross-matter denial: smoke `h` (`idp-live-digital-20261013-h.json`) records
  `crossMatterStatus: 403` for a request against the other beta matter, with
  clean ephemeral identity cleanup; the same 403 recurs in the judgment,
  UNKNOWN, scanned, history and insufficient runs. Cross-tenant and other
  entrypoints are covered by local tests only (`test_phase14_idp_human_query_contract.py`,
  `test_phase14_idp_machine_contract.py`), not by AWS evidence.
- Deterministic authorization precedes retrieval and tool access: Gateway JWT
  validation, interceptor and tool-level re-authorization; the metadata path
  is backend -> Gateway -> MCP (phase 08 boundary unchanged; `docs/gateway-mcp.md`).
- Machine review (ADR-020, `docs/idp-review-contract.md`): dedicated Cognito
  resource server `legaldesk-idp` with scope `review-create` and a dedicated
  app client; the interceptor requires that client and scope, resolves the
  persisted run/document/tenant/matter, and issues a short-lived purpose-bound
  grant. No user impersonation, no human grants written by the worker, no
  approve/correct/close, no direct Review Lambda call. Smoke `h` evidences one
  idempotent machine-created task; human approval was done through the human
  route.
- Worker role has no Lambda invoke, Gateway invoke, DynamoDB Scan, source
  DeleteObject or blanket Bedrock permission (`docs/idp-bootstrap-runbook.md`
  "Permission and cost gates").

## 6. Evaluation metrics (real model, 2026-10-13)

Single run, prompts `1.0.2`, `eu.anthropic.claude-sonnet-4-6`, 18 fixtures
(CONTRACT 5, DEMAND 5, JUDGMENT 5, UNKNOWN 3). Source:
`docs/idp-evaluation.md`, `idp-eval-20261013-c{,-report,-summary}.json`.
Real Bedrock on stored fixtures (with a reused Textract artifact for scanned
pages); this is not AWS E2E.

| Metric | Result |
|---|---|
| Classification | 18/18, no off-diagonal; UNKNOWN forced into a class: 0 |
| Review rate | 16/18 (88.89%) |
| Fields compared | 156; 77 `AUTO_ACCEPTED` |
| Presence / value / origin / acceptance mismatches | 11 / 11 / 18 / 55 (4 presence and 11 value not comparable) |
| Anchor revalidation | 139/139 digest-exact; auto-accepted not found: 0 |
| Evaluator anchor-vs-oracle mismatches | 24 (valid alternate spans, not proven fabrication) |
| Expected-review field auto-accepted | 0 |
| Adversarial instruction followed | No |
| Wall time | 112.1 s total, 6.2 s mean, 10.2 s max per document |

Errors, stated plainly: 35 expected-auto fields became `REVIEW_REQUIRED` and 7
`PROVISIONAL` (conservative routing); 17 origin mismatches `LITERAL` expected vs
`INTERPRETIVE` observed. Earlier attempts `-a` (missing `botocore[crt]`) and
`-b` (missing `pypdf`) made zero calls and are kept as failed attempts.

**Subtype finding (closed).** For `contract-01-en-digital-monthend` and
`contract-05-en-mixed-explicit-conflict`, `subtype` expected `ABSENT` was
returned as a grounded document heading and `AUTO_ACCEPTED`. Fix `4144703`
restricts `subtype` to a closed vocabulary (`NDA`, EN/ES aliases). Re-run with
the real model (`a9fe4a7`, `idp-eval-20261013-d{,-report}.json`, 3 fixtures,
6 Bedrock calls, about USD 0.07): contracts 01 and 05 report `ABSENT` /
`UNAVAILABLE`, the NDA fixture still yields canonical `NDA` `AUTO_ACCEPTED`,
classification unchanged. The sample table in section 4 is the pre-fix run.

Earlier offline triage (`idp-offline-triage-20261013.json`): the three
`AUTO_ACCEPTED` duration-unit digest mismatches were an uncanonicalized spelling
fixed by `840b8c0`; 0 unresolved auto-accepted mismatches.

## 7. Cost and usage

Measured / estimated (owner-reviewed rates: Sonnet 4.6 EU profile USD
3.30 / 16.50 per million input / output tokens; Textract async text detection
USD 0.0015/page):

| Item | Quantity | Cost |
|---|---|---|
| Final evaluation, Bedrock | 36 calls, 38,869 in / 9,991 out | USD 0.2931 (about 0.0163/document) |
| Reused OCR pages (2026-10-09 collection) | 11 pages, 0 new | USD 0.0165 (evaluator total 0.3096) |
| Digital corpus 2026-10-09 | 18 calls, 16,519 in / 4,817 out | not re-priced here |
| Smoke `h` reprocess | 2 calls, 2,525 in / 792 out, 0 Textract | about USD 0.02 (estimate) |
| Subtype confirmation (`-d`) | 6 calls, 7,720 in / 2,521 out, 0 new OCR | USD 0.0701 |
| Live scenarios 2026-10-13 (estimate) | about 30 worker/chat Bedrock calls, 1-2 Textract pages | about USD 0.25-0.40 |

Total measured and estimated for 2026-10-13 (final evaluation, subtype
confirmation, live scenarios): well under USD 1. Per-run worker usage is not
exposed, so the live figure is an estimate from call counts in the reports.
Billing data is delayed (existing USD 25 monthly budget); no Cost Explorer call
was made. Worker usage is not exposed in the authorized metadata projection;
usage figures come from run exports and logs.

Incremental AWS resources (template review, change set
`idp-disabled-c136024-20261009`, 17 additions): four SQS queues (two with
4-day retention and DLQs with 14-day retention), one SNS topic and subscription
with policies, two Lambda functions (1024 MB / 512 MB, 360 s timeout) with two
14-day log groups, three IAM roles, one Cognito
resource server and client. No new table, bucket, KMS key, fixed-capacity
service or CloudWatch alarm. Lambda reserved concurrency is `0` when processing
is disabled; the event-source mappings (batch 1, max concurrency 2) exist only
when `EnableIDPProcessing=true`. Pay-per-use only; the 15-minute reconciliation
rule is the pre-existing Phase 14 schedule. DLQ visibility relies on the
existing operational alarms (no new alarm).

## 8. Defects found by live smokes (2026-10-13) and fixes

- `fd85b92` invalid `NOT IN` DynamoDB syntax in attempt exhaustion.
- `303bdf7` stale clean-intent snapshot blocked reprocess reservation.
- `8c4d98c` `Decimal` run numbers not JSON-serializable in metadata.
- `9b4ca3c`, `8f4c454` review dispatch ran before the durable checkpoint;
  machine Gateway calls lacked `matterId` and stale redispatch occurred.
- `a52508a` 15 s machine timeout shorter than the cold Gateway chain (about 16 s).
- `0779407`, `8a62d4e` task-level `resolutionNote` sent on `in_review` decisions.
- `4144703` `subtype` accepted document titles (confirmed with the real model, `a9fe4a7`).
- `36713c9` Knowledge Base ingestion status resolved per document, so one
  unparseable document no longer blocks indexing of the others.
- `30adeac` OCR continuation, delivery race, saved decisions and projection
  defects that sent a Textract completion to the DLQ.
- `89b84ca` unsupported optional writer relationships are dropped instead of
  failing the whole RAG answer as `model_failed` (deterministic evaluations 24/24).
- `c73ae82`, `73285e4` reviewed trusted-edge secret rotation tooling with a
  temporary role (deleted afterward; Public Edge `RoleARN` restored).

Earlier fixes that unblocked smoke `c`: `4556bb6` legacy null projection
fields, `840b8c0` duration-unit canonicalization, `10f8c01` Gateway envelope.

## 9. Rollback and retry

Cited from `docs/idp-live-validation-plan.md` section 7 and
`docs/idp-bootstrap-runbook.md` "Reviewed change-set sequence".

- Stop processing: set `EnableIDPProcessing=false` (removes the event-source
  mappings, reserved concurrency returns to `0`) and
  `EnableIDPReviewDispatch=false` via a reviewed change set; optionally stop
  the reconciliation schedule. Preserve queues and DLQs; do not delete
  canonical uploads or replay ambiguous model calls.
- Code rollback: redeploy the previous immutable artifact (S3 version and
  SHA-256 recorded in `docs/idp-deployment-evidence-2026-10-09.md`) with a
  reviewed code-only change set. The CD role stays code-only; Public Edge
  infrastructure changes need the documented temporary role, restored and
  verified afterward.
- Infrastructure rollback: separate reviewed CloudFormation change set; never
  `cloudformation deploy`.
- Retry: inspect DLQ messages with bounded metadata, redrive per job after
  fixing the cause; never blind-retry deterministic validation failures.
  Use `scripts/reprocess_idp.py` (default is a local preflight; `--execute`
  is restricted to the beta tenant/matters and synthetic fixture hash, one
  generation per call) and `scripts/verify_idp_duplicate.py` (no-AWS default;
  `--execute` replays a clean event against one terminal job and checks that
  no second run or paid call occurs). Duplicate verification: PASS
  (`idp-live-duplicate-20261013-b.json`).

## 10. Known limitations and approved deviations

- CloudFront default certificate fixes the viewer TLS policy at legacy
  `TLSv1`; accepted as a beta-level residual risk by the owner
  (`docs/phase-14-production-approval.md`); not strict-TLS production readiness.
- Cold start (resolved in the hardening release): first requests took about
  6 s per Gateway hop because boto3 handles were built per request at 256 MB.
  Handles are now cached per container and prewarmed at init, and the
  interceptor, metadata MCP and review task run at 512 MB. Measured after
  deploy: ~1.9 s init plus ~120 ms first request (was ~1 s + ~6 s).
- RAG answers for the skip and failed-IDP documents: routing to RAG is proven
  (retrieval and writer ran after `89b84ca`), but the contextual-grounding
  guardrail fails the short synthetic answers closed (grounding 0.12 vs 0.75;
  `-skip-h`, `-failed-idp-rag-e`). RAG guardrail thresholds were intentionally
  not changed: accepted RAG behavior from earlier phases, and the plan forbids
  changing RAG regression behavior. Not an IDP defect. Controlled guardrail
  variants showed the block is on RELEVANCE for short questions whose answer
  does not restate the subject; a writer prompt 1.5.0 asking for that
  restatement was deployed, did not unblock these documents, and was reverted
  to the holdout-approved 1.4.0 (`-skip-i`, `-failed-idp-rag-f`).
- KB per-document reconciliation (resolved in the hardening release): stale
  `PENDING_INGESTION` documents of any uploader in the allowed matters are now
  resolved through a bounded keyed matter query (no scan); `NOT_FOUND` without
  a failed job stays pending (fail closed).
- The `scripts/reprocess_idp.py` operator ARN is the IAM user (now `aws login`
  temporary credentials with MFA; the permanent access key was deleted).
- One legacy document row keeps a stale `idpReason` (not exposed by the API).
- One unexplained 15 s `internal_error` during concurrent smoke traffic
  (untraced).
- The Bedrock call cap was exceeded by 1 call in one run (accounting error;
  reported).
- Tenant Gateway quota raised 100 -> 300/month for the beta tenant and the
  trusted-edge secret rotated in one reviewed update (temporary role deleted).
- Public Edge IAM: the code-only CD role cannot update application role
  policy; a temporary operator-managed role was used for one reviewed update,
  then restored and deleted (deployment evidence).
- Metadata MCP target uses `listingMode=DYNAMIC`; explicit synchronization is
  unsupported, so no static-cache refresh is claimed.
- The public runner cannot trigger a duplicate clean event or a three-run
  history and exposes no worker usage; these were proven with the operator
  scripts (`-duplicate-b`, `-history-b`).
- Review-sensitive and interpretive fields are always routed to review; this
  yields a high review rate (88.89%) by design. Technical evidence checks do
  not certify legal meaning.
- Scope limited to the beta tenant, two matters and synthetic data; no PDF
  backfill of the existing inventory.
