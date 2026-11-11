"use client";

import { useTranslations } from "next-intl";

import { ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * The correlation id of a failed request, printed so a person can quote it.
 *
 * `ApiError.traceId` has been parsed out of the envelope since `D10-72` and
 * nothing read it: the value existed to tie a user's "it broke" to the server
 * line that explains it, and no screen offered it, so the tie could only be made
 * by an operator who already had the browser's network log. This renders it at
 * the point a person is stuck.
 *
 * Three rules, each a case that would otherwise print something wrong:
 *
 * * **Nothing, not a placeholder, when the id is absent.** `trace_id` is `null`
 *   when the envelope was built outside a request — a CLI command, a worker tick.
 *   An empty label or a `Trace id: null` would be worse than silence.
 * * **Nothing when the cause is not our envelope.** `status: 0` means no response
 *   was received at all, so there is no server line to look up; and a plain
 *   `Error` from a bug in the screen is not an {@link ApiError} and carries no id.
 * * **Muted, and small.** It is for the support conversation, not the fix. The
 *   sentence above it says what went wrong; this says where to look.
 *
 * It is deliberately *not* a `role="alert"`: the failure itself is announced, and
 * a second alert would make a screen reader read one failure twice.
 */
export function TraceId({ error, className }: { error: unknown; className?: string }) {
  const t = useTranslations();

  if (!(error instanceof ApiError) || error.traceId === null) return null;

  return (
    <p className={cn("text-xs text-muted-foreground", className)}>
      {t("common.traceId", { id: error.traceId })}
    </p>
  );
}
