"use client";

import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TraceId } from "@/components/ui/trace-id";
import { ApiError, dashboard, type ContractStatus } from "@/lib/api";
import { authorized } from "@/lib/session";

/**
 * The label for each lifecycle state, drawn from the catalogue the contracts
 * screens already use.
 *
 * A map rather than six literals at the call site, and a `satisfies` clause rather
 * than a plain object, because that is what makes it exhaustive: a seventh member
 * of `ContractStatus` is a type error here, and `i18n/composed-keys.test.ts` reads
 * these values out of the source and asks whether each one resolves in both
 * catalogues. The relative keys point into `contracts.statuses.…` rather than a
 * second copy under `dashboard.…` — one vocabulary for one enum, so a reword cannot
 * make the list screen and this one call the same state by different names.
 */
const STATUS_KEYS = {
  active: "statuses.active",
  reminder_sent: "statuses.reminder_sent",
  responded: "statuses.responded",
  renewed: "statuses.renewed",
  expired: "statuses.expired",
  escalated: "statuses.escalated",
} as const satisfies Record<ContractStatus, string>;

/**
 * The portfolio dashboard — the client for `GET /api/v1/dashboard`.
 *
 * The screen the demo had and this app did not, and the reason it took a route
 * rather than a composition of screens that already existed: the question it
 * answers — *how much of this portfolio is about to lapse?* — had no counted answer
 * anywhere. `GET /contracts` returns `len(items)` for `count`, so a headline built
 * from it would print the size of the page and shrink to `0` for a tenant holding a
 * thousand contracts. `D10-136` wrote the rule down for the board view; this screen
 * is the first one whose *whole point* is a total, so it is the first that could not
 * be built without a counted route.
 *
 * Six choices are deliberate:
 *
 * * **No role gate.** Unlike the attention board, every number here is derived from
 *   rows the caller can already list from screens they can already open, so hiding
 *   it from an operator would hide the portfolio from the person working it.
 * * **`escalations_overdue` is printed as a subset, never added.** A late escalation
 *   is still an open one. Presenting the two as peers invites a reader to sum them,
 *   and that sum counts the same row twice.
 * * **The window travels with the number.** The route counts UTC calendar days and
 *   says which ones, because a contract's own timezone is a per-row value a `COUNT`
 *   cannot resolve. The screen prints `{start}–{end}` beside the count instead of
 *   implying the reader's local month.
 * * **The recent list is labelled as rows returned.** It is the one panel here that
 *   is not a total, so it says so in the sentence above it rather than letting its
 *   length read as an activity figure.
 * * **Empty is an answer, not an error.** A tenant with nothing ingested is a `200`
 *   with every count zero, so it gets a heading and a sentence.
 * * **Read-only, and it stays read-only.** Every count leads to the screen that owns
 *   the rows behind it. A button here would be a second path into that state
 *   machine, with its own refusals to translate.
 */
