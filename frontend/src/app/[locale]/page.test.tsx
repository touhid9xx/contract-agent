// @vitest-environment jsdom
/**
 * The home screen — the ninth screen under test, and the last one that had none.
 *
 * The eight before it each pinned the seam a screen is judged by: `login` that a
 * session can be obtained, `review` that a queue can be worked, `escalations`
 * that a filter can live in the address, `agent` that a refusal can carry the
 * answer, `notifications` that one action may ask before it acts, `contracts`
 * that a form can own two transports, the contract detail that five reads fail
 * independently, and `users` that a lifecycle can be a seam. This screen is the
 * only one that carries none of those and still matters:
 *
 *   * **it asks the API one question and no other.** `/health`, and nothing
 *     else. Every other screen is a reader or a writer of tenant data; this one
 *     must render for a visitor whose API is down, so the liveness line is a
 *     separate probe that touches no database and every statement it makes is
 *     about the process, never about the data.
 *   * **its two sign-out buttons fail differently, on purpose.** `signOut` ends
 *     the session locally *before* it tells the server, and swallows the
 *     revocation's failure: the token expires on its own and the user cannot act
 *     on a refusal, so a failed sign-out must never look broken. `signOutEverywhere`
 *     is the remedy for a suspected leak — its refusal is the one thing the user
 *     must be told, because it means the sessions they cannot see may still be
 *     open. Both rules live in `auth-provider.tsx`; the *screen* is what decides
 *     the second one is visible and the first is not, and that is what is pinned
 *     here.
 *   * **it links every gated screen without asking to be signed in.** Hiding the
 *     links from a visitor would leave them no way to learn the screens exist,
 *     and each screen asks for a session itself. The `users` link is not
 *     role-gated for the same reason: the screen says for itself that only an
 *     admin may use it.
 *   * **the locale switcher is a page load, not a route push** — the locale
 *     segment has to be re-resolved by the server — and it rewrites only the
 *     first segment, so a link to a screen survives the switch.
 *
 * The harness is the one the earlier screen specs established, with one addition:
 *
 *   * `@/lib/api` is doubled through `importOriginal`, so the real `ApiError`
 *     survives `instanceof` and the screen's error path is the screen's;
 *   * the real `NextIntlClientProvider` and the real catalogue, so every
 *     assertion is against the sentence a user reads — and `bn` is used where
 *     the point is that two locales say *different* things;
 *   * the real `AuthProvider` and the real session store, so "signed out" and
 *     "signed in" are produced by the session rather than passed in as props;
 *   * no `QueryClientProvider`: this screen performs no query. That is a fact
 *     about the source, and supplying one here would hide a screen that started
 *     reading tenant data without saying so.
 *   * `next/navigation` is doubled for `usePathname` only — there is no Next
 *     runtime in jsdom — and `next-themes` is doubled because the screen reads
 *     `theme` and calls `setTheme` and does nothing else with either. The theme
 *     chips are the only place in this app that touches `next-themes` from a
 *     screen, and its provider writes classes to `<html>`: asserting that a chip
 *     *asked* for a theme is the screen's contract, while asserting the document
 *     changed colour would be `next-themes`' promise, not this page's.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

/**
 * The three mutable facts the doubles read.
 *
 * `pathname` and `theme` are written per test *before* rendering, because both
 * are read during render; `setTheme` is a spy because the only honest question
 * to ask of a theme chip is which name it sent.
 */
const { pathname, theme, setTheme } = vi.hoisted(() => ({
  pathname: { current: "/en" },
  theme: { current: "system" as string },
  setTheme: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => pathname.current,
}));

vi.mock("next-themes", () => ({
  useTheme: () => ({ theme: theme.current, setTheme }),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    probeHealth: vi.fn(),
    auth: {
      login: vi.fn(),
      refresh: vi.fn(),
      logout: vi.fn(),
      logoutAll: vi.fn(),
      me: vi.fn(),
    },
  };
});

