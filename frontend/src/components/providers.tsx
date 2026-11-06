"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useState, type ReactNode } from "react";

import { AuthProvider } from "@/components/auth-provider";

/**
 * Server state lives in TanStack Query, UI state in Zustand, theme in
 * next-themes, and the session in memory (see `lib/session.ts`). Mirrors the
 * provider layering approved in the Phase 0 demo.
 *
 * `AuthProvider` sits inside the theme provider so its subtree can render
 * themed controls, and outside the page content so every route — including the
 * login page — resolves the same session.
 */
export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 15_000,
            retry: 1,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );

  return (
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>{children}</AuthProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
