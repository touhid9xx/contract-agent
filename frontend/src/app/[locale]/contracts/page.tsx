"use client";

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FieldLabel, Input } from "@/components/ui/input";
import { ApiError, contracts } from "@/lib/api";
import type {
  ContactChannel,
  ContractIngestRequest,
  ContractListItem,
  ContractSource,
  ContractStatus,
  RiskLevel,
} from "@/lib/api";
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
 * The contract listing, and the form that puts a contract in it.
 *
 * Until the listing the API could mint a contract id and echo it once, and every
 * other per-contract route (consent, fields, decisions, notifications) took that
 * id on faith. `GET /contracts` closed that on the server; this screen is what
 * makes it usable, and it is the first place in the product where a contract is
 * visible at all rather than merely existing.
 *
 * Five choices are deliberate:
 *
 * * **One form, not two.** `POST /contracts` and `POST /contracts/document` take
 *   the same four content fields and differ only in transport, so attaching a
 *   file is what switches routes. Two screens would have been two places to keep
 *   the date and time-zone rules in step, and the second would have drifted.
 * * **No source picker.** The server stamps `pdf` on the document route and
 *   defaults to `api` on the metadata one. A picker here would let a client claim
 *   a contract came from a document that was never sent.
 * * **The dates are not validated here.** The route refuses an inverted term and
 *   a malformed zone with 422, in one place, for both routes. A second copy of
 *   those rules in the browser is the copy that goes stale.
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
  const queryClient = useQueryClient();
  const { user, isAuthenticated } = useAuth();

  /** `null` means every status — the same meaning as an omitted `?status=`. */
  const [statusFilter, setStatusFilter] = useState<ContractStatus | null>(null);

  /**
   * Both ingestion routes are `require_roles(ADMIN, OPERATOR)`, so a viewer is
   * answered 403 by the server. The form is hidden rather than shown disabled:
   * the boundary is the route's, and offering an action whose only possible
   * outcome is a refusal is noise. A note takes its place, so the absence has a
   * reason on the screen rather than looking like a bug.
   */
  const canIngest = user !== null && user.role !== "viewer";

  const [customerRef, setCustomerRef] = useState("");
  /** `YYYY-MM-DD` from a date input, or `""` for "not recorded". */
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  /** Mirrors the route's own default, so an untouched form sends what it assumes. */
  const [timezone, setTimezone] = useState("UTC");
  const [document, setDocument] = useState<File | null>(null);
  /**
   * Remount key for the file input. Clearing `document` does not clear the DOM
   * input — a file input's value cannot be controlled — so without this it would
   * keep showing the filename of a document that has already been uploaded.
   */
  const [documentEpoch, setDocumentEpoch] = useState(0);

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
   * One mutation for both routes.
   *
   * Which route runs is decided here, from whether a file was staged, rather than
   * by giving the operator two buttons. The metadata is identical either way, and
   * asking them to know which endpoint a file triggers is asking them to think
   * about transport instead of about the contract.
   */
  const ingest = useMutation({
    mutationFn: (input: { metadata: ContractIngestRequest; document: File | null }) =>
      authorized((accessToken) =>
        input.document === null
          ? contracts.ingest(accessToken, input.metadata)
          : contracts.ingestDocument(accessToken, input.metadata, input.document),
      ),
    onSuccess: async () => {
      setCustomerRef("");
      setStartDate("");
      setEndDate("");
      setTimezone("UTC");
      setDocument(null);
      setDocumentEpoch((epoch) => epoch + 1);
      // Every status filter is its own cache entry and the new row lands in
      // whichever one it matches, so the whole prefix is invalidated rather than
      // the entry currently on screen.
      await queryClient.invalidateQueries({ queryKey: ["contracts", "list"] });
    },
  });

  function submitIngest() {
    const metadata: ContractIngestRequest = { customer_ref: customerRef.trim() };
    // Omitted, not sent empty: `""` is not a date, and the route answers a blank
    // optional field with 422 rather than reading it as "not recorded".
    if (startDate !== "") metadata.start_date = startDate;
    if (endDate !== "") metadata.end_date = endDate;
    if (timezone.trim() !== "") metadata.timezone = timezone.trim();
    ingest.mutate({ metadata, document });
  }

  /**
   * A refused write, keyed on status.
   *
   * Neither route returns a machine-readable code — the bodies carry a human
   * `detail` string — so matching the prose would break on the first rewording.
   * These are the four a person can actually provoke:
   *
   * **409** the tenant already holds this contract, by metadata fingerprint or by
   * document hash. **413** the file exceeded the store's cap. **422** a date, the
   * zone name, or an inverted term. **503** this deployment has no encryption key
   * or no document store, so it cannot seal a document at all — a deployment
   * state, not a bug in what was typed, which is why it says so in its own words.
   */
  function describeIngestFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.isOffline) return t("contracts.offline");
      if (cause.isForbidden) return t("contracts.ingestForbidden");
      if (cause.status === 409) return t("contracts.ingestConflict");
      if (cause.status === 413) return t("contracts.ingestTooLarge");
      if (cause.status === 422) return t("contracts.ingestInvalid");
      if (cause.status === 503) return t("contracts.ingestUnavailable");
    }
    return t("contracts.failed");
  }

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
          {canIngest ? (
            <Card>
              <CardHeader>
                <CardTitle>{t("contracts.ingestHeading")}</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="space-y-1">
                  <FieldLabel htmlFor="ingest-ref">{t("contracts.ingestRefLabel")}</FieldLabel>
                  <Input
                    id="ingest-ref"
                    value={customerRef}
                    autoComplete="off"
                    onChange={(event) => {
                      setCustomerRef(event.target.value);
                    }}
                  />
                  <p className="text-xs text-muted-foreground">{t("contracts.ingestRefHint")}</p>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div className="space-y-1">
                    <FieldLabel htmlFor="ingest-start">{t("contracts.ingestStartLabel")}</FieldLabel>
                    <Input
                      id="ingest-start"
                      type="date"
                      value={startDate}
                      onChange={(event) => {
                        setStartDate(event.target.value);
                      }}
                    />
                  </div>
                  <div className="space-y-1">
                    <FieldLabel htmlFor="ingest-end">{t("contracts.ingestEndLabel")}</FieldLabel>
                    <Input
                      id="ingest-end"
                      type="date"
                      value={endDate}
                      onChange={(event) => {
                        setEndDate(event.target.value);
                      }}
                    />
                  </div>
                </div>

                <div className="space-y-1">
                  <FieldLabel htmlFor="ingest-timezone">
                    {t("contracts.ingestTimezoneLabel")}
                  </FieldLabel>
                  <Input
                    id="ingest-timezone"
                    value={timezone}
                    autoComplete="off"
                    onChange={(event) => {
                      setTimezone(event.target.value);
                    }}
                  />
                  <p className="text-xs text-muted-foreground">
                    {t("contracts.ingestTimezoneHint")}
                  </p>
                </div>

                <div className="space-y-1">
                  <FieldLabel htmlFor="ingest-document">
                    {t("contracts.ingestDocumentLabel")}
                  </FieldLabel>
                  <Input
                    key={documentEpoch}
                    id="ingest-document"
                    type="file"
                    onChange={(event) => {
                      setDocument(event.target.files?.[0] ?? null);
                    }}
                  />
                  <p className="text-xs text-muted-foreground">
                    {t("contracts.ingestDocumentHint")}
                  </p>
                </div>

                <p className="text-xs text-muted-foreground">{t("contracts.ingestDatesHint")}</p>

                <Button
                  size="sm"
                  disabled={ingest.isPending || customerRef.trim() === ""}
                  onClick={submitIngest}
                >
                  {ingest.isPending ? t("contracts.ingesting") : t("contracts.ingestAction")}
                </Button>

                {ingest.isSuccess && (
                  <div className="space-y-1 rounded-lg bg-muted px-3 py-2" role="status">
                    <p className="text-xs">
                      {t("contracts.ingested", {
                        id: ingest.data.id,
                        ref: ingest.data.customer_ref,
                      })}
                    </p>
                    <p className="text-xs text-muted-foreground">{t("contracts.ingestedNote")}</p>
                  </div>
                )}

                {ingest.isError && (
                  <p className="text-xs text-danger" role="alert">
                    {describeIngestFailure(ingest.error)}
                  </p>
                )}
              </CardContent>
            </Card>
          ) : (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
              {t("contracts.ingestViewerNote")}
            </p>
          )}

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
