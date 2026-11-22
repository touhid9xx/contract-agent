// @vitest-environment jsdom
/**
 * The portfolio dashboard — the screen `D10-137` added a counted route for.
 *
 * `test_dashboard.py` proves the route counts tenant rows rather than page lengths.
 * What it cannot prove is that this *page* keeps that distinction visible, so the
 * rules pinned here are the ones the screen carries:
 *
 *   * a signed-out visitor **asks the API nothing** — the only answer a request could
 *     bring is the `401` the card already describes, and firing it would render an
 *     outage where there is merely no session.
 *   * `contracts_total` is printed as the server's figure, and the one number this
 *     page counts itself — the length of `recent_contracts` — is labelled as rows
 *     returned and never printed as a bare figure. A screen that printed both the
 *     same way would be the page-length mistake the route exists to end.
 *   * `escalations_overdue` is said **as a subset of** `escalations_open`, in the same
 *     sentence. A late escalation is an open one, so a second labelled figure beside
 *     the first invites a reader to add them and count the row twice.
 *   * the breakdown is read from the wire, zero-filled, so a lifecycle state nothing
 *     has reached still prints `0` instead of vanishing.
 *   * the expiring count carries the window the **server** counted in, formatted from
 *     the payload's own date strings.
 *   * the reminder ladder prints in the order the server sent it — loosest first,
 *     because that is the planner's own ladder — and a band nothing falls in still
 *     prints its `0`. No figure is derived by summing the bands: the ladder reaches
 *     further than the expiring window, so a sum of it sits beside that count as
 *     though the two answered the same question.
 *   * an empty portfolio is a heading and a sentence, not an error.
 *
 * The harness is the one `attention/page.test.tsx` established: a real `QueryClient`
 * per test with `retry: false`, the real `NextIntlClientProvider` and real catalogue
 * so assertions are against text a person reads, the real `AuthProvider` and session
 * store, and only `dashboard.read` replaced — with `importOriginal` keeping the real
 * `ApiError`, because the page branches on `instanceof` and on `isOffline`.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    dashboard: { read: vi.fn() },
  };
});

import DashboardPage from "@/app/[locale]/dashboard/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, dashboard } from "@/lib/api";
import type { ContractListItem, DashboardView, Session, UserRole } from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const ACCESS = "access-1";

/** Substitution for a plain (non-plural) ICU string. */
function say(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => String(values[key]));
}

/**
 * The day formatter the page builds, rebuilt here rather than hard-coded.
 *
 * `dateStyle: "medium"` is a locale decision, so a literal "Mar 1, 2026" would pin
 * the ICU version as well as the behaviour. What the test needs to know is that the
 * window sentence carries the *payload's* days, formatted the way the page formats.
 */
function day(locale: Locale, value: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeZone: "UTC" }).format(
    new Date(`${value}T00:00:00Z`),
  );
}

function row(overrides: Partial<ContractListItem> = {}): ContractListItem {
  return {
    id: 9,
    customer_ref: "ACME-9",
    status: "active",
    source: "pdf",
    risk: "low",
    start_date: "2025-03-01",
    end_date: "2026-03-20",
    timezone: "Europe/London",
    sha256: null,
    has_document: true,
    consent_email: true,
    consent_sms: false,
    consent_voice: false,
    do_not_contact: false,
    consent_channels: ["email"],
    may_contact: true,
    version: 1,
    created_at: "2026-02-01T09:00:00Z",
    ...overrides,
  };
}

/**
 * A board whose every figure differs from every other one, and which prints no bare
 * `1` or `2` anywhere — that is what lets the two "never a peer figure" assertions
 * below be `queryByText("1")` / `queryByText("2")` rather than something weaker.
 *
 * The breakdown sums to the total on purpose: a fixture that contradicted itself
 * would make a wrong reading of any single count invisible.
 */
function board(overrides: Partial<DashboardView> = {}): DashboardView {
  return {
    today: "2026-03-01",
    window_end: "2026-03-31",
    contracts_total: 1265,
    contracts_by_status: {
      active: 1200,
      reminder_sent: 31,
      responded: 30,
      renewed: 4,
      expired: 0,
      escalated: 0,
    },
    contracts_expiring: 7,
    // The planner's own ladder, tightest boundaries from a 2026-03-01 today. The
    // counts are 11 and 6 and a genuine `0` — the last one on purpose, because a band
    // nobody is in is the case a "skip the empty ones" shortcut would hide. None of
    // them is `1`, `2`, `3` or `7`, which the assertions above reserve.
    reminder_windows: [
      { milestone: "T-60", days: 60, start: "2026-04-01", end: "2026-04-30", contracts: 11 },
      { milestone: "T-30", days: 30, start: "2026-03-16", end: "2026-03-31", contracts: 6 },
      { milestone: "T-14", days: 14, start: "2026-03-01", end: "2026-03-15", contracts: 0 },
    ],
    fields_awaiting_review: 5,
    escalations_open: 3,
    escalations_overdue: 1,
    recent_contracts: [
      row({ id: 9, customer_ref: "ACME-9", status: "active", end_date: "2026-03-20" }),
      row({ id: 8, customer_ref: "ACME-8", status: "expired", end_date: null }),
    ],
    ...overrides,
  };
}

