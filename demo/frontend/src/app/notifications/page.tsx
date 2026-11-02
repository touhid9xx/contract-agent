"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { MockedBadge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui/states";
import { api } from "@/lib/api";
import type { Notification } from "@/lib/types";
import { useToast } from "@/lib/toast";
import { cn, formatDateTime, shortId } from "@/lib/utils";

const CHANNEL_FILTERS = [
  { value: "all", label: "All channels" },
  { value: "email", label: "Email" },
  { value: "sms", label: "SMS" },
  { value: "voice", label: "Voice" },
];

const STATUS_FILTERS = [
  { value: "all", label: "All statuses" },
  { value: "sent", label: "Sent" },
  { value: "failed", label: "Failed" },
  { value: "delivered", label: "Delivered" },
  { value: "answered", label: "Answered" },
  { value: "machine", label: "Machine" },
];

const statusTone: Record<Notification["status"], string> = {
  sent: "bg-accent text-accent-foreground",
  failed: "bg-danger-muted text-danger",
  delivered: "bg-success-muted text-success",
  answered: "bg-success-muted text-success",
  machine: "bg-warning-muted text-warning",
};

export default function NotificationsPage() {
  const queryClient = useQueryClient();
  const pushToast = useToast((state) => state.push);
  const [channel, setChannel] = useState("all");
  const [status, setStatus] = useState("all");

  const query = useQuery({
    queryKey: ["notifications", channel, status],
    queryFn: () => api.notifications({ channel, status }),
  });

  const retryMutation = useMutation({
    mutationFn: (id: string) => api.retryNotification(id),
    onSuccess: () => {
      pushToast("Notification retried — status set to sent (mocked).", "success");
      void queryClient.invalidateQueries({ queryKey: ["notifications"] });
    },
    onError: (error: Error) => pushToast(error.message, "error"),
  });

  const items = query.data?.items ?? [];

  return (
    <div className="space-y-4">
      <PageHeader
        title="Notifications"
        description={query.data ? `${query.data.count} dispatch attempts` : "Every send attempt across all channels."}
        actions={<MockedBadge />}
      />

      <Card className="flex flex-wrap items-center gap-2 p-3">
        <select
          value={channel}
          onChange={(event) => setChannel(event.target.value)}
          aria-label="Filter by channel"
          className="h-8 rounded-md border bg-input px-2 text-xs outline-none focus-visible:border-ring"
        >
          {CHANNEL_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <select
          value={status}
          onChange={(event) => setStatus(event.target.value)}
          aria-label="Filter by status"
          className="h-8 rounded-md border bg-input px-2 text-xs outline-none focus-visible:border-ring"
        >
          {STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
        <p className="ml-auto text-[11px] text-muted-foreground">Daily-scan scheduler · never delayed-60-day messages</p>
      </Card>

      {query.isError ? (
        <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
      ) : query.isLoading ? (
        <Card className="p-4">
          <SkeletonRows rows={6} />
        </Card>
      ) : items.length === 0 ? (
        <EmptyState title="No notifications match these filters" description="Clear a filter to see dispatch attempts." />
      ) : (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">Channel</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Sent at</th>
                  <th className="px-3 py-2 font-medium">Contract</th>
                  <th className="px-3 py-2 font-medium">Template</th>
                  <th className="px-3 py-2 font-medium">Idempotency key</th>
                  <th className="px-3 py-2 font-medium">Retries</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id} className="border-b last:border-0 hover:bg-muted/60">
                    <td className="px-3 py-2 capitalize">{item.channel}</td>
                    <td className="px-3 py-2">
                      <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium capitalize", statusTone[item.status])}>
                        {item.status}
                      </span>
                    </td>
                    <td className="px-3 py-2 tabular-nums text-muted-foreground">{formatDateTime(item.sent_at)}</td>
                    <td className="px-3 py-2">
                      <Link href={`/contracts/${item.contract_id}`} className="text-primary hover:underline">
                        {shortId(item.contract_id)}
                      </Link>
                    </td>
                    <td className="px-3 py-2 text-muted-foreground">{item.template}</td>
                    <td className="px-3 py-2 font-mono text-[11px] text-muted-foreground">{item.idempotency_key}</td>
                    <td className={cn("px-3 py-2 tabular-nums", item.retry_count > 0 && "text-warning")}>
                      {item.retry_count}/{item.retry_max}
                    </td>
                    <td className="px-3 py-2 text-right">
                      {item.status === "failed" ? (
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => retryMutation.mutate(item.id)}
                          disabled={retryMutation.isPending && retryMutation.variables === item.id}
                        >
                          <RotateCw className="h-3.5 w-3.5" aria-hidden />
                          Retry
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="border-t px-3 py-2 text-[10px] uppercase tracking-wide text-muted-foreground">
            Mocked · idempotency keys prevent duplicate sends for the same contract + template + day
          </p>
        </Card>
      )}
    </div>
  );
}
