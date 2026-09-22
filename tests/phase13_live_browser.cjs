/*
 * One-shot Phase 13 live browser smoke.
 *
 * This runner intentionally contains no route mocks, fixtures, retries,
 * image artifacts, or response-text logging. It must be pointed at the already
 * prepared loopback application and an actual Cognito hosted-login flow.
 */
const fs = require("node:fs");
const path = require("node:path");
const { findFirstVisible, waitForFirstVisible, waitForIndexedState } = require("./phase13_live_browser_helpers.cjs");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const BASE_URL = process.env.LEGALDESK_SMOKE_BASE_URL || "http://localhost:8000";
const USERNAME = process.env.LEGALDESK_SMOKE_USERNAME;
const PASSWORD = process.env.LEGALDESK_SMOKE_PASSWORD;
const PDF_PATH = process.env.LEGALDESK_SMOKE_PDF || path.resolve("output", "pdf", "fictional-storage-note.pdf");
const MATTER_A = "mat_phase13_a_20260921";
const MATTER_B = "mat_phase13_b_20260921";
const FACTUAL_QUESTION = "How long does Acme Orchard Ltd have to pay after receipt of an invoice?";
const ABSENT_QUESTION = "Where is the emergency assembly point?";
const PAYMENT_TERMS = ["23 calendar days", "receipt of an invoice"];
let currentPhase = "startup";
let currentStep = "startup";
const phaseProgress = Object.create(null);
const diagnostics = { http: [], factual: null };
const SAFE_ERROR_TYPES = new Set(["Error", "TimeoutError", "TypeError", "ReferenceError", "RangeError", "AssertionError"]);

class SmokeFailure extends Error {
  constructor(category, errorType = "SmokeFailure") {
    super(category);
    this.category = category;
    this.errorType = errorType;
  }
}

function fail(category) {
  throw new SmokeFailure(category);
}

function safeId(value) {
  return typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value);
}

async function fillFirstVisible(page, selectors, value, category) {
  const candidate = await findFirstVisible(page, selectors);
  if (!candidate) fail(category);
  await candidate.fill(value);
}

async function clickFirstVisible(page, selectors, category) {
  const candidate = await findFirstVisible(page, selectors);
  if (!candidate) fail(category);
  await candidate.click();
}

function beginPhase(name) {
  currentPhase = name;
  currentStep = name;
  phaseProgress[name] = "started";
}

function finishPhase(name) {
  phaseProgress[name] = "passed";
}

async function waitForIndexed(page) {
  const outcome = await waitForIndexedState(page);
  if (outcome !== "indexed") fail("index_application_error");
}

function safeErrorType(error) {
  const name = error && typeof error.name === "string" ? error.name : "";
  return SAFE_ERROR_TYPES.has(name) ? name : "UnknownError";
}

async function loginStep(name, action) {
  currentStep = name;
  try {
    return await action();
  } catch (error) {
    if (error instanceof SmokeFailure) throw error;
    throw new SmokeFailure(name, safeErrorType(error));
  }
}

async function responseJson(responsePromise, category) {
  const response = await responsePromise;
  if (!response.ok()) fail(category);
  try {
    return await response.json();
  } catch (_error) {
    fail(category);
  }
}

function apiResponse(page, pathSuffix, method, category) {
  return page.waitForResponse((response) => {
    try {
      const url = new URL(response.url());
      return url.origin === new URL(BASE_URL).origin && url.pathname.endsWith(pathSuffix) && response.request().method() === method;
    } catch (_error) {
      return false;
    }
  }, { timeout: 120_000 }).then((response) => responseJson(Promise.resolve(response), category));
}

