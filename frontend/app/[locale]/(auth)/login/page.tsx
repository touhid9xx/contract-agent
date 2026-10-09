// login.tsx

"use client";

import { useTranslations } from "next-intl";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export default function LoginPage() {
  const t = useTranslations("auth");

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm space-y-6">
        <div className="space-y-1 text-center">
          <h1 className="text-2xl font-semibold">{t("loginTitle")}</h1>
          <p className="text-sm text-muted-foreground">{t("loginSubtitle")}</p>
        </div>
        <form className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="email">{t("email")}</Label>
            <Input id="email" type="email" autoComplete="email" required />
          </div>
          <div className="space-y-2">
            <Label htmlFor="password">{t("password")}</Label>
            <Input id="password" type="password" autoComplete="current-password" required />
          </div>
          <Button type="submit" className="w-full">
            {t("login")}
          </Button>
        </form>
      </div>
    </main>
  );
}
