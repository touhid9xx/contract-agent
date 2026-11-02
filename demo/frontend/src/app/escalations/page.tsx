"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertOctagon, CheckCircle2, UserPlus } from "lucide-react";
import Link from "next/link";

import { MockedBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui/states";
import { api } from "@/lib/api";
import type { Escalation } from "@/lib/types";
import { useToast } from "@/lib/toast";
import { cn, formatDateTime, shortId } from "@/lib/utils";

export default function EscalationsPage() {
  const queryClient = useQueryClient();
  const pushToast = useToast((state) => state.push);

  const query = useQuery({ queryKey: ["escalations"], queryFn: api.escalations });

  const assignMutation = useMutation({
    mutationFn: ({ id, assignee }: { id: number; assignee: string }) => api.assignEscalation(id, assignee),
    onSuccess: () => {
      pushToast("Escalation assigned to you (mocked).", "success");
      void queryClient.invalidateQueries({ queryKey: ["escalations"] });
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const resolveMutation = useMutation({
    mutationFn: (id: number) => api.resolveEscalation(id, "renewal confirmed (demo)"),
    onSuccess: () => {
      pushToast("Escalation resolved (mocked).", "success");
      void queryClient.invalidateQueries({ queryKey: ["escalations"] });
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const data = query.data;
  const busy = (id: number) =>
    (assignMutation.isPending && assignMutation.variables?.id === id) ||
    (resolveMutation.isPending && resolveMutation.variables === id);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Escalations"
        description={
          data
            ? `2-week no-response queue · SLA ${data.sla_hours}h after an escalation is raised`
            : "Contracts with no meaningful response after two weeks."
        }
        actions={<MockedBadge />}
      />

      {query.isError ? (
        <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <Card className="p-4">
          <SkeletonRows rows={4} />
        </Card>
      ) : (
        <div className="space-y-5">
          <Section
            title="SLA breached"
            count={data?.breached.length ?? 0}
            tone="danger"
            emptyLabel="No breached SLAs — nice."
          >
            {(data?.breached ?? []).map((escalation) => (
              <EscalationRow
                key={escalation.id}
                escalation={escalation}
                breached
                busy={busy(escalation.id)}
                onAssign={() => assignMutation.mutate({ id: escalation.id, assignee: "me@acme.test" })}
                onResolve={() => resolveMutation.mutate(escalation.id)}
              />
            ))}
          </Section>

          <Section title="Open" count={data?.open.length ?? 0} tone="warning" emptyLabel="No open escalations.">
            {(data?.open ?? []).map((escalation) => (
              <EscalationRow
                key={escalation.id}
                escalation={escalation}
                busy={busy(escalation.id)}
                onAssign={() => assignMutation.mutate({ id: escalation.id, assignee: "me@acme.test" })}
                onResolve={() => resolveMutation.mutate(escalation.id)}
              />
            ))}
          </Section>

          <Section title="Resolved (last 24h)" count={data?.resolved.length ?? 0} tone="success" emptyLabel="Nothing resolved yet.">
            {(data?.resolved ?? []).map((escalation) => (
              <EscalationRow key={escalation.id} escalation={escalation} busy={false} onAssign={() => {}} onResolve={() => {}} />
            ))}
          </Section>

          <p className="text-[10px] uppercase tracking-wide text-muted-foreground">
            Mocked · SLA = 48h after escalation raised (defaults documented in DECISIONS.md)
          </p>
        </div>
      )}
    </div>
  );
}

function Section({
  title,
  count,
  tone,
  emptyLabel,
  children,
}: {
  title: string;
  count: number;
  tone: "danger" | "warning" | "success";
  emptyLabel: string;
  children: React.ReactNode;
}) {
  const toneClass = {
    danger: "text-danger",
    warning: "text-warning",
    success: "text-success",
  }[tone];

  return (
    <section className="space-y-2">
      <h2 className={cn("flex items-center gap-2 text-xs font-semibold uppercase tracking-wide", toneClass)}>
        {title} <span className="text-muted-foreground">({count})</span>
      </h2>
      {count === 0 ? (
        <EmptyState title={emptyLabel} description="Nothing to action in this bucket right now." compact />
      ) : (
        <div className="space-y-2">{children}</div>
      )}
    </section>
  );
}

function EscalationRow({
  escalation,
  breached = false,
  busy,
  onAssign,
  onResolve,
}: {
  escalation: Escalation;
  breached?: boolean;
  busy: boolean;
  onAssign: () => void;
  onResolve: () => void;
}) {
  const resolved = escalation.status === "resolved";

  return (
    <Card className={cn(breached && "border-danger/50")}>
      <CardContent className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <Link href={`/contracts/${escalation.contract_id}`} className="text-sm font-medium hover:underline">
              {shortId(escalation.contract_id)} · {escalation.customer_name}
            </Link>
            {breached ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-danger-muted px-2 py-0.5 text-[11px] font-medium text-danger">
                <AlertOctagon className="h-3 w-3" aria-hidden />
                SLA breached
              </span>
            ) : null}
            {resolved ? (
              <span className="inline-flex items-center gap-1 rounded-full bg-success-muted px-2 py-0.5 text-[11px] font-medium text-success">
                <CheckCircle2 className="h-3 w-3" aria-hidden />
                resolved
              </span>
            ) : null}
          </div>
          <p className="text-xs text-muted-foreground">
            raised {formatDateTime(escalation.raised_at)} · SLA {formatDateTime(escalation.sla_deadline)} ·{" "}
            {escalation.assigned_to ? `assigned: ${escalation.assigned_to}` : "unassigned"}
          </p>
          <p className="text-xs text-muted-foreground">{escalation.last_activity}</p>
          {resolved && escalation.resolution ? (
            <p className="text-xs text-success">resolution: {escalation.resolution}</p>
          ) : null}
        </div>

        {!resolved ? (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={onAssign} disabled={busy || escalation.assigned_to === "me@acme.test"}>
              <UserPlus className="h-3.5 w-3.5" aria-hidden />
              {escalation.assigned_to === "me@acme.test" ? "Assigned to me" : "Assign to me"}
            </Button>
            <Button size="sm" onClick={onResolve} disabled={busy}>
              Resolve
            </Button>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
