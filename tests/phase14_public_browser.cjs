/*
 * Bounded Phase 14 public-browser runner.
 *
 * This file is intentionally a child of phase14_public_smoke.py. It accepts
 * one short-lived Cognito user's credentials through the process environment,
 * emits only a closed metadata envelope, and never writes screenshots, bodies,
 * tokens, passages, signed URLs, or passwords.
 */
const { findFirstVisible, waitForFirstVisible } = require("./phase13_live_browser_helpers.cjs");

const BASE_URL = process.env.LEGALDESK_P14_BASE_URL || "";
const IDP_HOST = process.env.LEGALDESK_P14_IDP_HOST || "";
const USERNAME = process.env.LEGALDESK_P14_USERNAME;
const PASSWORD = process.env.LEGALDESK_P14_PASSWORD;
const MATTER_ID = process.env.LEGALDESK_P14_MATTER_ID || "";
const CROSS_MATTER_ID = process.env.LEGALDESK_P14_CROSS_MATTER_ID || "";
const QUESTION = process.env.LEGALDESK_P14_QUESTION || "";
const EXPECTED_FACT = process.env.LEGALDESK_P14_EXPECTED_FACT || "";
let phase = "startup";
let step = "startup";
let pageErrors = 0;
const cleanup = { conversationId: null, sessionId: null };
const diagnostics = { chat: null };

const SAFE_TYPES = new Set(["Error", "TimeoutError", "TypeError", "ReferenceError", "RangeError", "AssertionError"]);
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;

class SmokeFailure extends Error {
  constructor(category, errorType = "SmokeFailure") {
    super(category);
    this.category = category;
    this.errorType = errorType;
  }
}

function safeErrorType(error) {
  const name = error && typeof error.name === "string" ? error.name : "";
  return SAFE_TYPES.has(name) ? name : "UnknownError";
}

function validOrigin(value, protocol) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === protocol && !parsed.username && !parsed.password &&
      !parsed.search && !parsed.hash && (parsed.pathname === "" || parsed.pathname === "/") &&
      Boolean(parsed.hostname);
  } catch (_error) {
    return false;
  }
}

function loadChromium() {
  try {
    const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
    if (!playwright || !playwright.chromium) throw new TypeError("chromium unavailable");
    return playwright.chromium;
  } catch (error) {
    throw new SmokeFailure("playwright_module_unavailable", safeErrorType(error));
  }
}

function validateInputs() {
  if (!validOrigin(BASE_URL, "https:")) throw new SmokeFailure("invalid_public_origin");
  if (!IDP_HOST || IDP_HOST.includes("/") || IDP_HOST.includes("." ) === false) throw new SmokeFailure("invalid_idp_host");
  if (!SAFE_ID.test(MATTER_ID) || !SAFE_ID.test(CROSS_MATTER_ID) || MATTER_ID === CROSS_MATTER_ID) throw new SmokeFailure("invalid_matter_selector");
  if (typeof QUESTION !== "string" || !QUESTION.trim() || QUESTION.length > 1_000) throw new SmokeFailure("invalid_question");
  if (typeof EXPECTED_FACT !== "string" || !EXPECTED_FACT.trim() || EXPECTED_FACT.length > 1_000) throw new SmokeFailure("invalid_expected_fact");
  if (!USERNAME || !PASSWORD) throw new SmokeFailure("missing_credentials");
}

async function preflight() {
  const chromium = loadChromium();
  let browser;
  try {
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true });
  } catch (error) {
    throw new SmokeFailure("browser_launch_failed", safeErrorType(error));
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
  return { result: "PASS", smoke: "phase14-public-browser", phase: "preflight", browserExecutable: true };
}

async function responseJson(response, category) {
  if (!response.ok()) throw new SmokeFailure(category);
  try {
    return await response.json();
  } catch (_error) {
    throw new SmokeFailure(category);
  }
}

function setPhase(value) {
  phase = value;
  step = value;
  process.stdout.write(`${JSON.stringify({ smoke: "phase14-public-browser-progress", phase: value })}\n`);
}

function safeSelector(value) {
  return typeof value === "string" && SAFE_ID.test(value);
}

