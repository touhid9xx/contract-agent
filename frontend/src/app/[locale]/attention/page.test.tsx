// @vitest-environment jsdom
/**
 * The attention board — the client `D10-79` shipped a route for and nothing called.
 *
 * `test_attention_route.py` proved the route is tenant-scoped through the real ASGI
 * app; it could not prove that any screen reads it, because until now none did. So
 * the rules pinned here are the ones the *page* carries and the route cannot:
 *
 *   * a visitor who is not an administrator **asks the API nothing at all** — the
 *     route is `require_roles(UserRole.ADMIN)`, so an operator's request could only
 *     ever return a `403` that a retry cannot clear. `enabled: isAdmin` keeps that
 *     refusal off the wire, and this file asserts the *null hypothesis*: zero calls.
 *   * `needs_attention` is read from the wire rather than recomputed from the three
 *     counts, so a count the server adds later leaves this screen's verdict correct
 *     and a local `> 0` would not. The test for that hands the page a board whose
 *     counts are non-zero and whose flag is `false`.
 *   * `field_scan_truncated` travels beside the count, because that count is a
 *     floor when the scan filled its page; a screen printing the number alone would
 *     understate a deployment with more rows than one page.
 *   * the ids are ids and nothing else, and they lead to the review queue, which is
 *     the screen that can actually seal a value.
 *   * it stays read-only: every control that looks like a button is a link.
 *
 * The harness is the one `escalations/page.test.tsx` established, and it is
 * deliberately unchanged: a real `QueryClient` per test with `retry: false` (the
 * questions here are "how often" and "at all"), the real `NextIntlClientProvider`
 * and real catalogue so assertions are against text a person reads, the real
 * `AuthProvider` and real session store so role gating comes from a session rather
 * than a prop, and only `attention.read` replaced — with `importOriginal` keeping
 * the real `ApiError`, because the page branches on `instanceof` and on `isForbidden`
 * / `isOffline`.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    attention: { read: vi.fn() },
  };
});

import AttentionPage from "@/app/[locale]/attention/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, attention } from "@/lib/api";
import type { AttentionReport, Session, UserRole } from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const ACCESS = "access-1";

/** Substitution for a plain (non-plural) ICU string. */
function say(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => String(values[key]));
}

const CLEAN: AttentionReport = {
  notifications_failed: 0,
  notifications_suppressed: 0,
  unsealed_field_values: 0,
  unsealed_field_ids: [],
  fields_scanned: 0,
  field_scan_truncated: false,
  needs_attention: false,
};

