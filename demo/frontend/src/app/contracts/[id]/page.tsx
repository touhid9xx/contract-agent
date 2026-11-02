"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowLeft,
  ChevronRight,
  Flag,
  MessageSquarePlus,
  RefreshCw,
  Send,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useMemo, useState } from "react";

import { ConfidenceBar } from "@/components/confidence-bar";
import { RiskLabel, StatusBadge } from "@/components/status-badge";
import { MockedBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, Skeleton, SkeletonRows } from "@/components/ui/states";
import { api } from "@/lib/api";
import type { AgentDecision, TimelineEvent } from "@/lib/types";
import { useToast } from "@/lib/toast";
import { cn, formatDate, formatDateTime, formatDayHeading, shortId } from "@/lib/utils";

export default function ContractDetailPage() {
  const params = useParams<{ id: string }>();
  const contractId = Number(params.id);
  const queryClient = useQueryClient();
  const pushToast = useToast((state) => state.push);
  const [noteOpen, setNoteOpen] = useState(false);
  const [note, setNote] = useState("");

  const query = useQuery({
    queryKey: ["contract", contractId],
    queryFn: () => api.contract(contractId),
    enabled: Number.isFinite(contractId),
  });

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: ["contract", contractId] });

  const noteMutation = useMutation({
    mutationFn: () => api.addNote(contractId, note.trim()),
    onSuccess: () => {
      setNote("");
      setNoteOpen(false);
      pushToast("Note added to the timeline (mocked).", "success");
      invalidate();
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const rerunMutation = useMutation({
    mutationFn: () => api.rerunAgent(contractId),
    onSuccess: () => {
      pushToast("Agent decision re-run (simulated Ollama latency).", "success");
      invalidate();
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const escalateMutation = useMutation({
    mutationFn: () => api.escalate(contractId),
    onSuccess: (result) => {
      pushToast(result.created ? "Escalation raised (mocked)." : "An open escalation already exists.", result.created ? "success" : "info");
      invalidate();
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const respondedMutation = useMutation({
    mutationFn: () => api.markResponded(contractId),
    onSuccess: () => {
      pushToast("Customer marked as responded (mocked).", "success");
      invalidate();
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const groupedTimeline = useMemo(() => groupByDay(query.data?.timeline ?? []), [query.data?.timeline]);

  if (query.isError) {
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
      </div>
    );
  }

  const detail = query.data;
  const contract = detail?.contract;
  const decision = detail?.agent_decision ?? null;
  const blocked = contract?.pipeline_blocked ?? false;

  return (
    <div className="space-y-4">
      <BackLink />

      {contract && detail ? (
        <Card className="p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-lg font-semibold tracking-tight">{shortId(contract.id)} · {contract.customer_name}</h1>
                <StatusBadge status={contract.status} />
                <MockedBadge />
              </div>
              <p className="text-sm text-muted-foreground">
                Ends {formatDate(contract.end_date)} ({contract.days_to_expiry} days) · tenant:{" "}
                {contract.tenant_id} · risk <RiskLabel risk={contract.risk} />
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled
                title="Editing extracted fields opens the review queue in milestone P2-C"
                onClick={() => pushToast("Field editing lives in the review queue (P2-C).", "info")}
              >
                Edit fields
              </Button>
              <Button size="sm" variant="outline" onClick={() => setNoteOpen((open) => !open)}>
                <MessageSquarePlus className="h-3.5 w-3.5" aria-hidden />
                Add note
              </Button>
              <Button size="sm" variant="outline" onClick={() => rerunMutation.mutate()} disabled={rerunMutation.isPending || blocked} title={blocked ? "Pipeline blocked until review is resolved" : undefined}>
                <RefreshCw className={cn("h-3.5 w-3.5", rerunMutation.isPending && "animate-spin")} aria-hidden />
                {rerunMutation.isPending ? "Running…" : "Re-run agent"}
              </Button>
              <Button size="sm" variant="danger" onClick={() => escalateMutation.mutate()} disabled={escalateMutation.isPending}>
                <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
                Escalate now
              </Button>
              <Button size="sm" variant="secondary" onClick={() => respondedMutation.mutate()} disabled={respondedMutation.isPending}>
                <Send className="h-3.5 w-3.5" aria-hidden />
                Mark responded
              </Button>
            </div>
          </div>

          {blocked ? (
            <p className="mt-3 flex items-start gap-2 rounded-md border border-warning/40 bg-warning-muted px-3 py-2 text-xs text-warning">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              Pipeline blocked — a field is awaiting human review. The agent will not run and no notification can be
              sent until it is resolved.
            </p>
          ) : null}

          {detail.escalation ? (
            <p className="mt-2 flex items-start gap-2 rounded-md border border-danger/40 bg-danger-muted px-3 py-2 text-xs text-danger">
              <Flag className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              Escalation {detail.escalation.status.replace("_", " ")} · SLA {formatDateTime(detail.escalation.sla_deadline)}
            </p>
          ) : null}

          {noteOpen ? (
            <div className="mt-3 space-y-2">
              <textarea
                value={note}
                onChange={(event) => setNote(event.target.value)}
                rows={2}
                maxLength={500}
                placeholder="Internal note (visible in the timeline, PII-free)"
                className="w-full rounded-md border bg-input px-3 py-2 text-sm outline-none focus-visible:border-ring"
              />
              <div className="flex gap-2">
                <Button size="sm" onClick={() => noteMutation.mutate()} disabled={!note.trim() || noteMutation.isPending}>
                  Save note
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setNoteOpen(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : null}
        </Card>
      ) : (
        <Skeleton className="h-28 w-full" />
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Extracted fields</CardTitle>
            <MockedBadge />
          </CardHeader>
          <CardContent>
            {query.isLoading ? (
              <SkeletonRows rows={5} />
            ) : (
              <ul className="divide-y text-sm">
                {detail?.fields.map((field) => {
                  const low = field.confidence < 0.8;
                  return (
                    <li key={field.id} className="flex items-center justify-between gap-2 py-2">
                      <div className="min-w-0">
                        <p className="text-xs uppercase tracking-wide text-muted-foreground">{field.field}</p>
                        <p className="truncate font-medium">{field.value}</p>
                        <p className="truncate text-[11px] text-muted-foreground">{field.source}</p>
                      </div>
                      <div className="flex shrink-0 flex-col items-end gap-1">
                        <ConfidenceBar value={field.confidence} />
                        {field.status === "pending_review" ? (
                          <Link href="/review" className="text-[11px] font-medium text-warning hover:underline">
                            review →
                          </Link>
                        ) : low ? (
                          <span className="text-[11px] text-muted-foreground">reviewed</span>
                        ) : null}
                      </div>
                    </li>
                  );
                })}
              </ul>
            )}
          </CardContent>
        </Card>

        <AgentDecisionCard decision={decision} loading={query.isLoading} blocked={blocked} contractId={contractId} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Timeline</CardTitle>
          <MockedBadge />
        </CardHeader>
        <CardContent>
          {query.isLoading ? (
            <SkeletonRows rows={6} />
          ) : groupedTimeline.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">No timeline events for this contract yet.</p>
          ) : (
            <div className="space-y-5">
              {groupedTimeline.map(([day, events]) => (
                <section key={day}>
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                    {formatDayHeading(day)}
                  </h3>
                  <ol className="relative space-y-2 border-l pl-4">
                    {events.map((event) => (
                      <TimelineRow key={event.id} event={event} />
                    ))}
                  </ol>
                </section>
              ))}
            </div>
          )}
          <p className="mt-3 text-[10px] uppercase tracking-wide text-muted-foreground">
            Mocked · click any event to expand its JSON payload
          </p>
        </CardContent>
      </Card>
    </div>
  );
}

function BackLink() {
  return (
    <Link href="/contracts" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
      <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
      Contracts
    </Link>
  );
}

function AgentDecisionCard({
  decision,
  loading,
  blocked,
  contractId,
}: {
  decision: AgentDecision | null;
  loading: boolean;
  blocked: boolean;
  contractId: number;
}) {
  const queryClient = useQueryClient();
  const pushToast = useToast((state) => state.push);
  const [channel, setChannel] = useState<"email" | "sms" | "voice">("email");

  const overrideMutation = useMutation({
    mutationFn: () => api.overrideChannel(contractId, channel),
    onSuccess: () => {
      pushToast("Channel overridden by operator (mocked).", "success");
      void queryClient.invalidateQueries({ queryKey: ["contract", contractId] });
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const flagMutation = useMutation({
    mutationFn: () => api.flagDecision(contractId),
    onSuccess: () => {
      pushToast("Decision flagged for human review.", "success");
      void queryClient.invalidateQueries({ queryKey: ["contract", contractId] });
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  if (loading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Agent decision</CardTitle>
        </CardHeader>
        <CardContent>
          <SkeletonRows rows={4} />
        </CardContent>
      </Card>
    );
  }

  if (!decision) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Agent decision</CardTitle>
          <MockedBadge />
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted-foreground">
            {blocked
              ? "No decision — the agent is not invoked while the pipeline is blocked by human review."
              : "No agent decision recorded for this contract."}
          </p>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agent decision</CardTitle>
        <MockedBadge />
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Chosen channel</p>
          <div className="mt-1 flex items-center gap-2">
            <span className="rounded-md bg-accent px-2 py-1 text-sm font-medium capitalize text-accent-foreground">
              {decision.channel}
            </span>
            <span className="text-xs text-muted-foreground">confidence {decision.confidence.toFixed(2)}</span>
            {decision.original_channel ? (
              <span className="text-[11px] text-muted-foreground">(was {decision.original_channel})</span>
            ) : null}
          </div>
        </div>

        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Reasoning</p>
          <p className="mt-1 leading-snug">{decision.reasoning}</p>
        </div>

        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Provenance</p>
          <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
            <div>prompt <span className="text-foreground">{decision.prompt_version}</span></div>
            <div>model <span className="text-foreground">{decision.llm_model}</span></div>
            <div>temperature <span className="text-foreground">{decision.temperature}</span></div>
            <div>tokens <span className="text-foreground">{decision.tokens_in}/{decision.tokens_out}</span></div>
            <div>latency <span className="text-foreground">{decision.latency_ms} ms</span></div>
            <div>repairs <span className="text-foreground">{decision.schema_repairs}</span></div>
          </dl>
          <p className="mt-1 text-[11px] text-muted-foreground">
            mlflow run <span className="text-foreground">{decision.mlflow_run_id}</span>
          </p>
        </div>

        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Input signals (PII-free)</p>
          <pre className="mt-1 overflow-x-auto rounded-md bg-muted p-2 text-[11px] leading-relaxed">
{JSON.stringify(decision.input_signals, null, 2)}
          </pre>
        </div>

        <div>
          <p className="text-xs uppercase tracking-wide text-muted-foreground">Safety</p>
          <p className="mt-1 text-[11px] text-muted-foreground">
            needs_review <span className={cn("font-medium", decision.needs_review ? "text-warning" : "text-success")}>{String(decision.needs_review)}</span>
            {" · "}injection tags present{" "}
            <span className={cn("font-medium", decision.injection_tags_present ? "text-danger" : "text-success")}>
              {decision.injection_tags_present ? "yes" : "no"}
            </span>
          </p>
        </div>

        <div className="flex flex-wrap items-center gap-2 border-t pt-3">
          <select
            value={channel}
            onChange={(event) => setChannel(event.target.value as "email" | "sms" | "voice")}
            aria-label="Override channel"
            className="h-8 rounded-md border bg-input px-2 text-xs outline-none focus-visible:border-ring"
          >
            <option value="email">email</option>
            <option value="sms">sms</option>
            <option value="voice">voice</option>
          </select>
          <Button size="sm" variant="outline" onClick={() => overrideMutation.mutate()} disabled={overrideMutation.isPending}>
            Override channel
          </Button>
          <Button size="sm" variant="ghost" onClick={() => flagMutation.mutate()} disabled={flagMutation.isPending}>
            <Flag className="h-3.5 w-3.5" aria-hidden />
            Flag for review
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function TimelineRow({ event }: { event: TimelineEvent }) {
  const [open, setOpen] = useState(false);
  const warn = event.type.includes("escalation") || event.type === "review.queued";

  return (
    <li className="relative">
      <span
        className={cn("absolute -left-[21px] top-2 h-2 w-2 rounded-full", warn ? "bg-warning" : "bg-primary")}
        aria-hidden
      />
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-muted"
      >
        <ChevronRight className={cn("mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform", open && "rotate-90")} aria-hidden />
        <span className="w-16 shrink-0 text-[11px] tabular-nums text-muted-foreground">
          {formatDateTime(event.at).split(" · ")[1]}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
              {event.type}
            </span>
            <span className="text-sm">{event.summary}</span>
          </span>
        </span>
      </button>
      {open ? (
        <pre className="mb-1 ml-8 overflow-x-auto rounded-md border bg-muted/50 p-2 text-[11px] leading-relaxed">
{JSON.stringify(event.payload, null, 2)}
        </pre>
      ) : null}
    </li>
  );
}

function groupByDay(events: TimelineEvent[]): [string, TimelineEvent[]][] {
  const map = new Map<string, TimelineEvent[]>();
  for (const event of events) {
    const day = event.at.slice(0, 10);
    const bucket = map.get(day);
    if (bucket) bucket.push(event);
    else map.set(day, [event]);
  }
  return Array.from(map.entries())
    .sort((a, b) => (a[0] < b[0] ? 1 : -1))
    .map(([day, list]) => [day, list.sort((a, b) => (a.at < b.at ? 1 : -1))]);
}
