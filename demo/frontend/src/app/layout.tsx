import type { Metadata } from "next";

import { AppShell } from "@/components/app-shell";

import "./globals.css";
import { Providers } from "./providers";

export const metadata: Metadata = {
  title: "Contract Expiry Agent — MOCKED demo",
  description:
    "Phase 0 interactive demo of the Contract-Expiry Notification Agent. Every screen and number is mocked fixture data.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        <Providers>
          <AppShell>{children}</AppShell>
        </Providers>
      </body>
    </html>
  );
}
