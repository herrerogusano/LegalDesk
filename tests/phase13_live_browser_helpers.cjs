/* Browser-runner seams that can be tested without Playwright or network I/O. */

async function findFirstVisible(page, selectors) {
  for (const selector of selectors) {
    const candidates = page.locator(selector);
    const count = await candidates.count();
    for (let index = 0; index < count; index += 1) {
      const candidate = candidates.nth(index);
      if (await candidate.isVisible()) return candidate;
    }
  }
  return null;
}

async function waitForFirstVisible(page, selectors, options = {}) {
  const timeout = options.timeout ?? 120_000;
  const handle = await page.waitForFunction((candidateSelectors) => candidateSelectors.some((selector) => {
    return Array.from(document.querySelectorAll(selector)).some((element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    });
  }), selectors, { timeout });
  if (handle && typeof handle.dispose === "function") await handle.dispose();
  const candidate = await findFirstVisible(page, selectors);
  if (!candidate) throw new TypeError("visible browser control disappeared");
  return candidate;
}

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

module.exports = { findFirstVisible, waitForFirstVisible, waitForIndexedState };
