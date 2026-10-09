"use strict";

const assert = require("assert");
const crypto = require("crypto");
const {
  classifySelectedResponse,
  parseMcpMetadata,
  quoteDigest,
  selectBoundReviewTask,
  selectBoundReviewField,
  safeResponseWait,
  responseJson,
} = require("./phase14_idp_live_browser.cjs");

assert.deepStrictEqual(
  parseMcpMetadata({ content: [{ type: "text", text: JSON.stringify({ idp: { status: "IDP_COMPLETED" } }) }] }),
  { idp: { status: "IDP_COMPLETED" } },
);
assert.throws(() => parseMcpMetadata({ result: { content: [{ type: "text", text: "{}" }] } }), /metadata_shape/);
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
