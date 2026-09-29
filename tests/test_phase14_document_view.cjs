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
    this.attributes = {};
    this.listeners = Object.create(null);
  }

  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  getAttribute(name) { return this.attributes[name]; }
  addEventListener(type, listener) {
    if (!this.listeners[type]) this.listeners[type] = [];
    this.listeners[type].push(listener);
  }
  dispatchEvent(event) {
    const dispatched = event || {};
    if (!dispatched.target) dispatched.target = this;
    if (!dispatched.preventDefault) dispatched.preventDefault = () => { dispatched.defaultPrevented = true; };
    (this.listeners[dispatched.type] || []).forEach((listener) => listener(dispatched));
    return !dispatched.defaultPrevented;
  }
  click() { this.dispatchEvent({ type: "click", target: this }); }
  focus() { documentApi.activeElement = this; }
  querySelectorAll() { return []; }
}

const ids = [
  "document-count", "document-status", "document-list", "document-incomplete-list", "document-tabs",
  "document-tab-available", "document-tab-available-count", "document-tab-incomplete", "document-tab-incomplete-count",
  "document-panel-available", "document-panel-incomplete", "matter-select", "document-file",
  "upload-button", "question", "ask-button", "sync-button", "sync-helper", "metadata-button",
  "review-button", "audit-button", "login-button", "logout-button", "review-reason",
  "review-note", "review-due-at",
];
const elements = Object.fromEntries(ids.map((id) => [id, new Element()]));
Object.entries(elements).forEach(([id, element]) => { element.id = id; });
elements["document-tab-available"].setAttribute("aria-selected", "true");
elements["document-tab-incomplete"].setAttribute("aria-selected", "false");
const documentApi = {
  activeElement: null,
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
  { documentId: "indexed-2", name: "manual-demo-contract.txt", status: "INDEXED", fileSizeBytes: 42, uploadedAt: "2026-09-29T12:35:56Z" },
  { documentId: "reconciled-1", name: "manual-demo-evidence.txt", status: "FAILED", malwareScanStatus: "PENDING", fileSizeBytes: 38, uploadedAt: "2026-09-29T12:36:56Z" },
  { documentId: "reconciled-2", name: "manual-demo-contract.txt", status: "FAILED", malwareScanStatus: "PENDING", fileSizeBytes: 42, uploadedAt: "2026-09-29T12:37:56Z" },
  { documentId: "reconciled-3", name: "manual-demo-annex.txt", status: "FAILED", malwareScanStatus: "PENDING", fileSizeBytes: 41, uploadedAt: "2026-09-29T12:38:56Z" },
];
assert.equal(JSON.stringify(view.summarizeDocuments(documents)), JSON.stringify({ indexed: 2, processing: 0, incomplete: 3, failed: 0 }));
assert.equal(view.documentSummaryLabel(view.summarizeDocuments(documents)), "2 listos · 3 cargas incompletas");
assert.equal(view.documentViewModel(documents[2]).statusLabel, "Carga incompleta");
assert.match(view.documentViewModel(documents[2]).metadata, /ID reconcil/);
assert.match(view.documentViewModel(documents[2]).metadata, /38 B/);
assert.match(view.documentViewModel(documents[2]).metadata, /autorizado/);
assert.equal(view.isIncompleteUpload({ status: "PENDING_UPLOAD" }), true);
assert.equal(view.isIncompleteUpload({ status: "FAILED", malwareScanStatus: "PENDING" }), true);
assert.equal(view.isIncompleteUpload({ status: "FAILED", malwareScanStatus: "THREATS_FOUND" }), false);
assert.equal(view.documentClassification({ status: "FAILED", malwareScanStatus: "PENDING" }), "incomplete");
assert.equal(view.documentClassification({ status: "FAILED", malwareScanStatus: "THREATS_FOUND" }), "failed");

// INDEXED, UPLOADED, PENDING_INGESTION and real FAILED records are operational;
// PENDING_UPLOAD and reconciled FAILED + PENDING scan records are incomplete.
// Unknown/future states remain visible in the main tab instead of disappearing.
const statusGroups = view.partitionDocuments([
  { documentId: "indexed", status: "INDEXED" },
  { documentId: "uploaded", status: "UPLOADED" },
  { documentId: "processing", status: "PENDING_INGESTION" },
  { documentId: "failed", status: "FAILED", malwareScanStatus: "THREATS_FOUND" },
  { documentId: "future", status: "FUTURE_STATUS" },
  { documentId: "pending-upload", status: "PENDING_UPLOAD" },
  { documentId: "reconciled-abandoned", status: "FAILED", malwareScanStatus: "PENDING" },
]);
assert.deepEqual(statusGroups.operational.map((item) => item.documentId), ["indexed", "uploaded", "processing", "failed", "future"]);
assert.deepEqual(statusGroups.incomplete.map((item) => item.documentId), ["pending-upload", "reconciled-abandoned"]);

