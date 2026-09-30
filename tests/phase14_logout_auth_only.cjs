/*
 * Gated Cognito-only logout regression.
 *
 * This test intentionally exercises no matter, document, retrieval, tool, or
 * review data. It proves that the hosted-UI cookie is cleared: after the first
 * login -> local POST -> provider GET -> landing flow, a second /login must
 * show the Cognito username form instead of silently returning to the app.
 * Credentials stay in the process environment and are never emitted.
 */
const { findFirstVisible, waitForFirstVisible } = require("./phase13_live_browser_helpers.cjs");

const BASE_URL = process.env.LEGALDESK_P14_BASE_URL || "";
const IDP_HOST = process.env.LEGALDESK_P14_IDP_HOST || "";
const USERNAME = process.env.LEGALDESK_P14_USERNAME;
const PASSWORD = process.env.LEGALDESK_P14_PASSWORD;
const APPROVED = process.env.LEGALDESK_P14_AUTH_ONLY_APPROVED === "1";
let stage = "startup";

const SAFE_CATEGORIES = new Set([
  "auth_only_gate_required", "invalid_public_origin", "invalid_idp_host", "missing_credentials",
  "cognito_login_controls", "first_login_session", "logout_response", "provider_logout_not_requested",
  "provider_logout_uri", "provider_logout_params", "local_session_survived",
]);

function setStage(value) {
  stage = value;
}

function validOrigin(value) {
  try {
    const parsed = new URL(value);
    return parsed.protocol === "https:" && !parsed.username && !parsed.password && !parsed.search && !parsed.hash && ["", "/"].includes(parsed.pathname) && Boolean(parsed.hostname);
  } catch (_error) {
    return false;
  }
}

function safeErrorType(error) {
  const allowed = new Set(["Error", "TimeoutError", "TypeError", "ReferenceError", "RangeError", "AssertionError"]);
  return error && allowed.has(error.name) ? error.name : "UnknownError";
}

function safeCategory(error) {
  const message = error && typeof error.message === "string" ? error.message : "";
  if (SAFE_CATEGORIES.has(message)) return message;
  if (/locator|selector|visible|click|fill/i.test(message)) return "selector_failure";
  if (/goto|waitForURL|navigation|net::|ERR_FAILED|connection|response/i.test(message)) return "navigation_or_network_failure";
  if (error && error.name === "AssertionError") return "assertion_failure";
  return "unexpected_failure";
}

function validateInputs() {
  if (!APPROVED) throw new Error("auth_only_gate_required");
  if (!validOrigin(BASE_URL)) throw new Error("invalid_public_origin");
  if (!IDP_HOST || IDP_HOST.includes("/") || IDP_HOST.includes(".") === false) throw new Error("invalid_idp_host");
  if (!USERNAME || !PASSWORD) throw new Error("missing_credentials");
}

