"use client";

import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, attention } from "@/lib/api";
import { authorized } from "@/lib/session";

/**
 * The attention board — the client for the one route only curl had reached.
 *
 * `GET /api/v1/attention` landed with `D10-79` and was pinned by twelve tests
 * through the real ASGI app, but nothing outside those tests called it: a tenant
 * with a failed send or a plaintext field value still learned about it by reading
 * deployment logs. That was the half of gap 22 the route alone did not close.
 *
 * Five choices are deliberate:
 *
 * * **Admin only, and the query is not fired for anyone else.** The route is
 *   `require_roles(UserRole.ADMIN)`, so anyone else gets a 403 that no retry can
 *   change. `enabled` keeps that refusal off the wire, and the card below says why
 *   in the operator's own language rather than showing an error box.
 * * **Read-only, and it stays read-only.** There is no action here. Retrying a
 *   notification belongs to the queue that owns its state machine, and sealing a
 *   value belongs to the review queue, which is where the returned ids lead. A
 *   button here would be a second path into both, with its own refusals to
 *   translate.
 * * **Two counts, not three.** `outbox_parked` is not on the wire at all: a parked
 *   event is a relay fault the tenant neither causes nor clears. The board shows
 *   the two states they can act on from screens they already have.
 * * **The scan's bound travels with the count.** `unsealed_field_values` is a floor
 *   when the scan filled its page, so `field_scan_truncated` is rendered beside the
 *   number instead of being swallowed. A screen that printed the count alone would
 *   quietly understate a deployment with more rows than one page.
 * * **A clean board is an answer, not an empty state.** `needs_attention: false` is
 *   a `200` with every count zero — the same property that makes `doctor` exit `0`
 *   on a clean database — so it gets a heading and a sentence, not a blank card.
 */
export default function AttentionPage() {
  const t = useTranslations();
  const locale = useLocale();
  const { user, isAuthenticated } = useAuth();

  const isAdmin = user !== null && user.role === "admin";

  const report = useQuery({
    queryKey: ["attention", "report"],
    queryFn: () => authorized((accessToken) => attention.read(accessToken)),
    // Admin only on the server, so an operator's 403 is not information: there is
    // no second request that could succeed.
    enabled: isAdmin,
    retry: false,
  });

  /**
   * A refusal in the operator's own language, falling back to a generic failure.
   *
   * This route carries no machine refusal codes — it answers `200` or `403` — so
   * the screen keys on the status rather than on English prose that a reword would
   * break.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.isForbidden) return t("attention.forbidden");
      if (cause.isOffline) return t("attention.offline");
    }
    return t("attention.failed");
  }

  const board = report.data;

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("attention.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("attention.subtitle")}</p>
      </header>

      {!isAuthenticated || user === null ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("attention.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("attention.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : !isAdmin ? (
        <Card>
          <CardContent className="space-y-1">
            <p className="text-sm">{t("attention.forbidden")}</p>
            <p className="text-xs text-muted-foreground">{t("attention.forbiddenHint")}</p>
          </CardContent>
        </Card>
      ) : report.isPending ? (
        <Card>
          <CardContent>
            <p className="text-sm text-muted-foreground">{t("attention.loading")}</p>
          </CardContent>
        </Card>
      ) : report.isError ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm">{describeFailure(report.error)}</p>
            <Button size="sm" variant="secondary" onClick={() => void report.refetch()}>
              {t("attention.retry")}
            </Button>
          </CardContent>
        </Card>
      ) : board === undefined ? null : (
        <>
          {/*
            `needs_attention` is a derived flag, but it is read from the wire rather
            than recomputed from the counts: if the server ever adds a third count
            this screen has not learned about, this branch stays correct and the
            recomputed one would not.
          */}
          {board.needs_attention ? null : (
            <Card>
              <CardHeader>
                <CardTitle>{t("attention.cleanHeading")}</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("attention.cleanBody")}</p>
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle>{t("attention.failedHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-2xl font-semibold tabular-nums">
                {board.notifications_failed}
              </p>
              <p className="text-sm text-muted-foreground">{t("attention.failedBody")}</p>
              <Link href={`/${locale}/notifications`}>
                <Button size="sm" variant="secondary">
                  {t("attention.failedAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("attention.suppressedHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-2xl font-semibold tabular-nums">
                {board.notifications_suppressed}
              </p>
              <p className="text-sm text-muted-foreground">
                {t("attention.suppressedBody")}
              </p>
              <Link href={`/${locale}/notifications`}>
                <Button size="sm" variant="secondary">
                  {t("attention.suppressedAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("attention.unsealedHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-2xl font-semibold tabular-nums">
                {board.unsealed_field_values}
              </p>
              <p className="text-sm text-muted-foreground">{t("attention.unsealedBody")}</p>

              {/*
                Ids, never values. The route returns no value in any form, so there
                is nothing here that could be mistaken for one.
              */}
              <div className="space-y-1">
                <p className="text-xs font-medium text-muted-foreground">
                  {t("attention.unsealedIds")}
                </p>
                {board.unsealed_field_ids.length === 0 ? (
                  <p className="text-sm">{t("attention.unsealedNone")}</p>
                ) : (
                  <div className="flex flex-wrap gap-1">
                    {board.unsealed_field_ids.map((fieldId) => (
                      <span
                        key={fieldId}
                        className="rounded border border-border px-2 py-0.5 text-xs tabular-nums"
                      >
                        #{fieldId}
                      </span>
                    ))}
                  </div>
                )}
              </div>

              <p className="text-xs text-muted-foreground">
                {t("attention.scanned", { count: board.fields_scanned })}
              </p>

              {board.field_scan_truncated ? (
                <p className="text-xs text-muted-foreground">{t("attention.truncated")}</p>
              ) : null}

              <Link href={`/${locale}/review`}>
                <Button size="sm" variant="secondary">
                  {t("attention.unsealedAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>
        </>
      )}

      {/*
        Not a link when signed out: the home screen is the only place a visitor can
        learn which screens exist, and it shows this link itself.
      */}
      <div>
        <Link
          className="text-primary underline-offset-4 hover:underline"
          href={`/${locale}`}
        >
          {t("attention.backToHome")}
        </Link>
      </div>
    </main>
  );
}
