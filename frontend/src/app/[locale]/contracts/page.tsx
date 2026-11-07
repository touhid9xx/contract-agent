"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ApiError, contracts } from "@/lib/api";
import type { ContactChannel, ContractListItem, ContractSource, ContractStatus, RiskLevel } from "@/lib/api";
import { authorized } from "@/lib/session";

/**
 * The status filter, in the order it is offered.
 *
 * `null` is "all", and it is first because it is the default a reader expects.
 * `sla_breached` is absent for the same reason it is absent from the escalations
 * queue: it belongs to `EscalationStatus`, not to the contract lifecycle.
 */
const STATUS_FILTERS: (ContractStatus | null)[] = [
  null,
  "active",
  "reminder_sent",
  "responded",
  "renewed",
  "expired",
  "escalated",
];

/**
 * Wire value → message key (relative to the `contracts` namespace), as records
 * rather than string surgery.
 *
 * `t(\`contracts.statuses.${value}\`)` would resolve to a missing key the day an
 * enum gains a member, and the compiler cannot check a concatenated key. These
 * maps are exhaustive by construction: `satisfies Record<…, string>` turns a new
 * enum member into a type error here instead of a raw key on the screen.
 *
 * The values are the *nested* keys the message files actually carry
 * (`contracts.statuses.active`, not `contracts.statusActive`), so the caller's
 * `t(\`contracts.${…}\`)` resolves instead of printing a path.
 */
const STATUS_KEYS = {
  active: "statuses.active",
  reminder_sent: "statuses.reminder_sent",
  responded: "statuses.responded",
  renewed: "statuses.renewed",
  expired: "statuses.expired",
  escalated: "statuses.escalated",
} as const satisfies Record<ContractStatus, string>;

const SOURCE_KEYS = {
  pdf: "sources.pdf",
  api: "sources.api",
} as const satisfies Record<ContractSource, string>;

const RISK_KEYS = {
  low: "risks.low",
  medium: "risks.medium",
  high: "risks.high",
} as const satisfies Record<RiskLevel, string>;

const CHANNEL_KEYS = {
  email: "channels.email",
  sms: "channels.sms",
  voice: "channels.voice",
} as const satisfies Record<ContactChannel, string>;

/**
 * Tone per status. Two states mean "this needs a person": `escalated` and
 * `expired`. The rest are lifecycle steps, not alarms, so they stay muted.
 */
const STATUS_TONES = {
  active: "bg-muted text-muted-foreground",
  reminder_sent: "bg-muted text-muted-foreground",
  responded: "bg-muted text-muted-foreground",
  renewed: "bg-muted text-muted-foreground",
  expired: "bg-danger-muted text-danger",
  escalated: "bg-danger-muted text-danger",
} as const satisfies Record<ContractStatus, string>;

/**
 * The contract listing — the read half of the aggregate root.
 *
 * Until this screen the API could mint a contract id and echo it once, and every
 * other per-contract route (consent, fields, decisions, notifications) took that
 * id on faith. `GET /contracts` closed that on the server; this is the screen that
 * makes it usable, and it is the first place in the product where a contract is
 * visible at all rather than merely existing.
 *
 * Three choices are deliberate:
 *
 * * **No create action.** Ingestion is a route without a screen (`POST /contracts`
 *   and `POST /contracts/document`), so there is nothing truthful to put here. A
 *   disabled button would be the same lie the demo's placeholder upload button is.
 * * **No "expires in N days".** That number is computed against the *contract's*
 *   time zone, which is exactly why the row carries one. Deriving it in the
 *   browser would use the reader's zone and could be a day out, so the screen
 *   shows the stored end date and its time zone instead of a number it cannot
 *   compute correctly.
 * * **Contactability is read, not re-derived.** `may_contact` and
 *   `consent_channels` come from the model. Rebuilding them from the four booleans
 *   would be a second implementation of the contact policy, free to disagree with
 *   the one the decision worker enforces.
 */
