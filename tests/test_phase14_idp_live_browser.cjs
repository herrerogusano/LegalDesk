"use strict";

const assert = require("assert");
const crypto = require("crypto");
const fs = require("fs");
const {
  assertSelectedExpectations,
  documentIndexTerminal,
  readWithTransientRetry,
  SmokeFailure,
  classifySelectedResponse,
  parseMcpMetadata,
  quoteDigest,
  selectBoundReviewTask,
  selectBoundReviewField,
  validateExistingMetadata,
  safeResponseWait,
  responseJson,
  pageErrorDiagnostic,
  humanDecisionMismatchDiagnostic,
} = require("./phase14_idp_live_browser.cjs");

assert.deepStrictEqual(
  parseMcpMetadata({ status: "SUCCESS", tool: "@legaldesk_gateway/metadata-mcp___get_document_metadata", result: { idp: { status: "IDP_COMPLETED" } } }),
  { idp: { status: "IDP_COMPLETED" } },
);
assert.throws(() => parseMcpMetadata({ result: { content: [{ type: "text", text: "{}" }] } }), /metadata_shape/);
assert.throws(() => parseMcpMetadata({ status: "SUCCESS", tool: "@legaldesk_gateway/metadata-mcp___get_document_metadata", result: { content: [{ type: "text", text: "{}" }] } }), /metadata_shape/);
assert.strictEqual(quoteDigest("ámbito"), crypto.createHash("sha256").update("ámbito", "utf8").digest("hex"));
assert.strictEqual(classifySelectedResponse({ idp: { field: "effective_date", acceptance: "PROVISIONAL", presence: "PRESENT", origin: "LITERAL" } }, "effective_date"), "IDP");
assert.strictEqual(classifySelectedResponse({ idp: { source: "IDP" }, citations: [] }, "effective_date"), "NONE");

const expected = { documentId: "doc-1", runId: "run-1", documentSha256: "a".repeat(64) };
const matching = { reviewTaskId: "task-1", idp: { ...expected, fields: [{ name: "effective_date" }] } };
const wrongHash = { reviewTaskId: "task-2", idp: { ...expected, documentSha256: "b".repeat(64), fields: [{ name: "effective_date" }] } };
assert.strictEqual(selectBoundReviewTask([wrongHash, matching], expected), matching);
assert.strictEqual(selectBoundReviewTask([{ reviewTaskId: "task-3", source: "IDP", idpDocumentId: "doc-1" }], expected), null);
const taskWithTwoFields = { reviewTaskId: "task-4", idp: { ...expected, fields: [{ name: "governing_law" }, { name: "effective_date" }] } };
assert.strictEqual(selectBoundReviewField(taskWithTwoFields, "effective_date").name, "effective_date");
assert.strictEqual(selectBoundReviewField(taskWithTwoFields, "missing_field"), null);
assert.strictEqual(selectBoundReviewField(taskWithTwoFields, "").name, "governing_law");
const resumeExpected = { documentId: "doc-existing", sourceSha256: "c".repeat(64), documentType: "CONTRACT", matterId: "matter-synthetic" };
const resumeMetadata = { document: { documentId: "doc-existing", matterId: "matter-synthetic" }, idp: { documentSha256: "c".repeat(64), documentType: "CONTRACT", status: "IDP_COMPLETED" } };
assert.strictEqual(validateExistingMetadata(resumeMetadata, resumeExpected).status, "IDP_COMPLETED");
assert.throws(() => validateExistingMetadata({ ...resumeMetadata, idp: { ...resumeMetadata.idp, documentSha256: "d".repeat(64) } }, resumeExpected), /resume_source_hash_mismatch/);
assert.throws(() => validateExistingMetadata({ ...resumeMetadata, document: { documentId: "doc-other", matterId: "matter-synthetic" } }, resumeExpected), /resume_document_scope_mismatch/);
assert.throws(() => validateExistingMetadata({ ...resumeMetadata, document: { documentId: "doc-existing", matterId: "matter-other" } }, resumeExpected), /resume_document_scope_mismatch/);
assert.throws(() => validateExistingMetadata({ ...resumeMetadata, idp: { ...resumeMetadata.idp, documentType: "DEMAND" } }, resumeExpected), /resume_document_type_mismatch/);
assert.deepStrictEqual(pageErrorDiagnostic({ name: "ReferenceError", message: "boom at https://example.invalid/assets/app.js:7" }), { name: "ReferenceError", basename: "app.js" });
assert.deepStrictEqual(pageErrorDiagnostic({ name: "Error", message: "secret response body" }), { name: "Error", basename: "unknown" });
const browserSource = fs.readFileSync(require.resolve("./phase14_idp_live_browser.cjs"), "utf8");
assert(browserSource.indexOf('locator("#workspace-tab-documents").click') < browserSource.indexOf('locator("#document-file").setInputFiles'), "documents tab must be selected before upload");
assert(browserSource.indexOf('locator("#workspace-tab-reviews").click') < browserSource.indexOf('locator(`#reviews-pending details'), "reviews tab must be selected before review task interaction");
assert(browserSource.includes('LEGALDESK_IDP_EXISTING_DOCUMENT_ID'), "resume mode must be explicit");
assert(browserSource.indexOf('loadMetadata(EXISTING_DOCUMENT_ID') < browserSource.indexOf('setInputFiles(FIXTURE_PATH)'), "resume metadata must precede any upload");