/** An organisation that has ingested nothing — every count zero, no rows. */
function emptyBoard(): DashboardView {
  return board({
    contracts_total: 0,
    contracts_by_status: {
      active: 0,
      reminder_sent: 0,
      responded: 0,
      renewed: 0,
      expired: 0,
      escalated: 0,
    },
    contracts_expiring: 0,
    // The ladder itself survives an empty portfolio — the planner still runs those
    // milestones — so the bands are re-counted rather than emptied. A screen that hid
    // them at zero would make "the ladder exists and nothing is due" read as "this
    // deployment has no ladder".
    reminder_windows: board().reminder_windows.map((band) => ({ ...band, contracts: 0 })),
    fields_awaiting_review: 0,
    escalations_open: 0,
    escalations_overdue: 0,
    recent_contracts: [],
  });
}

function sessionFor(role: UserRole): Session {
  return {
    access_token: ACCESS,
    token_type: "bearer",
    expires_in: 900,
    expires_at: "2026-11-04T12:15:00Z",
    refresh_token: "refresh-1",
    refresh_expires_at: "2026-12-04T12:00:00Z",
    user: {
      id: "u-1",
      email: "operator@example.test",
      role,
      tenant_id: "t-1",
      tenant_slug: "acme",
      is_active: true,
    },
  };
}

