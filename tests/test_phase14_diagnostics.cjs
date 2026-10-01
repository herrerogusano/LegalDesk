"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const windowApi = {};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, "..", "frontend", "diagnostics.js"), "utf8"),
  { window: windowApi },
);
const diagnostics = windowApi.LegalDeskDiagnostics;
assert.ok(diagnostics);

const nestedMcp = diagnostics.toDiagnosticsModel({
  status: "SUCCESS",
  tool: "@legaldesk_gateway/metadata-mcp___list_matter_documents",
  correlationId: "corr-123",
  result: { content: [{ type: "text", text: JSON.stringify({ documents: [
    { name: "Authorization agreement.txt", status: "INDEXED", mediaType: "text/plain", fileSizeBytes: 38 },
  ] }) }] },
});
assert.equal(JSON.stringify(nestedMcp.summary), JSON.stringify([
  ["Estado", "SUCCESS"],
  ["Herramienta", "@legaldesk_gateway/metadata-mcp___list_matter_documents"],
  ["Correlación", "corr-123"],
  ["Documentos", "1 encontrados"],
]));
assert.equal(JSON.stringify(nestedMcp.documents[0]), JSON.stringify({
  name: "Authorization agreement.txt",
  status: "INDEXED",
  mediaType: "text/plain",
  sizeBytes: 38,
}));

const audit = diagnostics.toDiagnosticsModel({ events: [
  { operation: "chat", timestampMs: 1_700_000_000_000, correlationId: "corr-1", accessToken: "must-not-be-read" },
  { event_type: "grounding_validate", timestamp_ms: 1_700_000_001_000, correlation_id: "corr-2" },
] });
assert.equal(JSON.stringify(audit.summary), JSON.stringify([["Eventos", "2 registrados"]]));
assert.equal(JSON.stringify(audit.events.map(({ operation, timestampMs, correlationId }) => ({ operation, timestampMs, correlationId }))), JSON.stringify([
  { operation: "chat", timestampMs: 1_700_000_000_000, correlationId: "corr-1" },
  { operation: "grounding_validate", timestampMs: 1_700_000_001_000, correlationId: "corr-2" },
]));
assert.equal(JSON.stringify(audit).includes("accessToken"), false);
assert.equal(JSON.stringify(audit).includes("must-not-be-read"), false);

const bounded = diagnostics.toDiagnosticsModel({
  documents: Array.from({ length: 51 }, (_, index) => ({ name: `doc-${index}.txt`, status: "INDEXED" })),
  events: Array.from({ length: 51 }, (_, index) => ({ operation: "chat", timestampMs: index })),
});
assert.ok(bounded.summary.some(([label, value]) => label === "Documentos" && value === "Mostrando 50 de 51"));
assert.ok(bounded.summary.some(([label, value]) => label === "Eventos" && value === "Mostrando 50 de 51"));
assert.equal(bounded.documents.length, 50);
assert.equal(bounded.events.length, 50);
console.log(JSON.stringify({ result: "PASS", test: "phase14-diagnostics" }));
