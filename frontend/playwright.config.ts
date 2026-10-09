import { defineConfig, devices } from "@playwright/test";

const IS_CI = !!process.env.CI;
const USE_PROD = process.env.PLAYWRIGHT_USE_PROD === "true" || IS_CI;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: IS_CI,
  retries: IS_CI ? 2 : 1,
  workers: IS_CI ? 2 : 4,
  reporter: IS_CI ? "github" : "list",
  timeout: 60_000, // ← increased from 30s (dev server slow)

  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile-chrome", use: { ...devices["Pixel 5"] } },
  ],

  webServer: {
    // Production build is much faster to boot AND more stable.
    // Dev server (Turbopack + cacheComponents) is slow on Windows.
    command: USE_PROD ? "npm run build && npm run start" : "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: !IS_CI,
    timeout: USE_PROD ? 240_000 : 180_000, // ← increased from 120s
    stdout: "pipe",
    stderr: "pipe",
    env: {
      NODE_ENV: USE_PROD ? "production" : "development",
    },
  },
});