const responseDiagnostics = {};
safeResponseWait({ waitForResponse: () => Promise.reject(new Error("secret URL/body must not escape")) }, () => true, "upload_authorization", responseDiagnostics).then(response => {
  assert.strictEqual(response, null);
  assert.deepStrictEqual(responseDiagnostics, { responseWait: "upload_authorization" });
  process.stdout.write("phase14 idp browser helper contracts: ok\n");
}).catch(error => {
  process.stderr.write(`${error.name}: browser helper contract failed\n`);
  process.exitCode = 1;
});

const statusDiagnostics = {};
responseJson({ ok: () => false, status: () => 403 }, "upload_authorization", statusDiagnostics).catch(error => {
  assert.strictEqual(error.message, "upload_authorization");
  assert.deepStrictEqual(statusDiagnostics, { httpStatus: 403, responseWait: "upload_authorization" });
}).catch(error => {
  process.stderr.write(`${error.name}: HTTP status contract failed\n`);
  process.exitCode = 1;
});

(async () => {
  const noSleep = { sleep: async () => {} };
  let calls = 0;
  const retried = [];
  assert.strictEqual(await readWithTransientRetry(async () => { calls += 1; if (calls === 1) throw new SmokeFailure("api_500"); return "ok"; }, { ...noSleep, onRetry: c => retried.push(c) }), "ok");
  assert.deepStrictEqual([calls, retried], [2, ["api_500"]]);
  calls = 0;
  await assert.rejects(readWithTransientRetry(async () => { calls += 1; throw new SmokeFailure("api_403"); }, noSleep), /api_403/);
  assert.strictEqual(calls, 1);
  calls = 0;
  await assert.rejects(readWithTransientRetry(async () => { calls += 1; throw new SmokeFailure("api_502"); }, noSleep), /api_502/);
  assert.strictEqual(calls, 2);
  calls = 0;
  await assert.rejects(readWithTransientRetry(async () => { calls += 1; throw new SmokeFailure("metadata_shape"); }, noSleep), /metadata_shape/);
  assert.strictEqual(calls, 1);
})().catch(error => { console.error(error); process.exitCode = 1; });

// Human-decision mismatch diagnostics keep only closed-vocabulary codes.
assert.deepStrictEqual(humanDecisionMismatchDiagnostic({ error: "invalid_request" }), { humanDecisionPayloadKind: "object", humanDecisionError: "invalid_request", humanDecisionTaskIdPresent: false });
assert.deepStrictEqual(humanDecisionMismatchDiagnostic({ error: "Free text with secrets", reviewTaskId: "review-x" }), { humanDecisionPayloadKind: "object", humanDecisionError: null, humanDecisionTaskIdPresent: true });
assert.deepStrictEqual(humanDecisionMismatchDiagnostic(null), { humanDecisionPayloadKind: "null", humanDecisionError: null, humanDecisionTaskIdPresent: false });
assert.deepStrictEqual(humanDecisionMismatchDiagnostic(["x"]), { humanDecisionPayloadKind: "object", humanDecisionError: null, humanDecisionTaskIdPresent: false });

