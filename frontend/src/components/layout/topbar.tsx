"use client";

import { useTranslations } from "next-intl";

import { Menu } from "lucide-react";
import { Sidebar } from "./sidebar";
import { ModeToggle } from "./mode-toggle";
import { LangSwitcher } from "./lang-switcher";
import { useState } from "react";
import { Sheet, SheetContent, SheetTrigger } from "../ui/sheet";
import { Button } from "../ui/button";

export function Topbar() {
  const t = useTranslations("common");
  const [open, setOpen] = useState(false);

  return (
    <header className="flex h-14 items-center justify-between border-b px-4">
      <div className="flex items-center gap-2">
        {/* Mobile drawer */}
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetTrigger
            render={
              <Button variant="ghost" size="icon" className="md:hidden" aria-label="Open menu">
                <Menu className="h-5 w-5" />
              </Button>
            }
          />
          <SheetContent side="left" className="w-64 p-0">
            <Sidebar onNavigate={() => setOpen(false)} />
          </SheetContent>
        </Sheet>
        <span className="font-semibold">{t("appName")}</span>
      </div>

      <div className="flex items-center gap-1">
        <LangSwitcher />
        <ModeToggle />
      </div>
    </header>
  );
}
