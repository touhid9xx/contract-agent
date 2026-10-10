import { Sidebar } from "@/components/layout/sidebar";
import { Topbar } from "@/components/layout/topbar";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid min-h-screen grid-rows-[auto_1fr]">
      <Topbar />
      <div className="grid md:grid-cols-[240px_1fr]">
        <aside className="hidden border-r md:block">
          <Sidebar />
        </aside>
        <main className="p-4 md:p-6">{children}</main>
      </div>
    </div>
  );
}
