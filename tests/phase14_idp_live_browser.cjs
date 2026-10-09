/*
 * Bounded production-IDP browser child.
 *
 * The parent supplies one synthetic fixture and one short-lived Cognito user.
 * This child never receives AWS credentials, an M2M secret, document text, or
 * a real user's credentials.  It emits only hashes, IDs, statuses and counts.
 */
const fs = require("fs");
const crypto = require("crypto");
const { findFirstVisible, waitForFirstVisible } = require("./phase13_live_browser_helpers.cjs");

const BASE_URL = process.env.LEGALDESK_P14_BASE_URL || "";
const IDP_HOST = process.env.LEGALDESK_P14_IDP_HOST || "";
const USERNAME = process.env.LEGALDESK_P14_USERNAME || "";
const PASSWORD = process.env.LEGALDESK_P14_PASSWORD || "";
const MATTER_ID = process.env.LEGALDESK_P14_MATTER_ID || "";
const CROSS_MATTER_ID = process.env.LEGALDESK_P14_CROSS_MATTER_ID || "";
const FIXTURE_PATH = process.env.LEGALDESK_IDP_FIXTURE_PATH || "";
const FIXTURE_ID = process.env.LEGALDESK_IDP_FIXTURE_ID || "";
const EXPECTED_SOURCE = process.env.LEGALDESK_IDP_EXPECTED_SOURCE || "IDP";
const IDP_QUESTION = process.env.LEGALDESK_IDP_QUESTION || "";
const EXPECTED_SKIP_REASON = process.env.LEGALDESK_IDP_EXPECTED_SKIP_REASON || "";
const REVIEW_ACTION = process.env.LEGALDESK_IDP_REVIEW_ACTION || "none";
const REVIEW_FIELD = process.env.LEGALDESK_IDP_REVIEW_FIELD || "";
const POLL_LIMIT = 20;
const POLL_MS = 15_000;
const API_CALL_LIMIT = 120;
const DEADLINE_EPOCH_MS = Number(process.env.LEGALDESK_IDP_DEADLINE_EPOCH_MS || 0);
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
let phase = "startup";
let step = "startup";
let diagnostics = {};
const startedAt = Date.now();

class SmokeFailure extends Error {
  constructor(category, errorType = "SmokeFailure") { super(category); this.category = category; this.errorType = errorType; }
}

function safeErrorType(error) {
  const value = error && typeof error.name === "string" ? error.name : "UnknownError";
  return new Set(["Error", "TimeoutError", "TypeError", "ReferenceError", "RangeError", "AssertionError", "SmokeFailure"]).has(value) ? value : "UnknownError";
}

function setPhase(value) { phase = value; step = value; process.stdout.write(`${JSON.stringify({ smoke: "phase14-idp-browser-progress", phase: value })}\n`); }

function emitCleanupProgress(value, ids = {}) {
  const safe = { smoke: "phase14-idp-browser-progress", phase: value };
  if (ids.conversationId) safe.conversationId = ids.conversationId;
  if (ids.sessionId) safe.sessionId = ids.sessionId;
  if (ids.documentId) safe.documentId = ids.documentId;
  if (Array.isArray(ids.scopes)) safe.scopes = ids.scopes;
  process.stdout.write(`${JSON.stringify(safe)}\n`);
}

function validOrigin(value, protocol) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === protocol && !parsed.username && !parsed.password && !parsed.search && !parsed.hash && (parsed.pathname === "" || parsed.pathname === "/") && Boolean(parsed.hostname);
  } catch (_error) { return false; }
}

function fixtureDigest() {
  if (!FIXTURE_PATH || !fs.existsSync(FIXTURE_PATH)) throw new SmokeFailure("fixture_missing");
  const stat = fs.statSync(FIXTURE_PATH);
  if (!stat.isFile() || stat.size > 20 * 1024 * 1024) throw new SmokeFailure("fixture_size_invalid");
  return crypto.createHash("sha256").update(fs.readFileSync(FIXTURE_PATH)).digest("hex");
}

