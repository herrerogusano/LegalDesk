# LegalDesk — Phase 14: Intelligent Document Processing (IDP)

**Status:** Approved implementation plan, subject only to the explicit mandatory stop gates below.  
**Target:** Existing LegalDesk AWS deployment and repository.  
**Primary objective:** Make LegalDesk classify legal documents, extract and preserve traceable structured facts, calculate clearly labelled derived metadata, support human correction, and answer structured questions without unnecessary RAG calls.  
**Language of implementation/docs:** English. User-facing strings may be Spanish/English as supported by the existing UI.

> **INSTRUCTIONS TO CODEX (MANDATORY):** This file is the complete source of requirements for Phase 14. Do not treat conversations, suggestions outside this file, or undocumented assumptions as requirements. Inspect the actual repository and existing deployed architecture before coding; existing public contracts and security invariants take precedence. Do not silently choose a different architecture or functional policy. If a STOP gate occurs, pause implementation, explain the finding and 2–3 options with trade-offs, recommend one, and wait for the user's explicit approval. Never quietly continue past a gate. Minor implementation details consistent with the decisions below do not require approval.

## 0. Scope and locked decisions

| Area | Approved decision |
| --- | --- |
| Product scope | LegalDesk handles legal **matters** with heterogeneous documents; Phase 14 initially supports **CONTRACT**, **DEMAND** (claim/lawsuit filing), **JUDGMENT**, plus **UNKNOWN**. An NDA is a contract/subtype, not a separate specialized MVP schema. |
| Extensibility | Registry-driven, versioned schemas. Adding a supported field must not require rewriting the processing pipeline. |
| AI | **Amazon Bedrock** LLM classifies documents, extracts facts/conditions and evidence, and may suggest applicable calculation rules. Reuse the already-approved model/invocation configuration if suitable. |
| Backend | Backend owns access control, input/schema/evidence checks, approved deterministic calculations, job state, persistence, retries and human-review routing. The LLM does **not** decide authorization, commit database changes, run arbitrary code or create privileged actions. |
| Source and derived metadata | Preserve origin and provenance: **literal extracted**, **derived by a deterministic backend rule**, or **interpretive/provisional**. Technical checks do not certify legal meaning. |
| Persistence | **Option B:** document record and IDP extraction/run records are logically independent but linked, preserving versions, evidence and human corrections. Continue to use DynamoDB + S3. Whether items share an existing table is determined by documented access patterns and actual repository inspection; no schema/table refactor without the stop policy. |
| Execution | Automatic **asynchronous** IDP initiated after a verified upload; use **SQS + Lambda**, retries, DLQ and idempotency, **not Step Functions**. Do not block existing upload, ingestion, indexing or RAG. |
| Text acquisition | Digital PDF: Python library; image/scanned PDF: **Amazon Textract OCR** only when needed. No Bedrock Data Automation, no Ollama, no fixed-cost OCR infrastructure. |
| Retrieval behavior | For supported structured document questions, **accepted IDP field first → existing RAG fallback if missing/unavailable → insufficient evidence if neither grounds an answer**. Clearly label provisional facts. General/open questions continue to use RAG. |
| Review policy | Each field has its own presence, origin and review/validation state. Clear literal facts with verifiable anchors may be technically accepted; calculated facts preserve rule and inputs; ambiguities remain provisional; materially interpretive legal outcomes require human confirmation before being represented as confirmed. Missing optional fields must not spam reviewers. |
| Limits | PDF only, at most **20 MB** and **100 pages** per IDP job by default, configurable. If exceeded/unsupported, set `IDP_SKIPPED` with a reason. Do not silently truncate, and do not block RAG if RAG supports the document. Verify these limits against existing upload rules. |
| Evaluation | 15 synthetic or properly licensed ground-truth documents: 5 contracts, 5 demands, 5 judgments, plus negative/adversarial fixtures. Evaluate fields, evidence, classification, calculations, authorization, prompt injection, retries and true AWS E2E. Measure token use and estimated cost/document; no arbitrary accuracy threshold. |
| Cost | Minimize variable spend and avoid new always-on/fixed-cost infrastructure. Existing AWS credits, usage alerts and small pay-per-use Bedrock/Textract costs are acceptable. Never interpret this as a demand for literal zero cost. |

