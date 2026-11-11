"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FieldLabel, Input } from "@/components/ui/input";
import { TraceId } from "@/components/ui/trace-id";
import { agent, ApiError } from "@/lib/api";
import type { AgentDecision, ContactChannel } from "@/lib/api";
import {
  contractIdFromInput,
  inputNamesAContract,
  readContractScope,
  withContractScope,
} from "@/lib/contract-scope";
import { AGENT_REFUSALS, refusalKey } from "@/lib/refusals";
import { authorized } from "@/lib/session";

/** The channels an override may ask for — one per `app.db.enums.Channel`. */
const CHANNELS: ContactChannel[] = ["email", "sms", "voice"];

/**
 * Channel → message key.
 *
 * A record rather than string surgery on the wire value, for the reason D10-23
 * gives: `t("…channel" + value)` would resolve to a missing key the day `Channel`
 * gains a member, and the compiler cannot check a concatenated key.
 */
const CHANNEL_KEYS = {
  email: "channelEmail",
  sms: "channelSms",
  voice: "channelVoice",
} as const satisfies Record<ContactChannel, string>;

/**
 * The channels a refusal said *would* have been allowed.
 *
 * `CHANNEL_NOT_PERMITTED` carries them under `details.permitted`, and this is the
 * only place the client learns which channels consent allows — deliberately. The
 * alternative, re-deriving the permitted set here from consent flags, would put a
 * second copy of the contact policy in the browser, where it would drift from the
 * one that actually refuses. Narrowed from `unknown` and filtered, so a shape the
 * server never sends yields an empty list rather than a crash.
 */
function permittedChannelsFrom(cause: ApiError): ContactChannel[] {
  const details: unknown = cause.details;
  if (typeof details !== "object" || details === null) return [];
  const raw = (details as { permitted?: unknown }).permitted;
  if (!Array.isArray(raw)) return [];

  const found: ContactChannel[] = [];
  for (const value of raw) {
    const match = CHANNELS.find((channel) => channel === value);
    if (match !== undefined) found.push(match);
  }
  return found;
}

/**
 * The channel decisions the agent made, and the two ways a person can disagree
 * with one.
 *
 * Before this screen the `agent` route family had only ever been reached by
 * `TestClient` (carried GATE 2 item 5). It is also the family whose output the
 * notification slice consumes: `POST /notifications` schedules from a *stored*
 * decision, so a recommendation nobody can inspect is a recommendation that
 * decides by default.
 *
 * Three distinctions the UI keeps:
 *
 * * **A re-run appends; a correction edits.** "The policy changed its mind" is a
 *   new fact and gets a new row; "a person disagrees with *this* row" is a fact
 *   about that row, so the row is edited in place with `original_channel`
 *   preserved. Deleting either way would destroy the audit trail that F9 exists
 *   to leave behind.
 * * **Corrections only appear on the current recommendation.** The override and
 *   flag routes resolve "the latest decision for this contract" themselves, so
 *   offering them on a superseded row would send a version the server has already
 *   moved past — every click would be a `409`.
 * * **The client never computes the permitted channels.** It offers all three and
 *   lets the policy answer, then reports what the policy said.
 */
export default function AgentPage() {
  return (
    // `useSearchParams` suspends while the address is resolved, and Next refuses to
    // prerender a route that reads it without a boundary. The fallback is `null`
    // rather than a translated sentence because this shell is only ever reached
    // before hydration — the screen underneath renders its own loading card, with
    // the catalogue's own words, as soon as it runs.
    <Suspense fallback={null}>
      <AgentScreen />
    </Suspense>
  );
}

