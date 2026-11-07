"use client";

import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import Link from "next/link";
import { useState } from "react";

import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { FieldLabel, Input } from "@/components/ui/input";
import { ApiError, review } from "@/lib/api";
import type { ReviewDecision, ReviewItem } from "@/lib/api";
import { authorized } from "@/lib/session";

/** The queue's query key — one place, so a decision's invalidation cannot miss it. */
const QUEUE_KEY = ["review", "queue"] as const;

/**
 * The human review queue (F8), and the first screen that can *complete* work.
 *
 * Sign-in proved a session exists; this screen is the point at which the app can
 * do the thing the non-negotiables demand — no field below the confidence
 * threshold reaches a customer until a person decides about it. Until now that
 * gate was reachable only through `TestClient`, so the pipeline was operable in
 * tests and not in the product.
 *
 * Three rules from the API shape the screen, and each is deliberate rather than
 * incidental:
 *
 * * The queue carries no values. `review.reveal` is a separate `POST` that writes
 *   an audit row, so "show me the value" is an action with a cost, not a re-render.
 * * A decision carries the `version` the reviewer saw. A stale one is a `409`, and
 *   the screen reloads instead of retrying — two reviewers must not silently
 *   overwrite each other.
 * * `viewer` may read this queue and may not decide. The UI hides the buttons, but
 *   the real boundary is the route's 403, which is why the refusal is translated
 *   rather than assumed impossible.
 */