**Non-goals:** A full legal practice management product; automatic legal advice, filing, communications or decisions; globally correct legal deadlines without jurisdiction-specific approved rules; fine-tuning; unrestricted document types; automatic edits to uploaded originals; broadly indexed cross-matter reporting; replacing RAG; replacing the existing authentication and tool architecture.

## 1. Preflight repository audit (implement only after this)

1. Read `README`, current phase plans/ADRs, IaC, upload/confirm/ingest code, document metadata storage, DynamoDB table keys and indexes, RAG ingestion/extraction and citations, authentication/authorization, backend API, Gateway/MCP tools, `create_review_task`, test conventions and CI/CD.
2. Establish the **actual** state machine and where the successful, server-verified upload is committed. Use that event to trigger IDP; do **not** trust an unauthenticated S3 notification to infer tenant/matter ownership, or enqueue before `HeadObject`/ownership confirmation.
3. Confirm region/service/model compatibility with the current `eu-west-1` environment, and account for the existing prompt version/hash and evaluation conventions. Do not invent repository paths or deployed resource names.
4. Check the chosen Bedrock model's structured-output support and context/token limits in the actual code and service configuration; add server-side JSON/schema validation regardless of model guarantees. Verify Textract OCR API limits and notification integration before deployment.
5. Document current entity access patterns: get document in a matter; get latest usable IDP extraction; get historical extraction; get review/correction; and structured lookup for a *selected authorized document*. Assess whether existing DynamoDB keys suffice before proposing GSI/new table. Never introduce DynamoDB scans to answer end-user queries.
6. Report a concise audit table of existing components reused, required additions, migrations, rollout impacts, risks and AWS resources/cost class. Record actual paths/commit in the implementation notes.

### Mandatory STOP gates (request approval **before** making the affected change)

- A requirement conflicts with a verified existing security invariant, public API/contract, prior ADR (including ADR-016), or deployed data model, and cannot be implemented with a backward-compatible adapter.
- Preserving the approved Option B data model requires destructive migration, a new database technology, bulk reindexing, cross-table transaction redesign or expensive new indices. Present access patterns and options; do not arbitrarily pick a physical design.
- Automatic creation or completion of human review tasks would require forging a user JWT, bypassing matter authorization, or expanding Gateway privileges. **Never** impersonate a user; propose an explicitly authorized service-principal design or approved backend API with equivalent scope and audit guarantees.
- A new recurring cost, always-on component, meaningful uncontrolled fan-out, unapproved paid AI service, account-wide IAM privilege or unsupported AWS region/model is required.
- The existing UI/API cannot support the agreed IDP-first/fallback path without a material product-contract change; or the known schema/field policy cannot be implemented securely as designed.
- An unexpected ambiguity requires changing approved functional rules (such as classifying UNKNOWN as CONTRACT, treating an interpretation as legally confirmed, or overriding a human correction).

In a STOP response provide: **finding → why it matters → 2–3 options with cost/security trade-offs → recommended option → exact approval needed**. Wait for the user; do not deploy or move on through the blocker. If the audit finds no gates, continue through the implementation milestones autonomously. Do not request approval for every internal class or variable.

## 2. Architecture and lifecycle

