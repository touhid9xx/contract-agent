"use client";

import {
  AlertTriangle,
  ClipboardCheck,
  FileText,
  LayoutDashboard,
  Menu,
  Moon,
  Search,
  Send,
  Sun,
  Workflow,
  X,
} from "lucide-react";
import { useTheme } from "next-themes";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { MockedBadge } from "@/components/ui/badge";
import { useUiStore } from "@/lib/store";
import { cn } from "@/lib/utils";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard, shortcut: "g d" },
  { href: "/contracts", label: "Contracts", icon: FileText, shortcut: "g c" },
  { href: "/review", label: "Review", icon: ClipboardCheck },
  { href: "/escalations", label: "Escalations", icon: AlertTriangle },
  { href: "/notifications", label: "Notifications", icon: Send },
] as const;

const SHORTCUTS = [
  { keys: "g then d", description: "Go to dashboard" },
  { keys: "g then c", description: "Go to contracts" },
  { keys: "/", description: "Focus the contract search" },
  { keys: "?", description: "Show this shortcut help" },
  { keys: "Esc", description: "Close dialog / clear focus" },
] as const;

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const sidebarOpen = useUiStore((state) => state.sidebarOpen);
  const setSidebarOpen = useUiStore((state) => state.setSidebarOpen);
  const shortcutsOpen = useUiStore((state) => state.shortcutsOpen);
  const setShortcutsOpen = useUiStore((state) => state.setShortcutsOpen);
  const searchRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");

  // Global keyboard shortcuts (shell behaviours — F6 makes these real).
  useEffect(() => {
    let lastKey = "";
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);

      if (event.key === "Escape") {
        setShortcutsOpen(false);
        (target as HTMLElement | null)?.blur?.();
        return;
      }
      if (typing) return;

      if (event.key === "/") {
        event.preventDefault();
        searchRef.current?.focus();
        return;
      }
      if (event.key === "?") {
        event.preventDefault();
        setShortcutsOpen(true);
        return;
      }
      if (lastKey === "g" && event.key === "d") {
        lastKey = "";
        router.push("/dashboard");
        return;
      }
      if (lastKey === "g" && event.key === "c") {
        lastKey = "";
        router.push("/contracts");
        return;
      }
      lastKey = event.key;
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [router, setShortcutsOpen]);

  // Close the mobile drawer whenever the route changes.
  useEffect(() => {
    setSidebarOpen(false);
  }, [pathname, setSidebarOpen]);

  const submitSearch = (event: React.FormEvent) => {
    event.preventDefault();
    const trimmed = query.trim();
    router.push(trimmed ? `/contracts?q=${encodeURIComponent(trimmed)}` : "/contracts");
  };

  return (
    <div className="flex min-h-screen">
      {sidebarOpen ? (
        <div
          className="fixed inset-0 z-30 bg-black/40 lg:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden
        />
      ) : null}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-40 flex w-60 flex-col border-r bg-card transition-transform lg:static lg:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
        aria-label="Primary navigation"
      >
        <div className="flex h-14 items-center gap-2 border-b px-4">
          <Workflow className="h-5 w-5 text-primary" aria-hidden />
          <span className="text-sm font-semibold tracking-tight">ContractExpiry</span>
          <button
            type="button"
            className="ml-auto rounded p-1 hover:bg-muted lg:hidden"
            onClick={() => setSidebarOpen(false)}
            aria-label="Close navigation"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>

        <nav className="flex-1 space-y-1 p-3">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
                  active ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
                )}
              >
                <Icon className="h-4 w-4" aria-hidden />
                {item.label}
                {"shortcut" in item ? (
                  <kbd className="ml-auto hidden rounded border px-1 text-[10px] text-muted-foreground lg:inline">
                    {item.shortcut}
                  </kbd>
                ) : null}
              </Link>
            );
          })}
          <Link
            href="/"
            className={cn(
              "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
              pathname === "/" ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            <Workflow className="h-4 w-4" aria-hidden />
            System flow
          </Link>
        </nav>

        <div className="space-y-2 border-t p-3">
          <MockedBadge />
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            Phase 0 demo · all data is fictional fixture data.
          </p>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex h-14 items-center gap-2 border-b bg-background/90 px-3 backdrop-blur sm:px-4">
          <button
            type="button"
            className="rounded p-1.5 hover:bg-muted lg:hidden"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
          >
            <Menu className="h-5 w-5" aria-hidden />
          </button>

          <form onSubmit={submitSearch} className="relative flex-1 sm:max-w-md">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <input
              ref={searchRef}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search contracts…  (press /)"
              aria-label="Search contracts"
              className="h-9 w-full rounded-md border bg-input pl-8 pr-3 text-sm outline-none focus-visible:border-ring"
            />
          </form>

          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => setShortcutsOpen(true)}
              className="hidden rounded border px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted sm:inline"
              aria-label="Show keyboard shortcuts"
            >
              ?
            </button>
            <ThemeToggle />
            <div className="flex items-center gap-2 rounded-full border px-2 py-1">
              <span className="grid h-5 w-5 place-items-center rounded-full bg-accent text-[10px] font-semibold text-accent-foreground">
                DA
              </span>
              <span className="hidden text-xs text-muted-foreground sm:inline">admin@acme.test</span>
            </div>
          </div>
        </header>

        <main className="mx-auto w-full max-w-7xl flex-1 px-3 py-5 sm:px-5">{children}</main>
      </div>

      {shortcutsOpen ? <ShortcutsDialog onClose={() => setShortcutsOpen(false)} /> : null}
    </div>
  );
}

function ThemeToggle() {
  const { resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const isDark = mounted && resolvedTheme === "dark";

  return (
    <button
      type="button"
      onClick={() => setTheme(isDark ? "light" : "dark")}
      className="rounded p-1.5 hover:bg-muted"
      aria-label={isDark ? "Switch to light theme" : "Switch to dark theme"}
    >
      {isDark ? <Sun className="h-4 w-4" aria-hidden /> : <Moon className="h-4 w-4" aria-hidden />}
    </button>
  );
}

function ShortcutsDialog({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Keyboard shortcuts"
        className="w-full max-w-sm card-surface p-5"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold">Keyboard shortcuts</h2>
          <button type="button" onClick={onClose} aria-label="Close shortcuts" className="rounded p-1 hover:bg-muted">
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <ul className="space-y-2">
          {SHORTCUTS.map((shortcut) => (
            <li key={shortcut.keys} className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{shortcut.description}</span>
              <kbd className="rounded border px-1.5 py-0.5 text-xs">{shortcut.keys}</kbd>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