async function run() {
  if (!USERNAME || !PASSWORD) fail("missing_credentials");
  if (!fs.existsSync(PDF_PATH)) fail("missing_smoke_pdf");
  const base = new URL(BASE_URL);
  if (base.protocol !== "http:" || base.hostname !== "localhost") fail("invalid_loopback_base");

  let browser;
  let context;
  let page;
  let pageErrorCount = 0;
  const requestCounts = Object.create(null);
  try {
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE, headless: true });
    context = await browser.newContext();
    page = await context.newPage();
    page.on("pageerror", () => { pageErrorCount += 1; });
    page.on("response", response => {
      const url = new URL(response.url());
      if (url.origin !== base.origin) return;
      const route = ["/callback", "/api/chat", "/confirm", "/sync"].find(value => url.pathname.endsWith(value));
      if (route && diagnostics.http.length < 12) diagnostics.http.push({ route, status: response.status() });
    });
    page.on("request", (request) => {
      try {
        const url = new URL(request.url());
        if (url.origin !== base.origin || !url.pathname.startsWith("/api/")) return;
        const key = `${request.method()} ${url.pathname}`;
        requestCounts[key] = (requestCounts[key] || 0) + 1;
      } catch (_error) {
        // Request accounting is diagnostic only; never emit URL/error data.
      }
    });

    beginPhase("login");
    await loginStep("login.goto", () => page.goto(base.toString(), { waitUntil: "domcontentloaded", timeout: 30_000 }));
    await loginStep("login.click", () => page.locator("#login-button").click());
    await loginStep("login.form_visible", () => waitForFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"], { timeout: 120_000 }));
    await loginStep("login.fill_username", () => fillFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"], USERNAME, "cognito_username_form"));
    await loginStep("login.fill_password", () => fillFirstVisible(page, ["input[name='password']", "#signInFormPassword"], PASSWORD, "cognito_password_form"));
    await loginStep("login.submit", () => clickFirstVisible(page, ["button[name='signInSubmitButton']", "input[type='submit']", "button[type='submit']"], "cognito_submit"));
    await loginStep("login.callback", () => page.waitForURL((url) => url.origin === base.origin && ["/", "/index.html"].includes(url.pathname), { timeout: 120_000 }));
    const me = await loginStep("login.session_bootstrap", () => page.evaluate(async () => {
      const response = await fetch("/api/me", { credentials: "same-origin" });
      return { status: response.status, body: response.ok ? await response.json() : null };
    }));
    if (me.status !== 200 || !me.body || typeof me.body.csrfToken !== "string" || !me.body.csrfToken) fail("session_bootstrap");
    const csrfToken = me.body.csrfToken;
    finishPhase("login");

    beginPhase("matter_selection");
    await page.locator("#matter-select").waitFor({ state: "visible", timeout: 30_000 });
    await page.locator("#matter-select").selectOption(MATTER_A);
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled, null, { timeout: 30_000 });
    finishPhase("matter_selection");

    beginPhase("upload_and_index");
    const authorizationPromise = apiResponse(page, "/upload-authorizations", "POST", "upload_authorization");
    await page.locator("#document-file").setInputFiles(PDF_PATH);
    await page.locator("#upload-button").click();
    const authorization = await authorizationPromise;
    const documentId = authorization && authorization.document && authorization.document.documentId;
    if (!safeId(documentId)) fail("upload_metadata");
    await waitForIndexed(page);
    finishPhase("upload_and_index");

    beginPhase("factual_chat");
    await page.locator("#question").fill(FACTUAL_QUESTION);
    const factualResponsePromise = apiResponse(page, "/api/chat", "POST", "factual_chat_response");
    await page.locator("#ask-button").click();
    const factual = await factualResponsePromise;
    diagnostics.factual = {
      operationStatus: ["ok", "error", "blocked"].includes(factual.operationStatus) ? factual.operationStatus : "unknown",
      evidenceStatus: ["answerable", "ambiguous", "insufficient_evidence"].includes(factual.evidenceStatus) ? factual.evidenceStatus : null,
      citationCount: Array.isArray(factual.citations) ? factual.citations.length : 0,
    };
    if (factual.operationStatus !== "ok" || factual.evidenceStatus !== "answerable" || !safeId(factual.correlationId)) fail("factual_contract");
    const factualCitations = Array.isArray(factual.citations) ? factual.citations : [];
    if (!factualCitations.length || !factualCitations.some((item) => item && item.documentId === documentId && safeId(item.handle))) fail("factual_citation");
    await page.waitForFunction(() => /\b(?:23|twenty[ -]three)\s+calendar\s+days\b/i.test(document.querySelector("#answer").textContent), null, { timeout: 30_000 });
    await page.locator(".citation-inspect").first().click();
    await page.waitForFunction((terms) => terms.every((term) => document.querySelector("#inspection-passage").textContent.includes(term)), PAYMENT_TERMS, { timeout: 30_000 });
    finishPhase("factual_chat");

    beginPhase("absent_chat");
    await page.locator("#question").fill(ABSENT_QUESTION);
    const absentResponsePromise = apiResponse(page, "/api/chat", "POST", "absent_chat_response");
    await page.locator("#ask-button").click();
    const absent = await absentResponsePromise;
    if (absent.operationStatus !== "ok" || absent.evidenceStatus !== "insufficient_evidence" || !Array.isArray(absent.citations) || absent.citations.length !== 0) fail("absent_contract");
    await page.waitForFunction(() => document.querySelector("#evidence-status").dataset.status === "insufficient_evidence", null, { timeout: 30_000 });
    if (await page.locator(".citation-inspect").count() !== 0) fail("absent_citations");
    finishPhase("absent_chat");

    beginPhase("metadata_tool");
    const operationCorrelationId = absent.correlationId;
    if (!safeId(operationCorrelationId)) fail("absent_correlation");
    const metadataResponsePromise = apiResponse(page, "/api/mcp", "POST", "metadata_response");
    await page.locator("#metadata-button").click();
    const metadata = await metadataResponsePromise;
    const metadataResult = metadata && metadata.result;
    if (metadata.status !== "SUCCESS" || typeof metadata.tool !== "string" || !metadata.tool.endsWith("list_matter_documents") || metadata.correlationId !== operationCorrelationId || !metadataResult || !Array.isArray(metadataResult.documents)) fail("metadata_contract");
    finishPhase("metadata_tool");

    beginPhase("review_tool");
    const reviewResponsePromise = apiResponse(page, "/review", "POST", "review_response");
    await page.locator("#review-button").click();
    const review = await reviewResponsePromise;
    if (!safeId(review.reviewTaskId) || review.status !== "open" || review.correlationId !== operationCorrelationId) fail("review_contract");
    finishPhase("review_tool");

    beginPhase("cross_matter_denial");
    const deniedStatus = await page.evaluate(async ({ matterId, csrf }) => {
      const response = await fetch(`/api/matters/${encodeURIComponent(matterId)}/documents`, {
        credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf },
      });
      return response.status;
    }, { matterId: MATTER_B, csrf: csrfToken });
    if (deniedStatus !== 403) fail("cross_matter_not_denied");
    finishPhase("cross_matter_denial");

    beginPhase("audit");
    const auditResponsePromise = apiResponse(page, "/api/audit", "GET", "audit_response");
    await page.locator("#audit-button").click();
    const audit = await auditResponsePromise;
    const events = audit && Array.isArray(audit.events) ? audit.events : [];
    const safeOperations = events.filter((event) => event && event.matterId === MATTER_A && typeof event.operation === "string").map((event) => event.operation);
    if (!safeOperations.includes("chat") || !safeOperations.includes("mcp_metadata") || !safeOperations.includes("review_created")) fail("audit_contract");
    const reviewAudit = events.find((event) => event && event.matterId === MATTER_A && event.operation === "review_created" && event.correlationId === review.correlationId);
    if (!reviewAudit) fail("review_audit_scope");
    if (pageErrorCount !== 0) fail("browser_page_error");
    finishPhase("audit");

    beginPhase("logout");
    const logoutResponsePromise = apiResponse(page, "/logout", "POST", "logout_response");
    await page.locator("#logout-button").click();
    await logoutResponsePromise;
    await page.waitForFunction(() => document.querySelector("#question").disabled, null, { timeout: 30_000 });
    finishPhase("logout");

    return {
      result: "PASS",
      smoke: "phase13-live-browser",
      matterId: MATTER_A,
      documentId,
      factual: { evidenceStatus: factual.evidenceStatus, citationCount: factualCitations.length, correlationId: factual.correlationId },
      absent: { evidenceStatus: absent.evidenceStatus, citationCount: Array.isArray(absent.citations) ? absent.citations.length : 0 },
      metadata: { status: metadata.status, tool: metadata.tool, correlationId: metadata.correlationId, documentCount: metadataResult.documents.length },
      review: { reviewTaskId: review.reviewTaskId, status: review.status, correlationId: review.correlationId },
      denied: { matterId: MATTER_B, status: deniedStatus },
      audit: { eventCount: events.length, operations: [...new Set(safeOperations)].sort() },
      requestCounts,
    };
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
}

run().then((result) => {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}).catch((error) => {
  const category = error instanceof SmokeFailure ? error.category : "smoke_failed";
  const errorType = error instanceof SmokeFailure ? error.errorType : safeErrorType(error);
  process.stdout.write(`${JSON.stringify({ result: "FAIL", smoke: "phase13-live-browser", phase: currentPhase, step: currentStep, category, errorType, progress: phaseProgress, diagnostics })}\n`);
  process.exitCode = 1;
});
