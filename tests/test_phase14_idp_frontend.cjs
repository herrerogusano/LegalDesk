"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "..", "frontend", "app.js"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "frontend", "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "..", "frontend", "styles.css"), "utf8");
const elements = new Proxy({}, { get: (_target, id) => ({ id, hidden: false, disabled: false, dataset: {}, children: [], textContent: "", replaceChildren() {}, append() {}, addEventListener() {}, querySelectorAll() { return []; }, setAttribute() {}, removeAttribute() {} }) });
const documentApi = {
  addEventListener() {},
  getElementById(id) { return elements[id]; },
  createElement() { return elements.node; },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  documentElement: {},
};
const windowApi = { matchMedia: () => ({ matches: false }) };
vm.runInNewContext(source, { window: windowApi, document: documentApi, Intl, DOMException, AbortController, Headers, fetch() {}, setTimeout, clearTimeout, getComputedStyle: () => ({ getPropertyValue: () => "" }) });
const view = windowApi.LegalDeskIDPView;
assert.ok(view, "IDP helpers are exported for focused offline verification");

assert.equal(view.idpStatusLabel("IDP_SKIPPED"), "Extracción omitida");
assert.equal(view.idpStatusLabel("IDP_FAILED"), "Extracción fallida");
assert.equal(view.idpStatusLabel(undefined), "Estado de extracción no disponible");
assert.equal(view.idpPresenceLabel("PRESENT"), "Presente");
assert.equal(view.idpAcceptanceLabel("REVIEW_REQUIRED"), "Requiere revisión");

const response = { operationStatus: "ok", answer: "Importe: 1250. Origen: LITERAL. requiere revisión humana.", idpMetadata: { field: "amount", value: 1250, presence: "PRESENT", acceptance: "REVIEW_REQUIRED", origin: "LITERAL" } };
assert.equal(view.selectedIdpAnswer(response, "amount").value, 1250);
assert.equal(view.selectedIdpAnswer(response, "currency").fallback, true, "a mismatched field remains a readable non-IDP fallback");
assert.equal(view.selectedIdpAnswer({ operationStatus: "documents_processing", answer: response.answer, idpMetadata: response.idpMetadata }, "amount"), null);

const field = { name: "amount", result: { value: 1250, origin: "EXTRACTED", presence: "PRESENT", acceptance: "REVIEW_REQUIRED", evidence: [{ page: 2, quote: "1.250 EUR", contentSha256: "abc", start: 1, end: 9 }] } };
const decision = view.idpDecisionPayload(field, "CORRECT", "La cifra legible en la página dos", 1300);
assert.equal(JSON.stringify(decision), JSON.stringify({ fieldName: "amount", action: "CORRECT", reason: "La cifra legible en la página dos", evidence: [{ page: 2, quote: "1.250 EUR", contentSha256: "abc", start: 1, end: 9 }], proposedValue: 1300 }));
assert.equal(Object.hasOwn(decision, "origin"), false);
assert.equal(Object.hasOwn(decision, "presence"), false);
assert.equal(Object.hasOwn(decision, "acceptance"), false);
assert.equal(Object.hasOwn(decision, "documentSha256"), false);
assert.equal(JSON.stringify(view.parseIdpCorrection("uno\ndos", "array[string]")), JSON.stringify(["uno", "dos"]));
assert.equal(view.parseIdpCorrection("true", "boolean"), true);

assert.match(html, /id="idp-panel"/);
assert.match(html, /id="idp-field-select"/);
assert.match(html, /Datos extraídos/);
assert.match(html, /id="idp-history"/);
assert.match(css, /prefers-reduced-motion/);
assert.match(css, /idp-review-field/);
assert.doesNotMatch(source, /selectedDocumentId\s*:\s*[^,]+,\s*selectedFieldName\s*:\s*[^,]+,[\s\S]{0,300}question\s*:/, "selected IDP query remains an explicit action body, not a global chat selector");

console.log(JSON.stringify({ result: "PASS", test: "phase14-idp-frontend" }));

