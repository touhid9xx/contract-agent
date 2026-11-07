"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FieldLabel, Input } from "@/components/ui/input";
import { ApiError, notifications } from "@/lib/api";
import type {
  ContactChannel,
  DispatchOutcome,
  NotificationRow,
  NotificationStatus,
} from "@/lib/api";
import {
  contractIdFromInput,
  inputNamesAContract,
  readContractScope,
  withContractScope,
} from "@/lib/contract-scope";
import { authorized } from "@/lib/session";

/**
 * The status filter, in the order it is offered.
 *
 * `null` is "all". Every member of `NotificationStatus` is here — unlike the
 * contracts screen, which omits a status that belongs to another enum, this one has
 * no derived members to leave out.
 */
const STATUS_FILTERS: (NotificationStatus | null)[] = [
  null,
  "pending",
  "sent",
  "delivered",
  "answered",
  "machine",
  "failed",
  "suppressed",
];

/** One per `app.db.enums.Channel`. */
const CHANNEL_FILTERS: (ContactChannel | null)[] = [null, "email", "sms", "voice"];

/**
 * Wire value → message key (relative to the `notifications` namespace).
 *
 * Records rather than string surgery for the same reason the other screens use
 * them: `t(\`notifications.statuses.${value}\`)` would resolve to a missing key the
 * day the enum gains a member, and a concatenated key is not something the
 * compiler can check. `satisfies Record<…, string>` makes a new member a type
 * error here instead of a raw path on the screen.
 */
const STATUS_KEYS = {
  pending: "statuses.pending",
  sent: "statuses.sent",
  delivered: "statuses.delivered",
  answered: "statuses.answered",
  machine: "statuses.machine",
  failed: "statuses.failed",
  suppressed: "statuses.suppressed",
} as const satisfies Record<NotificationStatus, string>;

const CHANNEL_KEYS = {
  email: "channels.email",
  sms: "channels.sms",
  voice: "channels.voice",
} as const satisfies Record<ContactChannel, string>;

/**
 * Tone per status.
 *
 * Only the two *parked* states are alarmed. `sent`/`delivered`/`answered` are the
 * system having worked, and `pending` is the normal state of a scheduled row —
 * painting those red would make a healthy queue look like an incident. There is no
 * success token in the theme, so "fine" is the muted default rather than a colour
 * invented here.
 */
const STATUS_TONES = {
  pending: "bg-muted text-muted-foreground",
  sent: "bg-muted text-muted-foreground",
  delivered: "bg-muted text-muted-foreground",
  answered: "bg-muted text-muted-foreground",
  machine: "bg-muted text-muted-foreground",
  failed: "bg-danger-muted text-danger",
  suppressed: "bg-danger-muted text-danger",
} as const satisfies Record<NotificationStatus, string>;

/**
 * Server refusal code → message key.
 *
 * The codes are strings on the wire (`DispatchRefusedError.code`), not a TypeScript
 * enum, so this map cannot be exhaustive `satisfies`-checked the way the status
 * maps are. It is therefore looked up with a fallback: an unrecognised code still
 * says "that did not work" instead of printing a key or a raw code.
 *
 * Each code is translated rather than collapsed into one sentence because they name
 * different, actionable facts. "A field still needs approval" and "the retry budget
 * is spent" call for two different next actions, and an operator who is told only
 * "refused" has to read the server logs to find out which.
 *
 * Every code this screen's two actions can provoke is mapped. The codes *not* here
 * — `NOTIFICATION_EXISTS`, `NO_DECISION`, `DECISION_MISMATCH` — come only from
 * `POST /notifications`, which schedules a row and has no button on this screen, and
 * `TENANT_MISMATCH`, which only the scheduler records onto `last_error`. They fall
 * through to the generic failure rather than being translated for a click that
 * cannot happen.
 */
