/* Browser-runner seams that can be tested without Playwright or network I/O. */

async function waitForIndexedState(page) {
  const handle = await page.waitForFunction(() => {
    const error = document.querySelector("#app-error");
    if (error && !error.hidden) return "error";
    if (document.querySelector("#document-list").textContent.includes("INDEXED")) return "indexed";
    return false;
  }, null, { timeout: 300_000 });
  if (!handle || typeof handle.jsonValue !== "function") {
    throw new TypeError("waitForFunction did not return a JSHandle");
  }
  try {
    return await handle.jsonValue();
  } finally {
    if (handle && typeof handle.dispose === "function") await handle.dispose();
  }
}

module.exports = { waitForIndexedState };
