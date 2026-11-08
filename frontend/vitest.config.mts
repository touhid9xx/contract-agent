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
 * installed — and the default `node` environment stays the default: it is right
 * for pure modules. A component test needs a DOM instead, which `D10-43` paid
 * for by adding `jsdom` and `@testing-library/react`; the file that needs it
 * asks for it in its own first line (`// @vitest-environment jsdom`) rather than
 * moving the default, so the three pure specs keep running without a DOM at all.
 *
 * `oxc.jsx` is load-bearing, and specifically *not* `esbuild.jsx`. Vitest 5
 * runs on Vite 8, which transforms with oxc; an `esbuild` block is accepted by
 * the config schema and then ignored at runtime (Vite warns that oxc wins), so
 * the obvious-looking fix does nothing at all. The other half is `tsconfig.json`,
 * which says `"jsx": "preserve"` because Next compiles JSX itself — but nothing
 * downstream of Vitest does, so a `.tsx` spec transformed under `preserve`
 * reaches Node as raw JSX syntax and dies with a parse error. Asking oxc for the
 * automatic runtime (`react/jsx-runtime`, already a dependency of `react`) is
 * what makes a `.tsx` file runnable here at all.
 */
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  oxc: {
    jsx: { runtime: "automatic" },
  },
  test: {
    environment: "node",
  },
});
