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
    assert.equal(await page.locator("#upload-button").isDisabled(), true);
    await page.locator("#document-file").setInputFiles({ name: "fictional.txt", mimeType: "text/plain", buffer: Buffer.from("The inspection period is four years.") });
    assert.match(await page.locator("#selected-file-status").innerText(), /fictional\.txt/);
    await page.locator("#upload-button").click();
    await page.waitForFunction(() => {
      const status = document.querySelector("#app-status");
      return status?.dataset.state === "success" && /Subida completada\. Estado de indexación: Listo para consultar\./.test(status.textContent || "");
    }, { timeout: 15000 });
    assert.match(await page.locator("#document-list").innerText(), /Listo para consultar/);
    assert.doesNotMatch(await page.locator("#document-list").innerText(), /[0-9a-f]{8}-[0-9a-f-]{27,}/i);
    assert.match(await page.locator("#app-status").innerText(), /Subida completada\. Estado de indexación: Listo para consultar\./);
    assert.equal(await page.locator("#app-status").getAttribute("role"), "status");
    assert.equal(await page.locator("#app-status").getAttribute("data-state"), "success");
    assert.equal(await page.locator("#sync-button").isHidden(), true);
    assert.match(await page.locator("#review-button").innerText(), /Guardar para revisión/i);
    assert.equal(await page.locator("#technical-diagnostics").getAttribute("open"), null);
    await page.locator("#question").fill("What is the inspection period?");
    await page.locator("#ask-button").click();
    await page.waitForFunction(() => document.querySelector("#answer").textContent.includes("four years"));
    await page.locator(".citation-inspect").first().click();
    await page.waitForFunction(() => document.querySelector("#inspection-passage").textContent.includes("four years"));
    assert.equal(await page.locator("#inspection-passage").innerText(), "The inspection period is four years.");
    assert.match(await page.locator(".citation-title").first().innerText(), /fictional\.txt/);
    assert.match(await page.locator("#inspection-meta").innerText(), /fictional\.txt · pasaje autorizado/);
    assert.doesNotMatch(await page.locator(".sources").innerText(), /matter-integration|[0-9a-f]{8}-[0-9a-f-]{27,}/i);
    assert.ok(!(await page.locator("body").innerText()).includes("s3://"));
    await page.locator("#technical-diagnostics summary").click();
    assert.match(await page.locator("#technical-diagnostics summary").innerText(), /Ocultar herramientas/);
    assert.match(await page.locator("#technical-diagnostics summary").innerText(), /2 herramientas/i);
    assert.equal(await page.locator("#technical-diagnostics #review-button").count(), 0);
    await page.locator("#metadata-button").click();
    await page.waitForFunction(() => document.querySelector("#operator-output").textContent.includes("fictional.txt"));
    await page.locator("#review-reason").selectOption("user_requested_review");
    await page.locator("#review-note").fill("Revisar el cómputo con criterio profesional.");
    const dueAtBeforeSubmit = await page.locator("#review-due-at").inputValue();
    let reviewPostSeen = false;
    await page.route("**/api/matters/matter-integration/reviews", async route => {
      if (route.request().method() === "POST" && !reviewPostSeen) {
        reviewPostSeen = true;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      await route.continue();
    });
    const reviewResponsePromise = page.waitForResponse(response => response.url().includes("/api/matters/matter-integration/reviews") && response.request().method() === "POST");
    await page.locator("#review-button").click();
    await page.waitForFunction(() => {
      const button = document.querySelector("#review-button");
      return button?.textContent === "Guardando…" && button.disabled && button.getAttribute("aria-busy") === "true";
    });
    const reviewResponse = await reviewResponsePromise;
    const createdReview = await reviewResponse.json();
    assert.equal(reviewPostSeen, true);
    assert.equal(typeof createdReview.reviewTaskId, "string");
    await page.waitForFunction(() => document.querySelector("#app-status").textContent.includes("Revisión guardada en estado Pendiente"));
    await page.waitForFunction(() => document.querySelector("#reviews-pending").textContent.includes("Pendiente"));
    assert.equal(await page.locator("#review-note").inputValue(), "");
    assert.equal(await page.locator("#review-reason").inputValue(), "user_requested_review");
    assert.equal(await page.locator("#review-due-at").inputValue(), dueAtBeforeSubmit);
    await page.waitForFunction((reviewTaskId) => {
      const details = Array.from(document.querySelectorAll("#reviews-pending details[data-review-id]"))
        .find(candidate => candidate.dataset.reviewId === reviewTaskId);
      return details && details.querySelector("summary") === document.activeElement && details.closest(".review-item")?.classList.contains("review-item--new");
    }, createdReview.reviewTaskId);
    assert.equal(await page.locator(`#reviews-pending details[data-review-id="${createdReview.reviewTaskId}"]`).count(), 1);
    assert.equal(await page.locator("#review-button").textContent(), "Guardar para revisión");
    assert.equal(await page.locator("#review-button").getAttribute("aria-busy"), null);
    assert.doesNotMatch(await page.locator("#operator-output").innerText(), /reviewTaskId/);
    const firstReview = page.locator("#reviews-pending details").first();
    await firstReview.click();
    await page.waitForFunction(() => document.querySelector("#reviews-pending .review-item-body").textContent.includes("What is the inspection period?"));
    assert.match(await page.locator("#reviews-pending .review-item-body").innerText(), /four years/);
    assert.match(await page.locator("#reviews-pending .review-item-body").innerText(), /fictional\.txt/);
    await page.locator("#audit-button").click();
    await page.waitForFunction(() => document.querySelector("#operator-output").textContent.includes("grounding_validate"));
    assert.ok((await page.locator("#history-list").innerText()).includes("four years"));

    // Validate responsive layout and basic operability without repeating the
    // authenticated business flow at each viewport.
    const layouts = [
      { width: 375, height: 844 },
      { width: 768, height: 900 },
      { width: 1024, height: 900 },
      { width: 1365, height: 1000 },
      { width: 1440, height: 1000 },
    ];
    for (const viewport of layouts) {
      await page.setViewportSize(viewport);
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
        true,
        `horizontal overflow at ${viewport.width}px`,
      );
      for (const selector of ["#matter-select", "#document-file", "#question", "#ask-button", "#review-button", "#technical-diagnostics summary"]) {
        assert.equal(await page.locator(selector).isVisible(), true, `${selector} not visible at ${viewport.width}px`);
      }
    }
    const artifactDir = process.env.ARTIFACT_DIR || "test-results";
    await page.setViewportSize({ width: 1365, height: 1000 });
    await page.screenshot({ path: path.join(artifactDir, "phase13-desktop.png"), fullPage: true });
    await page.setViewportSize({ width: 375, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, "mobile horizontal overflow");
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
    assert.equal(await page.locator("#app-error").getAttribute("data-state"), "error");
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
    console.log(JSON.stringify({ result: "PASS", browser: "Chromium", layouts: [375, 768, 1024, 1365, 1440], awsCalls: 0, route: "login/upload/index/chat/citation/MCP/review/history/audit/no-evidence/logout" }));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
