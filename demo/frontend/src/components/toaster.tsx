"use client";

import { CheckCircle2, Info, X, XCircle } from "lucide-react";

import { useToast, type ToastTone } from "@/lib/toast";
import { cn } from "@/lib/utils";

const toneStyles: Record<ToastTone, string> = {
  success: "border-success/40 bg-success-muted text-success",
  error: "border-danger/40 bg-danger-muted text-danger",
  info: "border-border bg-card text-foreground",
};

const toneIcon = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
} as const;

/** Fixed, non-blocking feedback stack. Used for demo mutation results and 409s. */
export function Toaster() {
  const toasts = useToast((state) => state.toasts);
  const dismiss = useToast((state) => state.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div
      className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(20rem,calc(100vw-2rem))] flex-col gap-2"
      role="status"
      aria-live="polite"
    >
      {toasts.map((toast) => {
        const Icon = toneIcon[toast.tone];
        return (
          <div
            key={toast.id}
            className={cn(
              "pointer-events-auto flex items-start gap-2 rounded-card border px-3 py-2 text-sm shadow-lg",
              "animate-in-up",
              toneStyles[toast.tone],
            )}
          >
            <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <p className="flex-1 leading-snug">{toast.message}</p>
            <button
              type="button"
              onClick={() => dismiss(toast.id)}
              className="rounded p-0.5 opacity-70 transition-opacity hover:opacity-100"
              aria-label="Dismiss notification"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
        );
      })}
    </div>
  );
}