function renderScreen(locale: Locale = "en") {
  // Built per test rather than reused, so one test's cache cannot answer another's
  // question — the same reason the attention and escalations harnesses do it.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
        <AuthProvider>
          <DashboardPage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

function hrefOf(name: string): string | null {
  return screen.getByRole("link", { name }).getAttribute("href");
}

/**
 * The `<dl>` a caption introduces, rather than "the first `<dl>` on the page".
 *
 * There are two definition lists here now — the lifecycle breakdown and the reminder
 * ladder — so a positional lookup would have quietly started reading the wrong card
 * the day one was inserted above the other. Anchoring on the caption's own text keeps
 * each helper on the card it names, and fails loudly (returns `null`) if the markup
 * stops putting the list beside its caption.
 */
function listUnder(caption: string): HTMLElement | null {
  const next = screen.getByText(caption).nextElementSibling;
  return next instanceof HTMLElement && next.tagName === "DL" ? next : null;
}

/** The figure printed beside one lifecycle state's label. */
function countFor(label: string): string | null {
  const list = listUnder(EN.dashboard.breakdownLabel);
  if (list === null) return null;
  const term = within(list).getByText(label);
  const row = term.closest("div");
  return row === null ? null : (row.querySelector("dd")?.textContent ?? null);
}

/**
 * The reminder ladder as the page printed it, in document order.
 *
 * Order is the assertion, not an incidental of the lookup: the bands arrive loosest
 * first because that is the order the planner checks them in, and re-sorting them on
 * the screen would be a second opinion about which reminder fires first.
 */
function ladderRows(): { label: string; count: string }[] {
  const list = listUnder(EN.dashboard.reminderBody);
  if (list === null) return [];
  return [...list.querySelectorAll("div")].map((band) => ({
    label: band.querySelector("dt")?.textContent ?? "",
    count: band.querySelector("dd")?.textContent ?? "",
  }));
}

beforeEach(() => {
  setSession(null);
  vi.mocked(dashboard.read).mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("who may read the portfolio", () => {
  it("asks a signed-out visitor to sign in, and asks the API nothing", () => {
    renderScreen();

    expect(screen.getByText(EN.dashboard.signInRequired)).toBeDefined();
    expect(dashboard.read).not.toHaveBeenCalled();
    // No session is not an outage, and saying so would blame the server for it.
    expect(alertTexts()).toStrictEqual([]);
    expect(hrefOf(EN.dashboard.signIn)).toBe("/en/login");
  });

  it("shows a viewer the same board, because the payload is rows they can already list", async () => {
    // `D10-137` chose no role gate, unlike the attention board: nothing here is
    // derived from a row an operator cannot see. So a viewer must read the totals,
    // not a forbidden card — and the read has to actually be on the wire.
    setSession(sessionFor("viewer"));
    vi.mocked(dashboard.read).mockResolvedValue(board());
    renderScreen();

    // Awaited, because the first paint is the loading card: asserting before the
    // read settles would pass on a screen that never rendered a figure at all.
    expect(
      await screen.findByRole("heading", { name: EN.dashboard.totalsHeading }),
    ).toBeDefined();
    expect(countFor(EN.contracts.statuses.active)).toBe("1200");
    expect(dashboard.read).toHaveBeenCalledTimes(1);
    expect(vi.mocked(dashboard.read).mock.calls[0]).toStrictEqual([ACCESS]);
  });
});

describe("a total is the server's figure, and a window says so", () => {
  it("prints the counted total, and never the length of the recent slice as a figure", async () => {
    // The contract of the route: `1265` is what a `COUNT` returned. The page also
    // holds `2` recent rows, and the mistake this screen was built to avoid is
    // printing that `2` where a reader would take it for activity. So the note says
    // "rows this response carried", and no bare `2` appears anywhere.
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(board());
    renderScreen();

    expect(await screen.findByText(EN.dashboard.totalBody)).toBeDefined();
    expect(screen.getByText("1265")).toBeDefined();
    expect(
      screen.getByText(say(EN.dashboard.recentNote, { count: board().recent_contracts.length })),
    ).toBeDefined();
    expect(screen.queryByText("2")).toBeNull();
  });

  it("says the overdue count as a subset of the open one, never as a second figure", async () => {
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(board());
    renderScreen();

    expect(await screen.findByText(EN.dashboard.escalationsBody)).toBeDefined();
    // `3` is the queue's own total and is printed as a figure.
    expect(screen.getByText("3")).toBeDefined();
    // `1` is the subset, and it exists only inside the sentence. A card that gave it
    // its own paragraph would let a reader add the two and count a late escalation
    // twice — the exact error the route's docstring warns about.
    expect(screen.queryByText("1")).toBeNull();
    expect(
      screen.getByText(say(EN.dashboard.escalationsOverdue, { overdue: 1, open: 3 })),
    ).toBeDefined();
  });

  it("labels the expiring count with the window the server counted, in UTC", async () => {
    // The route counts UTC calendar days and returns which ones, because a
    // contract's own timezone is a per-row value a `COUNT` cannot resolve. A page
    // that formatted its own month here would label the figure with a window nobody
    // counted, so both boundaries are asserted from the payload's strings.
    const view = board();
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(view);
    renderScreen();

    expect(await screen.findByText(EN.dashboard.expiringBody)).toBeDefined();
    expect(screen.getByText("7")).toBeDefined();
    expect(
      screen.getByText(
        say(EN.dashboard.windowNote, {
          start: day("en", view.today),
          end: day("en", view.window_end),
        }),
      ),
    ).toBeDefined();
  });
});

describe("the breakdown reads the wire", () => {
  it("lists every lifecycle state, and prints `0` for the ones nothing has reached", async () => {
    // Zero-filled on the server from its own enum. A screen that skipped absent keys
    // could not answer "has anything got as far as renewal?" — and `expired`/
    // `escalated` at `0` is the answer to the question this product is about.
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(board());
    renderScreen();

    expect(await screen.findByText(EN.dashboard.breakdownLabel)).toBeDefined();
    expect(countFor(EN.contracts.statuses.active)).toBe("1200");
    expect(countFor(EN.contracts.statuses.reminder_sent)).toBe("31");
    expect(countFor(EN.contracts.statuses.responded)).toBe("30");
    expect(countFor(EN.contracts.statuses.renewed)).toBe("4");
    expect(countFor(EN.contracts.statuses.expired)).toBe("0");
    expect(countFor(EN.contracts.statuses.escalated)).toBe("0");
  });
});

describe("the reminder ladder is a partition, printed as one", () => {
  it("prints every band in the planner's order, with the days the server counted", async () => {
    // The bands are the policy's own ladder (`DEFAULT_MILESTONE_DAYS`), not a list the
    // screen keeps, so the assertion is that each band arrives with the name, the days
    // and the *dates it counted* — a reader has to be able to see what `T-60` means
    // without knowing the policy. Order is asserted because the bands do not overlap:
    // loosest first is the planner's order and re-sorting is a different claim.
    const view = board();
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(view);
    renderScreen();

    expect(await screen.findByText(EN.dashboard.reminderBody)).toBeDefined();
    expect(ladderRows()).toStrictEqual(
      view.reminder_windows.map((band) => ({
        label: say(EN.dashboard.reminderBand, {
          milestone: band.milestone,
          days: band.days,
          start: day("en", band.start),
          end: day("en", band.end),
        }),
        count: String(band.contracts),
      })),
    );
    // The tightest band holds nobody in this fixture, and its `0` still printed —
    // which is what the assertion above it just proved by expecting `"0"` in the list.
    expect(ladderRows().map((band) => band.count)).toContain("0");
  });

  it("derives no total from the bands, and says why they are not comparable", async () => {
    // 11 + 6 + 0 is 17, and `17` must appear nowhere: a sum of the ladder is a figure
    // about the ladder, and it would sit beside the expiring count (which stops at 30
    // days) as though the two answered the same question. The warning that they do not
    // travels with the panel instead.
    const view = board();
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(view);
    renderScreen();

    expect(await screen.findByText(EN.dashboard.reminderNote)).toBeDefined();
    const summed = view.reminder_windows.reduce((total, band) => total + band.contracts, 0);
    expect(summed).toBeGreaterThan(0);
    expect(screen.queryByText(String(summed))).toBeNull();
  });
});

describe("an empty portfolio is an answer", () => {
  it("says nothing has been ingested, and reports no failure", async () => {
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(emptyBoard());
    renderScreen();

    expect(await screen.findByText(EN.dashboard.emptyHeading)).toBeDefined();
    expect(screen.getByText(EN.dashboard.emptyBody)).toBeDefined();
    // Nothing ending in the window is said as an answer too, not as a missing panel.
    expect(screen.getByText(EN.dashboard.expiringZero)).toBeDefined();
    expect(screen.queryByText(EN.dashboard.expiringBody)).toBeNull();
    expect(screen.getByText(EN.dashboard.recentEmpty)).toBeDefined();
    // A tenant with no rows got a `200`; an alert here would invent a fault.
    expect(alertTexts()).toStrictEqual([]);
  });

  it("shows no empty heading once the portfolio holds anything", async () => {
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(board());
    renderScreen();

    await screen.findByText(EN.dashboard.totalBody);
    expect(screen.queryByText(EN.dashboard.emptyHeading)).toBeNull();
  });
});

describe("a read that fails", () => {
  it("keeps 'the API is down' distinct from 'the portfolio could not be counted'", async () => {
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockRejectedValue(new ApiError(0, "unreachable", "Down."));
    renderScreen();

    expect(await screen.findByText(EN.dashboard.offline)).toBeDefined();
    expect(screen.queryByText(EN.dashboard.failed)).toBeNull();
  });

  it("falls back to a generic sentence, without leaking the server's prose", async () => {
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockRejectedValue(new ApiError(500, "server", "Boom."));
    renderScreen();

    expect(await screen.findByText(EN.dashboard.failed)).toBeDefined();
    expect(screen.queryByText("Boom.")).toBeNull();
    expect(alertTexts()).toStrictEqual([EN.dashboard.failed]);
    // A failed read is not an empty portfolio: the zero heading must not appear.
    expect(screen.queryByText(EN.dashboard.emptyHeading)).toBeNull();
  });
});

describe("the screen stays read-only", () => {
  it("offers no control of its own — every button is a link to the screen owning the rows", async () => {
    // Read-only is a claim the source makes in prose. The cheap way it could become
    // false is a button that retried a send or sealed a field from here, so the
    // assertion is that every button is inside a link — i.e. a navigation.
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(board());
    renderScreen();

    await screen.findByText(EN.dashboard.totalBody);
    const buttons = screen.queryAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons.filter((node) => node.closest("a") === null)).toStrictEqual([]);
    expect(screen.queryAllByRole("textbox")).toStrictEqual([]);

    expect(hrefOf(EN.dashboard.reviewAction)).toBe("/en/review");
    expect(hrefOf(EN.dashboard.escalationsAction)).toBe("/en/escalations");
    // Both contracts cards — the totals and the recent list — carry the same label
    // and the same destination, so the assertion is over the pair: one card losing
    // its link would shorten this list rather than hide behind a `getByRole`.
    expect(
      screen
        .getAllByRole("link", { name: EN.dashboard.totalsAction })
        .map((node) => node.getAttribute("href")),
    ).toStrictEqual(["/en/contracts", "/en/contracts"]);
  });
});

describe("a second locale", () => {
  it("counts in Bengali on a Bengali page, with the payload's own window", async () => {
    // Provisional: the same board read in the second catalogue, so a locale switch
    // that silently fell back to English fails here rather than in a screenshot.
    const bn = getMessages("bn");
    const view = board();
    setSession(sessionFor("operator"));
    vi.mocked(dashboard.read).mockResolvedValue(view);
    renderScreen("bn");

    expect(await screen.findByText(bn.dashboard.totalBody)).toBeDefined();
    expect(screen.getByRole("heading", { name: bn.dashboard.title })).toBeDefined();
    expect(
      screen.getByText(
        say(bn.dashboard.windowNote, {
          start: day("bn", view.today),
          end: day("bn", view.window_end),
        }),
      ),
    ).toBeDefined();
    // The headings are what a fallback to the English catalogue would leave in place.
    expect(screen.queryByText(EN.dashboard.totalBody)).toBeNull();
  });
});
