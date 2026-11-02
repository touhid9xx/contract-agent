import { cn, confidenceLabel } from "@/lib/utils";

/**
 * Compact confidence meter. Values below the auto-accept threshold (0.80) are
 * rendered in the warning tone because they must never auto-enter the pipeline.
 */
export function ConfidenceBar({
  value,
  threshold = 0.8,
  showLabel = true,
  className,
}: {
  value: number;
  threshold?: number;
  showLabel?: boolean;
  className?: string;
}) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const low = value < threshold;

  return (
    <div className={cn("flex items-center gap-2", className)} title={`confidence ${confidenceLabel(value)}`}>
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-muted" aria-hidden>
        <div
          className={cn("h-full rounded-full", low ? "bg-warning" : "bg-success")}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showLabel ? (
        <span className={cn("tabular-nums text-xs", low ? "text-warning" : "text-muted-foreground")}>
          {confidenceLabel(value)}
        </span>
      ) : null}
    </div>
  );
}
