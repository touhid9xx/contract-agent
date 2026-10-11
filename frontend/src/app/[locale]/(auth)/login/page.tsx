"use client";

import { Suspense, useState } from "react";
import { useTranslations } from "next-intl";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { useSearchParams } from "next/navigation";
import Link from "next/link";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toApiError } from "@/lib/api/client";
import { useAuth } from "@/lib/hooks/use-auth";
import { loginSchema, type LoginInput } from "@/lib/schemas/auth";

// ============================================================
// INNER COMPONENT — uses useSearchParams (needs <Suspense> around it)
// ============================================================
function LoginForm() {
  const t = useTranslations("auth");
  const { login } = useAuth();
  const searchParams = useSearchParams();
  const next = searchParams.get("next");
  const [error, setError] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginInput>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = async (data: LoginInput) => {
    setError(null);
    try {
      await login(data.email, data.password);
      // useAuth redirects to /dashboard (or `next` if provided)
    } catch (err) {
      const apiErr = toApiError(err);
      setError(apiErr.status === 401 ? t("invalidCredentials") : t("loginFailed"));
    }
  };

  return (
    <>
      {error && (
        <Alert variant="destructive" role="alert">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <div className="space-y-2">
          <Label htmlFor="email">{t("email")}</Label>
          <Input
            id="email"
            type="email"
            autoComplete="email"
            autoFocus
            aria-invalid={!!errors.email}
            aria-describedby={errors.email ? "email-error" : undefined}
            {...register("email")}
          />
          {errors.email && (
            <p id="email-error" className="text-destructive text-sm">
              {errors.email.message}
            </p>
          )}
        </div>

        <div className="space-y-2">
          <Label htmlFor="password">{t("password")}</Label>
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            aria-invalid={!!errors.password}
            aria-describedby={errors.password ? "password-error" : undefined}
            {...register("password")}
          />
          {errors.password && (
            <p id="password-error" className="text-destructive text-sm">
              {errors.password.message}
            </p>
          )}
        </div>

        <Button type="submit" className="w-full" disabled={isSubmitting}>
          {isSubmitting ? t("signingIn") : t("login")}
        </Button>
      </form>
    </>
  );
}

// ============================================================
// PAGE — layout + static parts + Suspense around the dynamic form
// ============================================================
export default function LoginPage() {
  const t = useTranslations("auth");

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm space-y-6">
        <header className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold">{t("loginTitle")}</h1>
          <p className="text-muted-foreground text-sm">{t("loginSubtitle")}</p>
        </header>

        <Suspense fallback={<p className="text-muted-foreground text-sm">Loading…</p>}>
          <LoginForm />
        </Suspense>

        <p className="text-muted-foreground text-center text-sm">
          {t("noAccount")}{" "}
          <Link href="/register" className="underline">
            {t("register")}
          </Link>
        </p>
      </div>
    </main>
  );
}
