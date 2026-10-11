/**
 * Next.js 16 proxy (replaces middleware.ts).
 *
 * Responsibilities:
 *   1. next-intl locale routing (/en, /bn)
 *   2. Auth guard: /dashboard/* requires a session cookie
 *
 * Why cookie, not localStorage?
 *   - Proxy runs on the edge — can't read localStorage
 *   - We mirror tokens to a non-httpOnly cookie for the guard
 *   - Real API auth still uses localStorage Bearer (M6); M6b moves to httpOnly
 */
import createMiddleware from "next-intl/middleware";
import { NextResponse, type NextRequest } from "next/server";
import { routing } from "./i18n/routing";

const intlMiddleware = createMiddleware(routing);

const PROTECTED_PREFIXES = ["/dashboard"];
const AUTH_PAGES = ["/login", "/register"];

export default function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Strip locale prefix for matching
  const segments = pathname.split("/").filter(Boolean);
  const locale = routing.locales.includes(segments[0] as never)
    ? segments[0]
    : routing.defaultLocale;
  const pathWithoutLocale = "/" + segments.slice(1).join("/");

  const hasSession = request.cookies.has("ca_session");

  // Guard: protected route + no session → /login
  if (PROTECTED_PREFIXES.some((p) => pathWithoutLocale.startsWith(p)) && !hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = `/${locale}/login`;
    url.searchParams.set("next", pathWithoutLocale);
    return NextResponse.redirect(url);
  }

  // Guard: auth page + session → /dashboard
  if (AUTH_PAGES.includes(pathWithoutLocale) && hasSession) {
    const url = request.nextUrl.clone();
    url.pathname = `/${locale}/dashboard`;
    return NextResponse.redirect(url);
  }

  return intlMiddleware(request);
}

export const config = {
  matcher: "/((?!api|trpc|_next|_vercel|.*\\..*).*)",
};