function AgentScreen() {
  const t = useTranslations();
  const locale = useLocale();
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();
  const { user, isAuthenticated } = useAuth();

  const canDecide = user !== null && user.role !== "viewer";

  /**
   * The contract this address names, if it names one.
   *
   * The scope lives in the address rather than in `useState`, so that a link from a
   * contract's own page (`D10-36`) still filters after a reload, a bookmark or the
   * back button. `readContractScope` distinguishes "no contract named" from "named
   * something that cannot be a contract", which is the difference between showing
   * the tenant-wide history and refusing to.
   */
  const scope = readContractScope(searchParams);
  const scopeId = scope.kind === "contract" ? scope.id : undefined;
  const scopeIsInvalid = scope.kind === "invalid";
  const scopeInAddress = scopeId === undefined ? "" : String(scopeId);

  const [draftFilter, setDraftFilter] = useState(scopeInAddress);

  /**
   * The input follows the address.
   *
   * A link, the back button and a typed address all change `scopeId` without
   * remounting this component, and an input still holding the previous id would
   * contradict the list underneath it.
   */
  useEffect(() => {
    setDraftFilter(scopeInAddress);
  }, [scopeInAddress]);

  const decisionsKey = ["agent", "decisions", scopeId ?? "recent"] as const;

  const decisions = useQuery({
    queryKey: decisionsKey,
    // An address that names nothing is not a filter, and it is also not permission
    // to show every decision in the tenant: the screen says so and this query stays
    // idle until the address is fixed.
    enabled: !scopeIsInvalid,
    queryFn: () => authorized((accessToken) => agent.decisions(accessToken, scopeId)),
  });

  async function refresh() {
    // One key prefix covers both the recent list and every filtered view: after an
    // append or an edit, whichever scope the operator is looking at is stale.
    await queryClient.invalidateQueries({ queryKey: ["agent", "decisions"] });
  }

  const rerun = useMutation({
    mutationFn: (input: { contractId: number }) =>
      authorized((accessToken) => agent.decide(accessToken, input.contractId)),
    onSuccess: refresh,
    onError: async (cause) => {
      // `PIPELINE_BLOCKED` means the contract moved out of a decidable state under
      // the operator; the list they are reading no longer describes the server.
      if (cause instanceof ApiError && (cause.isConflict || cause.status === 404)) await refresh();
    },
  });

  const override = useMutation({
    mutationFn: (input: { contractId: number; channel: ContactChannel; version: number; reason: string }) =>
      authorized((accessToken) =>
        agent.override(accessToken, input.contractId, {
          channel: input.channel,
          version: input.version,
          // Omitted rather than sent empty: a reason that says nothing is worse
          // than no reason, and the request model treats them differently.
          ...(input.reason === "" ? {} : { reason: input.reason }),
        }),
      ),
    onSuccess: refresh,
    onError: async (cause) => {
      // Refetch, never splice: a lost `version` race means the row on screen is
      // not the row the server holds, so the correction must be rebuilt from the
      // server's answer (D10-22 and D10-23 make the same call).
      if (cause instanceof ApiError && (cause.isConflict || cause.status === 404)) await refresh();
    },
  });

  const flag = useMutation({
    mutationFn: (input: { contractId: number; version: number; reason: string }) =>
      authorized((accessToken) =>
        agent.flagReview(accessToken, input.contractId, {
          version: input.version,
          ...(input.reason === "" ? {} : { reason: input.reason }),
        }),
      ),
    onSuccess: refresh,
    onError: async (cause) => {
      if (cause instanceof ApiError && (cause.isConflict || cause.status === 404)) await refresh();
    },
  });

  /** A refusal in the operator's own language. */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.code === "CHANNEL_NOT_PERMITTED") {
        const permitted = permittedChannelsFrom(cause);
        const alone = t(AGENT_REFUSALS.CHANNEL_NOT_PERMITTED);
        if (permitted.length === 0) return alone;
        return `${alone} ${t("agent.permittedChannels", {
          channels: permitted.map((channel) => t(`agent.${CHANNEL_KEYS[channel]}`)).join(", "),
        })}`;
      }
      // The whole code→sentence table lives in `@/lib/refusals`, so the names it
      // uses can be checked against the catalogues (`refusals.test.ts`). A code
      // the table does not know still walks the ladder below and lands on a
      // generic sentence, so a new server refusal degrades to vague, not blank.
      const known = refusalKey(AGENT_REFUSALS, cause.code);
      if (known !== undefined) return t(known);
      if (cause.isConflict) return t("agent.conflict");
      if (cause.isForbidden) return t("agent.forbidden");
      if (cause.isOffline) return t("agent.offline");
    }
    return t("agent.failed");
  }

  const decidedAtFormat = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium",
    timeStyle: "short",
  });
  const percent = new Intl.NumberFormat(locale, { style: "percent", maximumFractionDigits: 0 });

  const data = decisions.data;
  const items = useMemo(() => data?.items ?? [], [data]);

  /**
   * Which rows are the *current* recommendation for their contract.
   *
   * The list is the newest N decisions in the tenant, ordered newest first, so the
   * first row seen for a contract is that contract's latest: if an older decision
   * for X is on the page, every newer one is on the page too, above it. Deriving
   * this locally is what lets the page hide corrections that the server would
   * refuse — `latest_for_contract` is the server's own rule, and this is the same
   * rule restated over a page it already ordered.
   */
  const latestIds = useMemo(() => {
    const ids = new Set<number>();
    const seen = new Set<number>();
    for (const row of items) {
      if (seen.has(row.contract_id)) continue;
      seen.add(row.contract_id);
      ids.add(row.id);
    }
    return ids;
  }, [items]);

  const trimmedFilter = draftFilter.trim();
  // The same shape `readContractScope` accepts on the way back out, so a typed id
  // cannot be one the address then pronounces malformed.
  const filterIsValid = inputNamesAContract(trimmedFilter);

  /**
   * Put the scope in the address, which is the only place it lives.
   *
   * `replace`, not `push`: a half-typed id followed by a few corrections is not
   * several pages an operator wants to step back through. A link arriving from
   * another screen still pushes — that is the difference between coming here and
   * editing where you already are.
   */
  function scopeToAddress(id: number | undefined) {
    router.replace(withContractScope(`/${locale}/agent`, id));
  }

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("agent.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("agent.subtitle")}</p>
      </header>

      {!isAuthenticated ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("agent.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("agent.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!filterIsValid) return;
              scopeToAddress(contractIdFromInput(trimmedFilter));
            }}
          >
            <div className="space-y-1.5">
              <FieldLabel htmlFor="agent-contract-filter">{t("agent.filterLabel")}</FieldLabel>
              <Input
                id="agent-contract-filter"
                name="agent-contract-filter"
                className="w-40"
                inputMode="numeric"
                value={draftFilter}
                placeholder={t("agent.filterPlaceholder")}
                onChange={(event) => {
                  setDraftFilter(event.target.value);
                }}
              />
            </div>
            <Button size="sm" type="submit" disabled={!filterIsValid}>
              {t("agent.filterApply")}
            </Button>
            {(scopeId !== undefined || scopeIsInvalid) && (
              <Button size="sm" type="button" variant="ghost" onClick={() => scopeToAddress(undefined)}>
                {t("agent.filterClear")}
              </Button>
            )}
            {!filterIsValid && (
              <p className="text-xs text-danger" role="alert">
                {t("agent.filterInvalid")}
              </p>
            )}
          </form>

          {scopeIsInvalid && (
            // A hand-edited address, or a stale bookmark of a route that never had
            // this parameter. Saying "no such contract" and offering the way back is
            // the only honest screen: falling through to the tenant-wide history
            // would show every contract's decisions under a filter that never ran.
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {t("agent.filterInvalid")}
                </p>
                <p className="text-xs text-muted-foreground">{t("agent.filterInvalidHint")}</p>
                <Button size="sm" variant="outline" onClick={() => scopeToAddress(undefined)}>
                  {t("agent.filterClear")}
                </Button>
              </CardContent>
            </Card>
          )}

          {data !== undefined && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>{t("agent.countLabel", { count: data.count })}</span>
              <span>
                {data.contract_id === null
                  ? t("agent.scopeRecent")
                  : t("agent.scopeContract", { id: data.contract_id })}
              </span>
            </div>
          )}

          {!canDecide && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
              {t("agent.viewerNote")}
            </p>
          )}

          {rerun.isSuccess && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs" role="status">
              {t("agent.rerunDone")}
            </p>
          )}

          {!scopeIsInvalid && decisions.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("agent.loading")}</p>
              </CardContent>
            </Card>
          )}

          {decisions.isError && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {describeFailure(decisions.error)}
                </p>
                <TraceId error={decisions.error} />
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void decisions.refetch();
                  }}
                >
                  {t("agent.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {!decisions.isPending && !decisions.isError && items.length === 0 && (
            <Card>
              <CardContent className="space-y-1">
                <p className="text-sm">
                  {scopeId === undefined ? t("agent.empty") : t("agent.emptyForContract")}
                </p>
                <p className="text-xs text-muted-foreground">{t("agent.emptyHint")}</p>
              </CardContent>
            </Card>
          )}

          {items.map((row) => {
            // Corrections and re-runs are only offered on a contract's newest row,
            // so only that row may report their progress or their refusal. Matching
            // on `contract_id` alone would light up every historical row of the same
            // contract — a shared message blaming rows nobody acted on.
            const isLatest = latestIds.has(row.id);
            const actedOn = isLatest && rerun.variables?.contractId === row.contract_id;
            const correctingHere =
              isLatest &&
              ((override.isPending && override.variables?.contractId === row.contract_id) ||
                (flag.isPending && flag.variables?.contractId === row.contract_id));

            return (
              <DecisionCard
                key={row.id}
                row={row}
                canDecide={canDecide}
                isLatest={isLatest}
                decidedAt={decidedAtFormat.format(new Date(row.created_at))}
                confidence={percent.format(row.confidence)}
                busy={actedOn && rerun.isPending}
                correcting={correctingHere}
                error={
                  actedOn && rerun.isError
                    ? describeFailure(rerun.error)
                    : isLatest && override.isError && override.variables?.contractId === row.contract_id
                      ? describeFailure(override.error)
                      : isLatest && flag.isError && flag.variables?.contractId === row.contract_id
                        ? describeFailure(flag.error)
                        : null
                }
                onRerun={() => {
                  rerun.mutate({ contractId: row.contract_id });
                }}
                onOverride={(channel, reason) => {
                  override.mutate({
                    contractId: row.contract_id,
                    version: row.version,
                    channel,
                    reason,
                  });
                }}
                onFlag={(reason) => {
                  flag.mutate({ contractId: row.contract_id, version: row.version, reason });
                }}
              />
            );
          })}
        </>
      )}

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("agent.backToHome")}
        </Link>
      </p>
    </main>
  );
}