```text
Verified document upload/confirm (existing trust boundary)
    |\
    | \--> Existing ingestion / indexing --> RAG (independent lifecycle)
    |
    +--> durable IDP job claim / SQS queue --> IDP Lambda
                                          |--> preflight size/pages/type
                                          |--> Python page-aware text extraction
                                          |       \--> if image-only/mixed: async Textract OCR
                                          |            StartDocumentTextDetection
                                          |            -> SNS completion -> SQS -> Lambda continuation
                                          |--> normalized page/anchor text
                                          |--> Bedrock document classification
                                          |--> schema-selected field extraction and evidence
                                          |--> deterministic validation + rule engine
                                          |--> versioned extraction snapshot / human review
                                          \--> per-field accepted/provisional status

Structured authorized query --> current IDP metadata if usable
                          --> otherwise existing RAG with citations
                          --> otherwise insufficient evidence
```

- Keep **existing document ingestion/indexing status** and **IDP status** separate. IDP failure is not a document upload/indexing failure.
- Use existing IaC framework; add the minimum resources. Core path uses SQS+Lambda. Textract's asynchronous completion may require a narrowly scoped SNS topic/subscription and a completion SQS consumer; this is **not** Step Functions and must not involve a Lambda polling/waiting for an OCR job.
- Configure bounded concurrency, timeouts, visibility timeout, exponential backoff/retry policy, partial-batch failure handling when batching, and DLQ alerts. Assume at-least-once delivery and duplicate/out-of-order messages. Prefer idempotency and conditional writes over hoping for exactly-once events.
- For Textract, persist a mapping from `JobId` to authorized IDP job/run, verify the callback against persisted expected job status/identity, follow `GetDocumentTextDetection` pagination, and ensure duplicate completions have no side effect.
- Handle encrypted/password-protected, corrupt, empty-text, oversized, unsupported, mixed digital/scanned, image-heavy and 100-page documents explicitly. Never report a partially analyzed document as fully successful.

### Proposed IDP lifecycle (keep independent from RAG)

`PENDING_IDP → PROCESSING_IDP → IDP_COMPLETED | IDP_REVIEW_REQUIRED | IDP_FAILED | IDP_SKIPPED`

Optional implementation-internal substate: `WAITING_FOR_OCR`. Map it to the public state contract without affecting upload/index status. Store machine-readable reason codes for failures/skips, attempt count, timestamps and correlation ID. `UNKNOWN` classification can be a completed classification with no specialized fields, not automatically a system error.

## 3. Document classification and schema registry

Create a registry per document type, with a versioned JSON Schema (or existing repository-equivalent validated contract). The registry declares field names, types, extraction descriptions, normalization rules, evidence requirements and human-review sensitivity. The extraction driver is generic and reads the registry. New fields require a schema revision and tests, **not new hardcoded branches in the pipeline**.

Initial schemas (all fields nullable/optional when legitimately absent; distinguish missing from unknown):

| Type | Initially supported fields |
| --- | --- |
| `CONTRACT` | parties; effective_date; explicit_expiration_date; initial_duration_value/unit; automatic_renewal; renewal_period_value/unit; termination_notice_value/unit; amount/currency; jurisdiction; governing_law |
| `DEMAND` | claimant(s); defendant(s); court; case_number; filing_date; claims/requested_remedies; claimed_amount/currency |
| `JUDGMENT` | court; case_number; decision_date; parties; operative_ruling; costs_statement; appeal_information |
| `UNKNOWN` | document type and general document evidence only; no forced specialized extraction |

Contract/NDA classification: a confidentiality agreement is `CONTRACT` with optional `subtype: NDA` when explicit and supported by evidence; do not invent a fourth specialized extraction schema. Classification must avoid forcing an unsupported type and handle ambiguity as provisional/UNKNOWN.

- Process documents in Spanish and English at minimum when the model can handle them; preserve source quotations in their original language.
- Make fields independent: failure or ambiguity in one field must not invalidate correct fields in the entire extraction.
- Define a clear field-presence enum: `PRESENT`, `ABSENT`, `NOT_APPLICABLE`, `AMBIGUOUS`, `UNKNOWN`. `null` by itself is insufficient.
- Reprocessing with a new schema/prompt/model must create a new run; do not silently rewrite old extraction data or downgrade a human-confirmed value.

## 4. Extraction, provenance and evidence contract

