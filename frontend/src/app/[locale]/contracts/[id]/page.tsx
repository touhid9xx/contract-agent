"use client";

import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useParams } from "next/navigation";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { TraceId } from "@/components/ui/trace-id";
import { ApiError, agent, contracts, escalations, notifications } from "@/lib/api";
import type {
  AgentDecisionList,
  ContactChannel,
  ContractField,
  ContractListItem,
  ContractSource,
  ContractStatus,
  EscalationQueue,
  FieldName,
  FieldStatus,
  NotificationList,
  RiskLevel,
} from "@/lib/api";
import { withContractScope } from "@/lib/contract-scope";
import { authorized } from "@/lib/session";

/**
 * Wire value → message key, as records rather than string surgery.
 *
 * `t(\`contracts.statuses.${value}\`)` would resolve to a missing key the day an
 * enum gains a member and the compiler cannot check a concatenated key. These maps
 * are exhaustive by construction: a new enum member is a type error here rather
 * than a raw key on the screen.
 *
 * They are local to this page, like the maps the agent, notification and escalation
 * screens each carry. That is duplication of *labels*, not of policy: nothing here
 * decides anything, and a shared module would only move the same table one import
 * away from the pages that read it.
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
 * Field names and review states are labelled from the **review** namespace.
 *
 * Not copied into `contracts.*`: a field's name and its review state mean the same
 * thing on this page as in the queue that decides it, and two vocabularies for one
 * enum is how a screen ends up calling a rejected field "decided" while the queue
 * calls it "rejected". Reusing the message keys keeps the wording single.
 */
const FIELD_KEYS = {
  email: "fields.email",
  phone: "fields.phone",
  address: "fields.address",
  dob: "fields.dob",
  start_date: "fields.start_date",
  end_date: "fields.end_date",
  contract_value: "fields.contract_value",
} as const satisfies Record<FieldName, string>;

const FIELD_STATUS_KEYS = {
  auto_accepted: "statuses.auto_accepted",
  pending_review: "statuses.pending_review",
  approved: "statuses.approved",
  rejected: "statuses.rejected",
  unreadable: "statuses.unreadable",
} as const satisfies Record<FieldStatus, string>;

/**
 * Tone per status, matching the listing's cards: only `expired` and `escalated`
 * mean "a person has to act", so only those are coloured as an alarm.
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
 * One contract, at its own address.
 *
 * Until this page a contract had a listing and nothing behind it: the listing could
 * show a row, and every other per-contract read (fields, decisions, notifications,
 * escalations) took an id that no screen could open. A card linked to nothing, so
 * the id an operator read off the list was only usable by paging back to it.
 *
 * What this page deliberately is **not**:
 *
 * * **It does not edit consent.** The write lives on the listing's cards and stays
 *   there — one screen owns `PUT /contracts/{id}/consent`, so there is one place
 *   where a stale version can be submitted and one place to fix. This page states
 *   the derived answer and points at where it is changed.
 * * **It does not re-derive contactability.** `may_contact` and `consent_channels`
 *   come from the model; rebuilding them from the four booleans here would be a
 *   second implementation of the contact policy, free to disagree with the one the
 *   decision worker enforces.
 * * **It does not show a field's value.** `GET /contracts/{id}/fields` withholds
 *   every value, and revealing one is the audited `POST /review/{id}/reveal`. The
 *   table says so rather than looking broken.
 * * **It does not compute "expires in N days".** That number is measured on the
 *   *contract's* clock, which is why the row carries a time zone; deriving it here
 *   would use the reader's zone and could be a day out.
 * * **It does not own another screen's filter.** The related-reads card asks each of
 *   those routes with this contract's id, and links them with the same id through
 *   `withContractScope`, so a link cannot spell the parameter differently from the
 *   `readContractScope` the target screen parses with. Applying the filter stays
 *   that screen's job; this page only hands over the id.
 */
