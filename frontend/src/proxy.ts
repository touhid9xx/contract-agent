import createMiddleware from "next-intl/middleware";
import { NextRequest, NextResponse } from "next/server";
import { routing } from "./i18n/routing";

const intlMiddleware = createMiddleware(routing);

export default function proxy(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl;

  // ------------------------------------------------------------------
  // Root `/` → `/<default-locale>`
  // Handled here so we don't need a root page.tsx (which would trigger
  // NEXT_REDIRECT during Next.js 16 `instant` validation).
  // ------------------------------------------------------------------
  if (pathname === "/") {
    return NextResponse.redirect(new URL(`/${routing.defaultLocale}`, request.url));
  }

  // ------------------------------------------------------------------
  // Locale root `/<locale>` (e.g. `/en`, `/bn`) → `/<locale>/dashboard`
  // Same reasoning: keep redirects out of page.tsx to avoid instant
  // validation errors on prefetch.
  // ------------------------------------------------------------------
  const localeRootMatch = pathname.match(/^\/([a-z]{2})\/?$/);
  if (localeRootMatch) {
    const locale = localeRootMatch[1];
    if (routing.locales.includes(locale as "en" | "bn")) {
      return NextResponse.redirect(new URL(`/${locale}/dashboard`, request.url));
    }
  }

  // ------------------------------------------------------------------
  // Everything else → next-intl middleware
  // (handles locale detection, cookie, Accept-Language, etc.)
  // ------------------------------------------------------------------
  return intlMiddleware(request);
}

export const config = {
  matcher: "/((?!api|trpc|_next|_vercel|.*\\..*).*)",
};