For each reported field, persist at least:

```json
{
  "field": "effective_date",
  "value": "2026-01-15",
  "presence": "PRESENT",
  "origin": "LITERAL",
  "acceptance": "AUTO_ACCEPTED",
  "evidence": [
    {"page": 2, "quote": "... entrará en vigor el 15 de enero de 2026 ..."}
  ],
  "validation": {"schema_valid": true, "anchor_found": true},
  "schema_version": "1.0.0"
}
```

- `origin`: `LITERAL`, `DERIVED`, `INTERPRETIVE` (only if needed for cautious provisional observations). Do not use an LLM confidence number as authoritative truth.
- `acceptance`: `AUTO_ACCEPTED`, `PROVISIONAL`, `REVIEW_REQUIRED`, `HUMAN_CONFIRMED`, `REJECTED`, or `UNAVAILABLE`, with consistent transitions and who/what approved each state.
- Evidence anchors must identify the original document/content version, **page** and an actual quotation, plus optional verified span/bounding box if the extraction method supplies it. Validate the quote/page against canonical page text (with controlled normalization), and reject fabricated/nonmatching anchors.
- Backend evidence checks prove an anchor **exists**, **not** necessarily that its legal meaning supports the extracted conclusion. Treat this limitation honestly in API/UI labels and evaluations.
- For literal dates, amounts or durations, apply deterministic lexical/format sanity checks where feasible; do not silently label an unsupported semantic inference as verified.
- For absent/not-applicable optional fields, store presence reason, no fabricated value, and do **not** create automatic review tickets merely because a field is missing.
- Contradictory clauses or unclear classification/field extraction remain provisional or require review. Preserve both conflicting evidence references when possible.
- Document text is untrusted. Explicitly instruct the LLM to treat prompts inside a legal document as source content, not higher-priority commands; never allow document instructions to invoke tools or access unrelated documents.

## 5. Derived Metadata Engine (approved hybrid interpretation/calculation)

**Do not restrict IDP to literal extraction.** The LLM may identify relationships, conditions and propose a named calculation rule with cited source facts. The backend chooses only from an **allowlisted, versioned rule registry**, validates input types/preconditions and performs the arithmetic deterministically.

MVP demonstrable rule(s):
- `ADD_CALENDAR_MONTHS_V1` / `ADD_CALENDAR_YEARS_V1`: compute an **estimated anniversary date** from an explicit effective date and explicit initial duration, with documented end-of-month/leap-day behavior and Python unit tests.
- Optional straightforward date difference or a notice-date estimate **only** if the required day-count convention is explicit and rule tests establish unambiguous preconditions. Never assume business days vs calendar days.

For each derived value persist `origin=DERIVED`, input field IDs and source evidence, `rule_id`, `rule_version`, deterministic parameters, computed result, and `acceptance=PROVISIONAL` until the legal applicability is confirmed. Label the result as an **estimate**, not an enforceable contractual expiry/deadline. Do not infer applicable law, filing deadlines, legal effects, holidays or business-day rules from model speculation. If an LLM suggests an unknown rule, store a non-executable proposal or review reason; **never** dynamically evaluate model-supplied code or formulas.

If an explicit document date conflicts with a derived estimate, do not silently prefer either as definitive: preserve both, mark conflict, and route to review if material.

## 6. Human review: evidence-based, field-level, auditable

