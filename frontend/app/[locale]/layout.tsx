import { NextIntlClientProvider } from "next-intl";
import { getMessages, setRequestLocale } from "next-intl/server";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { routing } from "../../src/i18n/routing";
import { ThemeProvider } from "@/components/providers/theme-provider";
import { QueryProvider } from "@/components/providers/query-provider";

type Props = {
  children: React.ReactNode;
  params: Promise<{ locale: string }>;
};

async function LocaleContent({ children, params }: Props) {
  const { locale } = await params;
  setRequestLocale(locale);

  if (!routing.locales.includes(locale as "en" | "bn")) {
    notFound();
  }

  const messages = await getMessages();

  return (
    <html lang={locale} suppressHydrationWarning>
      <body
        suppressHydrationWarning
        className="min-h-screen bg-background text-foreground antialiased"
      >
        <NextIntlClientProvider messages={messages}>
          <ThemeProvider
            attribute="class"
            defaultTheme="system"
            enableSystem
            disableTransitionOnChange
          >
            <QueryProvider>{children}</QueryProvider>
          </ThemeProvider>
        </NextIntlClientProvider>
      </body>
    </html>
  );
}

export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }));
}

export default function LocaleLayout({ children, params }: Props) {
  return (
    <Suspense fallback={null}>
      <LocaleContent params={params}>{children}</LocaleContent>
    </Suspense>
  );
}
