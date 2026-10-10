"use client";

import { useLocale, useTranslations } from "next-intl";

import { Languages } from "lucide-react";
import { Button } from "../ui/button";
import { usePathname, useRouter } from "@/i18n/navigation";

export function LangSwitcher() {
  const locale = useLocale();
  const t = useTranslations("language");
  const router = useRouter();
  const pathname = usePathname();

  const nextLocale = locale === "en" ? "bn" : "en";

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => router.replace(pathname, { locale: nextLocale })}
      aria-label={t("switch")}
      title={t("switch")}
    >
      <Languages className="h-4 w-4" />
    </Button>
  );
}
