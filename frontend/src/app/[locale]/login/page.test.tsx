// @vitest-environment jsdom
/**
 * The sign-in screen — the first *screen* under test, and the only one that gates
 * everything else.
 *
 * `auth-provider.test.tsx` proves the binding; `session.test.ts` proves the store;
 * `api.test.ts` proves the wire. None of them can prove that a *page* is wired to
 * them correctly, and the login page is where a mistake costs the most: it is the
 * only place the client names a tenant, and it is the door every other screen
 * sits behind.
 *
 * The real `AuthProvider` and the real session store are used — only `auth.login`
 * is replaced, and `importOriginal` keeps the real `ApiError` because the screen
 * branches on `instanceof` and on the status getters. The real message catalogue
 * is used too, so every assertion below is against the string a user actually
 * reads rather than a stub that would pass no matter what the page rendered.
 *
 * What is pinned, each a rule the source states in prose and nothing checked:
 *
 *   * the slug and the email are trimmed, but the password is sent verbatim —
 *     whitespace is part of a password and trimming it would lock people out,
 *   * success navigates with `replace`, not `push`, so the credentials form is
 *     not one Back press away from the screen the user just authenticated into,
 *   * the destination is the locale segment, not a hardcoded `/en`,
 *   * refusal cases are mapped by status, not by matching the server's English
 *     sentence: 429, unreachable, 401 and everything else stay distinguishable,
 *   * the message for a refused password names no field, because the API does not
 *     say which one was wrong and guessing would be a disclosure,
 *   * a failed attempt navigates nowhere,
 *   * the form is disabled while the server is deciding and a second submit is
 *     ignored — not merely because the button is disabled but because the handler
 *     returns,
 *   * a previous error is cleared when the user tries again.
 *
 * `next/link` is the real one: the back link is part of the screen and replacing
 * it would hide a broken `href`. Only the router is a double, since there is no
 * Next runtime here.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

const { router } = vi.hoisted(() => ({
  router: {
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  },
}));

vi.mock("next/navigation", () => ({ useRouter: () => router }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    auth: {
      ...actual.auth,
      login: vi.fn(),
    },
  };
});

import LoginPage from "@/app/[locale]/login/page";
import { AuthProvider } from "@/components/auth-provider";
import { getMessages } from "@/i18n/messages";
import { ApiError, auth } from "@/lib/api";
import { setSession } from "@/lib/session";
import type { Locale } from "@/i18n/config";

const EN = getMessages("en");

const CREDENTIALS = {
  tenant: "  acme  ",
  email: "  operator@example.test  ",
  password: "  correct horse battery staple  ",
};

const SESSION = {
  access_token: "access-1",
  token_type: "bearer" as const,
  expires_in: 900,
  expires_at: "2026-11-04T12:15:00Z",
  refresh_token: "refresh-1",
  refresh_expires_at: "2026-12-04T12:00:00Z",
  user: {
    id: "u-1",
    email: "operator@example.test",
    role: "operator" as const,
    tenant_id: "t-1",
    tenant_slug: "acme",
    is_active: true,
  },
};

/** A promise the test releases by hand, so "while the server is deciding" is testable. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderLogin(locale: Locale = "en") {
  const view = render(
    <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
      <AuthProvider>
        <LoginPage />
      </AuthProvider>
    </NextIntlClientProvider>,
  );
  return { ...view, form: view.container.querySelector("form")! };
}

/**
 * Type into the three fields, found by their labels.
 *
 * `getByLabelText` is the assertion as much as the setup: a field whose label is
 * no longer associated with it fails here rather than silently going untested.
 */
function fill(locale: Locale = "en", values = CREDENTIALS) {
  const m = getMessages(locale);
  fireEvent.change(screen.getByLabelText(m.auth.tenant), { target: { value: values.tenant } });
  fireEvent.change(screen.getByLabelText(m.auth.email), { target: { value: values.email } });
  fireEvent.change(screen.getByLabelText(m.auth.password), { target: { value: values.password } });
}

function submit(form: HTMLFormElement) {
  fireEvent.submit(form);
}

function alertText(): string | null {
  return screen.queryByRole("alert")?.textContent ?? null;
}

