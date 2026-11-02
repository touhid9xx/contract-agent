"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Pencil, RefreshCw, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ConfidenceBar } from "@/components/confidence-bar";
import { MockedBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui/states";
import { api, ApiError } from "@/lib/api";
import type { ReviewItem } from "@/lib/types";
import { useToast } from "@/lib/toast";
import { formatDate, shortId } from "@/lib/utils";

type Action = "accept" | "reject" | "mark_unreadable";

export default function ReviewQueuePage() {
  const queryClient = useQueryClient();
  const pushToast = useToast((state) => state.push);

  const query = useQuery({ queryKey: ["review"], queryFn: api.reviewQueue });

  const mutation = useMutation({
    mutationFn: ({ id, action, value, version }: { id: string; action: "accept" | "reject" | "mark_unreadable" | "edit"; value?: string; version: number }) =>
      api.reviewField(id, { action, value, version }),
    onSuccess: (_data, variables) => {
      pushToast(`Field ${variables.action.replace("_", " ")} — pipeline re-evaluated.`, "success");
      void queryClient.invalidateQueries({ queryKey: ["review"] });
      void queryClient.invalidateQueries({ queryKey: ["contracts"] });
    },
    onError: (error: Error) => {
      if (error instanceof ApiError && error.status === 409) {
        pushToast("409 · Someone else already reviewed this field. The latest value has been reloaded.", "error");
        void queryClient.invalidateQueries({ queryKey: ["review"] });
        return;
      }
      pushToast(error.message, "error");
    },
  });

  const items = query.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Review queue"
        description={
          query.data
            ? `${query.data.count} field${query.data.count === 1 ? "" : "s"} awaiting review · oldest ${query.data.oldest_minutes} min`
            : "Low-confidence fields that block the notification pipeline."
        }
        actions={
          <div className="flex items-center gap-2">
            <MockedBadge />
            <Button size="sm" variant="outline" onClick={() => void query.refetch()} disabled={query.isFetching}>
              <RefreshCw className={query.isFetching ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"} aria-hidden />
              Refresh
            </Button>
          </div>
        }
      />

      {query.isError ? (
        <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <Card className="p-4">
          <SkeletonRows rows={4} />
        </Card>
      ) : items.length === 0 ? (
        <EmptyState
          title="Nothing to review"
          description="Every extracted field is above its auto-accept threshold."
          action={
            <Link href="/contracts" className="text-xs font-medium text-primary hover:underline">
              Back to contracts
            </Link>
          }
        />
      ) : (
        <div className="space-y-3">
          <p className="rounded-md border border-warning/40 bg-warning-muted px-3 py-2 text-xs text-warning">
            A contract cannot enter the notification pipeline while any field is unresolved. Review actions are
            version-checked — a stale edit returns <strong>409</strong> instead of overwriting.
          </p>
          {items.map((item) => (
            <ReviewCard
              key={item.id}
              item={item}
              busy={mutation.isPending && mutation.variables?.id === item.id}
              onAction={(action, value) => mutation.mutate({ id: item.id, action, value, version: item.version })}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ReviewCard({
  item,
  busy,
  onAction,
}: {
  item: ReviewItem;
  busy: boolean;
  onAction: (action: "accept" | "reject" | "mark_unreadable" | "edit", value?: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.value);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle>
            <Link href={`/contracts/${item.contract_id}`} className="hover:underline">
              {shortId(item.contract_id)}
            </Link>{" "}
            · {item.customer_name}
          </CardTitle>
          <span className="text-xs text-muted-foreground">
            ends {formatDate(item.end_date)} ({item.days_to_expiry}d) · waiting {item.age_minutes} min
          </span>
        </div>
        <MockedBadge />
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-wide text-muted-foreground">{item.field}</p>
            {editing ? (
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <input
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  aria-label={`Edit ${item.field}`}
                  className="h-8 w-72 rounded-md border bg-input px-2 text-sm outline-none focus-visible:border-ring"
                />
                <Button size="sm" onClick={() => onAction("edit", draft)} disabled={busy || !draft.trim()}>
                  Save &amp; approve
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
                  Cancel
                </Button>
              </div>
            ) : (
              <>
                <p className="truncate font-medium">
                  {item.value}
                  {item.original_value ? (
                    <span className="ml-2 text-xs text-muted-foreground line-through">{item.original_value}</span>
                  ) : null}
                </p>
                <p className="text-[11px] text-muted-foreground">source: {item.source} · v{item.version}</p>
              </>
            )}
            {item.conflict ? (
              <p className="mt-1 flex items-center gap-1 text-[11px] text-warning">
                <AlertTriangle className="h-3 w-3" aria-hidden />
                {item.conflict}
              </p>
            ) : null}
          </div>
          <ConfidenceBar value={item.confidence} threshold={item.threshold} />
        </div>

        {!editing ? (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => onAction("accept")} disabled={busy}>
              <Check className="h-3.5 w-3.5" aria-hidden />
              Accept
            </Button>
            <Button size="sm" variant="outline" onClick={() => setEditing(true)} disabled={busy}>
              <Pencil className="h-3.5 w-3.5" aria-hidden />
              Edit
            </Button>
            <Button size="sm" variant="outline" onClick={() => onAction("reject")} disabled={busy}>
              <X className="h-3.5 w-3.5" aria-hidden />
              Reject
            </Button>
            <Button size="sm" variant="ghost" onClick={() => onAction("mark_unreadable")} disabled={busy}>
              Mark unreadable
            </Button>
            {busy ? <span className="self-center text-xs text-muted-foreground">Saving…</span> : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
