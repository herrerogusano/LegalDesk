"use strict";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1"]);

function isLoopbackUrl(value) {
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    return false;
  }
  return (url.protocol === "http:" || url.protocol === "https:") && LOOPBACK_HOSTS.has(url.hostname);
}

function isFictionalIdpAuthorize(value) {
  let url;
  try {
    url = new URL(value);
  } catch (_error) {
    return false;
  }
  return url.protocol === "https:" && url.hostname === "issuer.integration" && url.pathname === "/authorize";
}

function callbackLocation(baseUrl, state) {
  if (typeof state !== "string" || !state) throw new Error("missing fictional IdP state");
  const callback = new URL("/callback", baseUrl);
  callback.searchParams.set("state", state);
  callback.searchParams.set("code", "integration-code");
  return callback.toString();
}

module.exports = { callbackLocation, isFictionalIdpAuthorize, isLoopbackUrl };