export default function ReviewQueuePage() {
  const t = useTranslations();
  const locale = useLocale();
  const queryClient = useQueryClient();
  const { user, isAuthenticated } = useAuth();

  const canDecide = user !== null && user.role !== "viewer";

  /** Values revealed in this visit, by field id. Never persisted, never re-fetched. */
  const [revealed, setRevealed] = useState<Record<number, string>>({});

  const queue = useInfiniteQuery({
    queryKey: QUEUE_KEY,
    queryFn: ({ pageParam }) =>
      authorized((accessToken) => review.queue(accessToken, pageParam)),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const decide = useMutation({
    mutationFn: (input: { fieldId: number; decision: ReviewDecision }) =>
      authorized((accessToken) => review.decide(accessToken, input.fieldId, input.decision)),
    onSuccess: async () => {
      // The decided row leaves the queue, so the page the reviewer was reading is
      // no longer the page the server would answer. Refetch rather than splice: a
      // spliced list would carry the wrong `next_cursor` for the rest of the visit.
      await queryClient.invalidateQueries({ queryKey: QUEUE_KEY });
    },
    onError: async (cause) => {
      // A conflict or a 404 means this list is stale in a way only a reload fixes.
      if (cause instanceof ApiError && (cause.isConflict || cause.status === 404)) {
        await queryClient.invalidateQueries({ queryKey: QUEUE_KEY });
      }
    },
  });

  const reveal = useMutation({
    mutationFn: (fieldId: number) =>
      authorized((accessToken) => review.reveal(accessToken, fieldId)),
    onSuccess: (value) => {
      setRevealed((current) => ({ ...current, [value.field_id]: value.value }));
    },
  });

  /**
   * A refusal in the reviewer's own language.
   *
   * The server's wording is English and, on a conflict, structural (the winning
   * row is nested in `detail`), so the cases the UI can name are translated here
   * and everything else falls back to one generic sentence.
   */
  function describeFailure(cause: unknown): string {
    if (cause instanceof ApiError) {
      if (cause.isConflict) return t("review.conflict");
      if (cause.isForbidden) return t("review.forbidden");
      if (cause.isOffline) return t("review.offline");
      if (cause.status === 422) return t("review.invalidValue");
    }
    return t("review.failed");
  }

  const pages = queue.data?.pages ?? [];
  const items = pages.flatMap((page) => page.items);
  const first = pages[0];
  const decidingId = decide.isPending ? decide.variables.fieldId : null;
  const revealingId = reveal.isPending ? reveal.variables : null;

  return (
    <main className="mx-auto flex min-h-screen max-w-3xl flex-col gap-6 px-6 py-12">
      <header className="space-y-2">
        <h1 className="text-2xl font-semibold">{t("review.title")}</h1>
        <p className="text-sm text-muted-foreground">{t("review.subtitle")}</p>
      </header>

      {!isAuthenticated ? (
        <Card>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{t("review.signInRequired")}</p>
            <Link href={`/${locale}/login`}>
              <Button size="sm">{t("review.signIn")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <>
          {first !== undefined && (
            <p className="flex flex-wrap gap-x-4 text-xs text-muted-foreground">
              <span>{t("review.countLabel", { count: items.length })}</span>
              <span>{t("review.oldestLabel", { minutes: first.oldest_minutes })}</span>
              <span>{t("review.thresholdLabel", { threshold: first.threshold.toFixed(2) })}</span>
            </p>
          )}

          {!canDecide && (
            <p className="rounded-lg bg-muted px-3 py-2 text-xs text-muted-foreground">
              {t("review.viewerNote")}
            </p>
          )}

          {queue.isPending && (
            <Card>
              <CardContent>
                <p className="text-sm text-muted-foreground">{t("review.loading")}</p>
              </CardContent>
            </Card>
          )}

          {queue.isError && (
            <Card>
              <CardContent className="space-y-3">
                <p className="text-sm text-danger" role="alert">
                  {describeFailure(queue.error)}
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => {
                    void queue.refetch();
                  }}
                >
                  {t("review.retry")}
                </Button>
              </CardContent>
            </Card>
          )}

          {!queue.isPending && !queue.isError && items.length === 0 && (
            <Card>
              <CardContent className="space-y-1">
                <p className="text-sm">{t("review.empty")}</p>
                <p className="text-xs text-muted-foreground">{t("review.emptyHint")}</p>
              </CardContent>
            </Card>
          )}

          {items.map((item) => (
            <ReviewCard
              key={item.field_id}
              item={item}
              canDecide={canDecide}
              revealedValue={revealed[item.field_id]}
              deciding={decidingId === item.field_id}
              revealing={revealingId === item.field_id}
              // Only the row the reviewer acted on may show an error: a shared
              // message would blame every card on the page for one refusal.
              error={decide.variables?.fieldId === item.field_id ? describeFailure(decide.error) : null}
              revealError={reveal.variables === item.field_id ? describeFailure(reveal.error) : null}
              decidedBlocking={
                decide.data?.item.field_id === item.field_id ? decide.data.blocking : null
              }
              onDecide={(decision) => {
                decide.mutate({ fieldId: item.field_id, decision });
              }}
              onReveal={() => {
                reveal.mutate(item.field_id);
              }}
            />
          ))}

          {queue.hasNextPage && (
            <div className="flex justify-center">
              <Button
                size="sm"
                variant="outline"
                disabled={queue.isFetchingNextPage}
                onClick={() => {
                  void queue.fetchNextPage();
                }}
              >
                {queue.isFetchingNextPage ? t("review.loadingMore") : t("review.loadMore")}
              </Button>
            </div>
          )}
        </>
      )}

      <p className="text-sm">
        <Link className="text-primary underline-offset-4 hover:underline" href={`/${locale}`}>
          {t("review.backToHome")}
        </Link>
      </p>
    </main>
  );
}

interface ReviewCardProps {
  item: ReviewItem;
  canDecide: boolean;
  revealedValue: string | undefined;
  deciding: boolean;
  revealing: boolean;
  error: string | null;
  revealError: string | null;
  /** How many fields still block this contract, once a decision landed. */
  decidedBlocking: number | null;
  onDecide: (decision: ReviewDecision) => void;
  onReveal: () => void;
}

/**
 * One field awaiting a person.
 *
 * The card states *why* it is here — a confidence below a named threshold, and how
 * long it has waited — because a queue that only says "low confidence" trains a
 * reviewer to accept without looking, which is the opposite of the gate's purpose.
 */
function ReviewCard({
  item,
  canDecide,
  revealedValue,
  deciding,
  revealing,
  error,
  revealError,
  decidedBlocking,
  onDecide,
  onReveal,
}: ReviewCardProps) {
  const t = useTranslations();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const busy = deciding || revealing;

  function submitEdit() {
    const value = draft.trim();
    if (value === "") return;
    onDecide({ action: "edit", version: item.version, value });
  }

  return (
    <Card aria-busy={busy}>
      <CardHeader className="flex flex-wrap items-baseline justify-between gap-2">
        <CardTitle>
          {t(`review.fields.${item.field_name}`)} · {t(`review.statuses.${item.status}`)}
        </CardTitle>
        <span className="text-xs text-muted-foreground">
          {t("review.contractLabel", { ref: item.customer_ref, id: item.contract_id })}
        </span>
      </CardHeader>

      <CardContent className="space-y-3">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted-foreground">{t("review.statusLabel")}</dt>
          <dd>{t(`review.contractStatuses.${item.contract_status}`)}</dd>

          <dt className="text-muted-foreground">{t("review.expiryLabel")}</dt>
          <dd>
            {item.days_to_expiry === null
              ? t("review.noEndDate")
              : item.days_to_expiry >= 0
                ? t("review.expiresInDays", { days: item.days_to_expiry })
                : t("review.expiredDaysAgo", { days: Math.abs(item.days_to_expiry) })}
          </dd>

          <dt className="text-muted-foreground">{t("review.confidenceLabel")}</dt>
          <dd>
            {t("review.confidenceValue", {
              confidence: item.confidence.toFixed(2),
              threshold: item.threshold.toFixed(2),
            })}
          </dd>

          <dt className="text-muted-foreground">{t("review.ageLabel")}</dt>
          <dd>{t("review.ageValue", { minutes: item.age_minutes })}</dd>

          {item.source_page !== null && (
            <>
              <dt className="text-muted-foreground">{t("review.sourceLabel")}</dt>
              <dd>
                {item.source_line === null
                  ? t("review.sourcePageOnly", { page: item.source_page })
                  : t("review.sourcePageLine", {
                      page: item.source_page,
                      line: item.source_line,
                    })}
              </dd>
            </>
          )}
        </dl>

        {item.blocks_pipeline && (
          <p className="text-xs font-medium text-danger">{t("review.blockingLabel")}</p>
        )}

        {revealedValue === undefined ? (
          <Button
            size="sm"
            variant="outline"
            disabled={busy || !canDecide}
            onClick={onReveal}
          >
            {revealing ? t("review.revealing") : t("review.reveal")}
          </Button>
        ) : (
          <div className="space-y-1">
            <p className="font-mono text-sm break-all">{revealedValue}</p>
            <p className="text-xs text-muted-foreground">{t("review.revealedNote")}</p>
          </div>
        )}

        {revealError !== null && (
          <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
            {revealError}
          </p>
        )}

        {canDecide && !editing && (
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              disabled={busy}
              onClick={() => {
                onDecide({ action: "accept", version: item.version });
              }}
            >
              {t("review.accept")}
            </Button>
            <Button
              size="sm"
              variant="danger"
              disabled={busy}
              onClick={() => {
                onDecide({ action: "reject", version: item.version });
              }}
            >
              {t("review.reject")}
            </Button>
            <Button
              size="sm"
              variant="secondary"
              disabled={busy}
              onClick={() => {
                onDecide({ action: "mark_unreadable", version: item.version });
              }}
            >
              {t("review.unreadable")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setDraft("");
                setEditing(true);
              }}
            >
              {t("review.edit")}
            </Button>
          </div>
        )}

        {canDecide && editing && (
          <div className="space-y-1.5">
            <FieldLabel htmlFor={`review-value-${item.field_id}`}>{t("review.editLabel")}</FieldLabel>
            <Input
              id={`review-value-${item.field_id}`}
              name={`review-value-${item.field_id}`}
              value={draft}
              maxLength={200}
              disabled={busy}
              onChange={(event) => {
                setDraft(event.target.value);
              }}
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={busy || draft.trim() === ""} onClick={submitEdit}>
                {deciding ? t("review.saving") : t("review.editSubmit")}
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                }}
              >
                {t("review.cancel")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{t("review.editHint")}</p>
          </div>
        )}

        {deciding && !editing && (
          <p className="text-xs text-muted-foreground">{t("review.saving")}</p>
        )}

        {error !== null && (
          <p className="rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger" role="alert">
            {error}
          </p>
        )}

        {decidedBlocking !== null && (
          <p className="text-xs text-muted-foreground">
            {t("review.decidedBlocking", { blocking: decidedBlocking })}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
