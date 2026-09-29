"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.files = [];
    this.hidden = false;
    this.disabled = false;
    this.textContent = "";
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
  removeAttribute() {}
  querySelectorAll() { return []; }
}

const ids = [
  "document-count", "document-status", "document-list", "matter-select", "document-file",
  "upload-button", "question", "ask-button", "sync-button", "sync-helper", "metadata-button",
  "review-button", "audit-button", "login-button", "logout-button", "review-reason",
  "review-note", "review-due-at",
];
const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
const documentApi = {
  addEventListener() {},
  getElementById(id) { return elements[id] || new Element(); },
  createElement(tagName) { return new Element(tagName); },
};
const windowApi = {};
const context = vm.createContext({ window: windowApi, document: documentApi, Intl, DOMException, AbortController, setTimeout, clearTimeout });
const source = fs.readFileSync(path.join(__dirname, "..", "frontend", "app.js"), "utf8");
vm.runInContext(source, context);
const view = windowApi.LegalDeskDocumentView;
assert.ok(view);

const documents = [
  { documentId: "indexed-1", name: "manual-demo-evidence.txt", status: "INDEXED", fileSizeBytes: 38, uploadedAt: "2026-09-29T12:34:56Z" },
  { documentId: "pending-1", name: "manual-demo-evidence.txt", status: "PENDING_UPLOAD", fileSizeBytes: 38, uploadedAt: "2026-09-29T12:35:56Z" },
  { documentId: "pending-2", name: "manual-demo-evidence.txt", status: "PENDING_UPLOAD", fileSizeBytes: 38, uploadedAt: "2026-09-29T12:36:56Z" },
];
assert.equal(JSON.stringify(view.summarizeDocuments(documents)), JSON.stringify({ indexed: 1, processing: 0, incomplete: 2, failed: 0 }));
assert.equal(view.documentSummaryLabel(view.summarizeDocuments(documents)), "1 listo · 2 cargas incompletas");
assert.equal(view.documentViewModel(documents[1]).statusLabel, "Subida incompleta");
assert.match(view.documentViewModel(documents[1]).metadata, /ID pending-/);
assert.match(view.documentViewModel(documents[1]).metadata, /38 B/);
assert.match(view.documentViewModel(documents[1]).metadata, /autorizado/);

view.renderDocuments(documents);
assert.equal(elements["document-count"].textContent, "1 listo · 2 cargas incompletas");
assert.match(elements["document-status"].textContent, /1 está listo para consultar/);
assert.match(elements["document-status"].textContent, /2 tienen cargas incompletas/);
assert.equal(elements["document-list"].children.length, 3);
assert.equal(elements["document-list"].children[1].children[1].textContent, "Subida incompleta");
assert.match(elements["document-list"].children[1].children[2].textContent, /38 B/);
console.log(JSON.stringify({ result: "PASS", test: "phase14-document-view" }));
