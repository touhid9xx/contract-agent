import { defaultLocale, isLocale, type Locale } from "@/i18n/config";
import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";

export type Messages = typeof en;

/**
 * No `as Messages` cast on `bn`, deliberately.
 *
 * The cast used to live here and it turned a missing translation into a runtime
 * gap that only showed up as a raw key on a Bengali screen. Uncast, a namespace
 * added to `en.json` and forgotten in `bn.json` is a compile error instead.
 */
const MESSAGES: Record<Locale, Messages> = {
  en,
  bn,
};

/** Resolve a locale string to messages, falling back to the default locale. */
export function getMessages(locale: string): Messages {
  return MESSAGES[isLocale(locale) ? locale : defaultLocale];
}