function digestValue(value) {
  return crypto.createHash("sha256").update(JSON.stringify(value, (_key, item) => item === undefined ? null : item)).digest("hex");
}

function quoteDigest(quote) {
  if (typeof quote !== "string") throw new SmokeFailure("quote_invalid");
  return crypto.createHash("sha256").update(quote, "utf8").digest("hex");
}

function parseMcpMetadata(raw) {
  const content = raw && Array.isArray(raw.content) ? raw.content : [];
  const text = content.find(item => item && item.type === "text" && typeof item.text === "string");
  if (!text) throw new SmokeFailure("metadata_shape");
  try { return JSON.parse(text.text); } catch (_error) { throw new SmokeFailure("metadata_json"); }
}

function classifySelectedResponse(response, fieldName) {
  const idp = response && response.idp;
  const hasIDPContract = idp && idp.field === fieldName
    && typeof idp.acceptance === "string"
    && typeof idp.presence === "string"
    && typeof idp.origin === "string";
  return hasIDPContract ? "IDP" : (Array.isArray(response && response.citations) && response.citations.length ? "RAG" : "NONE");
}

function selectBoundReviewTask(tasks, expected) {
  if (!Array.isArray(tasks)) return null;
  return tasks.find(task => {
    const idp = task && task.idp;
    return task && SAFE_ID.test(task.reviewTaskId || "")
      && idp && idp.documentId === expected.documentId
      && idp.runId === expected.runId
      && idp.documentSha256 === expected.documentSha256
      && Array.isArray(idp.fields)
      && idp.fields.length > 0;
  }) || null;
}

function selectBoundReviewField(reviewTask, requestedField = REVIEW_FIELD) {
  const fields = reviewTask && reviewTask.idp && Array.isArray(reviewTask.idp.fields) ? reviewTask.idp.fields : [];
  const fieldName = requestedField || (fields[0] && fields[0].name) || "";
  if (!SAFE_ID.test(fieldName)) return null;
  return fields.find(field => field && field.name === fieldName) || null;
}

function safeHash(value) { return typeof value === "string" && SHA256.test(value) ? value : null; }

function assertDeadline() {
  if (!Number.isFinite(DEADLINE_EPOCH_MS) || DEADLINE_EPOCH_MS <= 0 || Date.now() >= DEADLINE_EPOCH_MS) {
    throw new SmokeFailure("wall_deadline_exceeded_before_browser_call");
  }
}

function validateInputs() {
  if (!validOrigin(BASE_URL, "https:")) throw new SmokeFailure("invalid_public_origin");
  if (!IDP_HOST || IDP_HOST.includes("/") || !IDP_HOST.includes(".")) throw new SmokeFailure("invalid_idp_host");
  if (!SAFE_ID.test(MATTER_ID) || !SAFE_ID.test(CROSS_MATTER_ID) || MATTER_ID === CROSS_MATTER_ID) throw new SmokeFailure("invalid_matter_selector");
  if (!SAFE_ID.test(FIXTURE_ID) || !USERNAME || !PASSWORD) throw new SmokeFailure("missing_runner_inputs");
  if (process.env.LEGALDESK_IDP_FIELD_NAME && !SAFE_ID.test(process.env.LEGALDESK_IDP_FIELD_NAME)) throw new SmokeFailure("invalid_field_selector");
  if (REVIEW_FIELD && !SAFE_ID.test(REVIEW_FIELD)) throw new SmokeFailure("invalid_review_field_selector");
  if (!["IDP", "RAG", "NONE"].includes(EXPECTED_SOURCE) || !["none", "approve", "correct"].includes(REVIEW_ACTION)) throw new SmokeFailure("invalid_expected_contract");
}

async function preflight() {
  const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
  const browser = await playwright.chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true });
  await browser.close();
  return { result: "PASS", smoke: "phase14-idp-browser", phase: "preflight", browserExecutable: true, fixtureSha256: fixtureDigest() };
}