async function run() {
  if (process.argv.includes("--preflight")) return preflight();
  validateInputs();
  const chromium = loadChromium();
  const base = new URL(BASE_URL);
  let browser;
  let context;
  let page;
  const requestCounts = Object.create(null);
  const count = (name) => { requestCounts[name] = (requestCounts[name] || 0) + 1; return requestCounts[name]; };
  try {
    browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true });
    context = await browser.newContext();
    page = await context.newPage();
    page.on("pageerror", () => { pageErrors += 1; });
    await context.route("**/*", async route => {
      let url;
      try { url = new URL(route.request().url()); } catch (_error) { await route.abort("blockedbyclient"); return; }
      if (url.origin === base.origin || url.hostname === IDP_HOST) return route.continue();
      await route.abort("blockedbyclient");
    });

    setPhase("login");
    await page.goto(base.toString(), { waitUntil: "domcontentloaded", timeout: 60_000 });
    await page.locator("#login-button").click();
    await waitForFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"], { timeout: 120_000 });
    const username = await findFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"]);
    const password = await findFirstVisible(page, ["input[name='password']", "#signInFormPassword"]);
    const submit = await findFirstVisible(page, ["button[name='signInSubmitButton']", "input[type='submit']", "button[type='submit']"]);
    if (!username || !password || !submit) throw new SmokeFailure("cognito_login_controls");
    await username.fill(USERNAME);
    await password.fill(PASSWORD);
    await submit.click();
    await page.waitForURL(url => url.origin === base.origin && ["/", "/index.html"].includes(url.pathname), { timeout: 120_000 });
    const me = await page.evaluate(async () => {
      const response = await fetch("/api/me", { credentials: "same-origin" });
      return { status: response.status, body: response.ok ? await response.json() : null };
    });
    if (me.status !== 200 || !me.body || typeof me.body.csrfToken !== "string" || !me.body.csrfToken) throw new SmokeFailure("session_bootstrap");
    const csrf = me.body.csrfToken;

    setPhase("matter_selection");
    let observedConversation = null;
    let observedSession = null;
    page.on("response", async response => {
      try {
        if (new URL(response.url()).pathname === "/api/conversations" && response.request().method() === "POST" && response.ok()) {
          const body = await response.json();
          if (safeSelector(body.conversationId) && safeSelector(body.sessionId)) {
            observedConversation = body.conversationId;
            observedSession = body.sessionId;
            cleanup.conversationId = observedConversation;
            cleanup.sessionId = observedSession;
          }
        }
      } catch (_error) { /* closed diagnostic only */ }
    });
    await page.locator("#matter-select").waitFor({ state: "visible", timeout: 30_000 });
    await page.locator("#matter-select").selectOption(MATTER_ID);
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled, null, { timeout: 60_000 });
    await page.waitForFunction(() => document.querySelector("#document-list .document-item[data-status='INDEXED']"), null, { timeout: 30_000 });
    const indexedDocuments = await page.locator("#document-list .document-item[data-status='INDEXED']").count();
    if (indexedDocuments < 1) throw new SmokeFailure("indexed_document_missing");
    await page.waitForFunction(() => document.querySelector("#reviews-pending") && document.querySelector("#reviews-resolved"), null, { timeout: 30_000 });
    const reviewDetails = page.locator("#reviews-pending details, #reviews-resolved details");
    const reviewsListed = await reviewDetails.count();
    let reviewsOpened = 0;
    if (reviewsListed > 0) {
      await reviewDetails.first().click();
      await page.waitForFunction(() => Boolean(document.querySelector(".review-item-body")?.textContent?.trim()), null, { timeout: 30_000 });
      reviewsOpened = 1;
    }
    if (!observedConversation || !observedSession) throw new SmokeFailure("conversation_scope_missing");

    setPhase("chat_citation");
    const chatResponsePromise = page.waitForResponse(response => {
      try { return new URL(response.url()).origin === base.origin && new URL(response.url()).pathname === "/api/chat" && response.request().method() === "POST"; } catch (_error) { return false; }
    }, { timeout: 120_000 });
    await page.locator("#question").fill(QUESTION);
    await page.locator("#ask-button").click();
    count("POST /api/chat");
    const chatResponse = await chatResponsePromise;
    const chat = await responseJson(chatResponse, "chat_response");
    diagnostics.chat = {
      operationStatus: ["ok", "error", "blocked", "documents_processing"].includes(chat.operationStatus) ? chat.operationStatus : "unknown",
      evidenceStatus: ["answerable", "ambiguous", "insufficient_evidence"].includes(chat.evidenceStatus) ? chat.evidenceStatus : null,
      citationCount: Array.isArray(chat.citations) ? chat.citations.length : 0,
      answerContainsExpected: typeof chat.answer === "string" && chat.answer.toLowerCase().includes(EXPECTED_FACT.toLowerCase()),
    };
    if (chat.operationStatus !== "ok" || !["answerable", "ambiguous"].includes(chat.evidenceStatus) || !Array.isArray(chat.citations) || chat.citations.length < 1) throw new SmokeFailure("chat_contract");
    await page.waitForFunction(expected => (document.querySelector("#answer")?.textContent || "").toLowerCase().includes(expected.toLowerCase()), EXPECTED_FACT, { timeout: 30_000 });
    const citationButton = page.locator(".citation-inspect").first();
    await citationButton.waitFor({ state: "visible", timeout: 30_000 });
    await citationButton.click();
    await page.waitForFunction(expected => (document.querySelector("#inspection-passage")?.textContent || "").toLowerCase().includes(expected.toLowerCase()), EXPECTED_FACT, { timeout: 30_000 });

    setPhase("cross_matter");
    const deniedStatus = await page.evaluate(async matter => {
      const response = await fetch(`/api/matters/${encodeURIComponent(matter)}/documents`, { credentials: "same-origin" });
      return response.status;
    }, CROSS_MATTER_ID);
    if (deniedStatus !== 403) throw new SmokeFailure("cross_matter_not_denied");
    count("GET /api/cross-matter/documents");

    setPhase("audit");
    const auditResponsePromise = page.waitForResponse(response => {
      try { return new URL(response.url()).origin === base.origin && new URL(response.url()).pathname === "/api/audit" && response.request().method() === "GET"; } catch (_error) { return false; }
    }, { timeout: 60_000 });
    await page.locator("#audit-button").click();
    count("GET /api/audit");
    const audit = await responseJson(await auditResponsePromise, "audit_response");
    if (!Array.isArray(audit.events)) throw new SmokeFailure("audit_contract");

    setPhase("logout");
    const logoutResponsePromise = page.waitForResponse(response => {
      try { return new URL(response.url()).origin === base.origin && new URL(response.url()).pathname === "/logout" && response.request().method() === "POST"; } catch (_error) { return false; }
    }, { timeout: 60_000 });
    await page.locator("#logout-button").click();
    const logout = await logoutResponsePromise;
    if (!logout.ok()) throw new SmokeFailure("logout_response");
    await page.waitForFunction(() => document.querySelector("#question")?.disabled === true, null, { timeout: 30_000 });
    if (pageErrors !== 0 || (requestCounts["POST /api/chat"] || 0) !== 1) throw new SmokeFailure("browser_contract");
    return {
      result: "PASS", smoke: "phase14-public-browser", matterId: MATTER_ID,
      indexedDocuments, citationCount: chat.citations.length,
      reviewsListed, reviewsOpened, crossMatterStatus: deniedStatus,
      auditEventCount: audit.events.length, logoutStatus: logout.status,
      requestCounts, cleanup,
    };
  } finally {
    if (context) await context.close().catch(() => {});
    if (browser) await browser.close().catch(() => {});
  }
}

function closedFailure(error) {
  return {
    result: "FAIL", smoke: "phase14-public-browser", phase, step,
    category: error instanceof SmokeFailure ? error.category : "smoke_failed",
    errorType: error instanceof SmokeFailure ? error.errorType : safeErrorType(error),
    cleanup, diagnostics,
  };
}

(async () => {
  try {
    const result = await run();
    process.stdout.write(`${JSON.stringify(result)}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify(closedFailure(error))}\n`);
    process.exitCode = 1;
  }
})();
