"use client";

import { useTheme } from "next-themes";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { localeLabels, locales } from "@/i18n/config";

const THEMES = ["light", "dark", "system"] as const;

export default function HomePage() {
  const t = useTranslations();
  const locale = useLocale();
  const pathname = usePathname();
  const { theme, setTheme } = useTheme();

  /** Swap the leading locale segment while keeping the rest of the path. */
  function pathForLocale(target: string): string {
    const segments = pathname.split("/").filter(Boolean);
    segments[0] = target;
    return `/${segments.join("/")}`;
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <p className="text-xs font-medium tracking-wide text-primary uppercase">
          {t("home.phaseLabel")}
        </p>
        <h1 className="text-2xl font-semibold">{t("app.name")}</h1>
        <p className="text-sm text-muted-foreground">{t("app.tagline")}</p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>{t("home.heading")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">{t("home.body")}</p>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">
              {t("common.language")}
            </span>
            {locales.map((candidate) => (
              <Button
                key={candidate}
                size="sm"
                variant={candidate === locale ? "primary" : "outline"}
                aria-current={candidate === locale ? "true" : undefined}
                // Full page navigation keeps the locale segment authoritative.
                onClick={() => {
                  window.location.assign(pathForLocale(candidate));
                }}
              >
                {localeLabels[candidate]}
              </Button>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">{t("common.theme")}</span>
            {THEMES.map((candidate) => (
              <Button
                key={candidate}
                size="sm"
                variant={theme === candidate ? "primary" : "outline"}
                onClick={() => {
                  setTheme(candidate);
                }}
              >
                {t(`common.${candidate}`)}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>

      <nav className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href="/dashboard">
          {t("nav.dashboard")}
        </Link>
      </nav>
    </main>
  );
}
