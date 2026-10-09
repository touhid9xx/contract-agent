"use client";

export default function Error({ reset }: { error: Error; reset: () => void }) {
  return (
    <div className="space-y-2">
      <p className="text-destructive">Something went wrong.</p>
      <button onClick={reset} className="text-sm underline">
        Try again
      </button>
    </div>
  );
}
