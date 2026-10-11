import { test, expect } from "@playwright/test";

/**
 * Auth flow E2E — each test is fully independent.
 *
 * Why unique emails per test?
 *   Playwright runs tests in parallel by default. Sharing an email would
 *   cause races: register-then-login order isn't guaranteed, and retries
 *   would hit "email already registered".
 */
test.describe("Auth flow", () => {
  const unique = () => `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
  const password = "Sup3rSecret!Pass";

  const emailFor = (label: string) => `e2e-${label}-${unique()}@example.com`;

  async function registerUser(
    page: import("@playwright/test").Page,
    email: string,
    fullName = "E2E User",
  ) {
    await page.goto("/en/register");
    await page.getByLabel("Full name").fill(fullName);
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password").fill(password);
    await page.getByRole("button", { name: /Register|Create account/i }).click();
    await expect(page).toHaveURL(/\/en\/dashboard/, { timeout: 15_000 });
  }

  test("unauthenticated user is redirected to login", async ({ page }) => {
    await page.goto("/en/dashboard");
    await expect(page).toHaveURL(/\/en\/login/);
  });

  test("register lands on dashboard", async ({ page }) => {
    await registerUser(page, emailFor("register"));
  });

  test("login with valid credentials succeeds", async ({ page }) => {
    const email = emailFor("login");
    // Register first — login requires an existing user
    await registerUser(page, email);
    // Clear session cookie so we test the login page
    await page.context().clearCookies();

    await page.goto("/en/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill(password);
    await page.getByRole("button", { name: /Log in|Sign in/i }).click();
    await expect(page).toHaveURL(/\/en\/dashboard/, { timeout: 15_000 });
  });

  test("login with wrong password shows error", async ({ page }) => {
    await page.goto("/en/login");
    await page.getByLabel("Email").fill(emailFor("wrongpass"));
    await page.getByLabel("Password").fill("WrongPassword!1");
    await page.getByRole("button", { name: /Log in|Sign in/i }).click();
    await expect(page.getByRole("alert")).toContainText(/Invalid|failed/i, {
      timeout: 10_000,
    });
  });

  test("password policy enforced client-side", async ({ page }) => {
    await page.goto("/en/register");
    await page.getByLabel("Email").fill(emailFor("policy"));
    await page.getByLabel("Password", { exact: true }).fill("short");
    await page.getByLabel("Confirm password").fill("short");
    await page.getByRole("button", { name: /Register|Create account/i }).click();
    // No navigation — validation blocked
    await expect(page).toHaveURL(/\/en\/register/);
    // Client-side error visible (mirrors Zod policy)
    await expect(page.getByText(/12\+/i)).toBeVisible();
  });

  test("a11y: login page passes axe", async ({ page }) => {
    await page.goto("/en/login");
    const { default: AxeBuilder } = await import("@axe-core/playwright");
    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();
    expect(results.violations).toEqual([]);
  });
});
