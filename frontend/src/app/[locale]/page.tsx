import { redirect } from "next/navigation";

/**
 * Locale root — redirects to dashboard.
 *
 * Why this file exists:
 *   - Playwright's webServer health check probes the app root
 *   - next-intl redirects `/` → `/en`, which needs a page
 *   - Without this, `/en` returns 404 and Playwright times out
 */
export default async function LocaleRootPage({ params }: { params: Promise<{ locale: string }> }) {
  const { locale } = await params;
  redirect(`/${locale}/dashboard`);
}
