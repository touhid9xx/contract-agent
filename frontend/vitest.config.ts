import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    css: false,
    // Only run unit tests — E2E is Playwright's job
    include: ["tests/unit/**/*.{test,spec}.{ts,tsx}"],
    exclude: [
      "node_modules",
      "dist",
      ".next",
      "tests/e2e/**",
      "playwright-report/**",
      "test-results/**",
    ],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html"],
      // M2 scope: only files that already have tests.
      // As more components get tests (M6 auth forms, M9 contracts table,
      // M11 field review, ...), add their globs here. This keeps
      // thresholds meaningful without blocking development on unrelated,
      // untested components.
      include: ["src/components/common/**/*.{ts,tsx}", "src/lib/utils.ts"],
      exclude: [
        "src/**/*.d.ts",
        "src/**/layout.tsx",
        "src/**/page.tsx",
        "src/**/loading.tsx",
        "src/**/error.tsx",
        "src/**/not-found.tsx",
        "src/**/*.test.{ts,tsx}",
        "src/**/__tests__/**",
        "src/**/index.ts",
      ],
      thresholds: {
        lines: 80,
        functions: 80,
        branches: 80,
        statements: 80,
      },
    },
  },
});
