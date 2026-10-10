"use client";

import { useTranslations } from "next-intl";

import {
  LayoutDashboard,
  FileText,
  ClipboardCheck,
  AlertTriangle,
  Bell,
  Bot,
  ScrollText,
  Settings,
} from "lucide-react";
import { cn } from "cn";
import { Link, usePathname } from "@/i18n/navigation";

const navItems = [
  { href: "/dashboard", labelKey: "dashboard", icon: LayoutDashboard },
  { href: "/contracts", labelKey: "contracts", icon: FileText },
  { href: "/review", labelKey: "review", icon: ClipboardCheck },
  { href: "/escalations", labelKey: "escalations", icon: AlertTriangle },
  { href: "/notifications", labelKey: "notifications", icon: Bell },
  { href: "/agent", labelKey: "agent", icon: Bot },
  { href: "/audit", labelKey: "audit", icon: ScrollText },
  { href: "/settings", labelKey: "settings", icon: Settings },
] as const;

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const t = useTranslations("nav");
  const pathname = usePathname();

  return (
    <nav className="flex h-full flex-col gap-1 p-3" aria-label="Main navigation">
      {navItems.map(({ href, labelKey, icon: Icon }) => {
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <Link
            key={href}
            href={href}
            onClick={onNavigate}
            className={cn(
              "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
              active
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
            )}
            aria-current={active ? "page" : undefined}
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
            {t(labelKey)}
          </Link>
        );
      })}
    </nav>
  );
}
