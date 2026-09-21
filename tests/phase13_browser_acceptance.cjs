/* Offline browser acceptance. Start phase13_browser_server.py first. */
const assert = require("node:assert/strict");
const path = require("node:path");
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");

(async () => {
  const base = process.argv[2];
  assert.match(base || "", /^http:\/\/localhost:\d+$/);
  const browser = await chromium.launch({
    executablePath: process.env.BROWSER_EXECUTABLE,
    headless: true,
  });
  try {
    const context = await browser.newContext({ viewport: { width: 1365, height: 1000 } });
    const errors = [];
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await context.route("**/*", async route => {
      const url = new URL(route.request().url());
      if (url.hostname === "issuer.integration" && url.pathname === "/authorize") {
        return route.fulfill({ status: 302, headers: { location: `${base}/callback?state=${encodeURIComponent(url.searchParams.get("state"))}&code=integration-code` } });
      }
      if (["localhost", "127.0.0.1"].includes(url.hostname)) return route.continue();
      throw new Error(`Non-local browser request forbidden: ${url.hostname}`);
    });
    await page.goto(base);
    await page.locator("#login-button").click();
    await page.locator("#matter-select").selectOption("matter-integration");
    await page.locator("#question").waitFor({ state: "visible" });
    await page.waitForFunction(() => !document.querySelector("#document-file").disabled);
    await page.locator("#document-file").setInputFiles({ name: "fictional.txt", mimeType: "text/plain", buffer: Buffer.from("The inspection period is four years.") });
    await page.locator("#upload-button").click();
    await page.waitForFunction(() => document.querySelector("#document-list").textContent.includes("INDEXED"), { timeout: 15000 });
    await page.locator("#question").fill("What is the inspection period?");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#answer").textContent.includes("four years"));
    await page.locator(".citation-inspect").first().click();
    await page.waitForFunction(() => document.querySelector("#inspection-passage").textContent.includes("four years"));
    assert.equal(await page.locator("#inspection-passage").innerText(), "The inspection period is four years.");
    assert.ok(!(await page.locator("body").innerText()).includes("s3://"));
    await page.locator("#metadata-button").click();
    await page.waitForFunction(() => document.querySelector("#operator-output").textContent.includes("fictional.txt"));
    await page.locator("#review-button").click();
    await page.waitForFunction(() => document.querySelector("#operator-output").textContent.includes("reviewTaskId"));
    await page.locator("#audit-button").click();
    await page.waitForFunction(() => document.querySelector("#operator-output").textContent.includes("grounding_validate"));
    assert.ok((await page.locator("#history-list").innerText()).includes("four years"));
    const artifactDir = process.env.ARTIFACT_DIR || "test-results";
    await page.screenshot({ path: path.join(artifactDir, "phase13-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false, "mobile horizontal overflow");
    await page.screenshot({ path: path.join(artifactDir, "phase13-mobile.png"), fullPage: true });
    // UI error handling is exercised independently of the backend failure
    // scenarios: an operational response must clear the previous answer/cites.
    await page.route("**/api/chat", route => route.fulfill({
      status: 200, contentType: "application/json",
      body: JSON.stringify({ operationStatus: "error", evidenceStatus: null, answer: "Safe operation failure.", citations: [] }),
    }), { times: 1 });
    await page.locator("#question").fill("Exercise a local UI operational error.");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => !document.querySelector("#app-error").hidden);
    assert.ok(!(await page.locator("#answer").innerText()).includes("four years"));
    assert.equal(await page.locator(".citation-inspect").count(), 0);
    assert.notEqual(await page.locator("#evidence-status").getAttribute("data-status"), "insufficient_evidence");
    await page.locator("#question").fill("none: where is the missing emergency assembly point?");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#evidence-status").dataset.status === "insufficient_evidence");
    assert.equal(await page.locator(".citation-inspect").count(), 0);
    await page.locator("#logout-button").click();
    await page.waitForFunction(() => document.querySelector("#question").disabled);
    assert.ok(!(await page.locator("#answer").innerText()).includes("four years"));
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: "PASS", browser: "Chromium", layouts: [1365, 390], awsCalls: 0, route: "login/upload/index/chat/citation/MCP/review/history/audit/no-evidence/logout" }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