function board(overrides: Partial<AttentionReport> = {}): AttentionReport {
  return { ...CLEAN, ...overrides };
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
  // question — the same reason the escalations harness does it.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
        <AuthProvider>
          <AttentionPage />
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

beforeEach(() => {
  setSession(null);
  vi.mocked(attention.read).mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("who may read the board", () => {
  it("asks a signed-out visitor to sign in, and asks the API nothing", () => {
    renderScreen();

    expect(screen.getByText(EN.attention.signInRequired)).toBeDefined();
    // `authorized` refuses before a request is built, so nothing leaves the tab.
    expect(attention.read).not.toHaveBeenCalled();
    // A refusal is not a board failure: reporting one would say the board is broken.
    expect(alertTexts()).toStrictEqual([]);
    expect(hrefOf(EN.attention.signIn)).toBe("/en/login");
  });

  for (const role of ["operator", "viewer"] as const) {
    it(`tells the ${role} role why, and still asks the API nothing`, () => {
      setSession(sessionFor(role));
      renderScreen();

      expect(screen.getByText(EN.attention.forbidden)).toBeDefined();
      // The hint is the half that explains the board is not about this role's work.
      expect(screen.getByText(EN.attention.forbiddenHint)).toBeDefined();
      // This is the assertion the route's own tests cannot make: a 403 that a
      // retry cannot clear is not worth putting on the wire at all.
      expect(attention.read).not.toHaveBeenCalled();
      expect(alertTexts()).toStrictEqual([]);
      // No retry offered for a permission the role will not gain by trying again.
      expect(screen.queryByRole("button", { name: EN.attention.retry })).toBeNull();
    });
  }
});

describe("a clean board is an answer, not an empty state", () => {
  it("reads one board with the caller's own token and says nothing needs a person", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(board());
    renderScreen();

    expect(await screen.findByText(EN.attention.cleanHeading)).toBeDefined();
    expect(screen.getByText(EN.attention.cleanBody)).toBeDefined();
    // One read, addressed by the session's token and by nothing else.
    expect(vi.mocked(attention.read).mock.calls[0]).toStrictEqual([ACCESS]);

    // Three count paragraphs at zero, plus a scanned line that is one string.
    expect(screen.getAllByText("0")).toHaveLength(3);
    expect(screen.getByText(say(EN.attention.scanned, { count: 0 }))).toBeDefined();
    expect(screen.getByText(EN.attention.unsealedNone)).toBeDefined();
    // Nothing filled the scan's page, so the floor warning stays off.
    expect(screen.queryByText(EN.attention.truncated)).toBeNull();
  });

  it("trusts the wire's verdict over the counts it was handed", async () => {
    // The rule from the source: a server that adds a fourth count would leave this
    // screen's `needs_attention` branch correct and a local `> 0` wrong. A board
    // that is non-zero yet flagged calm must therefore render as calm.
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(
      board({ notifications_failed: 3, needs_attention: false }),
    );
    renderScreen();

    expect(await screen.findByText(EN.attention.cleanHeading)).toBeDefined();
  });

  it("reads the verdict off the wire in Bengali, not English", async () => {
    // The board here is the discriminating one on purpose: counts non-zero with the
    // flag `false`. That is the only fixture that tells a wire-read `needs_attention`
    // apart from a locally recomputed `> 0`, so asking the question in the second
    // locale as well means a mutation to that branch fails twice instead of once.
    // A clean board — what this case used to hand the page — agrees with both
    // readings and could not distinguish them.
    const bn = getMessages("bn");
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(
      board({ notifications_failed: 3, needs_attention: false }),
    );
    renderScreen("bn");

    expect(await screen.findByText(bn.attention.cleanHeading)).toBeDefined();
    expect(screen.getByRole("heading", { name: bn.attention.title })).toBeDefined();
    // A locale switch that fell back to the English catalogue would still render a
    // heading, so the calm sentence is the one that has to be the Bengali one.
    expect(screen.queryByText(EN.attention.cleanHeading)).toBeNull();
  });
});

describe("the cards a finding gets", () => {
  const FINDING = board({
    notifications_failed: 2,
    notifications_suppressed: 1,
    unsealed_field_values: 3,
    unsealed_field_ids: [41, 42, 43],
    fields_scanned: 500,
    needs_attention: true,
  });

  it("shows the three counts and the ids, and no clean heading", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(FINDING);
    renderScreen();

    expect(await screen.findByText(EN.attention.failedHeading)).toBeDefined();
    expect(screen.getByText(EN.attention.suppressedHeading)).toBeDefined();
    expect(screen.getByText(EN.attention.unsealedHeading)).toBeDefined();
    // A calm sentence above three findings would be a contradiction on one page.
    expect(screen.queryByText(EN.attention.cleanHeading)).toBeNull();

    for (const count of ["2", "1", "3"]) {
      expect(screen.getByText(count)).toBeDefined();
    }
    for (const id of FINDING.unsealed_field_ids) {
      expect(screen.getByText(`#${id}`)).toBeDefined();
    }
    expect(screen.getByText(say(EN.attention.scanned, { count: 500 }))).toBeDefined();
  });

  it("points both notification counts at the queue that owns them, and the ids at review", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(FINDING);
    renderScreen();

    // Two separate cards, two links — and both name the queue they lead to. The
    // catalogue happens to give them the same label, so this asserts *both* of
    // them rather than whichever role query came first.
    await waitFor(() => {
      expect(screen.getAllByRole("link", { name: EN.attention.failedAction })).toHaveLength(2);
    });
    const queueLinks = [
      ...screen.getAllByRole("link", { name: EN.attention.failedAction }),
      ...screen.getAllByRole("link", { name: EN.attention.suppressedAction }),
    ];
    expect(queueLinks.map((node) => node.getAttribute("href"))).toStrictEqual([
      "/en/notifications",
      "/en/notifications",
      "/en/notifications",
      "/en/notifications",
    ]);
    // Sealing a value happens in the review queue, so that is where an id leads.
    expect(hrefOf(EN.attention.unsealedAction)).toBe("/en/review");
  });

  it("says the count is a floor only when the scan filled its page", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(
      board({ unsealed_field_values: 1, unsealed_field_ids: [9], field_scan_truncated: true }),
    );
    renderScreen();

    expect(await screen.findByText(EN.attention.truncated)).toBeDefined();
  });

  it("offers no action of its own", async () => {
    // Read-only is a claim the source makes in prose. The cheap way it could become
    // false is a button that retried a send or sealed a value from here, so the
    // assertion is that every button is inside a link — i.e. a navigation.
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(FINDING);
    renderScreen();

    await screen.findByText(EN.attention.failedHeading);
    const buttons = screen.queryAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
    expect(buttons.filter((node) => node.closest("a") === null)).toStrictEqual([]);
    expect(screen.queryAllByRole("textbox")).toStrictEqual([]);
  });
});

describe("a read that fails", () => {
  it("names a forbidden read, and offers a retry that actually re-reads", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read)
      .mockRejectedValueOnce(new ApiError(403, "forbidden", "Nope."))
      .mockResolvedValueOnce(board());
    renderScreen();

    expect(await screen.findByText(EN.attention.forbidden)).toBeDefined();

    fireEvent.click(screen.getByRole("button", { name: EN.attention.retry }));

    expect(await screen.findByText(EN.attention.cleanHeading)).toBeDefined();
    expect(attention.read).toHaveBeenCalledTimes(2);
  });

  it("keeps 'the API is down' distinct from 'the board is broken'", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockRejectedValue(new ApiError(0, "unreachable", "Down."));
    renderScreen();

    expect(await screen.findByText(EN.attention.offline)).toBeDefined();
  });

  it("falls back to a generic sentence for anything else, without leaking prose", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockRejectedValue(new ApiError(500, "server", "Boom."));
    renderScreen();

    expect(await screen.findByText(EN.attention.failed)).toBeDefined();
    expect(screen.queryByText("Boom.")).toBeNull();
    // A failed read is not a calm board: the clean sentence must not appear.
    expect(screen.queryByText(EN.attention.cleanHeading)).toBeNull();
  });

  it("shows no retry once the board has loaded", async () => {
    setSession(sessionFor("admin"));
    vi.mocked(attention.read).mockResolvedValue(board());
    renderScreen();

    await screen.findByText(EN.attention.cleanHeading);
    expect(screen.queryByRole("button", { name: EN.attention.retry })).toBeNull();
  });
});
