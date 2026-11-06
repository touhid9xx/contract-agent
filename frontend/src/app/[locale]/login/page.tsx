"use client";

import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FieldLabel, Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api";

/**
 * Sign-in form.
 *
 * The tenant slug is a field because login happens before a principal exists —
 * it is the one and only place the client names a tenant, and the server still
 * takes the resulting token's tenant from the matched user row rather than from
 * this value.
 */
export default function LoginPage() {
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();
  const { signIn } = useAuth();

  const [tenant, setTenant] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  /**
   * A refusal the user can act on, in their own language.
   *
   * The server's own wording is deliberately uniform ("Invalid tenant, email or
   * password") and is English only, so the cases the UI can name are translated
   * here and anything else falls back to a generic sentence. Nothing about
   * *which* part was wrong is ever shown, because the API does not say.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.isRateLimited) return t("auth.rateLimited");
      if (cause.isOffline) return t("auth.offline");
      if (cause.isUnauthorized) return t("auth.invalidCredentials");
    }
    return t("auth.failed");
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) return;

    setSubmitting(true);
    setError(null);

    try {
      await signIn({ tenant: tenant.trim(), email: email.trim(), password });
      // `replace`, not `push`: the credentials form must not be one Back press
      // away from the page the user just authenticated in order to reach.
      router.replace(`/${locale}`);
    } catch (cause) {
      setError(describeFailure(cause));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("app.name")}</h1>
        <p className="text-sm text-muted-foreground">{t("auth.subtitle")}</p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>{t("auth.title")}</CardTitle>
        </CardHeader>
        <CardContent>
          <form className="space-y-4" onSubmit={handleSubmit}>
            <div className="space-y-1.5">
              <FieldLabel htmlFor="tenant">{t("auth.tenant")}</FieldLabel>
              <Input
                id="tenant"
                name="tenant"
                value={tenant}
                onChange={(event) => {
                  setTenant(event.target.value);
                }}
                autoComplete="organization"
                autoCapitalize="none"
                spellCheck={false}
                required
                disabled={submitting}
              />
              <p className="text-xs text-muted-foreground">{t("auth.tenantHint")}</p>
            </div>

            <div className="space-y-1.5">
              <FieldLabel htmlFor="email">{t("auth.email")}</FieldLabel>
              <Input
                id="email"
                name="email"
                type="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                }}
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                required
                disabled={submitting}
              />
            </div>

            <div className="space-y-1.5">
              <FieldLabel htmlFor="password">{t("auth.password")}</FieldLabel>
              <Input
                id="password"
                name="password"
                type="password"
                value={password}
                onChange={(event) => {
                  setPassword(event.target.value);
                }}
                autoComplete="current-password"
                required
                disabled={submitting}
              />
            </div>

            {error !== null && (
              <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
                {error}
              </p>
            )}

            <Button type="submit" className="w-full" disabled={submitting}>
              {submitting ? t("auth.submitting") : t("auth.submit")}
            </Button>
          </form>
        </CardContent>
      </Card>

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("auth.backToHome")}
        </Link>
      </p>
    </main>
  );
}
