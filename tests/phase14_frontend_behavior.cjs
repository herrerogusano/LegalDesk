/* Offline browser behavior test for the public upload safety gate and query loading state. */
const assert = require("node:assert/strict");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

(async () => {
  const base = process.argv[2];
  assert.match(base || "", /^http:\/\/localhost:\d+$/);
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE, headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } });
    const page = await context.newPage();
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.hostname === "issuer.integration" && url.pathname === "/authorize") {
        return route.fulfill({ status: 302, headers: { location: `${base}/callback?state=${encodeURIComponent(url.searchParams.get("state"))}&code=integration-code` } });
      }
      if (["localhost", "127.0.0.1"].includes(url.hostname)) return route.continue();
      throw new Error(`Non-local browser request forbidden: ${url.hostname}`);
    });

    let mode = "empty";
    let uploadMode = "pending-then-uploaded";
    let pendingPolls = 0;
    let documentGets = 0;
    let historyFailure = false;
    const ingestionPosts = [];
    let delayIntegrationDocuments = false;
    let releaseIntegrationDocuments = null;
    const documentsByMode = {
      empty: [],
      mixed: [
        { documentId: "doc-uploaded", name: "ready.txt", status: "UPLOADED", fileSizeBytes: 8 },
        { documentId: "doc-processing", name: "processing.txt", status: "PENDING_INGESTION", fileSizeBytes: 12 },
        { documentId: "doc-failed", name: "failed.txt", status: "FAILED", malwareScanStatus: "THREATS_FOUND", fileSizeBytes: 10 },
        { documentId: "doc-incomplete", name: "incomplete.txt", status: "PENDING_UPLOAD", fileSizeBytes: 9 },
      ],
      pending: [{ documentId: "doc-processing", name: "processing.txt", status: "PENDING_INGESTION", fileSizeBytes: 12 }],
      ready: [{ documentId: "doc-ready", name: "ready.txt", status: "INDEXED", fileSizeBytes: 8 }],
    };
    let documentGetFailure = false;
    let ingestionFailure = false;
    let chatCalls = 0;
    await page.route("**/api/chat", async route => {
      chatCalls += 1;
      if (chatCalls > 1) await new Promise(resolve => setTimeout(resolve, 700));
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ operationStatus: "ok", evidenceStatus: "answerable", answer: chatCalls > 1 ? "Second answer." : "Initial answer.", citations: [] }),
      });
    });
    await page.route("**/api/conversations/**", async route => {
      if (historyFailure) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "operation_failed" }) });
      return route.continue();
    });
    await page.route("**/api/conversations", async route => {
      if (route.request().method() === "POST" && route.request().postDataJSON()?.matterId === "matter-other") {
        return route.fulfill({ status: 201, contentType: "application/json", body: JSON.stringify({ matterId: "matter-other", conversationId: "other-conversation", sessionId: "other-session", correlationId: "other-correlation" }) });
      }
      return route.continue();
    });
    await page.route("**/api/matters/matter-other/documents", async route => {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ documents: [{ documentId: "other-doc", name: "other.txt", status: "INDEXED", fileSizeBytes: 5 }] }) });
    });
    await page.route("**/api/matters/matter-other/reviews**", async route => {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tasks: [] }) });
    });
    await page.route("**/api/conversations/other-conversation**", async route => {
      if (historyFailure) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "operation_failed" }) });
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ events: [] }) });
    });
    await page.route("**/api/matters/matter-integration/documents*", async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (request.method() === "GET" && url.pathname.endsWith("/documents")) {
        documentGets += 1;
        if (delayIntegrationDocuments) await new Promise(resolve => { releaseIntegrationDocuments = resolve; });
        if (documentGetFailure) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "operation_failed" }) });
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ documents: documentsByMode[mode] || [] }) });
      }
      const match = url.pathname.match(/\/documents\/([^/]+)(?:\/confirm)?$/);
      if (!match) return route.continue();
      const documentId = match[1];
      return route.continue();
    });
    await page.route("**/api/matters/matter-integration/documents/**", async route => {
      const request = route.request();
      const url = new URL(request.url());
      const match = url.pathname.match(/\/documents\/([^/]+)(?:\/confirm)?$/);
      if (!match) return route.continue();
      const documentId = match[1];
      if (request.method() === "POST" && url.pathname.endsWith("/confirm")) {
        const failed = uploadMode === "failed";
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ document: { documentId, status: failed ? "FAILED" : "PENDING_UPLOAD", malwareScanStatus: failed ? "THREATS_FOUND" : "PENDING" } }),
        });
      }
      if (request.method() === "GET" && uploadMode === "pending-then-uploaded") {
        pendingPolls += 1;
        const status = pendingPolls >= 2 ? "UPLOADED" : "PENDING_UPLOAD";
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ document: { documentId, status } }) });
      }
      return route.continue();
    });
    await page.route("**/api/matters/matter-integration/ingestions", async route => {
      if (route.request().method() !== "POST") return route.continue();
      if (ingestionFailure) {
        return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ error: "operation_failed" }) });
      }
      ingestionPosts.push(await route.request().postDataJSON());
      mode = "ready";
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ operationId: "behavior-op", operationStatus: "pending" }) });
    });
    await page.route("**/api/matters/matter-integration/ingestions/behavior-op", async route => {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ operationId: "behavior-op", operationStatus: "documents_indexed" }) });
    });

    await page.goto(base);
    await page.locator("#login-button").click();
    await page.locator("#matter-select").selectOption("matter-integration");
    await page.locator("#workspace-tab-documents").click();
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled);
    assert.equal(await page.locator("#sync-button").isDisabled(), false, "refresh is available for an authorized matter");
    assert.equal(await page.locator("#prepare-button").isHidden(), true, "empty matter has no preparation action");
    const getsBeforeMixed = documentGets;
    mode = "mixed";
    await page.locator("#sync-button").click();
    await page.waitForFunction(() => document.querySelector("#document-action-status").textContent.includes("Actualizado"));
    assert.ok(documentGets > getsBeforeMixed, "refresh must perform a GET");
    assert.equal(ingestionPosts.length, 0, "refresh must not start ingestion");
    assert.match(await page.locator("#document-list").innerText(), /Procesando para consulta/);
    assert.match(await page.locator("#document-list").innerText(), /Rechazado por análisis/);
    assert.match(await page.locator("#document-incomplete-list").innerText(), /Carga incompleta/);
    assert.match(await page.locator("#prepare-button").innerText(), /Preparar para consulta \(1\)/i);
    await page.locator("#prepare-button").click();
    await page.waitForFunction(() => document.querySelector("#document-action-status").textContent.includes("Preparación completada"), null, { timeout: 15000 });
    assert.equal(ingestionPosts.length, 1, "preparation starts exactly once after explicit intent");
    assert.deepEqual(ingestionPosts[0].documentIds, ["doc-uploaded"], "only UPLOADED documents may be prepared");

    mode = "pending";
    await page.locator("#sync-button").click();
    await page.waitForFunction(() => document.querySelector("#document-list").textContent.includes("Procesando para consulta"));
    assert.equal(await page.locator("#prepare-button").isHidden(), true, "PENDING_INGESTION must not restart ingestion");
    assert.equal(ingestionPosts.length, 1, "processing refresh has no ingestion side effect");

    documentGetFailure = true;
    await page.locator("#sync-button").click();
    await page.waitForFunction(() => document.querySelector("#document-action-status").dataset.state === "error");
    assert.equal(await page.locator("#prepare-button").isHidden(), true, "failed refresh blocks stale preparation");
    documentGetFailure = false;
    mode = "ready";
    await page.locator("#sync-button").click();
    await page.waitForFunction(() => document.querySelector("#document-action-status").textContent.includes("sin cambios") || document.querySelector("#document-action-status").textContent.includes("Actualizado"));

    mode = "mixed";
    await page.locator("#sync-button").click();
    await page.waitForFunction(() => !document.querySelector("#prepare-button").hidden);
    const ingestionBeforeFailedPrepare = ingestionPosts.length;
    ingestionFailure = true;
    await page.locator("#prepare-button").click();
    await page.waitForFunction(() => document.querySelector("#document-action-status").dataset.state === "error");
    assert.equal(ingestionPosts.length, ingestionBeforeFailedPrepare, "failed preparation must not record a started ingestion");
    assert.match(await page.locator("#document-action-status").innerText(), /Actualiza estados/);
    ingestionFailure = false;
    mode = "ready";
    await page.locator("#sync-button").click();
    await page.waitForFunction(() => document.querySelector("#document-action-status").textContent.includes("Actualizado"));

    const ingestionBeforeUpload = ingestionPosts.length;
    await page.locator("#document-file").setInputFiles({ name: "pending.txt", mimeType: "text/plain", buffer: Buffer.from("pending safety test") });
    await page.locator("#upload-button").click();
    await page.waitForFunction(() => document.querySelector("#app-status").textContent.includes("Estado de indexación"), null, { timeout: 15000 });
    assert.ok(pendingPolls >= 2, "PENDING_UPLOAD should be polled before ingestion");
    const verifiedPendingPolls = pendingPolls;
    assert.equal(ingestionPosts.length, ingestionBeforeUpload + 1, "ingestion starts exactly after UPLOADED");

    uploadMode = "failed";
    pendingPolls = 0;
    await page.locator("#document-file").setInputFiles({ name: "failed.txt", mimeType: "text/plain", buffer: Buffer.from("failed safety test") });
    await page.locator("#upload-button").click();
    await page.waitForFunction(() => !document.querySelector("#app-error").hidden, null, { timeout: 10000 });
    assert.match(await page.locator("#app-error").innerText(), /seguridad/);
    assert.equal(ingestionPosts.length, ingestionBeforeUpload + 1, "FAILED must never start ingestion");

    delayIntegrationDocuments = true;
    const delayedRefresh = page.waitForRequest(request => request.method() === "GET" && new URL(request.url()).pathname.endsWith("/api/matters/matter-integration/documents"));
    await page.locator("#sync-button").click();
    await delayedRefresh;
    await page.locator("#matter-select").evaluate(select => {
      const option = document.createElement("option");
      option.value = "matter-other";
      option.textContent = "Other Matter";
      select.append(option);
    });
    await page.locator("#matter-select").selectOption("matter-other");
    if (releaseIntegrationDocuments) releaseIntegrationDocuments();
    await page.waitForFunction(() => document.querySelector("#matter-kicker").textContent === "Other Matter");
    assert.match(await page.locator("#sync-button").innerText(), /^actualizar estados$/i, "a stale refresh label must not cross matters");
    assert.equal(await page.locator("#sync-button").getAttribute("aria-busy"), null, "a stale refresh busy state must not cross matters");
    assert.equal(await page.locator("#prepare-button").getAttribute("aria-busy"), null, "a stale preparation busy state must not cross matters");
    assert.match(await page.locator("#document-list").innerText(), /other\.txt/);
    assert.doesNotMatch(await page.locator("#document-list").innerText(), /ready\.txt|processing\.txt/);

    await page.locator("#workspace-tab-consultation").click();
    await page.locator("#question").fill("First query");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#answer").innerText.includes("Initial answer."));
    historyFailure = true;
    await page.locator("#question").fill("Second query");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#answer").getAttribute("aria-busy") === "true");
    assert.equal(await page.locator("#evidence-status").getAttribute("data-status"), "loading");
    assert.equal(await page.locator("#evidence-status").innerText(), "BUSCANDO RESPUESTA");
    assert.match(await page.locator("#answer").innerText(), /Initial answer\./);
    await page.waitForFunction(() => document.querySelector("#answer").getAttribute("aria-busy") === "false");
    assert.equal(await page.locator("#evidence-status").getAttribute("data-status"), "answerable");
    assert.match(await page.locator("#answer").innerText(), /Second answer\./);
    assert.equal(await page.locator("#review-button").isDisabled(), false, "history failure must not disable review of a valid answer");
    assert.match(await page.locator("#app-error").innerText(), /historial de actividad/);
    const loaders = page.locator(".answer-loading");
    if (await loaders.count()) {
      assert.equal(await loaders.getAttribute("hidden"), "");
      assert.equal(await loaders.isVisible(), false);
    }
    assert.doesNotMatch(await page.locator("#answer").innerText(), /Buscando en los documentos autorizados/);
    const motion = await page.evaluate(() => {
      const active = document.querySelector(".workspace [data-panel]:not([hidden])");
      const button = document.querySelector("#sync-button");
      return {
        panelAnimation: active ? getComputedStyle(active).animationName : "",
        panelDuration: active ? getComputedStyle(active).animationDuration : "",
        buttonTransition: button ? getComputedStyle(button).transitionProperty : "",
      };
    });
    assert.equal(motion.panelAnimation, "workspace-panel-enter");
    assert.notEqual(motion.panelDuration, "0s");
    assert.notEqual(motion.buttonTransition, "all");
    const reducedContext = await browser.newContext({ reducedMotion: "reduce", viewport: { width: 1365, height: 1000 } });
    const reducedPage = await reducedContext.newPage();
    await reducedPage.goto(base);
    const reduced = await reducedPage.evaluate(() => ({
      matches: window.matchMedia("(prefers-reduced-motion: reduce)").matches,
      animationDurationMs: Number.parseFloat(getComputedStyle(document.querySelector(".workspace [data-panel]:not([hidden])")).animationDuration) * (getComputedStyle(document.querySelector(".workspace [data-panel]:not([hidden])")).animationDuration.endsWith("s") ? 1000 : 1),
      transitionDurationMs: Number.parseFloat(getComputedStyle(document.querySelector("#sync-button")).transitionDuration) * (getComputedStyle(document.querySelector("#sync-button")).transitionDuration.endsWith("s") ? 1000 : 1),
    }));
    assert.equal(reduced.matches, true);
    assert.ok(reduced.animationDurationMs <= 0.01);
    assert.ok(reduced.transitionDurationMs <= 0.01);
    await reducedContext.close();
    console.log(JSON.stringify({ result: "PASS", documentGets, pendingPolls: verifiedPendingPolls, ingestionPosts: ingestionPosts.length, failedStarted: false, queryLoadingState: true }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