const mixedDocuments = [...documents, { documentId: "real-failure", status: "FAILED", malwareScanStatus: "THREATS_FOUND" }];
assert.equal(JSON.stringify(view.summarizeDocuments(mixedDocuments)), JSON.stringify({ indexed: 2, processing: 0, incomplete: 3, failed: 1 }));
assert.equal(view.documentViewModel(mixedDocuments.at(-1)).statusLabel, "Requiere atención");

view.bindDocumentTabs();
view.renderDocuments(documents);
assert.equal(elements["document-count"].textContent, "2 listos · 3 cargas incompletas");
assert.match(elements["document-status"].textContent, /2 están listos para consultar/);
assert.match(elements["document-status"].textContent, /3 tienen cargas incompletas/);
assert.equal(elements["document-tab-available-count"].textContent, "2");
assert.equal(elements["document-tab-incomplete-count"].textContent, "3");
assert.equal(elements["document-list"].children.length, 2);
assert.equal(elements["document-incomplete-list"].children.length, 3);
assert.equal(elements["document-list"].children[0].children[1].textContent, "Listo para consultar");
assert.equal(elements["document-incomplete-list"].children[0].children[1].textContent, "Carga incompleta");
assert.match(elements["document-incomplete-list"].children[0].children[2].textContent, /38 B/);
assert.equal(elements["document-tab-available"].getAttribute("aria-selected"), "true");
assert.equal(elements["document-tab-incomplete"].getAttribute("aria-selected"), "false");
assert.equal(elements["document-tab-available"].getAttribute("tabindex"), "0");
assert.equal(elements["document-tab-incomplete"].getAttribute("tabindex"), "-1");
assert.equal(elements["document-panel-available"].hidden, false);
assert.equal(elements["document-panel-incomplete"].hidden, true);

elements["document-tab-incomplete"].click();
assert.equal(elements["document-tab-incomplete"].getAttribute("aria-selected"), "true");
assert.equal(elements["document-tab-available"].getAttribute("aria-selected"), "false");
assert.equal(elements["document-tab-incomplete"].getAttribute("tabindex"), "0");
assert.equal(elements["document-tab-available"].getAttribute("tabindex"), "-1");
assert.equal(elements["document-panel-incomplete"].hidden, false);
assert.equal(elements["document-panel-available"].hidden, true);

elements["document-tabs"].dispatchEvent({ type: "keydown", key: "ArrowRight", target: elements["document-tab-incomplete"] });
assert.equal(elements["document-tab-available"].getAttribute("aria-selected"), "true");
assert.equal(documentApi.activeElement, elements["document-tab-available"]);
elements["document-tabs"].dispatchEvent({ type: "keydown", key: "ArrowLeft", target: elements["document-tab-available"] });
assert.equal(elements["document-tab-incomplete"].getAttribute("aria-selected"), "true");
assert.equal(documentApi.activeElement, elements["document-tab-incomplete"]);
elements["document-tabs"].dispatchEvent({ type: "keydown", key: "Home", target: elements["document-tab-incomplete"] });
assert.equal(elements["document-tab-available"].getAttribute("aria-selected"), "true");
assert.equal(documentApi.activeElement, elements["document-tab-available"]);
elements["document-tabs"].dispatchEvent({ type: "keydown", key: "End", target: elements["document-tab-available"] });
assert.equal(elements["document-tab-incomplete"].getAttribute("aria-selected"), "true");
assert.equal(documentApi.activeElement, elements["document-tab-incomplete"]);

view.renderDocuments(mixedDocuments);
assert.equal(elements["document-count"].textContent, "2 listos · 3 cargas incompletas · 1 fallo");
assert.equal(elements["document-tab-available-count"].textContent, "3");
assert.equal(elements["document-tab-incomplete-count"].textContent, "3");
assert.equal(elements["document-list"].children.length, 3);
assert.equal(elements["document-incomplete-list"].children.length, 3);
assert.equal(elements["document-list"].children.at(-1).children[1].textContent, "Requiere atención");
assert.equal(elements["document-incomplete-list"].children[0].children[1].textContent, "Carga incompleta");
console.log(JSON.stringify({ result: "PASS", test: "phase14-document-view" }));
