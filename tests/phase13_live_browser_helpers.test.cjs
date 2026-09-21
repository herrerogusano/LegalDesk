const assert = require("node:assert/strict");
const { waitForIndexedState } = require("./phase13_live_browser_helpers.cjs");

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
  process.stdout.write("browser helper test passed\n");
}

main().catch((error) => {
  process.stderr.write(`${error.name}\n`);
  process.exitCode = 1;
});