const REFUSAL_KEYS: Record<string, string> = {
  // Reached through `assert_may_contact`, which dispatch re-runs at send time
  // rather than trusting the decision row. These four are the consent gates, and
  // a withdrawn consent is the *most* likely refusal an operator sees here: the
  // row was schedulable yesterday and the customer opted out this morning.
  CONTRACT_CLOSED: "refusalContractClosed",
  CONTACT_REFUSED: "refusalContactRefused",
  NO_CONSENT: "refusalNoConsent",
  PIPELINE_BLOCKED: "refusalPipelineBlocked",
  NOT_PENDING: "refusalNotPending",
  NOT_YET_DUE: "refusalNotYetDue",
  NOT_IN_WINDOW: "refusalNotInWindow",
  MISSING_END_DATE: "refusalMissingEndDate",
  UNKNOWN_TIMEZONE: "refusalUnknownTimezone",
  NEEDS_REVIEW: "refusalNeedsReview",
  CHANNEL_NOT_CONSENTED: "refusalChannelNotConsented",
  ATTEMPTS_EXHAUSTED: "refusalAttemptsExhausted",
  SENDER_UNAVAILABLE: "refusalSenderUnavailable",
  RECIPIENT_UNAVAILABLE: "refusalRecipientUnavailable",
  NOT_FAILED: "refusalNotFailed",
  CONCURRENT_UPDATE: "refusalConcurrentUpdate",
};

/**
 * The notification queue — the last route family that only a `TestClient` had
 * called.
 *
 * Before this screen, "did the reminder go out, and if not why not?" was answerable
 * only with curl. The scheduler meanwhile raises rows on its own, so an unreadable
 * queue is the same shape of problem as the escalation queue was: a worker noticing
 * something nobody can act on.
 *
 * Four choices are deliberate:
 *
 * * **The recipient is never shown.** The route does not return one — a row records
 *   *that* a contact was attempted, not who it reached — and this screen does not
 *   reconstruct it. Reading an approved contact value is the review screen's audited
 *   reveal, and it stays the only door.
 * * **`template` is rendered as stored.** It is the milestone label (`T-60`…), a
 *   configured string rather than a closed enum, so it is displayed verbatim instead
 *   of being mapped to a message key that could silently miss.
 * * **Sending asks first.** `/dispatch` contacts a real customer immediately. The
 *   route exists for operators, but a single mis-click that mails a customer cannot
 *   be undone, so it takes a confirmation step that names the consequence.
 * * **Retry does not ask.** `/retry` only requeues a parked row — it sends nothing,
 *   and the scheduler still has to find it due. Requiring confirmation there would
 *   train people to click through dialogs before the one that matters.
 */
export default function NotificationsPage() {
  return (
    // `useSearchParams` suspends while the address is resolved, so the route needs a
    // boundary to prerender. `null` is enough: this shell is only reached before
    // hydration, and the screen under it renders a translated loading card itself.
    <Suspense fallback={null}>
      <NotificationsScreen />
    </Suspense>
  );
}

