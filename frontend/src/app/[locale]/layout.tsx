import { NextIntlClientProvider } from "next-intl";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { isLocale, locales } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";

export function generateStaticParams() {
  return locales.map((locale) => ({ locale }));
}

export default async function LocaleLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ locale: string }>;
}) {
  const { locale } = await params;

  if (!isLocale(locale)) {
    notFound();
  }

  return (
    <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
      {children}
    </NextIntlClientProvider>
  );
}
