import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Contract Agent",
  description: "AI Contract-Expiry Notification Agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    // suppressHydrationWarning is MANDATORY for next-themes
    children
  );
}