async function responseJson(response, category) {
  if (!response.ok()) throw new SmokeFailure(category);
  try { return await response.json(); } catch (_error) { throw new SmokeFailure(category); }
}

async function run() {
  if (process.argv.includes("--preflight")) return preflight();
  assertDeadline();
  validateInputs();
  const sourceSha256 = fixtureDigest();
  const expectedStatus = process.env.LEGALDESK_IDP_EXPECTED_STATUS || "";
  const expectedDocumentType = process.env.LEGALDESK_IDP_EXPECTED_DOCUMENT_TYPE || "";
  const fieldName = process.env.LEGALDESK_IDP_FIELD_NAME || "";
  const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
  const base = new URL(BASE_URL);
  const browser = await playwright.chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true });
  const context = await browser.newContext();
  const page = await context.newPage();
  const baseOrigin = base.origin;
  let csrf = "";
  let conversationId = "";
  let sessionId = "";
  let correlationId = "";
  const cleanupScopes = [];
  const requestCounts = {};
  let apiCallCount = 0;
  let apiRouteFailure = null;
  const count = name => { requestCounts[name] = (requestCounts[name] || 0) + 1; return requestCounts[name]; };

  await page.route("**/api/**", async route => {
    const request = route.request();
    let url;
    try { url = new URL(request.url()); } catch (_error) { return route.abort("blockedbyclient"); }
    if (url.origin !== baseOrigin || !url.pathname.startsWith("/api/")) return route.continue();
    try {
      assertDeadline();
      if (apiCallCount >= API_CALL_LIMIT) throw new SmokeFailure("browser_api_budget_exceeded");
      apiCallCount += 1;
      count(`${request.method()} ${url.pathname}`);
      await route.continue();
    } catch (error) {
      apiRouteFailure = error instanceof SmokeFailure ? error : new SmokeFailure("api_route_blocked");
      await route.abort("blockedbyclient");
    }
  });

  function assertApiRouteHealthy() { if (apiRouteFailure) throw apiRouteFailure; }

  async function apiJson(path, options = {}) {
    assertDeadline();
    assertApiRouteHealthy();
    const headers = { ...(options.headers || {}), "X-CSRF-Token": csrf, "Content-Type": "application/json" };
    const response = await page.evaluate(async ({ path, options, headers }) => {
      const result = await fetch(path, { ...options, headers, credentials: "same-origin" });
      return { status: result.status, body: result.ok ? await result.json() : null };
    }, { path, options: { method: options.method || "GET", body: options.body }, headers });
    assertApiRouteHealthy();
    if (!response.body) throw new SmokeFailure(`api_${response.status}`);
    return response.body;
  }

  async function loadMetadata(documentId, historyLimit = 10) {
    const raw = await apiJson("/api/mcp", { method: "POST", body: JSON.stringify({
      jsonrpc: "2.0", id: `idp-live-${Date.now()}`, method: "tools/call",
      params: { name: "get_document_metadata", arguments: { documentId, historyLimit } },
      matterId: MATTER_ID, conversationId, sessionId, originCorrelationId: correlationId,
    }) });
    return parseMcpMetadata(raw);
  }

  async function loadReviewTasks() {
    const query = `?limit=100&conversationId=${encodeURIComponent(conversationId)}&sessionId=${encodeURIComponent(sessionId)}&originCorrelationId=${encodeURIComponent(correlationId)}`;
    const payload = await apiJson(`/api/matters/${encodeURIComponent(MATTER_ID)}/reviews${query}`);
    const summaries = payload && Array.isArray(payload.tasks) ? payload.tasks : [];
    const details = [];
    for (const summary of summaries) {
      if (!summary || !SAFE_ID.test(summary.reviewTaskId || "")) continue;
      const detail = await apiJson(`/api/matters/${encodeURIComponent(MATTER_ID)}/reviews/${encodeURIComponent(summary.reviewTaskId)}${query}`);
      if (detail && detail.idp && Array.isArray(detail.idp.fields)) details.push({ ...summary, idp: detail.idp });
    }
    return details;
  }

  function sanitizeFields(idp) {
    const fields = {};
    for (const item of (idp && Array.isArray(idp.fields) ? idp.fields : [])) {
      if (!item || typeof item.name !== "string") continue;
      const result = item;
      fields[item.name] = {
        presence: typeof result.presence === "string" ? result.presence : null,
        origin: typeof result.origin === "string" ? result.origin : null,
        acceptance: typeof result.acceptance === "string" ? result.acceptance : null,
        valueDigest: Object.prototype.hasOwnProperty.call(result, "value") ? digestValue(result.value) : null,
        evidence: Array.isArray(result.evidence) ? result.evidence.map(anchor => ({
          page: Number.isInteger(anchor && anchor.page) ? anchor.page : null,
          quoteDigest: typeof (anchor && anchor.quote) === "string" ? quoteDigest(anchor.quote) : null,
          contentSha256: safeHash(anchor && anchor.contentSha256),
        })) : [],
      };
    }
    return fields;
  }

  try {
    setPhase("login");
    await page.goto(base.toString(), { waitUntil: "domcontentloaded", timeout: 60_000 });
    await page.locator("#login-button").click();
    await waitForFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"], { timeout: 120_000 });
    const username = await findFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"]);
    const password = await findFirstVisible(page, ["input[name='password']", "#signInFormPassword"]);
    const submit = await findFirstVisible(page, ["button[name='signInSubmitButton']", "input[type='submit']", "button[type='submit']"]);
    if (!username || !password || !submit) throw new SmokeFailure("cognito_login_controls");
    await username.fill(USERNAME); await password.fill(PASSWORD); await submit.click();
    await page.waitForURL(url => url.origin === base.origin && ["/", "/index.html"].includes(url.pathname), { timeout: 120_000 });
    const me = await page.evaluate(async () => { const response = await fetch("/api/me", { credentials: "same-origin" }); return { status: response.status, body: response.ok ? await response.json() : null }; });
    if (me.status !== 200 || !me.body || typeof me.body.csrfToken !== "string") throw new SmokeFailure("session_bootstrap");
    csrf = me.body.csrfToken;
    setPhase("matter_selection");
    await page.locator("#matter-select").waitFor({ state: "visible", timeout: 30_000 });
    const conversationResponsePromise = page.waitForResponse(response => { try { return new URL(response.url()).pathname === "/api/conversations" && response.request().method() === "POST" && response.ok(); } catch (_error) { return false; } }, { timeout: 60_000 });
    await page.locator("#matter-select").selectOption(MATTER_ID);
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled, null, { timeout: 30_000 });
    const conversationResponse = await conversationResponsePromise;
    const conversation = await responseJson(conversationResponse, "conversation_create");
    conversationId = conversation && conversation.conversationId;
    sessionId = conversation && conversation.sessionId;
    correlationId = conversation && conversation.correlationId || "";
    if (!conversationId || !sessionId) throw new SmokeFailure("conversation_scope_missing");
    cleanupScopes.push({ conversationId, sessionId });
    emitCleanupProgress("conversation_ready", { conversationId, sessionId, scopes: cleanupScopes });

    setPhase("upload");
    const uploadResponse = page.waitForResponse(response => { try { return new URL(response.url()).pathname.endsWith("/upload-authorizations") && response.request().method() === "POST" && response.ok(); } catch (_error) { return false; } });
    await page.locator("#document-file").setInputFiles(FIXTURE_PATH);
    await page.locator("#upload-button").click();
    const authorization = await responseJson(await uploadResponse, "upload_authorization");
    const documentId = authorization && authorization.document && authorization.document.documentId;
    if (!SAFE_ID.test(documentId || "")) throw new SmokeFailure("upload_metadata");
    emitCleanupProgress("document_uploaded", { conversationId, sessionId, documentId, scopes: cleanupScopes });

    setPhase("idp_metadata");
    let metadata = null;
    let polls = 0;
    let terminal = false;
    for (; polls < POLL_LIMIT; polls += 1) {
      try { metadata = await loadMetadata(documentId); }
      catch (error) {
        if (polls + 1 >= POLL_LIMIT) throw error;
        assertDeadline();
        await new Promise(resolve => setTimeout(resolve, POLL_MS));
        continue;
      }
      const idp = metadata && metadata.idp;
      const status = idp && idp.status;
      if (["IDP_COMPLETED", "IDP_REVIEW_REQUIRED", "IDP_FAILED", "IDP_SKIPPED"].includes(status)) { terminal = true; break; }
      assertDeadline();
      await new Promise(resolve => setTimeout(resolve, POLL_MS));
    }
    if (!terminal || !metadata || !metadata.idp || typeof metadata.idp.status !== "string") throw new SmokeFailure("idp_poll_exhausted");
    const idp = metadata.idp;
    if (expectedStatus && idp.status !== expectedStatus) throw new SmokeFailure("idp_status_mismatch");
    if (!expectedStatus && !["IDP_COMPLETED", "IDP_REVIEW_REQUIRED"].includes(idp.status)) throw new SmokeFailure("idp_unexpected_terminal_status");
    const expectedTerminalFailure = expectedStatus && ["IDP_FAILED", "IDP_SKIPPED"].includes(idp.status);
    const observedReason = idp.reason || idp.idpReason || idp.skipReason || "";
    if (expectedStatus === "IDP_SKIPPED" && EXPECTED_SKIP_REASON && observedReason && observedReason !== EXPECTED_SKIP_REASON) throw new SmokeFailure("idp_skip_reason_mismatch");
    if (!expectedTerminalFailure && (!Array.isArray(idp.fields) || idp.fields.length === 0)) throw new SmokeFailure("idp_fields_missing");
    if (expectedDocumentType && idp.documentType !== expectedDocumentType) throw new SmokeFailure("idp_type_mismatch");
    // A preflight-skip projection may intentionally have no IDP run/hash/type;
    // if the server does expose a hash, it must still match the uploaded bytes.
    if (typeof idp.documentSha256 === "string" && idp.documentSha256 !== sourceSha256) throw new SmokeFailure("source_hash_mismatch");

    // A second authorized metadata read is observable through the public
    // surface. It proves stable replay of the projection, but deliberately
    // does not claim duplicate clean-event/paid-stage idempotency.
    const replayMetadata = await loadMetadata(documentId);
    const replayIdp = replayMetadata && replayMetadata.idp;
    const replayRunStable = expectedTerminalFailure
      ? (!idp.runId || replayIdp.runId === idp.runId)
      : (typeof idp.runId === "string" && idp.runId && typeof replayIdp.runId === "string" && replayIdp.runId === idp.runId);
    const replayHashStable = expectedTerminalFailure
      ? (!idp.documentSha256 || replayIdp.documentSha256 === idp.documentSha256)
      : replayIdp.documentSha256 === idp.documentSha256;
    const publicReadReplay = replayIdp && idp && replayRunStable && replayHashStable && replayIdp.status === idp.status
      ? { status: "EXECUTED", kind: "authorized_metadata_read", runId: idp.runId || null }
      : { status: "FAIL", kind: "authorized_metadata_read" };
    if (publicReadReplay.status !== "EXECUTED") throw new SmokeFailure("metadata_replay_changed_projection");

    let reviewTask = null;
    if (REVIEW_ACTION !== "none") {
      if (idp.status !== "IDP_REVIEW_REQUIRED") throw new SmokeFailure("review_status_not_required");
      const tasks = await loadReviewTasks();
      reviewTask = selectBoundReviewTask(tasks, { documentId, runId: idp.runId, documentSha256: sourceSha256 });
      if (!reviewTask) throw new SmokeFailure("idp_review_task_binding_missing");
    }

    setPhase("selected_field");
    let selected = { source: "NONE", citationCount: 0, evidenceStatus: null };
    if (fieldName) {
      const response = await apiJson("/api/chat", { method: "POST", body: JSON.stringify({ matterId: MATTER_ID, conversationId, sessionId, question: IDP_QUESTION || `IDP field ${fieldName}`, selectedDocumentId: documentId, selectedFieldName: fieldName }) });
      selected = {
        source: classifySelectedResponse(response, fieldName),
        citationCount: Array.isArray(response && response.citations) ? response.citations.length : 0,
        evidenceStatus: typeof (response && response.evidenceStatus) === "string" ? response.evidenceStatus : null,
        answerDigest: typeof (response && response.answer) === "string" ? digestValue(response.answer) : null,
      };
      if (selected.source !== EXPECTED_SOURCE) throw new SmokeFailure("selected_source_mismatch");
      if (EXPECTED_SOURCE === "IDP" && (!Array.isArray(response.citations) || response.citations.length === 0)) throw new SmokeFailure("idp_citations_missing");
    }

    let review = { status: "NOT_EXECUTED", action: REVIEW_ACTION, reason: REVIEW_ACTION === "none" ? "review_action_not_requested" : "" };
    if (REVIEW_ACTION !== "none") {
      setPhase("human_review");
      // Reuse the normal authenticated review list/detail UI. The machine
      // client creates the task; this browser session is the human actor.
      const reloadConversationPromise = page.waitForResponse(response => { try { return new URL(response.url()).pathname === "/api/conversations" && response.request().method() === "POST" && response.ok(); } catch (_error) { return false; } }, { timeout: 60_000 });
      await page.reload({ waitUntil: "domcontentloaded", timeout: 60_000 });
      await page.locator("#matter-select").waitFor({ state: "visible", timeout: 30_000 });
      await page.locator("#matter-select").selectOption(MATTER_ID);
      const reloadConversation = await responseJson(await reloadConversationPromise, "conversation_reload");
      conversationId = reloadConversation && reloadConversation.conversationId;
      sessionId = reloadConversation && reloadConversation.sessionId;
      correlationId = reloadConversation && reloadConversation.correlationId || "";
      if (!conversationId || !sessionId) throw new SmokeFailure("review_conversation_scope_missing");
      cleanupScopes.push({ conversationId, sessionId });
      emitCleanupProgress("review_conversation_ready", { conversationId, sessionId, documentId, scopes: cleanupScopes });
      const taskDetails = page.locator(`#reviews-pending details[data-review-id="${reviewTask.reviewTaskId}"]`);
      await taskDetails.waitFor({ state: "visible", timeout: 120_000 });
      await taskDetails.click();
      const reviewField = selectBoundReviewField(reviewTask);
      const reviewFieldName = reviewField && reviewField.name;
      if (!reviewFieldName) throw new SmokeFailure("review_field_not_bound");
      const fieldCard = taskDetails.locator(`.idp-review-field[data-idp-field="${reviewFieldName}"]`);
      await fieldCard.waitFor({ state: "visible", timeout: 30_000 });
      const reason = fieldCard.locator(".idp-review-reason");
      await reason.fill(REVIEW_ACTION === "approve" ? "Synthetic bounded approval smoke." : "Synthetic bounded correction smoke.");
      const actionName = REVIEW_ACTION === "approve" ? "APPROVE" : "CORRECT";
      const actionButton = fieldCard.locator(`.idp-review-button[data-idp-action="${actionName}"]`);
      if (REVIEW_ACTION === "correct") {
        // The real UI reveals correction/evidence controls on the first click;
        // it cannot be filled while hidden.  The second click submits the
        // now-complete, server-bound correction form.
        await actionButton.click();
        const correction = fieldCard.locator(".idp-review-correction");
        await correction.waitFor({ state: "visible", timeout: 10_000 });
        const correctionValue = process.env.LEGALDESK_IDP_CORRECTION_VALUE || "Delaware";
        if (await correction.evaluate(element => element.tagName === "SELECT")) await correction.selectOption({ label: correctionValue }).catch(() => correction.selectOption(correctionValue));
        else await correction.fill(correctionValue);
      }
      const decisionResponse = page.waitForResponse(response => { try { return new URL(response.url()).pathname.includes("/reviews/") && response.request().method() === "PATCH"; } catch (_error) { return false; } }, { timeout: 60_000 });
      await actionButton.click();
      const decision = await decisionResponse;
      if (!decision.ok()) throw new SmokeFailure("human_decision_response");
      const decisionPayload = await decision.json();
      if (!decisionPayload || decisionPayload.reviewTaskId !== reviewTask.reviewTaskId) throw new SmokeFailure("human_decision_binding_mismatch");
      review = { status: "EXECUTED", action: REVIEW_ACTION, fieldName: reviewFieldName, taskId: reviewTask.reviewTaskId, documentId, runId: idp.runId };
    }

    setPhase("cross_matter");
    assertApiRouteHealthy();
    const denied = await page.evaluate(async matter => { const response = await fetch(`/api/matters/${encodeURIComponent(matter)}/documents`, { credentials: "same-origin" }); return response.status; }, CROSS_MATTER_ID);
    if (denied !== 403) throw new SmokeFailure("cross_matter_not_denied");
    assertApiRouteHealthy();

    const historyItems = idp && Array.isArray(idp.history) ? idp.history : [];
    const history = { status: historyItems.length >= 3 ? "EXECUTED" : "NOT_EXECUTED", count: historyItems.length, reason: historyItems.length >= 3 ? null : "three-run same-document history unavailable from current public trigger" };
    const coreProof = (["IDP_COMPLETED", "IDP_REVIEW_REQUIRED"].includes(idp.status) || Boolean(expectedTerminalFailure))
      && publicReadReplay.status === "EXECUTED" && denied === 403
      && (REVIEW_ACTION === "none" || review.status === "EXECUTED");
    const cases = [{ fixtureId: FIXTURE_ID, documentType: idp.documentType || null, documentSha256: idp.documentSha256 || sourceSha256, status: idp.status || "UNKNOWN", fields: sanitizeFields(idp), derived: null, observed: {} }];
    return {
      result: coreProof ? "PASS" : "FAIL",
      proofComplete: false,
      smoke: "phase14-idp-browser", fixtureId: FIXTURE_ID, documentId,
      runId: typeof idp.runId === "string" ? idp.runId : null,
      documentSha256: sourceSha256, idpStatus: idp.status, idpReason: observedReason || null, selected, review, crossMatterStatus: denied,
      polls: { iterations: polls, max: POLL_LIMIT, intervalSeconds: POLL_MS / 1000 },
      history,
      idempotencyReplay: { status: "NOT_EXECUTED", reason: "duplicate clean-event trigger unavailable to public runner", observedPublicReadReplay: publicReadReplay },
      workerBudget: { status: "NOT_EXECUTED", reason: "model/OCR usage is not exposed by authorized metadata projection" },
      provenance: { status: "INCOMPLETE_SERVER_PROJECTION", modelId: null, promptVersion: null, releaseSha256: null, schemaVersion: idp.schemaVersion || null, sourceSha256 },
      wallSeconds: Math.ceil((Date.now() - startedAt) / 1000),
      cases, requestCounts, apiCallCount, apiCallLimit: API_CALL_LIMIT, apiCallScope: "all_same_origin_api", cleanup: { conversationId, sessionId, documentId, scopes: cleanupScopes },
    };
  } finally {
    await context.close().catch(() => {}); await browser.close().catch(() => {});
  }
}

function closedFailure(error) { return { result: "FAIL", smoke: "phase14-idp-browser", phase, step, category: error instanceof SmokeFailure ? error.category : "smoke_failed", errorType: error instanceof SmokeFailure ? error.errorType : safeErrorType(error), diagnostics }; }

if (require.main === module) {
  (async () => {
    try { process.stdout.write(`${JSON.stringify(await run())}\n`); }
    catch (error) { process.stdout.write(`${JSON.stringify(closedFailure(error))}\n`); process.exitCode = 1; }
  })();
} else {
  module.exports = { classifySelectedResponse, parseMcpMetadata, quoteDigest, selectBoundReviewTask, selectBoundReviewField };
}
