/**
 * How the runner finds a module that imports by alias.
 *
 * `tsconfig.json` declares `@/*`, and every screen and most of `src/lib` import
 * through it — `session.ts` included, since it reaches `@/lib/api`. `tsc`
 * honours that mapping and so does Next; Vitest does not read `tsconfig.json`,
 * so without this it can only run a test whose subject uses relative imports.
 * That is exactly why `contract-scope.test.ts` was for a while the only test in
 * the app: it is the pure module that happens to import relatively.
 *
 * This adds no dependency — `vitest/config` and the alias are both already
 * installed — and no environment change: the default `node` environment is
 * right for pure modules, and a component test would need `jsdom`, which is a
 * decision rather than a tweak (`D10-37`).
 */
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "node",
  },
});
