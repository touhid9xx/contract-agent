import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test.describe("Login page accessibility", () => {
  test("has no automatically detectable a11y violations", async ({ page }) => {
    await page.goto("/en/login");
    await page.waitForLoadState("networkidle");

    await expect(page.getByRole("heading", { name: "Welcome back" })).toBeVisible();

    const results = await new AxeBuilder({ page })
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
      .analyze();

    expect(results.violations).toEqual([]);
  });

  test("form fields are labelled and keyboard-navigable", async ({ page }) => {
    await page.goto("/en/login");
    await page.waitForLoadState("networkidle");

    // Email field has label
    const email = page.getByLabel("Email");
    await expect(email).toBeVisible({ timeout: 10_000 });
    await email.focus();
    await expect(email).toBeFocused();

    // Tab moves to password
    await page.keyboard.press("Tab");
    const password = page.getByLabel("Password");
    await expect(password).toBeFocused();

    // Tab moves to submit
    await page.keyboard.press("Tab");
    const submit = page.getByRole("button", { name: "Log in" });
    await expect(submit).toBeFocused();
  });

  test("Bangla locale renders correctly", async ({ page }) => {
    await page.goto("/bn/login");
    await page.waitForLoadState("networkidle");

    await expect(page.getByRole("heading", { name: "স্বাগতম" })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByLabel("ইমেইল")).toBeVisible();
  });

  test("color contrast passes on primary CTA", async ({ page }) => {
    await page.goto("/en/login");
    await page.waitForLoadState("networkidle");

    // Wait for the form to render fully
    const submit = page.getByRole("button", { name: "Log in" });
    await expect(submit).toBeVisible({ timeout: 10_000 });

    const results = await new AxeBuilder({ page }).withTags(["wcag2aa"]).include("form").analyze();
    expect(results.violations).toEqual([]);
  });
});