import HomePage from "@/app/[locale]/page";
import { AuthProvider } from "@/components/auth-provider";
import { localeLabels, locales, type Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { auth, probeHealth } from "@/lib/api";
import type { Session, UserRole, UserSummary } from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const BN = getMessages("bn");
const ACCESS = "access-1";
const REFRESH = "refresh-1";

/** The signed-in account, as the session reports it. */
const SELF: UserSummary = {
  id: "u-1",
  email: "admin@example.test",
  role: "admin",
  tenant_id: "t-1",
  tenant_slug: "acme",
  is_active: true,
};

function sessionFor(role: UserRole = "admin"): Session {
  return {
    access_token: ACCESS,
    token_type: "bearer",
    expires_in: 900,
    expires_at: "2026-11-04T12:15:00Z",
    refresh_token: REFRESH,
    refresh_expires_at: "2026-12-04T12:00:00Z",
    user: { ...SELF, role },
  };
}

function signedIn(role: UserRole = "admin") {
  setSession(sessionFor(role));
}

function renderScreen(locale: Locale = "en") {
  return render(
    <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
      <AuthProvider>
        <HomePage />
      </AuthProvider>
    </NextIntlClientProvider>,
  );
}

/** Every `href` on the page, in document order. */
function hrefs(): (string | null)[] {
  return screen.getAllByRole("link").map((node) => node.getAttribute("href"));
}

/**
 * Replace `window.location` with an object that records where the page sent it.
 *
 * jsdom's `Location` members are `[LegacyUnforgeable]`, so `assign` cannot be
 * spied on in place — the whole object is swapped instead and the original
 * descriptor is restored in `afterEach`. Restoring matters: leaving a stub in
 * place would make every later test in this file believe navigation is free.
 */
function stubLocation(): ReturnType<typeof vi.fn> {
  const assign = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true,
    writable: true,
    value: { assign },
  });
  return assign;
}

const LOCATION_DESCRIPTOR = Object.getOwnPropertyDescriptor(window, "location");

/** The one button whose visible label is exactly this. */
function button(name: string): HTMLButtonElement {
  return screen.getByRole("button", { name }) as HTMLButtonElement;
}

beforeEach(() => {
  setSession(null);
  pathname.current = "/en";
  theme.current = "system";
  setTheme.mockReset();
  vi.mocked(probeHealth).mockReset();
  vi.mocked(auth.logout).mockReset();
  vi.mocked(auth.logoutAll).mockReset();

  vi.mocked(probeHealth).mockResolvedValue(true);
  vi.mocked(auth.logout).mockResolvedValue(undefined);
  vi.mocked(auth.logoutAll).mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  setSession(null);
  if (LOCATION_DESCRIPTOR !== undefined) {
    Object.defineProperty(window, "location", LOCATION_DESCRIPTOR);
  }
});

describe("the header", () => {
  it("names the app and what phase it is in", async () => {
    renderScreen();

    await screen.findByText(EN.home.healthOk);
    expect(screen.getByRole("heading", { name: EN.app.name })).toBeTruthy();
    expect(screen.getByText(EN.app.tagline)).toBeTruthy();
    expect(screen.getByText(EN.home.phaseLabel)).toBeTruthy();
  });

  it("reads the liveness line as reachable when the probe answers", async () => {
    renderScreen();

    // `probeHealth` resolves `true` for a 2xx on `/health`; the line is the whole
    // reason this screen is the one nobody can be locked out of.
    await screen.findByText(EN.home.healthOk);
    // The label and its colon are one text node, so this is a substring match:
    // the heading is rendered as "Backend connectivity:" and the state follows it.
    expect(screen.getByText(EN.home.healthHeading, { exact: false })).toBeTruthy();
    expect(probeHealth).toHaveBeenCalledTimes(1);
  });

  it("reads it as unreachable without failing the rest of the page", async () => {
    vi.mocked(probeHealth).mockResolvedValue(false);
    renderScreen();

    // A dead API must not look like a dead screen: the account card is local
    // state and renders regardless.
    await screen.findByText(EN.home.healthFail);
    expect(screen.getByText(EN.home.signedOut)).toBeTruthy();
    expect(screen.getAllByRole("link").length).toBeGreaterThan(0);
  });

  it("says it is still checking until the probe answers", async () => {
    vi.mocked(probeHealth).mockReturnValue(new Promise<boolean>(() => {}));
    renderScreen();

    expect(screen.getByText(EN.home.healthChecking)).toBeTruthy();
    expect(screen.queryByText(EN.home.healthOk)).toBeNull();
    expect(screen.queryByText(EN.home.healthFail)).toBeNull();
  });
});

