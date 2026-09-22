const assert = require("node:assert/strict");
const { findFirstVisible, waitForFirstVisible, waitForIndexedState } = require("./phase13_live_browser_helpers.cjs");

async function main() {
  let disposed = false;
  const calls = [];
  const page = {
    waitForFunction: async (_predicate, arg, options) => {
      calls.push({ arg, timeout: options.timeout });
      return {
        jsonValue: async () => "indexed",
        dispose: async () => { disposed = true; },
      };
    },
  };
  assert.equal(await waitForIndexedState(page), "indexed");
  assert.equal(disposed, true);
  assert.deepEqual(calls, [{ arg: null, timeout: 300_000 }]);

  let errorDisposed = false;
  const errorPage = {
    waitForFunction: async () => ({
      jsonValue: async () => "error",
      dispose: async () => { errorDisposed = true; },
    }),
  };
  assert.equal(await waitForIndexedState(errorPage), "error");
  assert.equal(errorDisposed, true);

  let rejectedDisposed = false;
  const rejectedPage = {
    waitForFunction: async () => ({
      jsonValue: async () => { throw new Error("provider value failure"); },
      dispose: async () => { rejectedDisposed = true; },
    }),
  };
  await assert.rejects(() => waitForIndexedState(rejectedPage), Error);
  assert.equal(rejectedDisposed, true);

  const primitivePage = { waitForFunction: async () => "indexed" };
  await assert.rejects(() => waitForIndexedState(primitivePage), TypeError);

  const nodes = {
    "input[name='username']": [
      { visible: false, filled: null, clicked: false },
      { visible: true, filled: null, clicked: false },
    ],
  };
  const locatorFor = (selector) => {
    const matches = nodes[selector] || [];
    return {
      count: async () => matches.length,
      nth: (index) => ({
        isVisible: async () => matches[index].visible,
        fill: async (value) => { matches[index].filled = value; },
        click: async () => { matches[index].clicked = true; },
      }),
    };
  };
  let waitArgs;
  let waitHandleDisposed = false;
  const duplicatePage = {
    locator: locatorFor,
    waitForFunction: async (_predicate, selectors, options) => {
      waitArgs = { selectors, timeout: options.timeout };
      return { dispose: async () => { waitHandleDisposed = true; } };
    },
  };
  const visible = await waitForFirstVisible(duplicatePage, ["input[name='username']"], { timeout: 7_000 });
  await visible.fill("synthetic-user");
  assert.deepEqual(waitArgs, { selectors: ["input[name='username']"], timeout: 7_000 });
  assert.equal(waitHandleDisposed, true);
  assert.equal(nodes["input[name='username']"][0].filled, null);
  assert.equal(nodes["input[name='username']"][1].filled, "synthetic-user");
  const found = await findFirstVisible(duplicatePage, ["input[name='missing']", "input[name='username']"]);
  await found.fill("synthetic-user-again");
  await found.click();
  assert.equal(nodes["input[name='username']"][1].filled, "synthetic-user-again");
  assert.equal(nodes["input[name='username']"][1].clicked, true);

  process.stdout.write("browser helper test passed\n");
}

main().catch((error) => {
  process.stderr.write(`${error.name}\n`);
  process.exitCode = 1;
});
