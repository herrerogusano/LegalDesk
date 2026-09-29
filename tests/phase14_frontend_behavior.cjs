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

    let mode = "pending-then-uploaded";
    let pendingPolls = 0;
    const ingestionPosts = [];
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
    await page.route("**/api/matters/matter-integration/documents/**", async route => {
      const request = route.request();
      const url = new URL(request.url());
      const match = url.pathname.match(/\/documents\/([^/]+)(?:\/confirm)?$/);
      if (!match) return route.continue();
      const documentId = match[1];
      if (request.method() === "POST" && url.pathname.endsWith("/confirm")) {
        const status = mode === "failed" ? "FAILED" : "PENDING_UPLOAD";
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ document: { documentId, status } }) });
      }
      if (request.method() === "GET" && mode === "pending-then-uploaded" && documentId) {
        pendingPolls += 1;
        const status = pendingPolls >= 2 ? "UPLOADED" : "PENDING_UPLOAD";
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ document: { documentId, status } }) });
      }
      return route.continue();
    });
    await page.route("**/api/matters/matter-integration/ingestions", async route => {
      if (route.request().method() !== "POST") return route.continue();
      ingestionPosts.push(await route.request().postDataJSON());
      return route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ operationId: "behavior-op", operationStatus: "pending" }) });
    });
    await page.route("**/api/matters/matter-integration/ingestions/behavior-op", async route => {
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ operationId: "behavior-op", operationStatus: "documents_indexed" }) });
    });

    await page.goto(base);
    await page.locator("#login-button").click();
    await page.locator("#matter-select").selectOption("matter-integration");
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled);

    await page.locator("#document-file").setInputFiles({ name: "pending.txt", mimeType: "text/plain", buffer: Buffer.from("pending safety test") });
    await page.locator("#upload-button").click();
    await page.waitForFunction(() => document.querySelector("#app-status").textContent.includes("Estado de indexación"), null, { timeout: 15000 });
    assert.equal(pendingPolls >= 2, true, "PENDING_UPLOAD should be polled before ingestion");
    const verifiedPendingPolls = pendingPolls;
    assert.equal(ingestionPosts.length, 1, "ingestion starts exactly after UPLOADED");

    mode = "failed";
    pendingPolls = 0;
    await page.locator("#document-file").setInputFiles({ name: "failed.txt", mimeType: "text/plain", buffer: Buffer.from("failed safety test") });
    await page.locator("#upload-button").click();
    await page.waitForFunction(() => !document.querySelector("#app-error").hidden, null, { timeout: 10000 });
    assert.match(await page.locator("#app-error").innerText(), /seguridad/);
    assert.equal(ingestionPosts.length, 1, "FAILED must never start ingestion");

    await page.locator("#question").fill("First query");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#answer").innerText.includes("Initial answer."));
    await page.locator("#question").fill("Second query");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#answer").getAttribute("aria-busy") === "true");
    assert.equal(await page.locator("#evidence-status").getAttribute("data-status"), "loading");
    assert.equal(await page.locator("#evidence-status").innerText(), "BUSCANDO RESPUESTA");
    assert.match(await page.locator("#answer").innerText(), /Initial answer\./);
    await page.waitForFunction(() => document.querySelector("#answer").getAttribute("aria-busy") === "false");
    assert.equal(await page.locator("#evidence-status").getAttribute("data-status"), "answerable");
    assert.match(await page.locator("#answer").innerText(), /Second answer\./);
    const loaders = page.locator(".answer-loading");
    if (await loaders.count()) {
      assert.equal(await loaders.getAttribute("hidden"), "");
      assert.equal(await loaders.isVisible(), false);
    }
    assert.doesNotMatch(await page.locator("#answer").innerText(), /Buscando en los documentos autorizados/);
    console.log(JSON.stringify({ result: "PASS", pendingPolls: verifiedPendingPolls, ingestionPosts: ingestionPosts.length, failedStarted: false, queryLoadingState: true }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
