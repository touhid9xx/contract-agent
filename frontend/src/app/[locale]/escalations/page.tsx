"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type ReactElement } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorNote } from "@/components/ui/error-note";
import { FieldLabel, Input } from "@/components/ui/input";
import { TraceId } from "@/components/ui/trace-id";
import { ApiError, escalations } from "@/lib/api";
import type {
  ContactChannel,
  Escalation,
  EscalationStatus,
  EscalationUpdate,
} from "@/lib/api";
import {
  contractIdFromInput,
  inputNamesAContract,
  readContractScope,
  readResolved,
  withContractScope,
  withResolved,
} from "@/lib/contract-scope";
import { ESCALATION_REFUSALS, refusalKey } from "@/lib/refusals";
import { authorized } from "@/lib/session";

/**
 * The states a *working* queue shows.
 *
 * `sla_breached` is deliberately absent: it is derived from `sla_deadline` by
 * `is_overdue` on the server and the update route refuses an operator who tries
 * to set it, so a row is late by its deadline rather than by its status. The
 * queue is filtered by state, lateness is a separate badge.
 */
const WORKING_STATUSES: EscalationStatus[] = ["open", "in_progress"];

/** The channels the reply buttons offer — one per `app.db.enums.Channel`. */
const REPLY_CHANNELS: ContactChannel[] = ["email", "sms", "voice"];

/**
 * Channel → message key.
 *
 * A record rather than string surgery on the wire value: `t("…channel" + value)`
 * would silently resolve to a missing key the day `Channel` gains a member, and
 * the compiler cannot check a concatenated key.
 */
const CHANNEL_KEYS = {
  email: "channelEmail",
  sms: "channelSms",
  voice: "channelVoice",
} as const satisfies Record<ContactChannel, string>;

/**
 * The escalation queue (gap 15, F9's human half).
 *
 * Before this screen an operator could see *that* a contract went quiet only by
 * calling the API: the `escalation` route family had been reached exclusively by
 * `TestClient`. The scanner shipped last slice now raises these rows on its own,
 * which makes a queue that nobody can work a strictly worse state than no queue —
 * so this is the screen that closes the loop between "something noticed" and
 * "someone acted".
 *
 * Three distinctions the UI keeps, because collapsing any of them would be a lie:
 *
 * * **Resolving is not responding.** Closing a queue item leaves the contract
 *   `escalated`: an operator working a row is not the customer answering. Only
 *   "customer replied" moves the contract to `responded`, and it closes the open
 *   escalation as a side effect on the server, in one transaction.
 * * **Late is computed, not stored.** The badge reads `overdue` from the server,
 *   which decided it against the same clock that wrote `sla_deadline`.
 * * **A reply has no body.** The route stores that the customer answered, on
 *   which channel and when; there is no field here to type what they said, and
 *   adding one would put the largest PII surface in the app in plaintext.
 */
export default function EscalationsPage() {
  return (
    // `useSearchParams` suspends while the address is resolved, so the route needs
    // a boundary to prerender. `null` is enough: this shell only runs before
    // hydration, and the screen under it renders its own translated cards.
    <Suspense fallback={null}>
      <EscalationsScreen />
    </Suspense>
  );
}

