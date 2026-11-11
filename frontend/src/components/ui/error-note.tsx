"use client";

import { TraceId } from "@/components/ui/trace-id";
import { cn } from "@/lib/utils";

/**
 * A refused write, said in the screen's own words, with the server's id beside it.
 *
 * `<TraceId>` arrived in `D10-83` for the reads that fail — a list, a queue, a board.
 * Every *write* that fails had the same gap one step further out: the mutation holds
 * an `ApiError` with a `traceId`, and the card that renders the refusal was handed
 * only `describeFailure(cause)`, a finished sentence. The cause was flattened at the
 * prop boundary, so by the time a card rendered, the id existed nowhere.
 *
 * The fix is to stop flattening: a card receives the cause, and this component turns
 * one cause into the two things a person needs — what happened, and where to look.
 * Taking `describe` as a callback rather than a finished string is what lets the
 * screen keep its own wording (each route refuses differently) while the id still
 * travels with it: both come from the same `cause`, so a sentence can never be
 * printed beside another request's id.
 *
 * The sentence is the `role="alert"`; the id is not — see {@link TraceId}.
 */
export function ErrorNote({
  cause,
  describe,
  className,
}: {
  cause: unknown;
  /** The screen's own wording for a failure, already translated. */
  describe: (cause: unknown) => string;
  /** For the places whose refusal is plain text rather than a tinted box. */
  className?: string;
}) {
  return (
    <div className={cn("rounded-lg bg-danger-muted px-3 py-2 text-xs text-danger", className)}>
      <p role="alert">{describe(cause)}</p>
      <TraceId error={cause} className="mt-0.5" />
    </div>
  );
}
