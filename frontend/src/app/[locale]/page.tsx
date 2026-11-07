"use client";

import { useTheme } from "next-themes";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { HealthStatus } from "@/components/health-status";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { localeLabels, locales } from "@/i18n/config";

const THEMES = ["light", "dark", "system"] as const;

export default function HomePage() {
  const t = useTranslations();
  const locale = useLocale();
  const pathname = usePathname();
  const { theme, setTheme } = useTheme();
  const { user, signOut, signOutEverywhere } = useAuth();

  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  /** Swap the leading locale segment while keeping the rest of the path. */
  function pathForLocale(target: string): string {
    const segments = pathname.split("/").filter(Boolean);
    segments[0] = target;
    return `/${segments.join("/")}`;
  }

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setActionError(null);
    try {
      await action();
    } catch {
      // The one thing worth saying is that the button did not do what it said.
      // Why is a server detail the user cannot act on.
      setActionError(t("home.actionFailed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <p className="text-xs font-medium tracking-wide text-primary uppercase">
          {t("home.phaseLabel")}
        </p>
        <h1 className="text-2xl font-semibold">{t("app.name")}</h1>
        <p className="text-sm text-muted-foreground">{t("app.tagline")}</p>
        <HealthStatus />
      </header>

      <Card>
        <CardHeader>
          <CardTitle>{t("home.accountHeading")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {user === null ? (
            <>
              <p className="text-sm text-muted-foreground">{t("home.signedOut")}</p>
              <Link href={`/${locale}/login`}>
                <Button size="sm">{t("home.signIn")}</Button>
              </Link>
            </>
          ) : (
            <>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                <dt className="text-muted-foreground">{t("common.email")}</dt>
                <dd className="break-all">{user.email}</dd>
                <dt className="text-muted-foreground">{t("common.role")}</dt>
                <dd>{user.role}</dd>
                <dt className="text-muted-foreground">{t("common.organisation")}</dt>
                <dd>{user.tenant_slug}</dd>
              </dl>

              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={busy}
                  onClick={() => {
                    void run(signOut);
                  }}
                >
                  {t("home.signOut")}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() => {
                    void run(signOutEverywhere);
                  }}
                >
                  {t("home.signOutEverywhere")}
                </Button>
              </div>

              <p className="text-xs text-muted-foreground">{t("home.signOutEverywhereHint")}</p>
            </>
          )}

          {actionError !== null && (
            <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
              {actionError}
            </p>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("home.heading")}</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">{t("home.body")}</p>

          {/*
            The two screens that do work rather than report it are linked from
            here instead of being reachable only by URL. Shown whether or not
            there is a session: each asks for one itself, and hiding the links
            would leave a signed-out visitor no way to learn they exist.
          */}
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/${locale}/review`}>
              <Button size="sm" variant="secondary">
                {t("nav.review")}
              </Button>
            </Link>
            <Link href={`/${locale}/escalations`}>
              <Button size="sm" variant="secondary">
                {t("nav.escalations")}
              </Button>
            </Link>
          </div>

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
    </main>
  );
}
