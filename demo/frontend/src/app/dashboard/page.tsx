"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import Link from "next/link";

import { MockedBadge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { UploadContractButton } from "@/components/upload-button";
import { ErrorState, PageHeader, Skeleton, SkeletonRows } from "@/components/ui/states";
import { api } from "@/lib/api";
import { cn, formatDate, formatNumber, formatTime } from "@/lib/utils";

export default function DashboardPage() {
  const query = useQuery({ queryKey: ["dashboard"], queryFn: api.dashboard });

  return (
    <div className="space-y-4">
      <PageHeader
        title="Dashboard"
        description={
          query.data
            ? `Today · ${formatDate(query.data.today)} · tenant acme (fictional)`
            : "Portfolio overview and reminder windows."
        }
        actions={
          <>
            <MockedBadge />
            <UploadContractButton />
          </>
        }
      />

      {query.isError ? (
        <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
      ) : null}

      <section aria-label="Headline counts" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {query.isLoading
          ? Array.from({ length: 4 }).map((_, index) => <Skeleton key={index} className="h-24 w-full" />)
          : query.data
            ? [
                { label: "Active contracts", value: query.data.counts.active_contracts, tone: "" },
                { label: "Expiring in 30 days", value: query.data.counts.expiring_in_30_days, tone: "" },
                { label: "Awaiting human review", value: query.data.counts.awaiting_review, tone: "text-warning" },
                { label: "Escalations (2wk)", value: query.data.counts.escalations, tone: "text-danger" },
              ].map((stat) => (
                <Card key={stat.label} className="p-4">
                  <p className="text-xs text-muted-foreground">{stat.label}</p>
                  <p className={cn("mt-1 text-2xl font-semibold tabular-nums", stat.tone)}>{formatNumber(stat.value)}</p>
                  <p className="mt-1 text-[10px] uppercase tracking-wide text-muted-foreground">Mocked</p>
                </Card>
              ))
            : null}
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Reminder windows (next 14 days)</CardTitle>
            <MockedBadge />
          </CardHeader>
          <CardContent className="space-y-3">
            {query.isLoading ? (
              <SkeletonRows rows={3} />
            ) : query.data ? (
              query.data.reminder_windows.map((window) => {
                const pct = Math.round((window.contracts / window.capacity) * 100);
                return (
                  <div key={window.milestone} className="space-y-1">
                    <div className="flex items-center justify-between text-xs">
                      <span className="font-medium">{window.milestone}</span>
                      <span className="text-muted-foreground">
                        {formatNumber(window.contracts)} contracts · {pct}% of capacity
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
                      <div className="h-full rounded-full bg-primary" style={{ width: `${Math.min(100, pct)}%` }} />
                    </div>
                    <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Mocked</p>
                  </div>
                );
              })
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Channel mix (30d)</CardTitle>
            <MockedBadge />
          </CardHeader>
          <CardContent className="space-y-3">
            {query.isLoading ? (
              <SkeletonRows rows={3} />
            ) : (
              query.data?.channel_mix.map((item) => (
                <div key={item.channel} className="flex items-center gap-2">
                  <span className="w-12 text-xs capitalize">{item.channel}</span>
                  <div className="h-2 flex-1 overflow-hidden rounded-full bg-muted" aria-hidden>
                    <div className="h-full rounded-full bg-primary" style={{ width: `${item.percent}%` }} />
                  </div>
                  <span className="w-9 text-right text-xs tabular-nums text-muted-foreground">{item.percent}%</span>
                </div>
              ))
            )}
            <p className="text-[10px] uppercase tracking-wide text-muted-foreground">Mocked · fixed fixture</p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Recent activity</CardTitle>
          <Link href="/contracts" className="flex items-center gap-1 text-xs text-primary hover:underline">
            All contracts <ArrowRight className="h-3 w-3" aria-hidden />
          </Link>
        </CardHeader>
        <CardContent>
          {query.isLoading ? (
            <SkeletonRows rows={5} />
          ) : (
            <ul className="divide-y">
              {query.data?.recent_activity.map((item) => (
                <li key={item.id} className="flex items-start gap-3 py-2 text-sm">
                  <span className="w-11 shrink-0 text-xs tabular-nums text-muted-foreground">{formatTime(item.at)}</span>
                  <span
                    className={cn(
                      "mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full",
                      item.tone === "warn" ? "bg-warning" : "bg-primary",
                    )}
                    aria-hidden
                  />
                  <div className="min-w-0 flex-1">
                    <p className="truncate">{item.text}</p>
                    <Link href={`/contracts/${item.contract_id}`} className="text-xs text-primary hover:underline">
                      {item.detail}
                    </Link>
                  </div>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[10px] uppercase tracking-wide text-muted-foreground">Mocked</p>
        </CardContent>
      </Card>
    </div>
  );
}
