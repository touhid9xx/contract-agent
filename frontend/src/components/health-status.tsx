"use client";

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { probeHealth } from "@/lib/api";

type HealthState = "checking" | "ok" | "down";

const DOT: Record<HealthState, string> = {
  checking: "bg-muted-foreground",
  ok: "bg-success",
  down: "bg-danger",
};

const TEXT: Record<HealthState, string> = {
  checking: "text-muted-foreground",
  ok: "text-success",
  down: "text-danger",
};

/**
 * A one-line liveness readout.
 *
 * Worth showing because the failure it names is the one users misdiagnose most:
 * when the API is down, a rejected sign-in and a wrong password are otherwise
 * the same experience. It asks `/health`, which touches no database on purpose —
 * a green dot means "the process is answering", never "everything works".
 */
export function HealthStatus() {
  const t = useTranslations();
  const [state, setState] = useState<HealthState>("checking");

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;

    void probeHealth(controller.signal).then((ok) => {
      if (!disposed) {
        setState(ok ? "ok" : "down");
      }
    });

    return () => {
      disposed = true;
      controller.abort();
    };
  }, []);

  const label =
    state === "ok"
      ? t("home.healthOk")
      : state === "down"
        ? t("home.healthFail")
        : t("home.healthChecking");

  return (
    <p className="flex items-center gap-2 text-xs">
      <span className={`size-2 rounded-full ${DOT[state]}`} aria-hidden />
      <span className="text-muted-foreground">{t("home.healthHeading")}:</span>
      <span className={TEXT[state]}>{label}</span>
    </p>
  );
}