beforeEach(() => {
  setSession(null);
  vi.mocked(auth.login).mockReset();
  router.push.mockReset();
  router.replace.mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("a successful sign-in", () => {
  it("trims the slug and the email but sends the password exactly as typed", async () => {
    vi.mocked(auth.login).mockResolvedValue(SESSION);
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(auth.login).toHaveBeenCalledWith({
        tenant: "acme",
        email: "operator@example.test",
        // Not trimmed: a leading space is part of the password, and stripping it
        // would turn a correct password into a wrong one.
        password: "  correct horse battery staple  ",
      });
    });
  });

  it("replaces the history entry rather than pushing a new one", async () => {
    vi.mocked(auth.login).mockResolvedValue(SESSION);
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en");
    });
    // `push` would leave the credentials form one Back press behind the area
    // the user has just signed in to.
    expect(router.push).not.toHaveBeenCalled();
  });

  it("navigates to the active locale, not to a hardcoded one", async () => {
    vi.mocked(auth.login).mockResolvedValue(SESSION);
    const { form } = renderLogin("bn");

    fill("bn");
    submit(form);

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/bn");
    });
  });

  it("shows no error and does not report one that is not there", async () => {
    vi.mocked(auth.login).mockResolvedValue(SESSION);
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalled();
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("a refused sign-in", () => {
  it("maps a 429 to the rate-limited sentence", async () => {
    vi.mocked(auth.login).mockRejectedValue(
      new ApiError(429, "rate_limited", "Too many attempts."),
    );
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(alertText()).toBe(EN.auth.rateLimited);
    });
  });

  it("tells an unreachable API apart from a refused password", async () => {
    vi.mocked(auth.login).mockRejectedValue(
      new ApiError(0, "unreachable", "The API is not reachable."),
    );
    const { form } = renderLogin();

    fill();
    submit(form);

    // The single most useful distinction on this screen: "wrong password" and
    // "the backend is down" must never look the same.
    await waitFor(() => {
      expect(alertText()).toBe(EN.auth.offline);
    });
  });

  it("does not say which field was wrong when the credentials are rejected", async () => {
    vi.mocked(auth.login).mockRejectedValue(
      new ApiError(401, "unauthorized", "Invalid tenant, email or password."),
    );
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(alertText()).toBe(EN.auth.invalidCredentials);
    });

    // The server's own wording is uniform on purpose; repeating its three nouns
    // beside a specific field would turn a deliberate opacity into a hint.
    const text = alertText() ?? "";
    expect(text).not.toMatch(/tenant|email|password/i);
  });

  it("falls back to the generic sentence for anything it cannot name", async () => {
    vi.mocked(auth.login).mockRejectedValue(
      new ApiError(500, "internal_error", "Something broke."),
    );
    const { form } = renderLogin();

    fill();
    submit(form);

    // A 403 is not a credential problem either, and neither is a 500: both land
    // on the generic sentence rather than borrowing a message that would blame
    // the user's password.
    await waitFor(() => {
      expect(alertText()).toBe(EN.auth.failed);
    });
  });

  it("navigates nowhere", async () => {
    vi.mocked(auth.login).mockRejectedValue(
      new ApiError(401, "unauthorized", "Invalid tenant, email or password."),
    );
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(alertText()).toBe(EN.auth.invalidCredentials);
    });
    expect(router.replace).not.toHaveBeenCalled();
    expect(router.push).not.toHaveBeenCalled();
  });
});

describe("while the server is deciding", () => {
  it("disables the form and ignores a second submit", async () => {
    const server = deferred<typeof SESSION>();
    vi.mocked(auth.login).mockReturnValue(server.promise);
    const { form } = renderLogin();

    fill();
    submit(form);

    // No `jest-dom` here: this file adds no dependency it does not need, so the
    // disabled state is read off the element rather than through a matcher.
    const button = screen.getByRole<HTMLButtonElement>("button", { name: EN.auth.submitting });
    expect(button.disabled).toBe(true);
    expect(screen.getByLabelText<HTMLInputElement>(EN.auth.tenant).disabled).toBe(true);
    expect(screen.getByLabelText<HTMLInputElement>(EN.auth.email).disabled).toBe(true);
    expect(screen.getByLabelText<HTMLInputElement>(EN.auth.password).disabled).toBe(true);

    // The disabled attribute is not the rule — the handler's own guard is, and a
    // programmatic submit goes straight past the attribute.
    submit(form);
    expect(auth.login).toHaveBeenCalledTimes(1);

    server.resolve(SESSION);
    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en");
    });
    expect(auth.login).toHaveBeenCalledTimes(1);
  });

  it("clears the previous error when the user tries again", async () => {
    vi.mocked(auth.login).mockRejectedValueOnce(
      new ApiError(401, "unauthorized", "Invalid tenant, email or password."),
    );
    const { form } = renderLogin();

    fill();
    submit(form);

    await waitFor(() => {
      expect(alertText()).toBe(EN.auth.invalidCredentials);
    });

    vi.mocked(auth.login).mockResolvedValueOnce(SESSION);
    submit(form);

    // A stale "Those details were not accepted." left under a form that is now
    // succeeding is the sort of thing users report as "it says wrong password
    // but it logged me in".
    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en");
    });
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