function EscalationsScreen() {
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { user, isAuthenticated } = useAuth();

  const canWork = user !== null && user.role !== "viewer";

  /** The address this screen owns, before any filter is added to it. */
  const basePath = `/${locale}/escalations`;

  /**
   * Both filters live in the address, not in `useState`.
   *
   * The contract a contract's own page links here for, and whether closed rows are
   * shown, are both facts about *which question is being asked* — so they belong
   * where a reload, a bookmark, the back button and a copied link can all carry
   * them (`D10-36`). `readContractScope` keeps "no contract named" apart from
   * "named something that cannot be a contract", because the second must not fall
   * through to the tenant-wide queue.
   */
  const scope = readContractScope(searchParams);
  const scopeId = scope.kind === "contract" ? scope.id : undefined;
  const scopeIsInvalid = scope.kind === "invalid";
  const scopeInAddress = scopeId === undefined ? "" : String(scopeId);

  /** Resolved rows are history, not work: absent from the address until asked for. */
  const showResolved = readResolved(searchParams);

  const [draftContract, setDraftContract] = useState(scopeInAddress);

  /** A link, the back button or a typed address change `scopeId` without a remount. */
  useEffect(() => {
    setDraftContract(scopeInAddress);
  }, [scopeInAddress]);

  const trimmedContract = draftContract.trim();
  // The shape `readContractScope` accepts on the way back out, so a typed id cannot
  // be one this screen then calls malformed.
  const contractIsUsable = inputNamesAContract(trimmedContract);

  const queueKey = ["escalations", "queue", showResolved, scopeId ?? "all"] as const;

  const queue = useQuery({
    queryKey: queueKey,
    // An address naming something that cannot be a contract is not a filter, and it
    // is not permission to show the tenant's whole queue either: the screen says so
    // and the read stays idle until the address is fixed.
    enabled: !scopeIsInvalid,
    queryFn: () =>
      authorized((accessToken) =>
        escalations.queue(accessToken, {
          statuses: showResolved ? undefined : WORKING_STATUSES,
          // Omitted rather than sent as `undefined`: the client builds the query
          // string, and a `null` would be neither a filter nor an absence.
          ...(scopeId === undefined ? {} : { contractId: scopeId }),
        }),
      ),
  });

  const update = useMutation({
    mutationFn: (input: { id: number; update: EscalationUpdate }) =>
      authorized((accessToken) => escalations.update(accessToken, input.id, input.update)),
    onSuccess: async () => {
      // The row may have left the working queue (resolved), so the page the
      // operator was reading is no longer the page the server would answer.
      // Refetch rather than splice — same rule as the review queue.
      await queryClient.invalidateQueries({ queryKey: ["escalations", "queue"] });
    },
    onError: async (cause) => {
      if (cause instanceof ApiError && (cause.isConflict || cause.status === 404)) {
        await queryClient.invalidateQueries({ queryKey: ["escalations", "queue"] });
      }
    },
  });

  const reply = useMutation({
    mutationFn: (input: { contractId: number; channel: ContactChannel }) =>
      authorized((accessToken) =>
        escalations.recordResponse(accessToken, input.contractId, input.channel),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["escalations", "queue"] });
    },
  });

  /**
   * A refusal in the operator's own language.
   *
   * The two 409 codes this route returns are *different facts* — the row was
   * closed under you, or you asked for a move the queue does not have — so they
   * are named separately instead of sharing one "conflict" sentence.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      // Named in `@/lib/refusals` rather than here, so the catalogue keys can be
      // checked against the messages this screen actually asks for.
      const known = refusalKey(ESCALATION_REFUSALS, cause.code);
      if (known !== undefined) return t(known);
      if (cause.isConflict) return t("escalations.conflict");
      if (cause.isForbidden) return t("escalations.forbidden");
      if (cause.isOffline) return t("escalations.offline");
    }
    return t("escalations.failed");
  }

  const deadlineFormat = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });

  const data = queue.data;
  const items = data?.items ?? [];

  /** Which row (if any) is mid-update — the mutation's own variables, not a flag. */
  const busyId = update.isPending ? update.variables?.id : undefined;

  /**
   * Claim: assign the row to the signed-in operator.
   *
   * No `status` is sent with it. The route moves an `open` row to `in_progress`
   * when an assignee is set, because a row in someone's hands that still reads
   * "open" is the queue lying about itself.
   */
  function claim(row: Escalation) {
    if (user === null) return;
    update.mutate({ id: row.id, update: { assigned_to: user.id, version: row.version } });
  }

  function resolve(row: Escalation, note: string) {
    const trimmed = note.trim();
    update.mutate({
      id: row.id,
      update: {
        status: "resolved",
        version: row.version,
        // Omitted rather than sent empty: the server only accepts a note with the
        // transition, and an empty string would be stored as a note that says nothing.
        ...(trimmed === "" ? {} : { resolution_note: trimmed }),
      },
    });
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("escalations.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("escalations.subtitle")}</p>
      </header>

      {!isAuthenticated ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("escalations.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("escalations.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <>
          {/*
            Every change to a filter is a navigation, never local state: the address
            stays the one description of what is being listed, so the back button and
            a shared link both work. `replace`, not `push`, so toggling twice does not
            leave two history entries for one question.
          */}
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!contractIsUsable) return;
              const id = contractIdFromInput(trimmedContract);
              if (id === undefined) return;
              setDraftContract(String(id));
              router.replace(withResolved(withContractScope(basePath, id), showResolved));
            }}
          >
            <div className="space-y-1">
              <FieldLabel htmlFor="escalations-contract-filter">
                {t("escalations.contractFilterLabel")}
              </FieldLabel>
              <Input
                id="escalations-contract-filter"
                className="w-40"
                inputMode="numeric"
                autoComplete="off"
                placeholder={t("escalations.contractPlaceholder")}
                value={draftContract}
                onChange={(event) => {
                  setDraftContract(event.target.value);
                }}
              />
            </div>
            <Button size="sm" type="submit" disabled={!contractIsUsable}>
              {t("escalations.contractApply")}
            </Button>
            {scopeId !== undefined || scopeIsInvalid ? (
              <Button
                size="sm"
                type="button"
                variant="outline"
                onClick={() => {
                  setDraftContract("");
                  router.replace(withResolved(basePath, showResolved));
                }}
              >
                {t("escalations.contractClear")}
              </Button>
            ) : null}
            {!contractIsUsable && draftContract.trim() !== "" ? (
              <p className="text-xs text-danger" role="alert">
                {t("escalations.contractInvalid")}
              </p>
            ) : null}
          </form>

          {/*
            A contract id the address names but this screen cannot read is shown as
            such. It is deliberately not treated as "no filter": falling back to the
            tenant-wide queue would answer a question nobody asked.
          */}
          {scopeIsInvalid && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {t("escalations.contractInvalid")}
                </p>
                <p className="text-xs text-muted-foreground">
                  {t("escalations.contractInvalidHint")}
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    setDraftContract("");
                    router.replace(basePath);
                  }}
                >
                  {t("escalations.clearFilter")}
                </Button>
              </CardContent>
            </Card>
          )}

          {data !== undefined && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
              <span>{t("escalations.countLabel", { count: data.count })}</span>
              {/*
                The server's echo, not the address read back: it is the filter the
                route actually applied, so the sentence cannot claim a narrower
                list than the one on screen.
              */}
              <span>
                {data.contract_id === null
                  ? t("escalations.scopeAllContracts")
                  : t("escalations.scopeContract", { id: data.contract_id })}
              </span>
              <span>{t("escalations.overdueLabel", { overdue: data.overdue })}</span>
              <span>{t("escalations.slaLabel", { hours: data.sla_hours })}</span>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  router.replace(
                    withResolved(withContractScope(basePath, scopeId), !showResolved),
                  );
                }}
              >
                {showResolved ? t("escalations.hideResolved") : t("escalations.showResolved")}
              </Button>
            </div>
          )}

          {!canWork && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
              {t("escalations.viewerNote")}
            </p>
          )}

          {/*
            The outcome of a recorded reply is reported here rather than on the
            card, because the reply usually closes the escalation — which removes
            that card from the working queue, taking the message with it.
          */}
          {reply.isSuccess && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs" role="status">
              {t("escalations.replyRecorded", {
                status: t(`escalations.contractStatuses.${reply.data.contract_status}`),
              })}{" "}
              {reply.data.resolved_escalation_id === null
                ? t("escalations.replyNoEscalation")
                : t("escalations.replyClosedEscalation")}
            </p>
          )}

          {reply.isError && <ErrorNote cause={reply.error} describe={describeFailure} />}

          {!scopeIsInvalid && queue.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("escalations.loading")}</p>
              </CardContent>
            </Card>
          )}

          {queue.isError && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {describeFailure(queue.error)}
                </p>
                <TraceId error={queue.error} />
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void queue.refetch();
                  }}
                >
                  {t("escalations.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {!scopeIsInvalid && !queue.isPending && !queue.isError && items.length === 0 && (
            <Card>
              <CardContent className="space-y-1">
                {/*
                  Scoped and unscoped empties mean different things: "this contract
                  has nothing waiting" is a good answer about one contract, and must
                  not read like the queue itself is clear.
                */}
                <p className="text-sm">
                  {scopeId === undefined
                    ? showResolved
                      ? t("escalations.nothingResolved")
                      : t("escalations.empty")
                    : t("escalations.emptyForContract")}
                </p>
                <p className="text-xs text-muted-foreground">{t("escalations.emptyHint")}</p>
              </CardContent>
            </Card>
          )}

          {items.map((row) => (
            <EscalationCard
              key={row.id}
              row={row}
              canWork={canWork}
              currentUserId={user?.id ?? null}
              deadline={deadlineFormat.format(new Date(row.sla_deadline))}
              resolvedAt={
                row.resolved_at === null ? null : deadlineFormat.format(new Date(row.resolved_at))
              }
              busy={busyId === row.id}
              replying={reply.isPending && reply.variables?.contractId === row.contract_id}
              // Only the row an operator acted on may show an error: a shared
              // message would blame every card on the page for one refusal. The
              // cause, not its wording, so the card keeps the refusal's trace id.
              error={
                update.isError && update.variables?.id === row.id ? (
                  <ErrorNote cause={update.error} describe={describeFailure} />
                ) : null
              }
              onClaim={() => {
                claim(row);
              }}
              onResolve={(note) => {
                resolve(row, note);
              }}
              onReply={(channel) => {
                reply.mutate({ contractId: row.contract_id, channel });
              }}
            />
          ))}
        </>
      )}

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("escalations.backToHome")}
        </Link>
      </p>
    </main>
  );
}

