import { cn } from "@/lib/utils";
import type { ContractStatus, RiskLevel } from "@/lib/types";

const statusTone: Record<ContractStatus, string> = {
  active: "bg-muted text-muted-foreground",
  reminder_sent: "bg-accent text-accent-foreground",
  responded: "bg-success-muted text-success",
  renewed: "bg-success-muted text-success",
  expired: "bg-muted text-muted-foreground",
  escalated: "bg-danger-muted text-danger",
  review: "bg-warning-muted text-warning",
};

const statusLabel: Record<ContractStatus, string> = {
  active: "active",
  reminder_sent: "reminder_sent",
  responded: "responded",
  renewed: "renewed",
  expired: "expired",
  escalated: "escalated",
  review: "review",
};

export function StatusBadge({ status, className }: { status: ContractStatus; className?: string }) {
  const flagged = status === "review" || status === "escalated";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium leading-5",
        statusTone[status],
        className,
      )}
    >
      {statusLabel[status]}
      {flagged ? <span aria-label="needs attention">!</span> : null}
    </span>
  );
}

const riskTone: Record<RiskLevel, string> = {
  low: "text-muted-foreground",
  medium: "text-warning",
  high: "text-danger",
};

export function RiskLabel({ risk }: { risk: RiskLevel }) {
  return <span className={cn("text-xs font-medium capitalize", riskTone[risk])}>{risk}</span>;
}