export default function ContractsPage() {
  const t = useTranslations();
  const locale = useLocale();
  const { isAuthenticated } = useAuth();

  /** `null` means every status — the same meaning as an omitted `?status=`. */
  const [statusFilter, setStatusFilter] = useState<ContractStatus | null>(null);

  const list = useInfiniteQuery({
    queryKey: ["contracts", "list", statusFilter],
    queryFn: ({ pageParam }) =>
      authorized((accessToken) =>
        contracts.list(accessToken, {
          cursor: pageParam,
          // Omitted rather than sent as `undefined`: the client builds the query
          // string, and an explicit `null` would be neither a filter nor an absence.
          ...(statusFilter === null ? {} : { status: statusFilter }),
        }),
      ),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  /**
   * A refusal in the reader's own language.
   *
   * Reading needs any authenticated role, so `forbidden` should be unreachable —
   * it is translated anyway, because the boundary is the route's 403 and a UI that
   * assumes it cannot happen turns a policy change into an untranslated envelope.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.isForbidden) return t("contracts.forbidden");
      if (cause.isOffline) return t("contracts.offline");
    }
    return t("contracts.failed");
  }

  const pages = list.data?.pages ?? [];
  const items = pages.flatMap((page) => page.items);

  /**
   * Date-only columns are formatted in UTC on purpose.
   *
   * `end_date` is a *calendar* date, not an instant. `new Date("2026-01-02")`
   * parses as UTC midnight, so formatting it in a negative-offset zone would
   * render 1 January — a stored date silently becoming a different day. The
   * instant columns below keep the reader's local zone, which is correct for them.
   */
  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });
  const instantFormat = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  function formatDate(value: string): string {
    return dateFormat.format(new Date(value));
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("contracts.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("contracts.subtitle")}</p>
      </header>

      {!isAuthenticated ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("contracts.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("contracts.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">
              {t("contracts.filterLabel")}
            </span>
            {STATUS_FILTERS.map((candidate) => (
              <Button
                key={candidate ?? "all"}
                size="sm"
                variant={candidate === statusFilter ? "primary" : "outline"}
                aria-current={candidate === statusFilter ? "true" : undefined}
                onClick={() => {
                  setStatusFilter(candidate);
                }}
              >
                {candidate === null
                  ? t("contracts.filterAll")
                  : t(`contracts.${STATUS_KEYS[candidate]}`)}
              </Button>
            ))}
          </div>

          {items.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {t("contracts.countLabel", { count: items.length })}
            </p>
          )}

          {list.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("contracts.loading")}</p>
              </CardContent>
            </Card>
          )}

          {list.isError && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {describeFailure(list.error)}
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void list.refetch();
                  }}
                >
                  {t("contracts.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {!list.isPending && !list.isError && items.length === 0 && (
            <Card>
              <CardContent className="space-y-1">
                <p className="text-sm">{t("contracts.empty")}</p>
                <p className="text-xs text-muted-foreground">{t("contracts.emptyHint")}</p>
              </CardContent>
            </Card>
          )}

          {items.map((row) => (
            <ContractCard key={row.id} row={row} dateFormat={formatDate} instantFormat={instantFormat} />
          ))}

          {list.hasNextPage && (
            <Button
              size="sm"
              variant="outline"
              disabled={list.isFetchingNextPage}
              onClick={() => {
                void list.fetchNextPage();
              }}
            >
              {list.isFetchingNextPage ? t("contracts.loadingMore") : t("contracts.loadMore")}
            </Button>
          )}
        </>
      )}

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("contracts.backToHome")}
        </Link>
      </p>
    </main>
  );
}

interface ContractCardProps {
  row: ContractListItem;
  /** Pre-bound so every card formats a calendar date the same way. */
  dateFormat: (value: string) => string;
  /** For the columns that are instants rather than calendar dates. */
  instantFormat: Intl.DateTimeFormat;
}

/**
 * One contract.
 *
 * It states what a reader needs in order to decide what to do next — is this
 * contactable, does it have a document, where in its life is it — and nothing
 * about who it belongs to, because no name is stored here to show.
 */
function ContractCard({ row, dateFormat, instantFormat }: ContractCardProps) {
  const t = useTranslations();

  const term =
    row.start_date === null && row.end_date === null
      ? t("contracts.noDates")
      : row.start_date === null
        ? formatDateOrNull(row.end_date, dateFormat)
        : row.end_date === null
          ? formatDateOrNull(row.start_date, dateFormat)
          : `${formatDateOrNull(row.start_date, dateFormat)} – ${formatDateOrNull(row.end_date, dateFormat)}`;

  const channels = row.consent_channels.map((channel) => t(`contracts.${CHANNEL_KEYS[channel]}`));

  /**
   * `may_contact` decides; the flags only explain. Reading them the other way
   * round would mean this screen had its own opinion about consent.
   */
  const contact = row.may_contact
    ? t("contracts.contactPermitted", { channels: channels.join(", ") })
    : row.do_not_contact
      ? t("contracts.contactOptedOut")
      : t("contracts.contactNoChannels");

  return (
    <Card>
      <CardHeader className="flex flex-wrap items-baseline justify-between gap-2">
        <CardTitle>{t("contracts.contractLabel", { id: row.id })}</CardTitle>
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_TONES[row.status]}`}
        >
          {t(`contracts.${STATUS_KEYS[row.status]}`)}
        </span>
      </CardHeader>

      <CardContent className="space-y-3">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">{t("contracts.refLabel")}</dt>
          <dd className="break-words">{row.customer_ref}</dd>

          <dt className="text-muted-foreground">{t("contracts.datesLabel")}</dt>
          <dd>{term}</dd>

          <dt className="text-muted-foreground">{t("contracts.timezoneLabel")}</dt>
          <dd>{row.timezone}</dd>

          <dt className="text-muted-foreground">{t("contracts.sourceLabel")}</dt>
          <dd>{t(`contracts.${SOURCE_KEYS[row.source]}`)}</dd>

          <dt className="text-muted-foreground">{t("contracts.riskLabel")}</dt>
          <dd>{t(`contracts.${RISK_KEYS[row.risk]}`)}</dd>

          <dt className="text-muted-foreground">{t("contracts.documentLabel")}</dt>
          <dd>
            {row.has_document ? t("contracts.documentStored") : t("contracts.documentMissing")}
          </dd>

          <dt className="text-muted-foreground">{t("contracts.contactLabel")}</dt>
          <dd>{contact}</dd>

          <dt className="text-muted-foreground">{t("contracts.createdAtLabel")}</dt>
          <dd>{instantFormat.format(new Date(row.created_at))}</dd>

          {row.sha256 !== null && (
            <>
              <dt className="text-muted-foreground">{t("contracts.shaLabel")}</dt>
              {/*
                Truncated: the fingerprint is 64 hex characters and no reader
                compares two by eye. It is a dedupe key, not a value to copy out.
              */}
              <dd className="font-mono break-all">{row.sha256.slice(0, 16)}…</dd>
            </>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}

/** Format a date that the caller has already established is not `null`. */
function formatDateOrNull(value: string | null, format: (value: string) => string): string {
  return value === null ? "" : format(value);
}
