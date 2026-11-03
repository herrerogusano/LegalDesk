/* Offline browser behavior test for the asynchronous agent chat UI (Phase 15 T6).
 *
 * The real local server provides login, matters and conversations; only the chat, job, document,
 * review and citation endpoints are faked, so the real DOM event path is exercised. */
const assert = require("node:assert/strict");
const { chromium, request } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

const JOB_ID = "11111111-2222-4333-8444-555555555555";
const DOCS = [
  { documentId: "doc-a", name: "Alfa.pdf", status: "INDEXED", fileSizeBytes: 8 },
  { documentId: "doc-b", name: "Beta.pdf", status: "INDEXED", fileSizeBytes: 9 },
];
const IDP_TASK = {
  reviewTaskId: "rt-idp-1", status: "open", source: "IDP", idpRunId: "run-1", idpDocumentId: "doc-b", idpDocumentSha256: "x",
  idpFieldNames: ["amount"], reason: "user_requested_review", dueAt: "2030-01-01", createdAt: "2026-11-02T10:00:00+00:00",
};
const XSS = "<img src=x onerror=window.__xss=1> El plazo es de 30 días.";
const MIXED_RESULT = {
  path: "mixed", operationStatus: "ok", correlationId: "corr-job", disclaimerRequired: true,
  citations: [
    { citationId: "idp-1", documentId: "doc-a", documentName: "Alfa.pdf", pageNumber: 1, handle: "h-idp-1" },
    { citationId: "c1", documentId: "doc-a", documentName: "Alfa.pdf", pageNumber: 2, handle: "h-c1" },
  ],
  blocks: [
    { type: "intro", text: "Esto es lo que consta en los documentos del expediente." },
    {
      type: "data", citationIds: ["idp-1"],
      groups: [{ title: "Documentos consultados", lines: [
        { text: "Alfa.pdf: Fecha de vencimiento: 2026-03-15", pending: false, citationIds: ["idp-1"], documentId: "doc-a", fieldName: "explicit_expiration_date" },
        { text: "Beta.pdf: Importe: 1200 (pendiente de revisión humana)", pending: true, citationIds: [], documentId: "doc-b", fieldName: "amount" },
      ] }],
      aggregates: [{ text: "Hay 2 documentos en el resultado.", citationIds: [] }],
    },
    { type: "transition", text: "A continuación, lo que dicen los documentos:" },
    { type: "text", text: XSS, evidenceStatus: "answerable", citationIds: ["c1"] },
  ],
};
const INVENTORY_RESULT = {
  path: "inventory", operationStatus: "ok", correlationId: "corr-job", disclaimerRequired: true, citations: [],
  blocks: [
    { type: "intro", text: "Estos son los documentos del expediente." },
    { type: "inventory", groups: [{ title: "Documentos del expediente", lines: [{ text: "Alfa.pdf: PDF, listo para consultar", pending: false, citationIds: [] }] }], aggregates: [{ text: "Hay 1 documentos en el resultado.", citationIds: [] }] },
  ],
};

