// frontend/components/common/confidence-badge.tsx
import { cn } from "cn";
import { CheckCircle2, AlertTriangle, XCircle } from "lucide-react";

type Confidence = "high" | "medium" | "low";

const THRESHOLDS = {
  high: 0.9, // ≥0.9 — green
  medium: 0.8, // 0.8–0.9 — yellow (needs review per settings)
  // <0.8 — red
} as const;

export function confidenceLevel(score: number): Confidence {
  if (score >= THRESHOLDS.high) return "high";
  if (score >= THRESHOLDS.medium) return "medium";
  return "low";
}

const STYLES: Record<Confidence, { icon: typeof CheckCircle2; classes: string; label: string }> = {
  high: {
    icon: CheckCircle2,
    classes:
      "bg-green-100 text-green-800 border-green-300 " +
      "dark:bg-green-950 dark:text-green-300 dark:border-green-800",
    label: "High confidence",
  },
  medium: {
    icon: AlertTriangle,
    classes:
      "bg-yellow-100 text-yellow-800 border-yellow-300 " +
      "dark:bg-yellow-950 dark:text-yellow-300 dark:border-yellow-800",
    label: "Medium confidence",
  },
  low: {
    icon: XCircle,
    classes:
      "bg-red-100 text-red-800 border-red-300 " +
      "dark:bg-red-950 dark:text-red-300 dark:border-red-800",
    label: "Low confidence — needs review",
  },
};

export interface ConfidenceBadgeProps {
  score: number;
  showScore?: boolean;
  className?: string;
}

export function ConfidenceBadge({ score, showScore = true, className }: ConfidenceBadgeProps) {
  const level = confidenceLevel(score);
  const { icon: Icon, classes, label } = STYLES[level];
  const percent = Math.round(score * 100);

  return (
    <span
      role="status"
      aria-label={`${label}: ${percent}%`}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5",
        "text-xs font-medium",
        classes,
        className,
      )}
    >
      <Icon className="h-3 w-3" aria-hidden="true" />
      <span>{label.split(" ")[0]}</span>
      {showScore && <span className="tabular-nums">{percent}%</span>}
    </span>
  );
}