describe("the account card when signed out", () => {
  it("offers a sign-in link in the current locale and no sign-out controls", async () => {
    renderScreen();

    await screen.findByText(EN.home.signedOut);
    expect(screen.getByRole("link", { name: EN.home.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );
    expect(screen.queryByRole("button", { name: EN.home.signOut })).toBeNull();
    expect(screen.queryByRole("button", { name: EN.home.signOutEverywhere })).toBeNull();
    expect(screen.queryByText(EN.home.signOutEverywhereHint)).toBeNull();
  });

  it("keeps the sign-in link inside the locale it is rendered in", async () => {
    renderScreen("bn");

    await screen.findByText(BN.home.signedOut);
    expect(screen.getByRole("link", { name: BN.home.signIn }).getAttribute("href")).toBe(
      "/bn/login",
    );
  });

  it("still shows the way to every working screen", async () => {
    renderScreen();

    await screen.findByText(EN.home.signedOut);

    // Shown to a visitor on purpose: each screen asks for a session itself, and
    // hiding the links would leave no way to learn they exist. `contracts` leads
    // because every per-contract route takes an id only it can produce.
    expect(hrefs()).toEqual([
      "/en/login",
      "/en/contracts",
      "/en/review",
      "/en/agent",
      "/en/escalations",
      "/en/notifications",
      "/en/attention",
      "/en/users",
    ]);
  });
});

describe("the account card when signed in", () => {
  it("shows the account the session actually carries", async () => {
    signedIn("operator");
    renderScreen();

    await screen.findByText(SELF.email);
    expect(screen.getByText("operator")).toBeTruthy();
    expect(screen.getByText(SELF.tenant_slug)).toBeTruthy();
    expect(screen.queryByText(EN.home.signedOut)).toBeNull();
    expect(screen.queryByRole("link", { name: EN.home.signIn })).toBeNull();
  });

  it("prints the wire role, not the users screen's translated label", async () => {
    signedIn("admin");
    renderScreen("bn");

    await screen.findByText(SELF.email);

    // The users screen maps `role` through `users.roles`; this card does not.
    // Pinned as it stands so a translation here is a deliberate change and not a
    // silent divergence between the two screens that show the same field.
    expect(screen.getByText("admin")).toBeTruthy();
    expect(screen.queryByText(BN.users.roles.admin)).toBeNull();
  });

  it("offers both sign-out actions and says what the wider one does", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    expect(button(EN.home.signOut).disabled).toBe(false);
    expect(button(EN.home.signOutEverywhere).disabled).toBe(false);
    expect(screen.getByText(EN.home.signOutEverywhereHint)).toBeTruthy();
  });
});

describe("signing out of this session", () => {
  it("forgets the session before the server answers, and names the refresh token", async () => {
    /**
     * The revocation is held open by hand. The screen must already read as signed
     * out: signing out of the UI cannot depend on the network, which is the whole
     * reason the token is read before the store is cleared.
     */
    let release!: () => void;
    vi.mocked(auth.logout).mockReturnValue(
      new Promise<void>((resolve) => {
        release = resolve;
      }),
    );
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    fireEvent.click(button(EN.home.signOut));

    expect(await screen.findByText(EN.home.signedOut)).toBeTruthy();
    expect(auth.logout).toHaveBeenCalledWith(REFRESH);
    expect(screen.queryByText(SELF.email)).toBeNull();

    release();
  });

  it("never reports a refused revocation, because the user cannot act on it", async () => {
    vi.mocked(auth.logout).mockRejectedValue(new Error("relay refused"));
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    fireEvent.click(button(EN.home.signOut));

    // `signOut` swallows it: the local session is gone and the token expires on
    // its own, so a refusal here must not make signing out look broken.
    expect(await screen.findByText(EN.home.signedOut)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("signing out everywhere", () => {
  it("hands the access token to the server and ends the session", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    fireEvent.click(button(EN.home.signOutEverywhere));

    expect(await screen.findByText(EN.home.signedOut)).toBeTruthy();
    expect(auth.logoutAll).toHaveBeenCalledWith(ACCESS);
    // Only the wider action was used: the narrow one would leave other sessions
    // standing, which is the opposite of what the button promises.
    expect(auth.logout).not.toHaveBeenCalled();
  });

  it("reports the failure, which is the one thing the user must know", async () => {
    vi.mocked(auth.logoutAll).mockRejectedValue(new Error("network down"));
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    fireEvent.click(button(EN.home.signOutEverywhere));

    // The screen's own sentence, not the server's: *why* is a detail the user
    // cannot act on, and the failure is reported inside the card it belongs to.
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(EN.home.actionFailed);
  });

  it("drops the local session even when the revocation is refused", async () => {
    vi.mocked(auth.logoutAll).mockRejectedValue(new Error("network down"));
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    fireEvent.click(button(EN.home.signOutEverywhere));

    // `signOutEverywhere` clears the session in a `finally`: the epoch bump either
    // happened or did not, and a token the user has declared compromised must not
    // stay in the UI while it is decided. So the alert and the signed-out copy
    // are both on screen — the failure outlives the session it was about.
    await screen.findByRole("alert");
    expect(screen.getByText(EN.home.signedOut)).toBeTruthy();
    expect(screen.queryByText(SELF.email)).toBeNull();
  });

  it("disables both sign-out buttons while the wider one is in flight", async () => {
    let release!: () => void;
    vi.mocked(auth.logoutAll).mockReturnValue(
      new Promise<void>((resolve) => {
        release = resolve;
      }),
    );
    signedIn();
    renderScreen();

    await screen.findByText(SELF.email);
    fireEvent.click(button(EN.home.signOutEverywhere));

    // The session clears in a `finally`, so both buttons exist during the flight
    // and neither may be pressed: a second click would ask the server to end a
    // session that is already on its way out.
    expect(button(EN.home.signOut).disabled).toBe(true);
    expect(button(EN.home.signOutEverywhere).disabled).toBe(true);

    release();
    expect(await screen.findByText(EN.home.signedOut)).toBeTruthy();
  });
});

describe("the locale switcher", () => {
  it("marks exactly the locale it is rendered in", async () => {
    renderScreen("bn");

    await screen.findByText(BN.home.signedOut);

    const current = locales
      .map((locale) => button(localeLabels[locale]))
      .filter((node) => node.getAttribute("aria-current") === "true");

    expect(current.length).toBe(1);
    expect(current[0]?.textContent).toBe(localeLabels.bn);
    // The other chip is present and unmarked: the attribute is the only handle a
    // screen offers without asserting on Tailwind classes.
    expect(button(localeLabels.en).getAttribute("aria-current")).toBeNull();
  });

  it("switches by loading a page, not by routing inside it", async () => {
    const assign = stubLocation();
    renderScreen();

    await screen.findByText(EN.home.signedOut);
    fireEvent.click(button(localeLabels.bn));

    // The locale segment has to be re-resolved by the server, so this is a full
    // navigation rather than a client-side push.
    expect(assign).toHaveBeenCalledTimes(1);
    expect(assign).toHaveBeenCalledWith("/bn");
  });

  it("keeps the rest of the path, so a screen survives the switch", async () => {
    const assign = stubLocation();
    pathname.current = "/en/notifications";
    renderScreen();

    await screen.findByText(EN.home.signedOut);
    fireEvent.click(button(localeLabels.bn));

    expect(assign).toHaveBeenCalledWith("/bn/notifications");
  });

  it("replaces the leading segment rather than appending to it", async () => {
    const assign = stubLocation();
    pathname.current = "/en/";
    renderScreen();

    await screen.findByText(EN.home.signedOut);
    fireEvent.click(button(localeLabels.bn));

    // A trailing slash (or a doubled one) must not produce `/bn//notifications`;
    // the empty segments are dropped before the first is replaced.
    expect(assign).toHaveBeenCalledWith("/bn");
  });
});

describe("the theme chips", () => {
  it("sends the theme name it was clicked for", async () => {
    renderScreen();

    await screen.findByText(EN.home.signedOut);
    fireEvent.click(button(EN.common.light));
    fireEvent.click(button(EN.common.dark));
    fireEvent.click(button(EN.common.system));

    expect(setTheme.mock.calls).toEqual([["light"], ["dark"], ["system"]]);
  });

  it("marks the current theme, and only that one", async () => {
    theme.current = "dark";
    renderScreen();

    await screen.findByText(EN.home.signedOut);

    // The theme chips carry no `aria-current` (unlike the locale chips); the
    // variant is the only handle, and it is the one the screen offers.
    const marked = [EN.common.light, EN.common.dark, EN.common.system].filter((label) =>
      button(label).className.includes("bg-primary"),
    );

    expect(marked).toEqual([EN.common.dark]);
  });

  it("shows a translated label for each theme in every locale", async () => {
    renderScreen("bn");

    await screen.findByText(BN.home.signedOut);
    for (const label of [BN.common.light, BN.common.dark, BN.common.system]) {
      expect(button(label)).toBeTruthy();
    }
    // The Bengali labels are not the English ones; a missing translation would
    // surface here as a raw key rather than as a chip.
    expect(BN.common.system).not.toBe(EN.common.system);
  });
});
