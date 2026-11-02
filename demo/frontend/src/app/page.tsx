import { ArrowDown, ArrowRight, FileText, ShieldAlert, UserCheck, Workflow } from "lucide-react";
import Link from "next/link";

import { MockedBadge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";

const FLOW = [
  {
    title: "Contract PDF / Company API",
    detail: "Ingested, hashed (sha256), stored once per contract.",
    tone: "border-border",
  },
  {
    title: "Extraction",
    detail: "pdfplumber + regex + spaCy → per-field confidence.",
    tone: "border-border",
  },
  {
    title: "Human review queue",
    detail: "Any field under its threshold blocks the pipeline.",
    tone: "border-warning/50",
  },
  {
    title: "Outbox → Kafka / StrikeMQ",
    detail: "Atomic DB + event write, at-least-once delivery.",
    tone: "border-border",
  },
  {
    title: "Agent decision (Ollama)",
    detail: "Advisory channel ranking only — never authorization.",
    tone: "border-border",
  },
  {
    title: "Notifications: email / sms / voice",
    detail: "Deterministic policy engine gates every send.",
    tone: "border-border",
  },
  {
    title: "Response tracking (2 weeks)",
    detail: "Meaningful reply vs. call answered are tracked separately.",
    tone: "border-border",
  },
  {
    title: "Escalation → human",
    detail: "No response after the window raises an SLA-tracked escalation.",
    tone: "border-danger/50",
  },
] as const;

export default function LandingPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="space-y-3 text-center">
        <div className="flex items-center justify-center gap-2">
          <Workflow className="h-6 w-6 text-primary" aria-hidden />
          <MockedBadge />
        </div>
        <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">Contract Expiry Notification Agent</h1>
        <p className="mx-auto max-w-xl text-sm text-muted-foreground">
          Interactive Phase 0 demo. Every screen, number and event below is simulated fixture data — no database,
          Kafka, Ollama, email, SMS or voice provider is contacted.
        </p>
      </header>

      <Card className="p-4 sm:p-6">
        <h2 className="mb-4 text-sm font-semibold">System flow</h2>
        <ol className="space-y-2">
          {FLOW.map((step, index) => (
            <li key={step.title}>
              <div className={`flex items-start gap-3 rounded-lg border ${step.tone} bg-card px-3 py-2.5`}>
                <span className="mt-0.5 grid h-6 w-6 shrink-0 place-items-center rounded-full bg-muted text-xs font-semibold">
                  {index + 1}
                </span>
                <div className="min-w-0">
                  <p className="text-sm font-medium">{step.title}</p>
                  <p className="text-xs text-muted-foreground">{step.detail}</p>
                </div>
              </div>
              {index < FLOW.length - 1 ? (
                <div className="flex justify-center py-1 text-muted-foreground" aria-hidden>
                  <ArrowDown className="h-3.5 w-3.5" />
                </div>
              ) : null}
            </li>
          ))}
        </ol>
        <p className="mt-4 flex items-center gap-1.5 text-xs text-muted-foreground">
          <ShieldAlert className="h-3.5 w-3.5" aria-hidden />
          Low-confidence fields never auto-enter the notification pipeline.
        </p>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2">
        <Link href="/dashboard" className="group">
          <Card className="flex h-full items-center gap-3 p-4 transition-colors group-hover:border-primary/50">
            <Workflow className="h-5 w-5 text-primary" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">Open the dashboard</p>
              <p className="text-xs text-muted-foreground">Portfolio counts, reminder windows, activity.</p>
            </div>
            <ArrowRight className="h-4 w-4 text-muted-foreground" aria-hidden />
          </Card>
        </Link>
        <Link href="/review" className="group">
          <Card className="flex h-full items-center gap-3 p-4 transition-colors group-hover:border-primary/50">
            <UserCheck className="h-5 w-5 text-warning" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-medium">Open the review queue</p>
              <p className="text-xs text-muted-foreground">Resolve fields blocking the pipeline.</p>
            </div>
            <ArrowRight className="h-4 w-4 text-muted-foreground" aria-hidden />
          </Card>
        </Link>
      </div>

      <Card className="p-4">
        <h2 className="mb-2 flex items-center gap-2 text-sm font-semibold">
          <FileText className="h-4 w-4" aria-hidden />
          Demo walkthrough
        </h2>
        <p className="text-xs text-muted-foreground">
          The six story contracts cover the full lifecycle — fresh upload → low-confidence review → agent email →
          agent SMS → voice + voicemail detection → escalation after two weeks. See{" "}
          <code className="rounded bg-muted px-1 py-0.5">DEMO.md</code> for the screen-to-milestone map.
        </p>
      </Card>
    </div>
  );
}
