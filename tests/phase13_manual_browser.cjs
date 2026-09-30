/*
 * Test-only interactive browser for the offline Phase 13 demo.
 *
 * It opens a visible browser and performs no business clicks. The only
 * intercepted non-loopback request is the fictional integration IdP redirect,
 * which completes the local PKCE fixture without credentials. Close the
 * browser or press Ctrl+C to end the session.
 */
"use strict";

const assert = require("node:assert/strict");
const {
  callbackLocation,
  isFictionalIdpAuthorize,
  isLoopbackUrl,
} = require("./phase13_manual_browser_helpers.cjs");

function loadChromium() {
  const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
  if (!playwright || !playwright.chromium) throw new TypeError("Playwright Chromium is unavailable");
  return playwright.chromium;
}

function parseBaseUrl(value) {
  assert.match(value || "", /^http:\/\/localhost:\d+$/);
  return new URL(value);
}

function printInstructions(baseUrl, prepareDemoFile) {
  console.log(`[manual-demo] browser open at ${baseUrl}`);
  console.log("[manual-demo] login is completed by the fictional local IdP redirect; no credentials are used.");
  console.log("[manual-demo] perform the business actions manually in the browser; close it or press Ctrl+C to stop.");
  if (prepareDemoFile) {
    console.log("[manual-demo] the fictional evidence file will be selected automatically after a matter is chosen; you still authorize the upload.");
  }
}

const MANUAL_DEMO_FILE = Object.freeze({
  name: "manual-demo-evidence.txt",
  mimeType: "text/plain",
  buffer: Buffer.from("The inspection period is four years.", "utf8"),
});

function runPreflight(baseUrl) {
  assert.equal(isLoopbackUrl(baseUrl.toString()), true);
  assert.equal(isLoopbackUrl("https://example.com"), false);
  assert.equal(isFictionalIdpAuthorize("https://issuer.integration/authorize?state=check"), true);
  assert.equal(isFictionalIdpAuthorize("https://issuer.integration/token"), false);
  assert.match(callbackLocation(baseUrl.toString(), "check"), /\/callback\?state=check&code=integration-code$/);
  console.log(JSON.stringify({
    result: "PASS",
    mode: "MANUAL_OFFLINE_PREFLIGHT",
    awsCalls: 0,
    headless: false,
    businessClicks: 0,
    fictionalIdpOnly: true,
    nonLoopbackDestinations: "blocked",
  }));
}

async function main() {
  const baseUrl = parseBaseUrl(process.argv[2]);
  const prepareDemoFile = process.argv.includes("--prepare-demo-file");
  if (process.argv.includes("--preflight")) {
    runPreflight(baseUrl);
    return;
  }

  const chromium = loadChromium();
  const browser = await chromium.launch({
    executablePath: process.env.BROWSER_EXECUTABLE,
    headless: false,
    args: ["--start-maximized"],
  });
  let closing = false;
  const finish = async () => {
    if (closing) return;
    closing = true;
    if (browser.isConnected()) await browser.close().catch(() => {});
  };
  process.once("SIGINT", () => { void finish(); });
  browser.on("disconnected", () => {
    closing = true;
    process.exitCode = 0;
  });

  // Follow the native maximized window instead of constraining the demo to a
  // fixed Playwright viewport. Automated acceptance keeps explicit viewports.
  const context = await browser.newContext({ viewport: null });
  await context.route("**/*", async (route) => {
    const requestUrl = route.request().url();
    if (isFictionalIdpAuthorize(requestUrl)) {
      const idpUrl = new URL(requestUrl);
      return route.fulfill({
        status: 302,
        headers: { location: callbackLocation(baseUrl.toString(), idpUrl.searchParams.get("state")) },
      });
    }
    const destination = new URL(requestUrl);
    if (destination.origin === "https://issuer.integration" && destination.pathname === "/logout") {
      return route.fulfill({ status: 302, headers: { location: `${baseUrl.origin}/logout` } });
    }
    if (isLoopbackUrl(requestUrl)) return route.continue();
    console.error(`[manual-demo] blocked non-loopback destination: ${new URL(requestUrl).hostname}`);
    return route.abort("blockedbyclient");
  });
  const page = await context.newPage();
  page.on("pageerror", (error) => {
    const errorType = error && typeof error.name === "string" ? error.name : "UnknownError";
    console.error(`[manual-demo] page error: ${errorType}`);
  });
  await page.goto(baseUrl.toString(), { waitUntil: "domcontentloaded" });
  printInstructions(baseUrl.toString(), prepareDemoFile);

  if (prepareDemoFile) {
    void page.waitForFunction(() => !document.querySelector("#document-file")?.disabled)
      .then(() => page.locator("#document-file").setInputFiles(MANUAL_DEMO_FILE))
      .then(() => console.log("[manual-demo] fictional evidence file selected; click Autorizar subida when ready."))
      .catch((error) => {
        if (!closing) {
          const errorType = error && typeof error.name === "string" ? error.name : "UnknownError";
          console.error(`[manual-demo] could not prepare fictional evidence: ${errorType}`);
        }
      });
  }

  await new Promise((resolve) => {
    browser.on("disconnected", resolve);
    process.once("SIGINT", resolve);
  });
  await context.close().catch(() => {});
}

main().catch((error) => {
  const errorType = error && typeof error.name === "string" ? error.name : "UnknownError";
  console.error(`[manual-demo] failed before interaction: ${errorType}`);
  process.exitCode = 1;
});