- Clear, technically verifiable literal fields may be `AUTO_ACCEPTED` with provenance, **not** `HUMAN_CONFIRMED` or “legally certified.”
- Ambiguous, contradictory or materially interpretive legal fields (especially judgments' operative effects, costs, appeals; conditional contract renewals) must be `PROVISIONAL`/`REVIEW_REQUIRED` before representing them as confirmed.
- If human action is materially needed, integrate with existing `create_review_task` in its **authorized backend → Gateway → MCP/Lambda** flow, consistent with ADR-016. Confirm that existing permissions support system-created review requests. If not, **STOP** and get approval for the service-principal approach; do not forge actor tokens or call the Harness in place of required backend actions.
- Task payload/reference: tenant, matter, document, extraction_run, relevant field IDs, proposed values, evidence anchors, reasons. Do not leak cross-tenant data.
- Implement or extend the existing reviewer action to approve/correct/reject individual fields with explicit matter authorization and audit of reviewer/time/old/new value/evidence/reason. If there is no existing review resolution workflow and this needs a material product/API redesign, **STOP**; present the smallest compatible option.
- Human corrections are immutable audit entries; an automatic re-extraction must **not** overwrite them. Specify current-effective-value resolution rule: use the latest applicable human-confirmed value over newer unreviewed model runs, and retain provenance of both. Surface conflicts for review.
- Do not automatically send communications, trigger legal deadlines or create consequential actions from provisional values.

## 7. Persistence and versioning (Option B)

Keep the existing `Document` record and introduce logically distinct `IDPExtractionRun` records linked by the authenticated `tenantId`, `matterId`, `documentId` and immutable run ID. Ensure a server-validated document version / SHA-256 content identity. Never derive tenant/matter solely from a queue payload. Minimal run envelope:

```json
{
  "document_id": "doc-123",
  "run_id": "run-001",
  "document_sha256": "...",
  "document_type": "CONTRACT",
  "schema_version": "1.0.0",
  "model_id": "<actual-configured-model>",
  "prompt_version": "<version-and-hash>",
  "status": "IDP_REVIEW_REQUIRED",
  "fields": {"effective_date": "<field-result>", "estimated_initial_end_date": "<derived-result>"},
  "created_at": "<UTC timestamp>"
}
```

- Keep document core metadata, IDP status and latest-effective-IDP-run pointer slim; keep full historical snapshots and audit separate. DynamoDB may hold bounded structured fields, while lengthy OCR text, detailed evidence or large results belong in private encrypted S3 with immutable references. Respect DynamoDB's 400 KB item limit.
- Use conditional state transitions/optimistic concurrency, idempotency key incorporating document content/version, schema/prompt/model version and intentional reprocess generation. Duplicate messages must not trigger duplicate paid processing or duplicate review tasks.
- A re-run is new immutable history. Use explicit promotion rules for which fields/runs become effective. Existing confirmed corrections cannot be silently demoted.
- Access patterns in the MVP: (a) get authorized document, (b) get latest accepted/provisional extraction by document, (c) list extraction history for a document, (d) retrieve review/correction, (e) return specific typed fields for a selected authorized document. Do not add broad filtering/index complexity or `Scan` unless a separately approved requirement appears.
- **Physical layout decision:** favor existing table/key patterns when they satisfy the above efficiently; document alternative one-table vs separate-table trade-offs. If neither works without material changes, invoke STOP gate before creating resources or migrations. Never silently change OWD-like ownership semantics or cross-tenant partitions.

## 8. Text acquisition and document limits

1. Inspect file signature/MIME and actual PDF reader result, not only the filename; enforce approved 20 MB / 100-page defaults with configuration and clearly report `IDP_SKIPPED` when exceeded. Compare with existing upload limits; do not narrow the upload/RAG allowance inadvertently.
2. For digital PDFs, use a maintained Python page-aware parser. Record 1-based page numbers and normalized page text suitable for locating anchors. Keep original file immutable in S3.
3. If PDF pages are scanned, image-only or mixed and extraction is insufficient, use Textract **text detection/OCR**, not expensive forms/expense/custom extraction APIs. Preserve page mappings; for a multipage PDF use Textract async `StartDocumentTextDetection` and completion notification (`SNS → SQS → Lambda`), then paginated `GetDocumentTextDetection`.
4. Do not OCR a fully readable digital PDF. But also do not silently ignore image-only pages in a mixed document. Mark extraction gaps and if needed OCR the original PDF; bound attempts/cost.
5. For long documents, select relevant evidence-bearing sections or chunk by page with context limits. Cross-page facts may require consolidation. **Never** silently analyze only the first N pages and report full completion. Bound Bedrock calls and token budgets by configuration; if evidence coverage is insufficient, mark affected fields unavailable/provisional and document the reason.
6. Reuse existing extracted text artifacts when they preserve correct pagination/provenance; otherwise introduce an isolated IDP text adapter rather than modifying RAG citations/indexing contracts.

## 9. Bedrock calls and prompts

- Isolate `DocumentClassifier`, `DocumentFieldExtractor`, `EvidenceNormalizer/Validator`, `DerivedMetadataEngine`, `IDPRepository`, `ReviewService`, and `IDPQueryService` behind narrow testable interfaces (naming may follow current conventions). Reuse the existing Bedrock client and prompt/version/hash mechanism where compatible.
- Classifier returns `CONTRACT | DEMAND | JUDGMENT | UNKNOWN` (optional subtype) plus evidence; extractor uses only the matched schema registry.
- Enforce a server-side strict JSON contract, allowed field keys, typed values, input size limits, output token bounds, and safe handling of malformed/repeated/partial responses. Do not treat JSON Schema compliance as factual correctness.
- Optimize to avoid needless calls: cache by document/version and extraction config; reuse page text; classify once then extract with bounded attempts; cap retries. Do not invent numeric model confidence thresholds as a substitute for evaluation.
- Never put secrets, other matter documents, broader tenant context, or credentials in an LLM request. Source text must be treated as lower-trust input. Log IDs, statuses and counts, not sensitive document text.

## 10. LegalDesk integration: IDP-first, RAG fallback

- Expand the existing authorized document-metadata retrieval to expose current IDP fields, provenance, status and evidence; preserve backwards compatibility and ADR-016's **explicit backend action** path (not relying on Harness `allowedTools` to force tools).
- Implement a **bounded structured-field query path** for a selected document and known fields (`effective_date`, `explicit_expiration_date`, `court`, `case_number`, etc.). Use a simple allowlisted mapping/field registry for recognized structured questions. Do **not** assume the backend can reliably interpret every natural-language question; route genuinely open/semantic questions through existing RAG.
- For a structured query: check authorization → select current effective IDP field → if usable, return with origin/evidence/review status → if absent/unavailable/failed, use the existing authorized RAG retrieval path → if neither is grounded, return the project's established insufficient-evidence response.
- If the only IDP candidate is **provisional**, show it explicitly labelled *provisional* with source and caveat; do not silently present it as confirmed. Depending on question intent, offer RAG corroboration without upgrading provisional status automatically.
- A RAG-derived fallback answer must carry RAG citations and **must not be auto-written into verified IDP metadata**. IDP-derived values must not silently turn into ground truth for the retriever. Maintain consistent answer contract and citation policy, including correct handling of `insufficient_evidence`.
- Enforce tenant/matter authorization **again** on each server-side lookup, independent of any frontend-provided ID; deny cross-matter probes, including history, extraction outputs, review tasks and derived values. No direct model-to-DynamoDB privileges.
- Start with selected-document structured queries; arbitrary cross-matter date-range searches are outside the MVP unless explicitly approved with access-pattern/index design.

## 11. Security, reliability and operations

- Least-privilege IAM for S3 objects, DynamoDB partitions/access patterns, SQS/SNS, Textract and Bedrock; reuse current encryption, JWT/actor identity, matter authorization and correlation IDs. Avoid global wildcard IAM resource actions unless the AWS API truly requires a justified scoped exception.
- AWS worker is a trusted service principal, not a stand-in for a human user. Resolve and verify persisted tenant/matter/document context before reading content, writing extraction, or opening review tasks.
- Protect against document prompt injection, fabricated quotes/pages, malicious file metadata, malformed PDFs, replayed OCR notifications, race conditions, duplicate SQS messages, a failed worker replaying paid calls, and deleted/replaced documents while processing.
- Persist step checkpoints before paid service calls where possible; define explicit idempotency/deduplication behavior after a crash between model output and persistence. No unlimited auto-retries for deterministic validation errors.
- Ensure RAG and existing upload continue to function while IDP is disabled, skipped, failed or in review.
- Log structured operational telemetry (job/doc/matter/tenant/correlation IDs, timings, pages, token counts, OCR pages, attempts, status codes) without raw document content. Provide basic CloudWatch alarms/visibility for DLQ and failures only if their cost profile is acceptable under existing infrastructure.

## 12. Test strategy and acceptance

**Ground truth:** At least 15 non-sensitive test PDFs: 5 contracts, 5 demands, 5 judgments. Include Spanish and English, digital and scanned PDFs where practical. Add UNKNOWN, malicious-instruction and negative/limit fixtures separately. For each, create independently reviewed expected document type, expected field values/presence, evidence page/text and applicable derived-rule expectations.

**Unit tests**
- Classification parsing, UNKNOWN, NDA subtype, schema registry evolution without new pipeline branches.
- Presence states, normalizers, dates/money, evidence anchor validity, contradictory and fabricated evidence, JSON invalidity, field-level acceptance policy.
- Deterministic derived rules across month ends/leap years, unsupported conventions, explicit-vs-derived conflict, provenance/versioning.
- Idempotency keys, duplicate job/event, out-of-order OCR completion, retry boundaries, DLQ transitions, independent RAG state.
- Human correction precedence over rerun; record/history read access and authorization.

**Integration/security tests**
- Existing verified upload → queue → digital or Textract OCR path → Bedrock mock → schema/evidence/rules → DynamoDB/S3 → metadata query.
- Full RAG fallback when IDP missing/skipped/failed; insufficient-evidence response when both are insufficient; provisional labels; no unverified writes from RAG.
- Cross-tenant and cross-matter denial for documents, IDP runs/history, review/correction and derived values.
- Embedded document prompt injection, forged evidence, malformed model output, malicious page/ID, concurrency, duplicate/replay messages.
- Regression tests for existing phase 2 upload/index lifecycle and ADR-016 explicit actions; no change to established auth guarantees.

**Evaluation report**
- Per-class classification confusion matrix, per-field present/absent/ambiguous precision/error categories and evidence correctness, derivation correctness, review rate, throughput and wall time.
- Explicitly separate deterministic/local tests, mocked integration, Bedrock real-model evaluation, and **real AWS end-to-end smoke**. Do not claim that a local composite test proves AWS E2E.
- Record estimated Bedrock input/output tokens and cost/document, Textract pages/cost, retries, and incremental AWS resources. Observe existing billing alarms/credits and bound the evaluation batch; don't run an uncontrolled fleet of OCR/LLM jobs.
- No arbitrary percentage success threshold; however **never** pass a test that auto-accepts nonexistent evidence, crosses matter boundaries, overwrites a human correction, changes RAG regression behavior or produces unlabelled high-impact legal interpretation. Surface other extraction errors transparently with reproduction fixtures and request prioritization if material.

## 13. Implementation milestones (in order)

### 14.0 — Audit and design record
- Complete Section 1 audit and STOP checks; document actual repo paths and schema/access-pattern fit.
- Record approved decisions in a phase-local ADR/decision section in repo root or its established ADR folder, without conflicting with existing ADR numbering.
- Deliver a short planned file-change map and dependency map, then continue automatically if no gate.

### 14.1 — Foundation and asynchronous processing
- Define independent IDP status/persistence/run interfaces, schema registry and queue contract.
- Wire verified upload to durable IDP job enqueue; introduce SQS/Lambda/DLQ with conditional claims, idempotency and controlled retries.
- Implement limit checks and `IDP_SKIPPED`, ensuring upload/index/RAG independence.

### 14.2 — Page extraction, OCR and classification
- Add page-aware digital text extraction and controlled OCR fallback (async Textract/SNS/SQS continuation).
- Normalize page/evidence anchors; implement Bedrock classifier, UNKNOWN handling, versioned model/prompt metadata, and tests.

### 14.3 — Structured fields, derived rules and evidence
- Implement CONTRACT/DEMAND/JUDGMENT schemas and generic extraction driver.
- Implement per-field validation, evidence checks, provenance and `DerivedMetadataEngine` deterministic rules; add regression/evaluation fixtures.

### 14.4 — History, review and query integration
- Persist immutable extraction history and human corrections, and define effective-value resolution.
- Integrate authorized `create_review_task` and completion/correction path (STOP if existing system cannot safely support system-triggered reviews).
- Expose structured metadata and implement IDP-first/fallback flow with RAG citation/insufficient-evidence contract preserved.

### 14.5 — Hardening, evaluation and AWS smoke
- Run unit, integration, adversarial, concurrency and authorization tests.
- Execute bounded real-AWS smoke: digital contract, scanned demand, judgment with provisional interpretive field, UNKNOWN, skip limit, deliberately failed IDP with working RAG, authorized review, unauthorized cross-matter denial, repeat processing idempotency, RAG fallback.
- Produce evaluation and cost report, operational/deploy/rollback instructions and clearly distinguish what was actually executed from what remains untested.

## 14. Final acceptance checklist

Phase 14 is **not complete** unless the following are evidenced:

- [ ] Supported documents classify correctly on the agreed evaluation set; UNKNOWN is not forced into a supported category.
- [ ] Correct schema/version-specific fields can be added without altering pipeline control flow.
- [ ] Every accepted important field has valid traceable evidence; fabricated anchors are rejected.
- [ ] Literal, derived and provisional/legal interpretation statuses are distinct in persistence and responses.
- [ ] Approved deterministic date rules execute in backend with input provenance and tests; unclear legal applicability remains labelled provisional/reviewed.
- [ ] Human review authorization is safe, correction history preserved, and reprocessing never overwrites a human-confirmed value silently.
- [ ] Independent async IDP with SQS/Lambda, retries, idempotency, DLQ and (where necessary) async Textract continuation works.
- [ ] 20 MB/100-page configurable PDF limits and skip behavior work without disabling RAG.
- [ ] Authorized structured lookup returns IDP first; missing/unavailable goes to cited RAG; neither leads to insufficient evidence; provisional data is not silently certified.
- [ ] Cross-tenant/matter attempts denied across all new entrypoints; malicious documents cannot issue commands or access secrets.
- [ ] Fifteen-document dataset, test results, AWS smoke evidence and real cost/usage measurements are documented.
- [ ] No unjustified always-on infrastructure, unapproved changes to original LegalDesk contracts, or unresolved mandatory STOP gate.

## 15. Handoff/output contract for Codex

At each milestone, report in a concise table: files changed, tests executed with actual counts and failures, observed AWS invocations/cost if applicable, and any outstanding issue. If a mandatory STOP gate is hit, stop **before implementing the contentious change** and request approval in the exact format from Section 1. Never declare a phase complete solely because the code compiles or mocked tests pass.

Final delivery must include: change summary, architecture diagram reflecting **actual deployed** components, schema/rule/example output, authorization proof, sample extraction evidence, evaluation metrics (including errors), bounded cost estimates, E2E smoke status, rollback/retry procedure and deviations from this plan approved by the user. Keep plans in the repository root as per project convention; this file is the authoritative Phase 14 plan, not merely a draft.

## 16. Official operational references

- Textract async PDF/text detection and SNS completion: https://docs.aws.amazon.com/textract/latest/dg/api-async.html
- Textract StartDocumentTextDetection API: https://docs.aws.amazon.com/textract/latest/APIReference/API_StartDocumentTextDetection.html
- AWS Lambda SQS partial batch failures: https://docs.aws.amazon.com/lambda/latest/dg/services-sqs-errorhandling.html
- AWS Lambda idempotency practices: https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html

**End of authoritative plan.**
