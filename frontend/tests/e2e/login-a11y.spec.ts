import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * Login page accessibility.
 *
 * Why `waitForLoadState("networkidle")` is risky in dev:
 *   Next.js dev has HMR websockets that never go idle. Use explicit
 *   element visibility waits instead.
 */
test.describe("Login page accessibility", () => {
  test("has no automatically detectable a11y violations", async ({ page }) => {
    await page.goto("/en/login");
    await expect(page.getByRole("heading", { name: /Welcome back/i })).toBeVisible({
      timeout: 15_000,
    });

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();

    expect(results.violations).toEqual([]);
  });

  test("form fields are labelled and keyboard-navigable", async ({ page }) => {
    await page.goto("/en/login");

    const email = page.getByLabel("Email");
    await expect(email).toBeVisible({ timeout: 15_000 });
    await email.focus();
    await expect(email).toBeFocused();

    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Password")).toBeFocused();

    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: /Log in|Sign in/i })).toBeFocused();
  });

  test("Bangla locale renders correctly", async ({ page }) => {
    await page.goto("/bn/login");
    await expect(page.getByRole("heading", { name: /স্বাগতম/ })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByLabel("ইমেইল")).toBeVisible();
  });

  test("color contrast passes on primary CTA", async ({ page }) => {
    await page.goto("/en/login");
    const submit = page.getByRole("button", { name: /Log in|Sign in/i });
    await expect(submit).toBeVisible({ timeout: 15_000 });

    const results = await new AxeBuilder({ page }).withTags(["wcag2aa"]).include("form").analyze();
    expect(results.violations).toEqual([]);
  });
});