interface DecisionCardProps {
  row: AgentDecision;
  canDecide: boolean;
  /** True when this row is the contract's current recommendation. */
  isLatest: boolean;
  /** Pre-formatted in the page, so every card renders the same way. */
  decidedAt: string;
  /** Also pre-formatted: percentage signs and digits are locale business. */
  confidence: string;
  busy: boolean;
  correcting: boolean;
  error: string | null;
  onRerun: () => void;
  onOverride: (channel: ContactChannel, reason: string) => void;
  onFlag: (reason: string) => void;
}

/**
 * One stored recommendation.
 *
 * It states what a person needs in order to agree or disagree — which channel, how
 * confidently, on what grounds, under which policy version — and keeps the
 * appending action visually apart from the correcting ones, because only one of
 * them adds history rather than rewriting it.
 */
function DecisionCard({
  row,
  canDecide,
  isLatest,
  decidedAt,
  confidence,
  busy,
  correcting,
  error,
  onRerun,
  onOverride,
  onFlag,
}: DecisionCardProps) {
  const t = useTranslations();
  const [overriding, setOverriding] = useState(false);
  const [flagging, setFlagging] = useState(false);
  const [reason, setReason] = useState("");

  const overridden = row.original_channel !== null;

  return (
    <Card aria-busy={busy || correcting}>
      <CardHeader className="flex flex-wrap items-baseline justify-between gap-2">
        <CardTitle>
          {t("agent.contractLabel", { id: row.contract_id })} ·{" "}
          {t(`agent.${CHANNEL_KEYS[row.channel]}`)}
        </CardTitle>
        {row.needs_review && (
          <span className="rounded-full bg-danger-muted px-2 py-0.5 text-xs font-medium text-danger">
            {t("agent.flaggedBadge")}
          </span>
        )}
      </CardHeader>

      <CardContent className="space-y-3">
        {overridden && row.original_channel !== null && (
          <p className="rounded-lg bg-muted px-3 py-2 text-xs">
            {t("agent.overriddenBadge", {
              from: t(`agent.${CHANNEL_KEYS[row.original_channel]}`),
              to: t(`agent.${CHANNEL_KEYS[row.channel]}`),
            })}
          </p>
        )}

        <p className="text-sm text-muted-foreground">{row.reasoning}</p>

        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">{t("agent.confidenceLabel")}</dt>
          <dd>{confidence}</dd>

          <dt className="text-muted-foreground">{t("agent.decidedAtLabel")}</dt>
          <dd>{decidedAt}</dd>

          <dt className="text-muted-foreground">{t("agent.policyLabel")}</dt>
          <dd>{row.prompt_version}</dd>

          <dt className="text-muted-foreground">{t("agent.modelLabel")}</dt>
          <dd>{row.llm_model}</dd>

          {row.rerun_count > 0 && (
            <>
              <dt className="text-muted-foreground">{t("agent.rerunCountLabel")}</dt>
              <dd>{row.rerun_count}</dd>
            </>
          )}

          {row.overridden_by !== null && (
            <>
              <dt className="text-muted-foreground">{t("agent.overriddenByLabel")}</dt>
              <dd>{row.overridden_by.slice(0, 8)}</dd>
            </>
          )}

          <dt className="text-muted-foreground">{t("agent.decisionLabel")}</dt>
          <dd>{row.id}</dd>
        </dl>

        {canDecide && isLatest && (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="secondary" disabled={busy || correcting} onClick={onRerun}>
              {busy ? t("agent.rerunning") : t("agent.rerun")}
            </Button>
            {!overriding && (
              <Button
                size="sm"
                disabled={busy || correcting}
                onClick={() => {
                  setReason("");
                  setFlagging(false);
                  setOverriding(true);
                }}
              >
                {t("agent.override")}
              </Button>
            )}
            {!flagging && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy || correcting}
                onClick={() => {
                  setReason("");
                  setOverriding(false);
                  setFlagging(true);
                }}
              >
                {t("agent.flag")}
              </Button>
            )}
          </div>
        )}

        {canDecide && isLatest && overriding && (
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`agent-override-reason-${row.id}`}>
              {t("agent.reasonLabel")}
            </FieldLabel>
            <Input
              id={`agent-override-reason-${row.id}`}
              name={`agent-override-reason-${row.id}`}
              value={reason}
              maxLength={500}
              placeholder={t("agent.reasonPlaceholder")}
              disabled={correcting}
              onChange={(event) => {
                setReason(event.target.value);
              }}
            />
            <p className="text-xs font-medium">{t("agent.overrideHeading")}</p>
            <div className="flex flex-wrap items-center gap-2">
              {CHANNELS.map((channel) => (
                <Button
                  key={channel}
                  size="sm"
                  variant="outline"
                  disabled={correcting}
                  onClick={() => {
                    onOverride(channel, reason.trim());
                  }}
                >
                  {t(`agent.${CHANNEL_KEYS[channel]}`)}
                </Button>
              ))}
              <Button
                size="sm"
                variant="ghost"
                disabled={correcting}
                onClick={() => {
                  setOverriding(false);
                }}
              >
                {t("agent.cancel")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("agent.overrideHint")}</p>
          </div>
        )}

        {canDecide && isLatest && flagging && (
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`agent-flag-reason-${row.id}`}>{t("agent.reasonLabel")}</FieldLabel>
            <Input
              id={`agent-flag-reason-${row.id}`}
              name={`agent-flag-reason-${row.id}`}
              value={reason}
              maxLength={500}
              placeholder={t("agent.reasonPlaceholder")}
              disabled={correcting}
              onChange={(event) => {
                setReason(event.target.value);
              }}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={correcting} onClick={() => onFlag(reason.trim())}>
                {correcting ? t("agent.flagging") : t("agent.flagSubmit")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={correcting}
                onClick={() => {
                  setFlagging(false);
                }}
              >
                {t("agent.cancel")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("agent.flagHint")}</p>
          </div>
        )}

        {canDecide && !isLatest && (
          <p className="text-xs text-muted-foreground">{t("agent.olderRowNote")}</p>
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