(async () => {
  const base = process.argv[2];
  assert.match(base || "", /^http:\/\/localhost:\d+$/);
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE, headless: true });
  const providerApi = await request.newContext();
  try {
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } });
    const page = await context.newPage();
    const pageErrors = [];
    page.on("pageerror", (error) => pageErrors.push(String(error && error.message)));
    const loginResponse = await providerApi.get(`${base}/login`, { maxRedirects: 0 });
    const loginState = new URL(loginResponse.headers()["location"]).searchParams.get("state");
    const providerState = await providerApi.storageState();
    if (providerState.cookies.length) await context.addCookies(providerState.cookies);
    await context.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (["localhost", "127.0.0.1"].includes(url.hostname) && url.pathname === "/login") {
        return route.fulfill({ status: 302, headers: { location: `${base}/callback?state=${encodeURIComponent(loginState)}&code=integration-code` } });
      }
      if (["localhost", "127.0.0.1"].includes(url.hostname)) return route.continue();
      throw new Error(`Non-local browser request forbidden: ${url.hostname}`);
    });

    // ---- scripted API ---------------------------------------------------------------
    const json = (route, status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    const script = { submit: null, jobs: [], jobPolls: 0, chatBodies: [], mcpCalls: 0, syncChat: null, reviewLists: 0 };
    await page.route("**/api/chat", async (route) => {
      const body = route.request().postDataJSON();
      script.chatBodies.push(body);
      if (body.selectedDocumentId) {
        return json(route, 200, {
          operationStatus: "ok", evidenceStatus: "answerable", answer: "Fecha de vencimiento: 2026-03-15", citations: [],
          idpMetadata: { field: body.selectedFieldName, value: "2026-03-15", presence: "PRESENT", acceptance: "ACCEPTED", origin: "LITERAL" },
        });
      }
      if (script.syncChat) return json(route, 200, script.syncChat);
      const { status, body: payload } = script.submit;
      return json(route, status, payload);
    });
    await page.route("**/api/matters/*/chat-jobs/*", async (route) => {
      script.jobPolls += 1;
      const next = script.jobs.length > 1 ? script.jobs.shift() : script.jobs[0];
      if (next.httpStatus) return json(route, next.httpStatus, { error: "access_denied" });
      return json(route, 200, { jobId: JOB_ID, conversationId: "c", correlationId: "corr-job", ...next });
    });
    await page.route("**/api/matters/matter-integration/documents*", async (route) => {
      if (route.request().method() !== "GET" || !new URL(route.request().url()).pathname.endsWith("/documents")) return route.continue();
      return json(route, 200, { documents: DOCS });
    });
    await page.route("**/api/matters/matter-integration/reviews*", async (route) => {
      if (route.request().method() !== "GET" || new URL(route.request().url()).pathname.split("/").length > 6) return route.continue();
      script.reviewLists += 1;
      return json(route, 200, { tasks: [IDP_TASK] });
    });
    await page.route("**/api/mcp", async (route) => {
      script.mcpCalls += 1;
      const body = route.request().postDataJSON();
      return json(route, 200, {
        status: "SUCCESS", tool: "@legaldesk_gateway/metadata-mcp___get_document_metadata", correlationId: body.originCorrelationId,
        result: { document: DOCS[0], idp: { status: "IDP_COMPLETED", fields: [{ name: "explicit_expiration_date" }], history: [] } },
      });
    });
    await page.route("**/api/citations*", (route) => json(route, 200, { passage: "El plazo será de 30 días." }));
    await page.route("**/api/conversations", async (route) => {
      if (route.request().method() === "POST" && route.request().postDataJSON()?.matterId === "matter-other") {
        return json(route, 201, { matterId: "matter-other", conversationId: "other-conversation", sessionId: "other-session", correlationId: "other-correlation" });
      }
      return route.continue();
    });
    await page.route("**/api/matters/matter-other/**", (route) => json(route, 200, route.request().url().includes("reviews") ? { tasks: [] } : { documents: [] }));
    await page.route("**/api/conversations/other-conversation**", (route) => json(route, 200, { events: [] }));

    // ---- helpers ---------------------------------------------------------------------
    await page.clock.install();
    await page.goto(base);
    await page.locator("#login-button").click();
    await page.locator("#matter-select").selectOption("matter-integration");
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled && !document.querySelector("#question").disabled);
    const answerText = () => page.locator("#answer").innerText();
    const accepted = { status: 202, body: { jobId: JOB_ID, status: "queued", conversationId: "c", correlationId: "corr-job" } };
    async function ask(question) {
      await page.locator("#question").fill(question);
      await page.locator("#ask-button").click();
    }
    async function tick(ms) { await page.clock.fastForward(ms); }
    async function settled() { await page.waitForFunction(() => document.querySelector("#answer").getAttribute("aria-busy") === "false"); }

    // ---- 1. progress states and a mixed result ----------------------------------------
    script.submit = accepted;
    script.jobs = [{ status: "queued" }, { status: "running" }, { status: "running" }];
    await ask("¿Cuándo vence el contrato y qué dice del plazo?");
    await page.waitForFunction(() => document.querySelector("#answer").innerText.includes("Pensando"));
    assert.equal(await page.locator("#ask-button").isDisabled(), true, "no second question while the job runs");
    assert.equal(await page.locator("#answer").getAttribute("aria-busy"), "true");
    await tick(2000);
    await tick(2000);
    assert.match(await answerText(), /Pensando/, "before 45 s the wording stays plain");
    assert.doesNotMatch(await answerText(), /tardando más de lo habitual/);
    await tick(46000);
    await page.waitForFunction(() => document.querySelector("#answer").innerText.includes("tardando más de lo habitual"));
    assert.equal(await page.locator("#evidence-status").getAttribute("data-status"), "loading");
    script.jobs = [{ status: "succeeded", result: MIXED_RESULT }];
    await tick(4000);
    await settled();
    const pollsAtSuccess = script.jobPolls;
    const rendered = await answerText();
    assert.match(rendered, /Esto es lo que consta en los documentos/);
    assert.match(rendered, /Alfa\.pdf: Fecha de vencimiento: 2026-03-15/);
    assert.match(rendered, /Hay 2 documentos en el resultado\./);
    assert.match(rendered, /A continuación, lo que dicen los documentos:/);
    assert.equal(await page.locator("#answer .agent-line[data-pending='true']").count(), 1);
    assert.equal(await page.evaluate(() => window.__xss), undefined, "backend text is rendered as text, never as markup");
    assert.equal(await page.locator("#answer img").count(), 0);
    assert.ok(rendered.includes("<img src=x onerror=window.__xss=1>"), "the markup characters are visible as text");
    assert.equal(await page.locator("#citation-list .citation-card").count(), 2);
    assert.equal(await page.locator("#answer .inline-citation").count(), 2, "one mark in the data line and one in the text block");
    await page.locator("#citation-list button[data-handle='h-c1']").click();
    await page.waitForFunction(() => document.querySelector("#inspection-passage").textContent.includes("El plazo será de 30 días."));
    assert.equal(await page.locator("#disclaimer").isHidden(), false);
    // The agentic chat offers no review of its answers.
    assert.equal(await page.locator(".agent-suggest-review").count(), 0);
    assert.equal(await page.locator(".review-request").isHidden(), true);
    await tick(20000);
    assert.equal(script.jobPolls, pollsAtSuccess, "polling stops once the job succeeded");
    assert.equal(script.chatBodies.length, 1);
    assert.deepEqual(Object.keys(script.chatBodies[0]).sort(), ["conversationId", "matterId", "question", "sessionId"], "the browser sends only the question and its scope handles");

    // ---- 2. data line of an accepted field: shows the extracted value with its source -------
    await page.locator("#workspace-tab-consultation").click();
    const accepteLink = page.locator("#answer .agent-line[data-field-name='explicit_expiration_date'] .agent-field-action");
    assert.match(await accepteLink.textContent(), /Ver de dónde sale este dato/);
    assert.match(await page.locator("#answer .agent-line[data-field-name='amount'] .agent-field-action").textContent(), /Ver la revisión pendiente de este dato/);
    assert.equal(await page.locator("#answer .agent-block-text .agent-field-action").count(), 0, "text blocks carry no field links");
    await accepteLink.click();
    await page.waitForFunction(() => document.querySelector("#idp-result") && !document.querySelector("#idp-result").hidden && document.querySelector("#idp-result").innerText.includes("2026-03-15"));
    assert.equal(await page.locator("#workspace-tab-documents").getAttribute("aria-selected"), "true");
    assert.equal(await page.locator("#idp-field-select").inputValue(), "explicit_expiration_date");
    assert.ok(script.mcpCalls >= 1);
    const shortcut = script.chatBodies.find((body) => body.selectedDocumentId);
    assert.equal(shortcut.selectedDocumentId, "doc-a");
    assert.equal(shortcut.selectedFieldName, "explicit_expiration_date");
    await page.locator("#workspace-tab-consultation").click();
    assert.match(await answerText(), /A continuación, lo que dicen los documentos/, "looking at a source does not replace the agent answer");

    // ---- 3. pending field: leads to its IDP review task ---------------------------------
    await page.locator("#workspace-tab-consultation").click();
    await page.locator("#answer .agent-line[data-field-name='amount'] .agent-field-action").click();
    await page.waitForFunction(() => document.querySelector("#workspace-tab-reviews").getAttribute("aria-selected") === "true");
    assert.ok(script.reviewLists >= 1);
    await page.waitForSelector("#reviews-section details[data-review-id='rt-idp-1']");
    assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.closest("details") && document.activeElement.closest("details").dataset.reviewId), "rt-idp-1");

    // ---- 4. inventory result ------------------------------------------------------------
    await page.locator("#workspace-tab-consultation").click();
    script.jobs = [{ status: "succeeded", result: INVENTORY_RESULT }];
    await ask("¿Qué documentos hay?");
    await tick(2000);
    await settled();
    assert.match(await answerText(), /Alfa\.pdf: PDF, listo para consultar/);
    assert.equal(await page.locator("#answer .agent-field-action").count(), 0, "inventory lines are not IDP fields");
    assert.equal(await page.locator("#evidence-status").getAttribute("data-status"), "inventory");

    // ---- 5. closed error codes ----------------------------------------------------------
    const errors = {
      session_closed: /Vuelve a enviar la pregunta/,
      deadline_exceeded: /tiempo máximo/,
      transient_exhausted: /saturado/,
      attempts_exhausted: /reintentarla/,
      agent_failed: /no pudo responder/,
      internal_error: /error interno/,
    };
    for (const [code, expected] of Object.entries(errors)) {
      script.jobs = [{ status: "failed", error: code }];
      await ask(`Pregunta que falla con ${code}`);
      await tick(2000);
      await settled();
      assert.match(await answerText(), expected, code);
      assert.match(await page.locator("#app-error").innerText(), expected, code);
      assert.equal(await page.locator("#ask-button").isDisabled(), false, `${code}: the user can ask again`);
    }
    script.jobs = [{ status: "failed", error: "something_new" }];
    await ask("Pregunta con un código desconocido");
    await tick(2000);
    await settled();
    assert.match(await page.locator("#app-error").innerText(), /Vuelve a enviar la pregunta/);

    // the session is about to expire: no job exists, the user is asked to sign in again
    script.submit = { status: 401, body: { error: "session_expiring" } };
    const pollsBefore = script.jobPolls;
    await ask("Pregunta con la sesión a punto de caducar");
    await page.waitForFunction(() => !document.querySelector("#app-error").hidden && document.querySelector("#app-error").innerText.includes("iniciar sesión"));
    await settled();
    assert.equal(script.jobPolls, pollsBefore);
    assert.equal(await page.locator("#ask-button").isDisabled(), false);

    // a job that cannot be read any more ends the wait instead of polling forever
    script.submit = accepted;
    script.jobs = [{ httpStatus: 403 }];
    await ask("Pregunta con un job ilegible");
    await tick(2000);
    await settled();
    assert.match(await page.locator("#app-error").innerText(), /No se pudo comprobar el estado/);

    // ---- 6. feature off: the synchronous answer is untouched -----------------------------
    script.syncChat = { operationStatus: "ok", evidenceStatus: "answerable", answer: "Respuesta síncrona.", citations: [], disclaimerRequired: true, correlationId: "sync-corr" };
    const pollsBeforeSync = script.jobPolls;
    await ask("Pregunta con el chat agentic apagado");
    await page.waitForFunction(() => document.querySelector("#answer").innerText.includes("Respuesta síncrona."));
    assert.equal(script.jobPolls, pollsBeforeSync, "no job is polled when the backend answers synchronously");
    await page.waitForFunction(() => !document.querySelector("#review-button").disabled);
    assert.equal(await page.locator(".review-request").isVisible(), true, "the existing review of a synchronous answer still works");
    script.syncChat = null;

    // ---- 7. polling stops when the matter changes ----------------------------------------
    script.submit = accepted;
    script.jobs = [{ status: "running" }];
    await ask("Pregunta que no termina");
    await tick(2000);
    await new Promise((resolve) => setTimeout(resolve, 400)); // let the poll in flight reach the fake API
    const pollsRunning = script.jobPolls;
    await page.locator("#matter-select").evaluate((select) => {
      const option = document.createElement("option");
      option.value = "matter-other";
      option.textContent = "Other Matter";
      select.append(option);
    });
    await page.locator("#matter-select").selectOption("matter-other");
    await page.waitForFunction(() => document.querySelector("#matter-kicker").textContent === "Other Matter");
    await tick(30000);
    assert.equal(script.jobPolls, pollsRunning, "no polling after switching matter");
    assert.doesNotMatch(await answerText(), /Pensando|tardando/);
    assert.equal(await page.locator("#ask-button").isDisabled(), false);

    // ---- 8. polling stops on logout -------------------------------------------------------
    await page.locator("#matter-select").selectOption("matter-integration");
    await page.waitForFunction(() => !document.querySelector("#question").disabled);
    script.jobs = [{ status: "running" }];
    await ask("Pregunta durante el cierre de sesión");
    await tick(2000);
    await new Promise((resolve) => setTimeout(resolve, 400));
    const pollsBeforeLogout = script.jobPolls;
    await page.locator("#logout-button").click();
    await page.waitForURL((url) => url.origin === new URL(base).origin);
    await tick(30000).catch(() => {});
    assert.equal(script.jobPolls, pollsBeforeLogout, "no polling after logout");

    assert.deepEqual(pageErrors, [], "the UI must not throw");
    console.log(JSON.stringify({ result: "PASS", smoke: "phase15-agent-chat-frontend", polls: script.jobPolls }));
  } finally {
    await providerApi.dispose();
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