function NotificationsScreen() {
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { user, isAuthenticated } = useAuth();

  const canAct = user !== null && user.role !== "viewer";

  const [statusFilter, setStatusFilter] = useState<NotificationStatus | null>(null);
  const [channelFilter, setChannelFilter] = useState<ContactChannel | null>(null);

  /**
   * The contract this address names, if it names one.
   *
   * In the address rather than in `useState` so a link from a contract's own page
   * survives a reload, a bookmark and the back button (`D10-36`), and so the count
   * the contract page quoted is the count this screen asks the server for.
   */
  const scope = readContractScope(searchParams);
  const scopeId = scope.kind === "contract" ? scope.id : undefined;
  const scopeIsInvalid = scope.kind === "invalid";
  const scopeInAddress = scopeId === undefined ? "" : String(scopeId);

  const [draftContract, setDraftContract] = useState(scopeInAddress);

  /** A link, the back button or a typed address change `scopeId` without a remount. */
  useEffect(() => {
    setDraftContract(scopeInAddress);
  }, [scopeInAddress]);

  const trimmedContract = draftContract.trim();
  // The shape `readContractScope` accepts on the way out, so a typed id cannot be
  // one this screen then calls malformed.
  const contractIsUsable = inputNamesAContract(trimmedContract);

  /**
   * Put the scope in the address, which is where it lives.
   *
   * `replace`, not `push`: typing an id and correcting it is not several pages to
   * step back through. A link arriving here still pushes.
   */
  function scopeToAddress(id: number | undefined) {
    router.replace(withContractScope(`/${locale}/notifications`, id));
  }

  const list = useQuery({
    queryKey: ["notifications", "list", statusFilter, channelFilter, scopeId ?? "all"],
    // An address naming something that cannot be a contract is not a filter, and it
    // is not permission to show the tenant's whole queue either: the screen says so
    // and the query stays idle.
    enabled: !scopeIsInvalid,
    queryFn: () =>
      authorized((accessToken) =>
        notifications.list(accessToken, {
          // Omitted rather than sent as `undefined`: the client builds the query
          // string, and a `null` would be neither a filter nor an absence.
          ...(scopeId === undefined ? {} : { contractId: scopeId }),
          ...(statusFilter === null ? {} : { status: statusFilter }),
          ...(channelFilter === null ? {} : { channel: channelFilter }),
        }),
      ),
  });

  const send = useMutation({
    mutationFn: (notificationId: number) =>
      authorized((accessToken) => notifications.dispatch(accessToken, notificationId)),
    onSuccess: async () => {
      // The row's status changed under the filter that selected it, so the page the
      // operator is reading is no longer the page the server would answer.
      await queryClient.invalidateQueries({ queryKey: ["notifications", "list"] });
    },
    onError: async (cause) => {
      // A refusal can park the row `failed` before answering 409 — that transition
      // is committed — so a conflict or a 404 also means "this page is stale".
      if (cause instanceof ApiError && (cause.isConflict || cause.status === 404)) {
        await queryClient.invalidateQueries({ queryKey: ["notifications", "list"] });
      }
    },
  });

  const requeue = useMutation({
    mutationFn: (notificationId: number) =>
      authorized((accessToken) => notifications.retry(accessToken, notificationId)),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["notifications", "list"] });
    },
  });

  /** A refusal in the operator's own language, falling back to a generic failure. */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      const key = REFUSAL_KEYS[cause.code];
      if (key !== undefined) return t(`notifications.${key}`);
      if (cause.isForbidden) return t("notifications.forbidden");
      if (cause.isOffline) return t("notifications.offline");
    }
    return t("notifications.failed");
  }

  const instantFormat = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  /**
   * `scheduled_date` is a calendar date in the contract's own time zone, not an
   * instant: formatting it in the reader's zone would render a different day for
   * anyone west of UTC. The instant columns keep the reader's local zone.
   */
  const dateFormat = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" });

  /**
   * Say what the dispatch attempt actually did.
   *
   * Three outcomes share one 200, so the order matters: a deferral is a *success*
   * with a different meaning — the system protected the customer's quiet hours —
   * and testing `sent` first would report it as a failed send. Ordered the other
   * way round, every quiet-hours deferral would read as an error.
   *
   * Defined inside the component because it translates: `useTranslations` is a
   * hook, and a module-level helper calling it would be a hook outside React.
   */
  function describeOutcome(outcome: DispatchOutcome): string {
    if (outcome.deferred_until !== null) {
      return t("notifications.sendDeferred", {
        when: instantFormat.format(new Date(outcome.deferred_until)),
      });
    }
    if (outcome.sent) return t("notifications.sendSucceeded");
    if (outcome.error !== null) {
      return t("notifications.sendAttemptFailed", { error: outcome.error });
    }
    return t("notifications.failed");
  }

  const listData = list.data;
  const items = listData?.items ?? [];

  const busyId = send.isPending
    ? send.variables
    : requeue.isPending
      ? requeue.variables
      : undefined;

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("notifications.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("notifications.subtitle")}</p>
      </header>

      {!isAuthenticated ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("notifications.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("notifications.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!contractIsUsable) return;
              scopeToAddress(contractIdFromInput(trimmedContract));
            }}
          >
            <div className="space-y-1.5">
              <FieldLabel htmlFor="notifications-contract-filter">
                {t("notifications.contractFilterLabel")}
              </FieldLabel>
              <Input
                id="notifications-contract-filter"
                name="notifications-contract-filter"
                className="w-40"
                inputMode="numeric"
                value={draftContract}
                placeholder={t("notifications.contractPlaceholder")}
                onChange={(event) => {
                  setDraftContract(event.target.value);
                }}
              />
            </div>
            <Button size="sm" type="submit" disabled={!contractIsUsable}>
              {t("notifications.contractApply")}
            </Button>
            {(scopeId !== undefined || scopeIsInvalid) && (
              <Button size="sm" type="button" variant="ghost" onClick={() => scopeToAddress(undefined)}>
                {t("notifications.contractClear")}
              </Button>
            )}
            {!contractIsUsable && (
              <p className="text-xs text-danger" role="alert">
                {t("notifications.contractInvalid")}
              </p>
            )}
          </form>

          {scopeIsInvalid && (
            // The same screen the agent route shows for the same address: an id
            // that cannot be a contract is not permission to list the tenant's
            // whole queue behind a filter that never ran.
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {t("notifications.contractInvalid")}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t("notifications.contractInvalidHint")}
                </p>
                <Button size="sm" variant="outline" onClick={() => scopeToAddress(undefined)}>
                  {t("notifications.contractClear")}
                </Button>
              </CardContent>
            </Card>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">
              {t("notifications.statusFilterLabel")}
            </span>
            {STATUS_FILTERS.map((candidate) => (
              <Button
                key={candidate ?? "all-statuses"}
                size="sm"
                variant={candidate === statusFilter ? "primary" : "outline"}
                aria-current={candidate === statusFilter ? "true" : undefined}
                onClick={() => {
                  setStatusFilter(candidate);
                }}
              >
                {candidate === null
                  ? t("notifications.filterAll")
                  : t(`notifications.${STATUS_KEYS[candidate]}`)}
              </Button>
            ))}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-medium text-muted-foreground">
              {t("notifications.channelFilterLabel")}
            </span>
            {CHANNEL_FILTERS.map((candidate) => (
              <Button
                key={candidate ?? "all-channels"}
                size="sm"
                variant={candidate === channelFilter ? "primary" : "outline"}
                aria-current={candidate === channelFilter ? "true" : undefined}
                onClick={() => {
                  setChannelFilter(candidate);
                }}
              >
                {candidate === null
                  ? t("notifications.filterAll")
                  : t(`notifications.${CHANNEL_KEYS[candidate]}`)}
              </Button>
            ))}
          </div>

          {listData !== undefined && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>{t("notifications.countLabel", { count: listData.count })}</span>
              {/*
                The server's own echo, not the address read back: `contract_id` is
                the filter the route actually applied, so the sentence cannot say
                "contract #12 only" over a list the server answered unfiltered.
              */}
              <span>
                {listData.contract_id === null
                  ? t("notifications.scopeAllContracts")
                  : t("notifications.scopeContract", { id: listData.contract_id })}
              </span>
            </div>
          )}

          {!canAct && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
              {t("notifications.viewerNote")}
            </p>
          )}

          {/*
            Reported at page level rather than on the card: a successful dispatch can
            change the row's status, which can move it out from under the active
            filter — taking a card-local message with it.
          */}
          {send.isSuccess && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs" role="status">
              {describeOutcome(send.data)}
            </p>
          )}

          {requeue.isSuccess && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs" role="status">
              {t("notifications.retrySucceeded")}
            </p>
          )}

          {!scopeIsInvalid && list.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("notifications.loading")}</p>
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
                  {t("notifications.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {!list.isPending && !list.isError && items.length === 0 && (
            <Card>
              <CardContent className="space-y-1">
                <p className="text-sm">
                  {scopeId === undefined ? t("notifications.empty") : t("notifications.emptyForContract")}
                </p>
                <p className="text-xs text-muted-foreground">{t("notifications.emptyHint")}</p>
              </CardContent>
            </Card>
          )}

          {items.map((row) => (
            <NotificationCard
              key={row.id}
              row={row}
              canAct={canAct}
              busy={busyId === row.id}
              scheduledDate={dateFormat.format(new Date(row.scheduled_date))}
              deferredUntil={
                row.scheduled_at === null ? null : instantFormat.format(new Date(row.scheduled_at))
              }
              sentAt={row.sent_at === null ? null : instantFormat.format(new Date(row.sent_at))}
              // Only the row an operator acted on may show an error: one shared
              // message would blame every card on the page for one refusal.
              error={
                send.isError && send.variables === row.id
                  ? describeFailure(send.error)
                  : requeue.isError && requeue.variables === row.id
                    ? describeFailure(requeue.error)
                    : null
              }
              onSend={() => {
                send.mutate(row.id);
              }}
              onRequeue={() => {
                requeue.mutate(row.id);
              }}
            />
          ))}

          <p className="text-xs text-muted-foreground">{t("notifications.recipientNote")}</p>
        </>
      )}

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("notifications.backToHome")}
        </Link>
      </p>
    </main>
  );
}

