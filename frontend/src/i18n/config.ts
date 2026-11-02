export const locales = ["en", "bn"] as const;

export type Locale = (typeof locales)[number];

export const defaultLocale: Locale = "en";

/** Human labels for the locale switcher. */
export const localeLabels: Record<Locale, string> = {
  en: "English",
  bn: "বাংলা",
};

export function isLocale(value: string): value is Locale {
  return (locales as readonly string[]).includes(value);
}
