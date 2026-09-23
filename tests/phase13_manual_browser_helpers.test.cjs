"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  callbackLocation,
  isFictionalIdpAuthorize,
  isLoopbackUrl,
} = require("./phase13_manual_browser_helpers.cjs");

assert.equal(isLoopbackUrl("http://localhost:41231"), true);
assert.equal(isLoopbackUrl("http://127.0.0.1:8000/path"), true);
assert.equal(isLoopbackUrl("https://example.com"), false);
assert.equal(isLoopbackUrl("file:///tmp/demo"), false);
assert.equal(isFictionalIdpAuthorize("https://issuer.integration/authorize?state=abc"), true);
assert.equal(isFictionalIdpAuthorize("https://issuer.integration/token"), false);
assert.equal(isFictionalIdpAuthorize("https://example.com/authorize?state=abc"), false);
assert.equal(
  callbackLocation("http://localhost:41231", "state with spaces"),
  "http://localhost:41231/callback?state=state+with+spaces&code=integration-code",
);
const manualBrowserSource = fs.readFileSync(path.join(__dirname, "phase13_manual_browser.cjs"), "utf8");
assert.match(manualBrowserSource, /args:\s*\["--start-maximized"\]/);
assert.match(manualBrowserSource, /newContext\(\{ viewport: null \}\)/);
console.log(JSON.stringify({ result: "PASS", test: "manual-browser-helpers" }));