export default function DashboardPage() {
  const t = useTranslations();
  const locale = useLocale();
  const { user, isAuthenticated } = useAuth();

  const view = useQuery({
    queryKey: ["dashboard", "view"],
    queryFn: () => authorized((accessToken) => dashboard.read(accessToken)),
    // Signed out, the only answer is a 401 that no retry can change; the card below
    // says so without firing a request whose failure would be shown as an outage.
    enabled: isAuthenticated,
    retry: false,
  });

  /**
   * A refusal in the reader's own language, falling back to a generic failure.
   *
   * The route carries no machine refusal codes — it answers `200` or `401` — so the
   * screen keys on the status rather than on English prose that a reword would break.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError && cause.isOffline) return t("dashboard.offline");
    return t("dashboard.failed");
  }

  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });
  const board = view.data;

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("dashboard.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("dashboard.subtitle")}</p>
      </header>

      {!isAuthenticated || user === null ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("dashboard.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("dashboard.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : view.isPending ? (
        <Card>
          <CardContent>
            <p className="text-sm text-muted-foreground">{t("dashboard.loading")}</p>
          </CardContent>
        </Card>
      ) : view.isError ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-danger" role="alert">
              {describeFailure(view.error)}
            </p>
            <TraceId error={view.error} />
            <Button size="sm" variant="secondary" onClick={() => void view.refetch()}>
              {t("dashboard.retry")}
            </Button>
          </CardContent>
        </Card>
      ) : board === undefined ? null : (
        <>
          {board.contracts_total === 0 ? (
            <Card>
              <CardHeader>
                <CardTitle>{t("dashboard.emptyHeading")}</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("dashboard.emptyBody")}</p>
              </CardContent>
            </Card>
          ) : null}

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.totalsHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div>
                <p className="text-2xl font-semibold tabular-nums">{board.contracts_total}</p>
                <p className="text-sm text-muted-foreground">{t("dashboard.totalBody")}</p>
              </div>

              {/*
                Read from the wire rather than counted here: the map is zero-filled
                on the server from its own enum, so a lifecycle state nothing has
                reached still prints `0` instead of disappearing — which is the
                answer to "has anything got as far as renewal?".
              */}
              <div className="space-y-1">
                <p className="text-xs font-medium text-muted-foreground">
                  {t("dashboard.breakdownLabel")}
                </p>
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
                  {(Object.keys(STATUS_KEYS) as ContractStatus[]).map((status) => (
                    <div key={status} className="contents">
                      <dt className="text-muted-foreground">
                        {t(`contracts.${STATUS_KEYS[status]}`)}
                      </dt>
                      <dd className="tabular-nums">{board.contracts_by_status[status]}</dd>
                    </div>
                  ))}
                </dl>
              </div>

              <Link href={`/${locale}/contracts`}>
                <Button size="sm" variant="secondary">
                  {t("dashboard.totalsAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.expiringHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-2xl font-semibold tabular-nums">{board.contracts_expiring}</p>
              <p className="text-sm text-muted-foreground">
                {board.contracts_expiring === 0
                  ? t("dashboard.expiringZero")
                  : t("dashboard.expiringBody")}
              </p>
              {/*
                The window the server counted in, printed as it sent it. A screen that
                formatted its own month here would label the number with a window
                nobody counted.
              */}
              <p className="text-xs text-muted-foreground">
                {t("dashboard.windowNote", {
                  start: formatDay(board.today),
                  end: formatDay(board.window_end),
                })}
              </p>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.reviewHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-2xl font-semibold tabular-nums">
                {board.fields_awaiting_review}
              </p>
              <p className="text-sm text-muted-foreground">{t("dashboard.reviewBody")}</p>
              <Link href={`/${locale}/review`}>
                <Button size="sm" variant="secondary">
                  {t("dashboard.reviewAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.escalationsHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <p className="text-2xl font-semibold tabular-nums">{board.escalations_open}</p>
              <p className="text-sm text-muted-foreground">{t("dashboard.escalationsBody")}</p>
              {/*
                Said as a subset, in words, in the same breath as the number. Written
                as a second figure with its own label, the two invite a reader to add
                them — and a late escalation is an open one.
              */}
              <p className="text-sm text-muted-foreground">
                {t("dashboard.escalationsOverdue", {
                  overdue: board.escalations_overdue,
                  open: board.escalations_open,
                })}
              </p>
              <Link href={`/${locale}/escalations`}>
                <Button size="sm" variant="secondary">
                  {t("dashboard.escalationsAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.recentHeading")}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {/*
                "Rows returned", not "contracts": this panel is the one figure on the
                screen the client counted, so the sentence above it says which number
                is a total and which is a window. The route bounds the slice, so the
                length here says nothing about the portfolio size.
              */}
              <p className="text-sm text-muted-foreground">
                {t("dashboard.recentNote", { count: board.recent_contracts.length })}
              </p>

              {board.recent_contracts.length === 0 ? (
                <p className="text-sm">{t("dashboard.recentEmpty")}</p>
              ) : (
                <ul className="space-y-1">
                  {board.recent_contracts.map((row) => (
                    <li key={row.id}>
                      <Link
                        className="text-primary underline-offset-4 hover:underline"
                        href={`/${locale}/contracts/${row.id}`}
                      >
                        #{row.id} {row.customer_ref}
                      </Link>{" "}
                      <span className="text-xs text-muted-foreground">
                        {t(`contracts.${STATUS_KEYS[row.status]}`)}
                      </span>
                      {row.end_date === null ? null : (
                        <span className="text-xs text-muted-foreground">
                          {" "}
                          {t("dashboard.recentEnds", { date: formatDay(row.end_date) })}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}

              <Link href={`/${locale}/contracts`}>
                <Button size="sm" variant="secondary">
                  {t("dashboard.recentAction")}
                </Button>
              </Link>
            </CardContent>
          </Card>
        </>
      )}

      <div>
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("dashboard.backToHome")}
        </Link>
      </div>
    </main>
  );

  /**
   * A `YYYY-MM-DD` day, rendered in UTC.
   *
   * The server sends these strings without a time; parsing one as a `Date` and
   * formatting it in the reader's zone can move it to the previous day, so the UTC
   * formatter is pinned to match how the day was counted. Not a `Temporal`-style
   * helper, because the whole surface here is day strings from one route.
   */
  function formatDay(value: string): string {
    return dateFormat.format(new Date(`${value}T00:00:00Z`));
  }
}
