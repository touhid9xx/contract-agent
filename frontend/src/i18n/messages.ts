import { defaultLocale, isLocale, type Locale } from "@/i18n/config";
import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";

export type Messages = typeof en;

const MESSAGES: Record<Locale, Messages> = {
  en,
  bn: bn as Messages,
};

/** Resolve a locale string to messages, falling back to the default locale. */
export function getMessages(locale: string): Messages {
  return MESSAGES[isLocale(locale) ? locale : defaultLocale];
}
