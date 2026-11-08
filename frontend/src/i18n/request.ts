import { getRequestConfig } from "next-intl/server";

import { defaultLocale, isLocale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";

/**
 * The one piece of next-intl configuration the build cannot infer.
 *
 * `/[locale]/layout.tsx` hands `NextIntlClientProvider` an explicit locale and
 * an explicit message catalogue, so every *screen* already renders from props.
 * What was missing is the server-side request config: on recent next-intl
 * versions the provider still consults it while rendering on the server, and
 * `next build` fails the whole prerender pass without it ("Couldn't find
 * next-intl config file") — the production build had never been run, so the
 * omission went unnoticed until D10-47.
 *
 * There is no next-intl middleware in this app (locale selection is the route
 * segment's job, and the client names a tenant, not a language), so the locale
 * here is a default only: the segment is validated in the layout, which is what
 * actually decides what a screen renders. Falling back to `defaultLocale`
 * instead of throwing keeps a request that ignores the segment from 500ing.
 */
export default getRequestConfig(async ({ locale }) => {
  const resolved = locale && isLocale(locale) ? locale : defaultLocale;

  return {
    locale: resolved,
    messages: getMessages(resolved),
  };
});