// Scenario expectations: provisional labelling and evidence status.
const provisionalResponse = { idp: { field: "operative_ruling", acceptance: "REVIEW_REQUIRED", presence: "PRESENT", origin: "INTERPRETIVE", provisional: true } };
assertSelectedExpectations(provisionalResponse, { source: "IDP", evidenceStatus: "ambiguous" }, { expectProvisional: true });
assert.throws(() => assertSelectedExpectations({ idp: { ...provisionalResponse.idp, acceptance: "AUTO_ACCEPTED", provisional: false } }, { source: "IDP", evidenceStatus: "answerable" }, { expectProvisional: true }), /selected_not_labelled_provisional/);
assert.throws(() => assertSelectedExpectations({ idp: { ...provisionalResponse.idp, acceptance: "HUMAN_CONFIRMED" } }, { source: "IDP", evidenceStatus: "ambiguous" }, { expectProvisional: true }), /selected_not_labelled_provisional/);
assert.throws(() => assertSelectedExpectations({ citations: [{}] }, { source: "RAG", evidenceStatus: "answerable" }, { expectProvisional: true }), /selected_not_labelled_provisional/);
assertSelectedExpectations({}, { source: "NONE", evidenceStatus: "insufficient_evidence" }, { expectedEvidenceStatus: "insufficient_evidence" });
assert.throws(() => assertSelectedExpectations({}, { source: "NONE", evidenceStatus: "answerable" }, { expectedEvidenceStatus: "insufficient_evidence" }), /selected_evidence_status_mismatch/);
assertSelectedExpectations({}, { source: "RAG", evidenceStatus: "answerable" }, {});
assert.strictEqual(documentIndexTerminal("INDEXED"), true);
assert.strictEqual(documentIndexTerminal("FAILED"), true);
assert.strictEqual(documentIndexTerminal("PENDING_INGESTION"), false);
assert.strictEqual(documentIndexTerminal(null), false);

// Resume of a failed projection binds the hash through server run history.
const failedExpected = { ...resumeExpected, documentType: "UNKNOWN", expectedStatus: "IDP_FAILED" };
const failedMetadata = { document: { documentId: "doc-existing", matterId: "matter-synthetic" }, idp: { status: "IDP_FAILED", documentSha256: null, documentType: null, history: [{ documentSha256: "c".repeat(64) }] } };
assert.strictEqual(validateExistingMetadata(failedMetadata, failedExpected).status, "IDP_FAILED");
assert.throws(() => validateExistingMetadata({ ...failedMetadata, idp: { ...failedMetadata.idp, history: [] } }, failedExpected), /resume_source_hash_mismatch/);
assert.throws(() => validateExistingMetadata({ ...failedMetadata, idp: { ...failedMetadata.idp, history: [{ documentSha256: "c".repeat(64) }, { documentSha256: "e".repeat(64) }] } }, failedExpected), /resume_source_hash_mismatch/);
// Without an explicit failure expectation the null hash is still rejected.
assert.throws(() => validateExistingMetadata(failedMetadata, { ...failedExpected, expectedStatus: "" }), /resume_source_hash_mismatch/);
assert.throws(() => validateExistingMetadata({ ...failedMetadata, idp: { ...failedMetadata.idp, status: "IDP_COMPLETED" } }, failedExpected), /resume_source_hash_mismatch/);
assert(browserSource.indexOf('setPhase("rag_index_wait")') < browserSource.indexOf('setPhase("idp_metadata")'), "free RAG index wait must precede Gateway metadata polling");
assert(browserSource.includes('SKIP_REPLAY_READ ? metadata : await loadMetadata(documentId)'), "replay read may only be skipped explicitly");
assert(browserSource.includes('"SKIPPED_TO_CONSERVE_GATEWAY_QUOTA"'), "a skipped replay must be reported, not claimed as executed");