async function run() {
  validateInputs();
  const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
  const base = new URL(BASE_URL);
  const browser = await chromium.launch({ executablePath: process.env.BROWSER_EXECUTABLE || undefined, headless: true });
  let context;
  try {
    context = await browser.newContext();
    const page = await context.newPage();
    await context.route("**/*", async route => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.hostname === IDP_HOST) return route.continue();
      if (url.origin !== base.origin) return route.abort("blockedbyclient");
      const readOnlyPaths = new Set(["/", "/index.html", "/styles.css", "/citations.js", "/app.js", "/login", "/callback", "/logout", "/api/me", "/api/matters"]);
      const methodAllowed = ["GET", "HEAD", "OPTIONS"].includes(request.method()) || (url.pathname === "/logout" && request.method() === "POST");
      if (!methodAllowed || !readOnlyPaths.has(url.pathname)) return route.abort("blockedbyclient");
      return route.continue();
    });
    let providerLogoutUrl = null;
    page.on("request", request => {
      try {
        const url = new URL(request.url());
        if (request.method() === "GET" && url.hostname === IDP_HOST && url.pathname === "/logout") providerLogoutUrl = url;
      } catch (_error) { /* closed diagnostic only */ }
    });

    setStage("login");
    await page.goto(base.toString(), { waitUntil: "domcontentloaded", timeout: 120_000 });
    await page.locator("#login-button").click();
    setStage("login_form");
    await waitForFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"], { timeout: 120_000 });
    const username = await findFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"]);
    const password = await findFirstVisible(page, ["input[name='password']", "#signInFormPassword"]);
    const submit = await findFirstVisible(page, ["button[name='signInSubmitButton']", "input[type='submit']", "button[type='submit']"]);
    if (!username || !password || !submit) throw new Error("cognito_login_controls");
    await username.fill(USERNAME);
    await password.fill(PASSWORD);
    setStage("login_submit");
    await submit.click();
    setStage("callback");
    await page.waitForURL(url => url.origin === base.origin && ["/", "/index.html"].includes(url.pathname), { timeout: 120_000 });
    setStage("bootstrap");
    const me = await page.evaluate(async () => {
      const response = await fetch("/api/me", { credentials: "same-origin" });
      return response.status;
    });
    if (me !== 200) throw new Error("first_login_session");

    setStage("local_logout");
    const providerLogoutRequestPromise = page.waitForRequest(request => {
      try {
        const url = new URL(request.url());
        return request.method() === "GET" && url.hostname === IDP_HOST && url.pathname === "/logout";
      } catch (_error) {
        return false;
      }
    }, { timeout: 120_000 });
    const landingResponsePromise = page.waitForResponse(response => {
      try {
        const url = new URL(response.url());
        return url.origin === base.origin && url.pathname === "/logout" && response.request().method() === "GET";
      } catch (_error) {
        return false;
      }
    }, { timeout: 120_000 });
    const logoutResponsePromise = page.waitForResponse(response => {
      try {
        const url = new URL(response.url());
        return url.origin === base.origin && url.pathname === "/logout" && response.request().method() === "POST";
      } catch (_error) {
        return false;
      }
    }, { timeout: 120_000 });
    await page.locator("#logout-button").click();
    const logoutResponse = await logoutResponsePromise;
    if (!logoutResponse.ok()) throw new Error("logout_response");
    setStage("provider_logout");
    await providerLogoutRequestPromise;
    setStage("landing");
    await landingResponsePromise;
    await page.waitForURL(url => url.origin === base.origin && ["/", "/index.html"].includes(url.pathname), { timeout: 120_000 });
    // A URL match alone can still refer to the pre-logout document during
    // redirect processing. Its login button is hidden while authenticated;
    // wait for the returned logged-out page before evaluating a fetch.
    await page.locator("#login-button").waitFor({ state: "visible", timeout: 30_000 });
    await page.waitForLoadState("domcontentloaded");
    if (!providerLogoutUrl) throw new Error("provider_logout_not_requested");
    if (providerLogoutUrl.searchParams.get("logout_uri") !== `${BASE_URL.replace(/\/$/, "")}/logout`) throw new Error("provider_logout_uri");
    if (!providerLogoutUrl.searchParams.get("client_id") || [...providerLogoutUrl.searchParams.keys()].some(key => !["client_id", "logout_uri"].includes(key))) throw new Error("provider_logout_params");
    const afterLogout = await page.evaluate(async () => (await fetch("/api/me", { credentials: "same-origin" })).status);
    if (afterLogout !== 403) throw new Error("local_session_survived");

    // If Cognito retained its hosted-UI cookie, this click redirects straight
    // back to the app. A visible username form proves provider logout worked.
    setStage("second_login");
    await page.locator("#login-button").click();
    await waitForFirstVisible(page, ["input[name='username']", "input[type='email']", "#signInFormUsername"], { timeout: 120_000 });
    return { result: "PASS", smoke: "phase14-logout-auth-only", logoutStatus: logoutResponse.status(), providerLogoutPath: providerLogoutUrl.pathname, secondLoginFormVisible: true };
  } finally {
    if (context) await context.close().catch(() => {});
    await browser.close().catch(() => {});
  }
}

(async () => {
  try {
    process.stdout.write(JSON.stringify(await run()) + "\n");
  } catch (error) {
    process.stdout.write(JSON.stringify({ result: "FAIL", smoke: "phase14-logout-auth-only", stage, category: safeCategory(error), errorType: safeErrorType(error) }) + "\n");
    process.exitCode = 1;
  }
})();