interface EscalationCardProps {
  row: Escalation;
  canWork: boolean;
  currentUserId: string | null;
  /** Pre-formatted in the page, so every card renders the same way. */
  deadline: string;
  /** Pre-formatted too; `null` when the row is still open. */
  resolvedAt: string | null;
  busy: boolean;
  replying: boolean;
  /** The refusal, already rendered from its cause so its trace id survives. */
  error: ReactElement | null;
  onClaim: () => void;
  onResolve: (note: string) => void;
  onReply: (channel: ContactChannel) => void;
}

/**
 * One queue item.
 *
 * The card states what the operator needs to decide *now* — how late it is, who
 * holds it, and how long the tenant's SLA gives — and keeps the two closing
 * actions visibly separate, because they mean different things about the customer.
 */
function EscalationCard({
  row,
  canWork,
  currentUserId,
  deadline,
  resolvedAt,
  busy,
  replying,
  error,
  onClaim,
  onResolve,
  onReply,
}: EscalationCardProps) {
  const t = useTranslations();
  const [resolving, setResolving] = useState(false);
  const [note, setNote] = useState("");

  const resolved = row.status === "resolved";
  const mine = row.assigned_to !== null && row.assigned_to === currentUserId;

  return (
    <Card aria-busy={busy || replying}>
      <CardHeader className="flex flex-wrap items-baseline justify-between gap-2">
        <CardTitle>
          {t("escalations.contractLabel", { id: row.contract_id })} ·{" "}
          {t(`escalations.statuses.${row.status}`)}
        </CardTitle>
        {row.overdue && (
          <span className="rounded-full bg-danger-muted px-2 py-0.5 text-xs font-medium text-danger">
            {t("escalations.overdueBadge")}
          </span>
        )}
      </CardHeader>

      <CardContent className="space-y-3">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">{t("escalations.deadlineLabel")}</dt>
          <dd>{deadline}</dd>

          <dt className="text-muted-foreground">{t("escalations.assignedLabel")}</dt>
          <dd>
            {row.assigned_to === null
              ? t("escalations.unassigned")
              : mine
                ? t("escalations.you")
                : t("escalations.claimedByOther", { id: row.assigned_to.slice(0, 8) })}
          </dd>

          {resolvedAt !== null && (
            <>
              <dt className="text-muted-foreground">{t("escalations.resolvedAtLabel")}</dt>
              <dd>{resolvedAt}</dd>
            </>
          )}

          {row.resolution_note !== null && (
            <>
              <dt className="text-muted-foreground">{t("escalations.noteLabel")}</dt>
              <dd className="break-words">{row.resolution_note}</dd>
            </>
          )}
        </dl>

        {canWork && !resolved && (
          <div className="flex flex-wrap items-center gap-2">
            {!mine && (
              <Button size="sm" variant="secondary" disabled={busy} onClick={onClaim}>
                {busy ? t("escalations.claiming") : t("escalations.claim")}
              </Button>
            )}
            {!resolving && (
              <Button
                size="sm"
                disabled={busy}
                onClick={() => {
                  setNote("");
                  setResolving(true);
                }}
              >
                {t("escalations.resolve")}
              </Button>
            )}
          </div>
        )}

        {canWork && !resolved && resolving && (
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`escalation-note-${row.id}`}>
              {t("escalations.resolveLabel")}
            </FieldLabel>
            <Input
              id={`escalation-note-${row.id}`}
              name={`escalation-note-${row.id}`}
              value={note}
              maxLength={500}
              placeholder={t("escalations.resolvePlaceholder")}
              disabled={busy}
              onChange={(event) => {
                setNote(event.target.value);
              }}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={busy} onClick={() => onResolve(note)}>
                {busy ? t("escalations.resolving") : t("escalations.resolveSubmit")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setResolving(false);
                }}
              >
                {t("escalations.cancel")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("escalations.resolveHint")}</p>
          </div>
        )}

        {canWork && !resolved && (
          <div className="space-y-1.5">
            <p className="text-xs font-medium">{t("escalations.replyHeading")}</p>
            <div className="flex flex-wrap items-center gap-2">
              {REPLY_CHANNELS.map((channel) => (
                <Button
                  key={channel}
                  size="sm"
                  variant="outline"
                  disabled={busy || replying}
                  onClick={() => {
                    onReply(channel);
                  }}
                >
                  {t(`escalations.${CHANNEL_KEYS[channel]}`)}
                </Button>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">{t("escalations.replyHint")}</p>
          </div>
        )}

        {error}
      </CardContent>
    </Card>
  );
}
