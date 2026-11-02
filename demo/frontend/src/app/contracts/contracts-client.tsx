"use client";

import { useQuery } from "@tanstack/react-query";
import { LayoutGrid, List, Search } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { ConfidenceBar } from "@/components/confidence-bar";
import { RiskLabel, StatusBadge } from "@/components/status-badge";
import { MockedBadge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, PageHeader, SkeletonRows } from "@/components/ui/states";
import { UploadContractButton } from "@/components/upload-button";
import { api } from "@/lib/api";
import type { Contract, ContractStatus } from "@/lib/types";
import { cn, formatDate, formatNumber, shortId } from "@/lib/utils";

const STATUSES = [
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active" },
  { value: "reminder_sent", label: "Reminder sent" },
  { value: "responded", label: "Responded" },
  { value: "escalated", label: "Escalated" },
  { value: "review", label: "Review (!)" },
];

const CHANNELS = [
  { value: "all", label: "All channels" },
  { value: "email", label: "Email" },
  { value: "sms", label: "SMS" },
  { value: "voice", label: "Voice" },
];

const RISKS = [
  { value: "all", label: "All risk" },
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
];

const KANBAN_COLUMNS: ContractStatus[] = ["review", "active", "reminder_sent", "responded", "escalated"];

export function ContractsClient() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [status, setStatus] = useState("all");
  const [channel, setChannel] = useState("all");
  const [risk, setRisk] = useState("all");
  const [search, setSearch] = useState(searchParams.get("q") ?? "");
  const [page, setPage] = useState(1);
  const [view, setView] = useState<"list" | "kanban">("list");

  useEffect(() => {
    setSearch(searchParams.get("q") ?? "");
    setPage(1);
  }, [searchParams]);

  useEffect(() => {
    setPage(1);
  }, [status, channel, risk, search]);

  const params = useMemo(
    () => ({ status, channel, risk, q: search.trim() || undefined, page, page_size: 6 }),
    [status, channel, risk, search, page],
  );

  const query = useQuery({
    queryKey: ["contracts", params],
    queryFn: () => api.contracts(params),
  });

  const items = query.data?.items ?? [];
  const activeFilters = [status, channel, risk].filter((value) => value !== "all").length + (search.trim() ? 1 : 0);

  const onSearchSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    router.push(search.trim() ? `/contracts?q=${encodeURIComponent(search.trim())}` : "/contracts");
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Contracts"
        description={
          query.data
            ? `${formatNumber(query.data.total)} contracts · page ${query.data.page} of ${query.data.page_count}`
            : "Filter the portfolio and open any contract's timeline."
        }
        actions={
          <>
            <MockedBadge />
            <UploadContractButton />
          </>
        }
      />

      <Card className="space-y-3 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <FilterSelect label="Status" value={status} onChange={setStatus} options={STATUSES} />
          <FilterSelect label="Channel" value={channel} onChange={setChannel} options={CHANNELS} />
          <FilterSelect label="Risk" value={risk} onChange={setRisk} options={RISKS} />

          <form onSubmit={onSearchSubmit} className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Customer or ID"
              aria-label="Filter contracts by customer or ID"
              className="h-8 w-44 rounded-md border bg-input pl-7 pr-2 text-xs outline-none focus-visible:border-ring"
            />
          </form>

          <div className="ml-auto flex items-center gap-1 rounded-md border p-0.5">
            <ViewToggle active={view === "list"} onClick={() => setView("list")} label="List view">
              <List className="h-3.5 w-3.5" aria-hidden />
            </ViewToggle>
            <ViewToggle active={view === "kanban"} onClick={() => setView("kanban")} label="Kanban view">
              <LayoutGrid className="h-3.5 w-3.5" aria-hidden />
            </ViewToggle>
          </div>
        </div>
        <p className="text-[11px] text-muted-foreground">
          {activeFilters > 0 ? `${activeFilters} filter${activeFilters > 1 ? "s" : ""} active · ` : ""}all rows are
          fictional fixture data.
        </p>
      </Card>

      {query.isError ? (
        <ErrorState message={(query.error as Error).message} onRetry={() => void query.refetch()} />
      ) : null}

      {query.isLoading ? (
        <Card className="p-4">
          <SkeletonRows rows={6} />
        </Card>
      ) : items.length === 0 ? (
        <EmptyState
          title="No contracts match these filters"
          description="Try clearing a filter or searching for a different customer."
        />
      ) : view === "list" ? (
        <Card className="overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px] text-sm">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="px-3 py-2 font-medium">ID</th>
                  <th className="px-3 py-2 font-medium">Customer</th>
                  <th className="px-3 py-2 font-medium">Ends</th>
                  <th className="px-3 py-2 font-medium">Days</th>
                  <th className="px-3 py-2 font-medium">Channel</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  <th className="px-3 py-2 font-medium">Risk</th>
                  <th className="px-3 py-2 font-medium">Confidence</th>
                </tr>
              </thead>
              <tbody>
                {items.map((contract) => (
                  <tr key={contract.id} className="border-b last:border-0 hover:bg-muted/60">
                    <td className="px-3 py-2">
                      <Link href={`/contracts/${contract.id}`} className="font-medium text-primary hover:underline">
                        {shortId(contract.id)}
                      </Link>
                    </td>
                    <td className="px-3 py-2">{contract.customer_name}</td>
                    <td className="px-3 py-2 tabular-nums text-muted-foreground">{formatDate(contract.end_date)}</td>
                    <td className="px-3 py-2 tabular-nums">{contract.days_to_expiry}</td>
                    <td className="px-3 py-2 capitalize text-muted-foreground">{contract.channel ?? "—"}</td>
                    <td className="px-3 py-2">
                      <StatusBadge status={contract.status} />
                    </td>
                    <td className="px-3 py-2">
                      <RiskLabel risk={contract.risk} />
                    </td>
                    <td className="px-3 py-2">
                      <ConfidenceBar value={contract.confidence} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="border-t px-3 py-2 text-[10px] uppercase tracking-wide text-muted-foreground">
            Mocked · all rows are fake fixture data
          </p>
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
          {KANBAN_COLUMNS.map((column) => {
            const columnItems = items.filter((contract) => contract.status === column);
            return (
              <Card key={column} className="flex flex-col">
                <div className="flex items-center justify-between border-b px-3 py-2">
                  <span className="text-xs font-medium capitalize">{column.replace("_", " ")}</span>
                  <span className="text-xs text-muted-foreground">{columnItems.length}</span>
                </div>
                <div className="flex-1 space-y-2 p-2">
                  {columnItems.length === 0 ? (
                    <p className="px-1 py-4 text-center text-xs text-muted-foreground">None on this page</p>
                  ) : (
                    columnItems.map((contract) => <KanbanCard key={contract.id} contract={contract} />)
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}

      {query.data && query.data.page_count > 1 ? (
        <Pagination page={page} pageCount={Math.min(query.data.page_count, 128)} onChange={setPage} />
      ) : null}
    </div>
  );
}

function FilterSelect({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
}) {
  return (
    <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
      <span className="sr-only">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-label={label}
        className="h-8 rounded-md border bg-input px-2 text-xs text-foreground outline-none focus-visible:border-ring"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function ViewToggle({
  active,
  onClick,
  label,
  children,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={active}
      className={cn(
        "grid h-7 w-7 place-items-center rounded",
        active ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-muted",
      )}
    >
      {children}
    </button>
  );
}

function KanbanCard({ contract }: { contract: Contract }) {
  return (
    <Link
      href={`/contracts/${contract.id}`}
      className="block rounded-md border bg-card p-2 text-xs transition-colors hover:border-primary/50"
    >
      <div className="flex items-center justify-between">
        <span className="font-medium">{shortId(contract.id)}</span>
        <span className="text-muted-foreground">{contract.days_to_expiry}d</span>
      </div>
      <p className="truncate text-muted-foreground">{contract.customer_name}</p>
      <div className="mt-1 flex items-center justify-between">
        <span className="capitalize text-muted-foreground">{contract.channel ?? "—"}</span>
        <ConfidenceBar value={contract.confidence} showLabel={false} />
      </div>
    </Link>
  );
}

function Pagination({ page, pageCount, onChange }: { page: number; pageCount: number; onChange: (page: number) => void }) {
  return (
    <div className="flex items-center justify-center gap-2 text-xs">
      <button
        type="button"
        onClick={() => onChange(Math.max(1, page - 1))}
        disabled={page <= 1}
        className="rounded border px-2 py-1 disabled:opacity-40"
      >
        Prev
      </button>
      <span className="text-muted-foreground">
        Page {page} of {pageCount}
      </span>
      <button
        type="button"
        onClick={() => onChange(Math.min(pageCount, page + 1))}
        disabled={page >= pageCount}
        className="rounded border px-2 py-1 disabled:opacity-40"
      >
        Next
      </button>
    </div>
  );
}
