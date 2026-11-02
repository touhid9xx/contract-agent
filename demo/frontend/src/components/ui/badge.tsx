import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

type Tone = "neutral" | "primary" | "success" | "warning" | "danger" | "muted";

const toneClasses: Record<Tone, string> = {
  neutral: "border-border bg-muted text-foreground",
  primary: "border-transparent bg-accent text-accent-foreground",
  success: "border-transparent bg-success-muted text-success",
  warning: "border-transparent bg-warning-muted text-warning",
  danger: "border-transparent bg-danger-muted text-danger",
  muted: "border-transparent bg-muted text-muted-foreground",
};

export function Badge({
  children,
  tone = "neutral",
  className,
  title,
}: {
  children: ReactNode;
  tone?: Tone;
  className?: string;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-5",
        toneClasses[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/**
 * Mandatory Phase 0 marker. Every fixture-driven screen carries one so a
 * reviewer can never mistake the demo for the real system.
 */
export function MockedBadge({ className }: { className?: string }) {
  return (
    <Badge
      tone="warning"
      className={cn("uppercase tracking-[0.08em]", className)}
      title="This data is simulated fixture data"
    >
      Mocked
    </Badge>
  );
}