interface NotificationCardProps {
  row: NotificationRow;
  canAct: boolean;
  /** This row's mutation is in flight. */
  busy: boolean;
  /** Pre-formatted in the page so every card renders a calendar date the same way. */
  scheduledDate: string;
  /** `null` when quiet hours did not defer this row. */
  deferredUntil: string | null;
  /** `null` while the row has not reached a transport. */
  sentAt: string | null;
  error: string | null;
  onSend: () => void;
  onRequeue: () => void;
}

/**
 * One reminder.
 *
 * It answers "where is this, and if it is stuck, why" — milestone, channel, attempt
 * count and the recorded failure — and offers only the two actions the row's own
 * status permits, so the server's policy is not second-guessed here.
 */
function NotificationCard({
  row,
  canAct,
  busy,
  scheduledDate,
  deferredUntil,
  sentAt,
  error,
  onSend,
  onRequeue,
}: NotificationCardProps) {
  const t = useTranslations();
  const [confirming, setConfirming] = useState(false);

  /** Only a pending row can be dispatched; the policy refuses anything else. */
  const canSend = row.status === "pending";
  /** Only a parked row can be requeued — the same pair `RETRYABLE_STATUSES` holds. */
  const canRequeue = row.status === "failed" || row.status === "suppressed";

  return (
    <Card aria-busy={busy}>
      <CardHeader className="flex flex-wrap items-baseline justify-between gap-2">
        <CardTitle>
          {t("notifications.contractLabel", { id: row.contract_id })} · {row.template}
        </CardTitle>
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_TONES[row.status]}`}
        >
          {t(`notifications.${STATUS_KEYS[row.status]}`)}
        </span>
      </CardHeader>

      <CardContent className="space-y-3">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">{t("notifications.channelLabel")}</dt>
          <dd>{t(`notifications.${CHANNEL_KEYS[row.channel]}`)}</dd>

          <dt className="text-muted-foreground">{t("notifications.templateLabel")}</dt>
          <dd className="font-mono">{row.template}</dd>

          <dt className="text-muted-foreground">{t("notifications.scheduledLabel")}</dt>
          <dd>{scheduledDate}</dd>

          {deferredUntil !== null && (
            <>
              <dt className="text-muted-foreground">{t("notifications.queuedAtLabel")}</dt>
              <dd>{deferredUntil}</dd>
            </>
          )}

          <dt className="text-muted-foreground">{t("notifications.sentAtLabel")}</dt>
          <dd>{sentAt ?? t("notifications.notSent")}</dd>

          <dt className="text-muted-foreground">{t("notifications.attemptsLabel")}</dt>
          <dd>{row.attempts}</dd>

          {row.last_error !== null && (
            <>
              <dt className="text-muted-foreground">{t("notifications.lastErrorLabel")}</dt>
              {/* Stored redacted by the transport, so it is safe to render as-is. */}
              <dd className="break-words">{row.last_error}</dd>
            </>
          )}

          {row.provider_message_id !== null && (
            <>
              <dt className="text-muted-foreground">{t("notifications.providerLabel")}</dt>
              <dd className="font-mono break-all">{row.provider_message_id}</dd>
            </>
          )}
        </dl>

        {canAct && canRequeue && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="secondary" disabled={busy} onClick={onRequeue}>
              {busy ? t("notifications.retrying") : t("notifications.retryAction")}
            </Button>
          </div>
        )}

        {canAct && canSend && !confirming && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={busy}
              onClick={() => {
                setConfirming(true);
              }}
            >
              {t("notifications.sendNow")}
            </Button>
          </div>
        )}

        {canAct && canSend && confirming && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium">{t("notifications.sendConfirm")}</p>
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={busy} onClick={onSend}>
                {busy ? t("notifications.sending") : t("notifications.confirmSend")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setConfirming(false);
                }}
              >
                {t("notifications.cancel")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("notifications.sendConfirmHint")}</p>
          </div>
        )}

        {error !== null && (
          <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