export default function ContractDetailPage() {
  const t = useTranslations();
  const locale = useLocale();
  const params = useParams<{ id: string }>();

  const { isAuthenticated } = useAuth();

  /**
   * The route segment is a string, and the guard below is the thing that refuses
   * the shapes that must never become an id: the all-digits test rejects "12abc"
   * and the empty segment, `Number.isSafeInteger` rejects a digit string past the
   * safe range, and `> 0` rejects "0".
   *
   * **This comment credited the wrong line until it was measured (`D10-60`).** It
   * said `Number.parseInt` was deliberate over `Number` because "12abc" must not
   * read as 12 — but `Number.parseInt("12abc", 10)` *is* 12, the same answer
   * `Number` gives, so the conversion cannot be what refuses a tail. Split the two
   * shapes apart and the stated reason disappears: the only difference between the
   * two functions here is the empty segment, which `Number` reads as 0 and
   * `parseInt` as `NaN` — and that segment already fails the digits test. On every
   * string the test admits the two agree, so this conversion is a plain cast and
   * swapping it for `Number` would be behaviour-neutral. It is left as `parseInt`
   * because changing working code to no observable end is churn, not a fix.
   *
   * That digits test also admits "07", and the spec pins it as reading contract 7 —
   * which is what the API does: `D10-59` measured `GET /contracts/01` answering
   * **200**, because an `int` path parameter does not refuse a leading zero.
   *
   * **`contract-scope.ts` used to refuse that same string, and `D10-76` settled the
   * open call `D10-55` named in favour of this guard.** The two agree on every input
   * by two spellings of one rule: this side tests `/^\d+$/` and then `> 0`, the other
   * tests `/^0*[1-9][0-9]*$/`, and the pair admits exactly the same strings — zero
   * stays zero under any padding, and no other spelling reaches either. Neither
   * guard's code changed in that ruling: the *other* side was brought to match this
   * one, because this is the side the server agrees with, and a client stricter than
   * its own API turns an address that works into "invalid filter".
   */
  const raw = typeof params.id === "string" ? params.id : "";
  const contractId = Number.parseInt(raw, 10);
  const validId = /^\d+$/.test(raw) && Number.isSafeInteger(contractId) && contractId > 0;

  const contract = useQuery({
    queryKey: ["contracts", "detail", contractId],
    queryFn: () => authorized((accessToken) => contracts.get(accessToken, contractId)),
    enabled: isAuthenticated && validId,
    // A 404 and a 403 are answers, not transient failures: retrying them just
    // repeats the refusal and delays the message the reader needs.
    retry: false,
  });

  const fields = useQuery({
    queryKey: ["contracts", "detail", contractId, "fields"],
    queryFn: () => authorized((accessToken) => contracts.fields(accessToken, contractId)),
    enabled: isAuthenticated && validId,
    retry: false,
  });

  /**
   * The three per-contract reads that existed before this page did.
   *
   * Each one passes the id this page was opened with to the route the screen
   * already calls, so the card and the screen below it parse the same response
   * type and cannot disagree. They are separate queries rather than one combined
   * call because they fail independently: a viewer may read all three, but a
   * single 403 from any of them must not blank the other two.
   */
  const decisions = useQuery({
    queryKey: ["contracts", "detail", contractId, "decisions"],
    queryFn: () => authorized((accessToken) => agent.decisions(accessToken, contractId)),
    enabled: isAuthenticated && validId,
    retry: false,
  });

  const reminders = useQuery({
    queryKey: ["contracts", "detail", contractId, "notifications"],
    queryFn: () => authorized((accessToken) => notifications.list(accessToken, { contractId })),
    enabled: isAuthenticated && validId,
    retry: false,
  });

  const escalationsHere = useQuery({
    queryKey: ["contracts", "detail", contractId, "escalations"],
    queryFn: () => authorized((accessToken) => escalations.queue(accessToken, { contractId })),
    enabled: isAuthenticated && validId,
    retry: false,
  });

  /**
   * A refusal in the reader's own language.
   *
   * Every role may read this route, so `forbidden` should be unreachable — it is
   * translated anyway, because the boundary is the route's and a UI that assumes
   * it cannot happen turns a policy change into an untranslated envelope. 404 is
   * the answer for a contract in another tenant as well as one that never existed,
   * and the two are deliberately indistinguishable.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.status === 404) return t("contracts.detailNotFound");
      if (cause.isForbidden) return t("contracts.detailForbidden");
      if (cause.isOffline) return t("contracts.offline");
    }
    return t("contracts.failed");
  }

  /**
   * Calendar dates are formatted in UTC on purpose: `new Date("2026-01-02")` is
   * UTC midnight, so a negative-offset zone would render the day before. Instants
   * (the ingestion timestamp) keep the reader's zone, which is correct for them.
   */
  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });
  const instantFormat = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  function formatDate(value: string): string {
    return dateFormat.format(new Date(value));
  }

  const row = contract.data;

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <p className="text-sm">
        <Link
          className="text-primary underline-offset-4 hover:underline"
          href={`/${locale}/contracts`}
        >
          {t("contracts.detailBack")}
        </Link>
      </p>

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
          {contract.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("contracts.loading")}</p>
              </CardContent>
            </Card>
          )}

          {contract.isError && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {describeFailure(contract.error)}
                </p>
                <TraceId error={contract.error} />
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void contract.refetch();
                  }}
                >
                  {t("contracts.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {row !== undefined && (
            <>
              <header className="space-y-2">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h1 className="text-2xl font-semibold">
                    {t("contracts.contractLabel", { id: row.id })}
                  </h1>
                  <span
                    className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_TONES[row.status]}`}
                  >
                    {t(`contracts.${STATUS_KEYS[row.status]}`)}
                  </span>
                </div>
                <p className="break-words text-sm text-muted-foreground">{row.customer_ref}</p>
              </header>

              <ContractFacts row={row} formatDate={formatDate} instantFormat={instantFormat} />

              <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
                {t("contracts.detailConsentNote")}
              </p>

              <FieldsCard
                fields={fields}
                describeFailure={describeFailure}
                onRetry={() => {
                  void fields.refetch();
                }}
              />

              <RelatedReads
                contractId={contractId}
                decisions={decisions}
                reminders={reminders}
                escalationsHere={escalationsHere}
                describeFailure={describeFailure}
              />
            </>
          )}
        </>
      )}
    </main>
  );
}

interface ContractFactsProps {
  row: ContractListItem;
  /** Pre-bound so both date columns are formatted identically. */
  formatDate: (value: string) => string;
  /** For the columns that are instants rather than calendar dates. */
  instantFormat: Intl.DateTimeFormat;
}

/**
 * Everything the row stores that a reader may see.
 *
 * Nothing here is a customer value: `customer_ref` is the pseudonymous label the
 * model documents, and the identity lives in the encrypted field rows this page
 * never reads a value out of. `storage_path` and `created_by` are not on the wire
 * at all — `ContractListItem` withholds them — which is why there is no "where is
 * the file" line: the answer a reader needs is `has_document`.
 */
function ContractFacts({ row, formatDate, instantFormat }: ContractFactsProps) {
  const t = useTranslations();

  const term =
    row.start_date === null && row.end_date === null
      ? t("contracts.noDates")
      : row.start_date !== null && row.end_date !== null
        ? `${formatDate(row.start_date)} – ${formatDate(row.end_date)}`
        : formatDate(row.start_date ?? row.end_date ?? "");

  const channels = row.consent_channels.map((channel) => t(`contracts.${CHANNEL_KEYS[channel]}`));

  /**
   * `may_contact` decides and the flags only explain. Reading them the other way
   * round would give this page its own opinion about consent.
   */
  const contact = row.may_contact
    ? t("contracts.contactPermitted", { channels: channels.join(", ") })
    : row.do_not_contact
      ? t("contracts.contactOptedOut")
      : t("contracts.contactNoChannels");

  return (
    <Card>
      <CardContent>
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

/**
 * Only the shape this card reads, so it cannot reach for the query object itself
 * and cannot be tempted to refetch something the page already owns.
 */
type FieldsQuery = {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: { fields: ContractField[]; blocking: number } | undefined;
};

interface FieldsCardProps {
  fields: FieldsQuery;
  describeFailure: (cause: unknown) => string;
  onRetry: () => void;
}

function FieldsCard({ fields, describeFailure, onRetry }: FieldsCardProps) {
  const t = useTranslations();

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("contracts.fieldsHeading")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* Stated before anything is listed: an empty table would read as a clean
            contract rather than as one nothing has been read from. */}
        <p className="text-xs text-muted-foreground">{t("contracts.fieldsHint")}</p>

        {fields.isPending && (
          <p className="text-sm text-muted-foreground">{t("contracts.loading")}</p>
        )}

        {fields.isError && (
          <div className="space-y-3">
            <p className="text-sm text-danger" role="alert">
              {describeFailure(fields.error)}
            </p>
            <TraceId error={fields.error} />
            <Button size="sm" variant="outline" onClick={onRetry}>
              {t("contracts.retry")}
            </Button>
          </div>
        )}

        {fields.data !== undefined && (
          <>
            {fields.data.fields.length === 0 ? (
              <div className="space-y-1">
                <p className="text-sm">{t("contracts.fieldsEmpty")}</p>
                <p className="text-xs text-muted-foreground">{t("contracts.fieldsEmptyHint")}</p>
              </div>
            ) : (
              <>
                <p className="text-xs">
                  {fields.data.blocking === 0
                    ? t("contracts.fieldsClear")
                    : t("contracts.fieldsBlocking", { count: fields.data.blocking })}
                </p>

                <ul className="space-y-2">
                  {fields.data.fields.map((field) => (
                    <FieldRow key={field.field_name} field={field} />
                  ))}
                </ul>
              </>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/** One extracted field: what it is, how well it was read, and whether it blocks. */
function FieldRow({ field }: { field: ContractField }) {
  const t = useTranslations();

  return (
    <li className="space-y-1 border-t border-border pt-2 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">{t(`review.${FIELD_KEYS[field.field_name]}`)}</span>
        <span className={field.blocks_pipeline ? "text-danger" : "text-muted-foreground"}>
          {field.blocks_pipeline ? t("contracts.fieldBlocks") : t("contracts.fieldDoesNotBlock")}
        </span>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-muted-foreground">
        <dt>{t("contracts.fieldStatusColumn")}</dt>
        <dd>{t(`review.${FIELD_STATUS_KEYS[field.status]}`)}</dd>

        <dt>{t("contracts.fieldConfidenceColumn")}</dt>
        {/* Two decimals, like the review queue: a confidence is a score, not a number
            to compare beyond the hundredth. */}
        <dd>{field.confidence.toFixed(2)}</dd>

        {field.source_page !== null && (
          <>
            <dt>{t("contracts.fieldSourceColumn")}</dt>
            <dd>
              {field.source_line === null
                ? t("review.sourcePageOnly", { page: field.source_page })
                : t("review.sourcePageLine", {
                    page: field.source_page,
                    line: field.source_line,
                  })}
            </dd>
          </>
        )}

        {field.review_reason !== null && (
          <>
            <dt>{t("contracts.fieldReasonColumn")}</dt>
            <dd className="break-words">{field.review_reason}</dd>
          </>
        )}
      </dl>
    </li>
  );
}

/**
 * Only the parts of a query this card reads, so it cannot reach for the query
 * object itself and cannot refetch something the page owns.
 */
type RelatedQuery<T> = {
  isPending: boolean;
  isError: boolean;
  error: unknown;
  data: T | undefined;
  /** Void-returning on purpose: the card starts the refetch and never awaits it. */
  refetch: () => void;
};

interface RelatedReadsProps {
  /** The contract whose address this card belongs to; carried into every link. */
  contractId: number;
  decisions: RelatedQuery<AgentDecisionList>;
  reminders: RelatedQuery<NotificationList>;
  escalationsHere: RelatedQuery<EscalationQueue>;
  describeFailure: (cause: unknown) => string;
}

/**
 * What the rest of the system has recorded about this contract.
 *
 * The card states a count, not a list: the queue screens are where a row is
 * worked on, and a second read-only rendering of their rows here would be a
 * second place for the wording of a status to drift. The counts come from the
 * responses' own `count`, so nothing is summed up on the client.
 */
function RelatedReads({
  contractId,
  decisions,
  reminders,
  escalationsHere,
  describeFailure,
}: RelatedReadsProps) {
  const t = useTranslations();
  const locale = useLocale();

  const latestDecision = decisions.data?.items[0];
  const latestReminder = reminders.data?.items[0];

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("contracts.detailRelatedHeading")}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {/*
          This card used to say none of these reads took a contract at all, and then
          that its links stayed unfiltered because the screens ignored a contract in
          their address. Both are now false, so the note says what the card does. The
          links carry the contract id through `withContractScope`, the same helper
          each screen parses with, so a link cannot spell the parameter differently
          from the screen that reads it.
        */}
        <p className="text-xs text-muted-foreground">{t("contracts.detailRelatedNote")}</p>

        <RelatedRead
          href={withContractScope(`/${locale}/agent`, contractId)}
          label={t("contracts.detailDecisionsLabel")}
          linkLabel={t("contracts.detailOpen")}
          state={decisions}
          describeFailure={describeFailure}
          onRetry={() => {
            void decisions.refetch();
          }}
          stat={
            decisions.data === undefined
              ? null
              : t("contracts.detailDecisionsCount", { count: decisions.data.count })
          }
          detail={
            latestDecision === undefined
              ? null
              : t("contracts.detailLatestDecision", {
                  channel: t(`contracts.${CHANNEL_KEYS[latestDecision.channel]}`),
                })
          }
        />

        <RelatedRead
          href={withContractScope(`/${locale}/notifications`, contractId)}
          label={t("contracts.detailNotificationsLabel")}
          linkLabel={t("contracts.detailOpen")}
          state={reminders}
          describeFailure={describeFailure}
          onRetry={() => {
            void reminders.refetch();
          }}
          stat={
            reminders.data === undefined
              ? null
              : t("contracts.detailNotificationsCount", { count: reminders.data.count })
          }
          /* The slot is a calendar date on the contract's clock, so it is printed
             as the server sent it rather than through a local formatter. */
          detail={
            latestReminder === undefined
              ? null
              : t("contracts.detailLatestReminder", { date: latestReminder.scheduled_date })
          }
        />

        <RelatedRead
          href={withContractScope(`/${locale}/escalations`, contractId)}
          label={t("contracts.detailEscalationsLabel")}
          linkLabel={t("contracts.detailOpen")}
          state={escalationsHere}
          describeFailure={describeFailure}
          onRetry={() => {
            void escalationsHere.refetch();
          }}
          stat={
            escalationsHere.data === undefined
              ? null
              : t("contracts.detailEscalationsCount", { count: escalationsHere.data.count })
          }
          /* `overdue` is the server's answer, counted across the rows it returned;
             recomputing it from `sla_deadline` here would drift from the queue. */
          detail={
            escalationsHere.data === undefined
              ? null
              : t("contracts.detailEscalationOverdue", {
                  count: escalationsHere.data.overdue,
                  hours: escalationsHere.data.sla_hours,
                })
          }
        />
      </CardContent>
    </Card>
  );
}

interface RelatedReadProps {
  href: string;
  label: string;
  linkLabel: string;
  state: { isPending: boolean; isError: boolean; error: unknown };
  describeFailure: (cause: unknown) => string;
  onRetry: () => void;
  /** The headline answer, or `null` while there is none to state. */
  stat: string | null;
  /** A second line the answer allows, when the route returns one. */
  detail: string | null;
}

/** One read: what it answered, or why it could not be asked. */
function RelatedRead({
  href,
  label,
  linkLabel,
  state,
  describeFailure,
  onRetry,
  stat,
  detail,
}: RelatedReadProps) {
  const t = useTranslations();

  return (
    <div className="space-y-1 border-t border-border pt-2 text-xs">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">{label}</span>
        <Link className="text-primary underline-offset-4 hover:underline" href={href}>
          {linkLabel}
        </Link>
      </div>

      {state.isPending && <p className="text-muted-foreground">{t("contracts.loading")}</p>}

      {state.isError && (
        <div className="space-y-2">
          <p className="text-danger" role="alert">
            {describeFailure(state.error)}
          </p>
          <TraceId error={state.error} />
          <Button size="sm" variant="outline" onClick={onRetry}>
            {t("contracts.retry")}
          </Button>
        </div>
      )}

      {!state.isPending && !state.isError && stat !== null && <p>{stat}</p>}
      {detail !== null && <p className="text-muted-foreground">{detail}</p>}
    </div>
  );
}