// Behavioral browser coverage: exercise the real DOM event path and fake only
// the same-origin API. This catches contracts that helper-only tests cannot.
(async () => {
  let chromium;
  const playwrightModule = process.env.PLAYWRIGHT_MODULE || "playwright";
  try { ({ chromium } = require(playwrightModule)); } catch (error) {
    if (process.env.PLAYWRIGHT_MODULE) {
      console.error(`Configured Playwright module failed to load: ${error.message}`);
      process.exitCode = 1;
      return;
    }
    console.log(JSON.stringify({ result: "SKIP", test: "phase14-idp-frontend-browser", reason: "playwright unavailable; browser CI covers DOM flow" }));
    return;
  }
  const http = require("node:http");
  const frontendRoot = path.join(__dirname, "..", "frontend");
  const requests = [];
  let chatFailures = 0;
  let delayNextChat = false;
  let reviewPostBody = null;
  let reviewPatchBody = null;
  const idpReviewTask = {
    reviewTaskId: "idp-review-1",
    status: "pending",
    reasonCode: "ambiguous_evidence",
    dueAt: "2099-01-01",
    createdAt: "2026-10-01T00:00:00Z",
    idp: {
      documentType: "CONTRACT",
      fields: [{
        name: "amount",
        valueType: "number",
        result: {
          value: 1250,
          presence: "PRESENT",
          origin: "LITERAL",
          acceptance: "REVIEW_REQUIRED",
          evidence: [{ page: 2, quote: "1.250 EUR", contentSha256: "b".repeat(64) }],
        },
        applicableDecisions: [],
      }],
    },
  };
  const server = http.createServer((request, response) => {
    const url = new URL(request.url, "http://localhost");
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf8");
      let parsed = null; try { parsed = body ? JSON.parse(body) : null; } catch (_error) { parsed = null; }
      if (url.pathname.startsWith("/api/")) {
        requests.push({ path: url.pathname, body: parsed });
        let payload = {};
        let status = 200;
        if (url.pathname === "/api/me") payload = { userId: "tester", csrfToken: "csrf" };
        else if (url.pathname === "/api/matters") payload = { matters: [{ matterId: "matter-1", name: "Expediente de prueba" }, { matterId: "matter-2", name: "Segundo expediente" }] };
        else if (url.pathname === "/api/conversations" && request.method === "POST") {
          const matterId = parsed && parsed.matterId;
          payload = matterId === "matter-2" ? { conversationId: "conversation-2", sessionId: "session-2", correlationId: "corr-2" } : { conversationId: "conversation-1", sessionId: "session-1", correlationId: "corr-1" };
        }
        else if (url.pathname === "/api/matters/matter-1/documents") payload = { documents: [
          { documentId: "doc-1", matterId: "matter-1", name: "contrato.pdf", status: "INDEXED", fileSizeBytes: 10, uploadedAt: "2026-10-01T00:00:00Z" },
          { documentId: "doc-2", matterId: "matter-1", name: "anexo.pdf", status: "INDEXED", fileSizeBytes: 10, uploadedAt: "2026-10-01T00:00:00Z" },
        ] };
        else if (url.pathname === "/api/matters/matter-2/documents") payload = { documents: [{ documentId: "doc-3", matterId: "matter-2", name: "segundo.pdf", status: "INDEXED", fileSizeBytes: 10, uploadedAt: "2026-10-02T00:00:00Z" }] };
        else if (url.pathname === "/api/conversations/conversation-1" || url.pathname === "/api/conversations/conversation-2") payload = { events: [] };
        else if (url.pathname === "/api/matters/matter-1/reviews" && request.method === "GET") payload = { tasks: [idpReviewTask] };
        else if (url.pathname === "/api/matters/matter-2/reviews" && request.method === "GET") payload = { tasks: [] };
        else if (url.pathname === "/api/matters/matter-1/reviews" && request.method === "POST") { reviewPostBody = parsed; payload = { reviewTaskId: "created-review" }; }
        else if (url.pathname === "/api/matters/matter-1/reviews/idp-review-1" && request.method === "PATCH") { reviewPatchBody = parsed; payload = { ...idpReviewTask, status: "in_review" }; }
        else if (url.pathname === "/api/mcp" && parsed && parsed.params && parsed.params.name === "get_document_metadata") {
          const args = parsed.params.arguments || {};
          const isSecond = args.documentId === "doc-2" || args.documentId === "doc-3";
          const matterId = args.documentId === "doc-3" ? "matter-2" : "matter-1";
          const older = Boolean(args.historyCursor);
          payload = { content: [{ type: "text", text: JSON.stringify({
            document: { documentId: args.documentId, matterId, name: args.documentId === "doc-3" ? "segundo.pdf" : isSecond ? "anexo.pdf" : "contrato.pdf", status: "INDEXED" },
            ...(isSecond ? {} : { idp: { status: "IDP_COMPLETED", runId: "run-1", documentSha256: "a".repeat(64), documentType: "CONTRACT", fields: [{ name: "amount", valueType: "number" }], history: older ? [{ runId: "run-0", status: "IDP_FAILED", createdAt: "2026-09-01T00:00:00Z" }] : [{ runId: "run-1", status: "IDP_COMPLETED", createdAt: "2026-10-01T00:00:00Z" }], nextCursor: older ? null : "older-1" } }),
          }) }] };
        } else if (url.pathname === "/api/chat") {
          if (chatFailures) { chatFailures -= 1; status = 500; payload = { error: "failed" }; }
          else if (parsed && (parsed.selectedDocumentId === "doc-2" || parsed.selectedDocumentId === "doc-3")) payload = { answer: "La respuesta canónica sigue disponible desde las fuentes autorizadas.", citations: [], evidenceStatus: "answerable", operationStatus: "ok", correlationId: "corr-fallback" };
          else payload = { answer: "Importe: 1250 EUR. Origen: LITERAL. requiere revisión humana.", idpMetadata: { field: "amount", value: 1250, presence: "PRESENT", origin: "LITERAL", acceptance: "REVIEW_REQUIRED", evidence: [{ page: 2, quote: "1.250 EUR", contentSha256: "b".repeat(64) }] }, citations: [{ citationId: "citation-1", documentId: "doc-1", documentName: "contrato.pdf", pageNumber: 2 }], evidenceStatus: "ambiguous", disclaimerRequired: true, operationStatus: "ok", correlationId: "corr-2" };
        }
        const finish = () => { if (!response.headersSent) { response.writeHead(status, { "content-type": "application/json" }); response.end(JSON.stringify(payload)); } };
        if (url.pathname === "/api/chat" && delayNextChat) { delayNextChat = false; setTimeout(finish, 120); } else finish();
        return;
      }
      if (response.headersSent) return;
      const relative = url.pathname === "/" ? "/index.html" : url.pathname;
      const file = path.join(frontendRoot, relative.replace(/^\//, ""));
      if (!file.startsWith(frontendRoot)) { response.writeHead(404); response.end(); return; }
      try {
        const contentType = relative.endsWith(".css") ? "text/css" : relative.endsWith(".js") ? "application/javascript" : "text/html";
        const contents = fs.readFileSync(file);
        response.writeHead(200, { "content-type": contentType }); response.end(contents);
      }
      catch (_error) { response.writeHead(404); response.end(); }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE, headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 375, height: 800 } });
    await page.goto(`http://127.0.0.1:${address.port}/`, { waitUntil: "networkidle" });
    await page.selectOption("#matter-select", "matter-1");
    await page.locator("#workspace-tab-documents").click();
    assert.equal(await page.locator("#document-list .document-view-action").count(), 2);
    await page.locator("#document-list .document-item").first().click();
    await assert.doesNotReject(() => page.locator("#idp-status").filter({ hasText: "Extracción disponible" }).waitFor());
    assert.equal(await page.locator("#idp-field-controls").isHidden(), false);
    assert.notEqual(await page.locator("#idp-field-select option:checked").innerText(), "");
    assert.equal(await page.locator("#idp-field-query").isDisabled(), false);
    await page.locator("#idp-field-select").evaluate((select) => { select.value = ""; select.dispatchEvent(new Event("change", { bubbles: true })); });
    assert.equal(await page.locator("#idp-field-query").isDisabled(), true, "blank field selection cannot submit");
    await page.selectOption("#idp-field-select", "amount");
    assert.equal(await page.locator("#idp-field-query").isDisabled(), false);
    await page.locator("#idp-field-query").click();
    await page.locator("#idp-result").waitFor();
    assert.match(await page.locator("#idp-result").innerText(), /1250/);
    const chatRequest = requests.find((entry) => entry.path === "/api/chat");
    assert.equal(chatRequest.body.selectedDocumentId, "doc-1");
    assert.equal(chatRequest.body.selectedFieldName, "amount");
    assert.equal(typeof chatRequest.body.question, "string");
    await page.getByRole("button", { name: "Cargar versiones anteriores" }).click();
    await page.waitForTimeout(50);
    const historyRequest = requests.filter((entry) => entry.path === "/api/mcp").at(-1);
    assert.equal(historyRequest.body.params.arguments.historyCursor, "older-1");
    chatFailures = 1;
    await page.locator("#idp-field-query").click();
    await page.locator("#idp-state").filter({ hasText: "no se pudo completar" }).waitFor();
    assert.match(await page.locator("#idp-result").innerText(), /1250/, "failed refresh retains the current result");

    // Existing review-form validation remains inline and resets after success.
    await page.locator("#workspace-tab-consultation").click();
    await page.locator("#question").fill("¿Cuál es el importe indicado?");
    await page.locator("#ask-button").click();
    await page.locator("#review-button").waitFor({ state: "visible" });
    await page.locator("#review-button").click();
    await page.locator("#review-submit-button").click();
    assert.equal(await page.locator("#review-reason").getAttribute("aria-invalid"), "true");
    assert.equal(await page.locator("#review-reason-error").isHidden(), false);
    assert.equal(await page.locator("#review-due-at").getAttribute("aria-invalid"), "true");
    assert.equal(await page.locator("#review-due-at-error").isHidden(), false);
    await page.locator("#review-reason").selectOption("ambiguous_evidence");
    await page.locator("#review-due-at").fill(new Date().toISOString().slice(0, 10));
    assert.equal(await page.locator("#review-reason-error").isHidden(), true);
    assert.equal(await page.locator("#review-due-at-error").isHidden(), true);
    await page.locator("#review-note").fill("Validación de contrato.");
    await page.locator("#review-submit-button").click();
    await page.locator("#app-status").filter({ hasText: "Revisión guardada" }).waitFor();
    assert.equal(reviewPostBody.reasonCode, "ambiguous_evidence");
    assert.equal(await page.locator("#review-form").isHidden(), true);
    assert.equal(await page.locator("#review-reason").inputValue(), "");
    assert.equal(await page.locator("#review-due-at").inputValue(), "");
    assert.equal(await page.locator("#review-note").inputValue(), "");

    // IDP correction submits the raw typed value and the edited page/quote
    // anchor, while keeping server-owned origin/presence/acceptance absent.
    await page.locator("#workspace-tab-reviews").click();
    const review = page.locator('details[data-review-id="idp-review-1"]');
    await review.locator("summary").click();
    const correct = review.locator('button[data-idp-action="CORRECT"]');
    assert.equal(await review.locator("button").count(), 3, await review.innerText());
    await correct.click();
    const reviewReason = review.locator('textarea[data-idp-reason="amount"]');
    const correction = review.locator('[data-idp-correction="amount"]');
    const evidencePage = review.locator('input[data-idp-evidence-page="amount"]');
    const evidenceQuote = review.locator('textarea[data-idp-evidence-quote="amount"]');
    await reviewReason.fill("El pasaje autorizado muestra otra cifra.");
    await correct.click();
    assert.equal(await review.locator('[data-idp-error="amount"]').isHidden(), false);
    assert.match(await review.locator('[data-idp-error="amount"]').innerText(), /página|cita|valor|motivo/);
    await correction.fill("1300");
    await evidencePage.fill("0");
    await correct.click();
    assert.match(await review.locator('[data-idp-error="amount"]').innerText(), /página válida/);
    await evidencePage.fill("4");
    await evidenceQuote.fill("");
    await correct.click();
    assert.match(await review.locator('[data-idp-error="amount"]').innerText(), /cita/);
    await evidenceQuote.fill("Total revisado: 1.300 EUR");
    await correction.fill("");
    await correct.click();
    assert.match(await review.locator('[data-idp-error="amount"]').innerText(), /valor corregido/);
    await correction.fill("1300");
    await correct.click();
    await page.waitForTimeout(250);
    assert.ok(reviewPatchBody, JSON.stringify(requests.slice(-5)));
    assert.match(await page.locator("#app-status").innerText(), /Decisión de campo guardada|Todos los campos han quedado resueltos/, `unexpected app status: ${await page.locator("#app-status").innerText()} / error: ${await page.locator("#app-error").innerText()}`);
    assert.equal(reviewPatchBody.idpDecision.fieldName, "amount");
    assert.equal(reviewPatchBody.idpDecision.action, "CORRECT");
    assert.equal(reviewPatchBody.idpDecision.proposedValue, 1300);
    assert.equal(reviewPatchBody.idpDecision.evidence[0].page, 4);
    assert.equal(reviewPatchBody.idpDecision.evidence[0].quote, "Total revisado: 1.300 EUR");
    assert.equal(Object.hasOwn(reviewPatchBody.idpDecision, "origin"), false);
    assert.equal(Object.hasOwn(reviewPatchBody.idpDecision, "presence"), false);
    assert.equal(Object.hasOwn(reviewPatchBody.idpDecision, "acceptance"), false);
    assert.equal(await review.locator('textarea[data-idp-reason="amount"]').inputValue(), "", "success rerender clears the reason");
    assert.equal(await review.locator('[data-idp-correction="amount"]').inputValue(), "", "success rerender clears the correction");

    // A stale response from the previous document must not render after the
    // user changes selection; the canonical fallback remains queryable.
    await page.locator("#workspace-tab-documents").click();
    await page.locator("#document-list .document-item").first().click();
    await page.locator("#idp-status").filter({ hasText: "Extracción disponible" }).waitFor();
    delayNextChat = true;
    await page.locator("#idp-field-query").click();
    await page.locator("#document-list .document-item").nth(1).click();
    await page.locator("#idp-state").filter({ hasText: "no expone" }).waitFor();
    await page.waitForTimeout(180);
    assert.equal(await page.locator("#idp-result").isHidden(), true, "stale doc-1 response is discarded after doc-2 selection");
    await page.selectOption("#idp-field-select", "amount");
    await page.locator("#idp-field-query").click();
    await page.locator("#idp-result").filter({ hasText: "respuesta documental" }).waitFor();
    assert.match(await page.locator("#idp-result").innerText(), /respuesta canónica/);
    const fallbackRequest = requests.filter((entry) => entry.path === "/api/chat").at(-1);
    assert.equal(fallbackRequest.body.selectedDocumentId, "doc-2");
    assert.equal(fallbackRequest.body.selectedFieldName, "amount");

    // Changing matter resets selected IDP result and does not leak the prior
    // answer into the second matter.
    await page.selectOption("#matter-select", "matter-2");
    assert.equal(await page.locator("#idp-result").isHidden(), true, "matter change clears the selected result immediately");
    await page.locator("#document-list .document-item").first().waitFor();
    assert.equal(await page.locator("#idp-result").isHidden(), true);
    await page.locator("#document-list .document-item").first().click();
    await page.locator("#idp-state").filter({ hasText: "no expone" }).waitFor();
    assert.equal(await page.locator("#idp-field-controls").isHidden(), false, "fallback field query remains available when IDP metadata is absent");
    if (process.env.IDP_UI_SCREENSHOTS) {
      await page.selectOption("#matter-select", "matter-1");
      await page.locator("#document-list .document-item").first().click();
      await page.locator("#idp-status").filter({ hasText: "Extracción disponible" }).waitFor();
      await page.locator("#idp-field-query").click();
      await page.locator("#idp-result").filter({ hasText: "1250" }).waitFor();
      await page.screenshot({ path: path.join(process.env.IDP_UI_SCREENSHOTS, "idp-375.png"), fullPage: true });
      const desktop = await browser.newPage({ viewport: { width: 1365, height: 1000 } });
      await desktop.goto(`http://127.0.0.1:${address.port}/`, { waitUntil: "networkidle" });
      await desktop.selectOption("#matter-select", "matter-1");
      await desktop.locator("#workspace-tab-documents").click();
      await desktop.locator("#document-list .document-item").first().click();
      await desktop.locator("#idp-status").filter({ hasText: "Extracción disponible" }).waitFor();
      await desktop.locator("#idp-field-query").click();
      await desktop.locator("#idp-result").filter({ hasText: "1250" }).waitFor();
      await desktop.screenshot({ path: path.join(process.env.IDP_UI_SCREENSHOTS, "idp-desktop.png"), fullPage: true });
      await desktop.close();
    }
    console.log(JSON.stringify({ result: "PASS", test: "phase14-idp-frontend-browser" }));
  } finally {
    await browser.close();
    await new Promise((resolve) => server.close(resolve));
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });

async function runVmBehavioralFlow() {
  class FakeElement {
    constructor(id = "", tagName = "div") { this.id = id; this.tagName = tagName; this.children = []; this.dataset = {}; this.hidden = false; this.disabled = false; this.value = ""; this.textContent = ""; this.files = []; this.attributes = {}; this.listeners = {}; this.parentElement = null; }
    get lastChild() { return this.children.at(-1) || null; }
    append(...children) { children.flat().forEach((child) => { if (child) { child.parentElement = this; this.children.push(child); } }); }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; }
    getAttribute(name) { return this.attributes[name]; }
    addEventListener(type, listener) { (this.listeners[type] ||= []).push(listener); }
    dispatchEvent(event) { event.target ||= this; event.preventDefault ||= (() => {}); (this.listeners[event.type] || []).forEach((listener) => listener(event)); }
    querySelectorAll() { return []; }
    querySelector() { return null; }
    closest() { return null; }
    focus() {}
  }
  const ids = ["matter-select", "document-file", "upload-button", "question", "ask-button", "sync-button", "sync-helper", "metadata-button", "review-button", "audit-button", "login-button", "logout-button", "review-reason", "review-note", "review-due-at", "document-count", "document-status", "document-list", "document-incomplete-list", "document-tabs", "document-tab-available", "document-tab-incomplete", "document-panel-available", "document-panel-incomplete", "document-tab-available-count", "document-tab-incomplete-count", "history-list", "answer", "citation-list", "empty-citations", "citation-inspection", "operator-output", "operator-summary", "operator-json", "operator-documents", "operator-timeline", "matter-kicker", "auth-status", "app-error", "app-status", "evidence-status", "question-count", "review-form", "review-submit-button", "review-cancel-button", "review-form-status", "review-reason-error", "review-due-at-error", "reviews-summary", "reviews-pending", "reviews-resolved", "technical-diagnostics", "idp-panel", "idp-heading", "idp-status", "idp-state", "idp-field-controls", "idp-field-select", "idp-field-query", "idp-result", "idp-history", "document-action-status", "prepare-button", "prepare-helper", "upload-workspace", "upload-progress", "upload-progress-message", "selected-file-status", "disclaimer", "inspection-meta", "inspection-passage"];
  const elements = Object.fromEntries(ids.map((id) => [id, new FakeElement(id)]));
  elements["auth-status"].append(new FakeElement("auth-dot", "span"), new FakeElement("auth-user", "span"));
  elements["evidence-status"].querySelector = () => new FakeElement("evidence-value", "span");
  elements["matter-select"].selectedOptions = [];
  let domReady;
  const documentApi = {
    activeElement: null,
    addEventListener(type, listener) { if (type === "DOMContentLoaded") domReady = listener; },
    getElementById(id) { return elements[id] || (elements[id] = new FakeElement(id)); },
    createElement(tagName) { return new FakeElement("", tagName); },
    querySelectorAll() { return []; }, querySelector() { return null; }, documentElement: {},
  };
  const calls = [];
  const fakeJson = (value, ok = true, status = 200) => ({ ok, status, headers: { get: () => "application/json" }, json: async () => value });
  const fakeFetch = async (url, options = {}) => {
    const parsed = options.body ? JSON.parse(options.body) : null; calls.push({ url, body: parsed });
    if (url === "/api/me") return fakeJson({ userId: "vm-user", csrfToken: "csrf" });
    if (url === "/api/matters") return fakeJson({ matters: [{ matterId: "matter-1", name: "Expediente VM" }] });
    if (url === "/api/conversations") return fakeJson({ conversationId: "conversation-1", sessionId: "session-1", correlationId: "corr-1" });
    if (url === "/api/matters/matter-1/documents") return fakeJson({ documents: [{ documentId: "doc-1", matterId: "matter-1", name: "contrato.pdf", status: "INDEXED", fileSizeBytes: 1, uploadedAt: "2026-10-01T00:00:00Z" }] });
    if (url.startsWith("/api/conversations/")) return fakeJson({ events: [] });
    if (url.startsWith("/api/matters/matter-1/reviews")) return fakeJson({ tasks: [] });
    if (url === "/api/mcp") return fakeJson({ content: [{ type: "text", text: JSON.stringify({ document: { documentId: "doc-1", matterId: "matter-1", name: "contrato.pdf", status: "INDEXED" }, idp: { status: "IDP_COMPLETED", fields: [{ name: "amount", valueType: "number" }], history: [{ runId: "run-1", status: "IDP_COMPLETED", createdAt: "2026-10-01T00:00:00Z" }], nextCursor: null } }) }] });
    if (url === "/api/chat") return fakeJson({ answer: "Importe: 1250 EUR. Origen: LITERAL.", idpMetadata: { field: "amount", value: 1250, presence: "PRESENT", origin: "LITERAL", acceptance: "REVIEW_REQUIRED", evidence: [{ page: 2, quote: "1.250 EUR", contentSha256: "a".repeat(64) }] }, operationStatus: "ok", evidenceStatus: "ambiguous", citations: [] });
    return fakeJson({}, false, 404);
  };
  const windowApi = { matchMedia: () => ({ matches: false }), setTimeout, clearTimeout, location: { assign() {} }, LegalDeskCitationPanel: { renderChatResponse() {}, renderLoadingState() {}, renderOperationalState() {} } };
  const context = vm.createContext({ window: windowApi, document: documentApi, Intl, DOMException, AbortController, Headers, fetch: fakeFetch, setTimeout, clearTimeout, getComputedStyle: () => ({ getPropertyValue: () => "" }) });
  vm.runInContext(source, context);
  domReady();
  await new Promise((resolve) => setTimeout(resolve, 0));
  elements["matter-select"].value = "matter-1"; elements["matter-select"].selectedOptions = [{ textContent: "Expediente VM" }];
  elements["matter-select"].listeners.change[0]({ type: "change", target: { value: "matter-1" } });
  await new Promise((resolve) => setTimeout(resolve, 100));
  const documentItem = elements["document-list"].children[0];
  assert.ok(documentItem, "document list rendered through the real app path");
  documentItem.dispatchEvent({ type: "click", target: documentItem });
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(elements["idp-status"].textContent, "Extracción disponible");
  elements["idp-field-query"].dispatchEvent({ type: "click", target: elements["idp-field-query"] });
  await new Promise((resolve) => setTimeout(resolve, 10));
  const chat = calls.find((call) => call.url === "/api/chat");
  assert.equal(chat.body.selectedDocumentId, "doc-1"); assert.equal(chat.body.selectedFieldName, "amount");
  const flattenText = (node) => [node.textContent || "", ...node.children.map(flattenText)].join(" ");
  assert.match(flattenText(elements["idp-result"]), /1250/);
  console.log(JSON.stringify({ result: "PASS", test: "phase14-idp-frontend-vm-behavior" }));
}
