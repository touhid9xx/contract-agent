import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";

const eslintConfig = defineConfig([
  ...nextVitals,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Project-specific ignores:
    "coverage/**", // Vitest coverage report (auto-generated)
    "playwright-report/**", // Playwright HTML report (auto-generated)
    "test-results/**", // Playwright test artifacts (auto-generated)
    "node_modules/**",
    "htmlcov/**", // Backend coverage report (if ever generated in frontend)
    "*.min.js",
    "*.min.css",
  ]),
]);

export default eslintConfig;
