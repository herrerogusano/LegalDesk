"use strict";

const assert = require("assert");
const crypto = require("crypto");
const {
  classifySelectedResponse,
  parseMcpMetadata,
  quoteDigest,
  selectBoundReviewTask,
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

process.stdout.write("phase14 idp browser helper contracts: ok\n");
