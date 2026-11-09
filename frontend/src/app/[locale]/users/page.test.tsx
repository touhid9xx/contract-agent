// @vitest-environment jsdom
/**
 * The users screen — the eighth screen under test, and the first whose seam is a
 * **lifecycle** rather than a read.
 *
 * The seven screens before it each proved one thing: `login` that a session can be
 * obtained, `review` that a queue can be worked, `escalations` that a filter can
 * live in the address, `agent` that a refusal can carry the answer,
 * `notifications` that one action may ask before it acts, `contracts` that a form
 * can own two transports, and the contract detail that five reads can fail
 * independently. This screen carries three rules none of them reach:
 *
 *   * **the page is admin-only, and the gate is a query gate, not a message.** All
 *     four routes are `require_roles(ADMIN)`, so the list is not even asked for
 *     anyone else — a 403 an operator can do nothing about is noise, not
 *     information. The refusal card only appears for someone who *is* signed in.
 *   * **the caller's own row has no controls.** The server refuses a self change
 *     with 409 on both mutating routes. Hiding the buttons is not the enforcement —
 *     the route still decides — it stops the screen offering an action whose only
 *     outcome is a refusal.
 *   * **a reset asks first and shows the value exactly once.** The request carries
 *     no body (the server generates the password), the response is the only copy
 *     that will ever exist, and it is rendered *outside* the row it came from so a
 *     refetch cannot take it away.
 *
 * Two smaller rules ride along and are pinned here because they are easy to get
 * wrong silently: the role chips send the **wire** value even when the label is
 * translated, and a password shorter than the hint is still submittable — the
 * client's copy of the floor is a label, never a block.
 *
 * The harness is the one the earlier screen specs established:
 *
 *   * `@/lib/api` is doubled through `importOriginal`, so the real `ApiError`
 *     survives `instanceof` and the error mapping is the screen's, not a stub's;
 *   * a real `QueryClientProvider` with a fresh client per test and `retry: false`,
 *     because these tests ask *what the screen asked and how often*;
 *   * the real `NextIntlClientProvider` and the real catalogue, so every assertion
 *     is against the sentence a user reads;
 *   * the real `AuthProvider` and the real session store, so "signed out" and "not
 *     an admin" are produced by the session rather than passed in as props.
 *
 * No `next/navigation` double is needed: this page reads `useLocale` from
 * next-intl and `next/link`, neither of which needs a Next runtime.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    users: {
      list: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      resetPassword: vi.fn(),
    },
  };
});

import UsersPage from "@/app/[locale]/users/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, users } from "@/lib/api";
import type { PasswordReset, Session, UserRole, UserSummary } from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const BN = getMessages("bn");
const ACCESS = "access-1";

/** The caller's own account id: the row the server refuses to let them change. */
const ME_ID = "u-1";

/**
 * A non-breaking space: the one kind of blank a `type="email"` box will keep.
 *
 * The platform strips ASCII whitespace out of an email field before the screen
 * ever sees it — `fireEvent.change` with `"   "` delivers `""`, and
 * `"  a@b.test  "` arrives already trimmed — so a test written with ordinary
 * spaces cannot tell `email === ""` from `email.trim() === ""`. One such test
 * was written here first and survived deleting the `trim()`: the DOM had
 * emptied the field for it. U+00A0 survives sanitisation and
 * `String.prototype.trim()` still removes it, which is exactly the
 * paste-from-a-word-processor case the guard exists for.
 */
const NBSP = "\u00a0";

/** Substitution for a plain (non-plural) ICU string. */
function say(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => String(values[key]));
}

/** A promise the test releases by hand, so "before the server answers" is testable. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

/**
 * One account, defaulting to a working colleague.
 *
 * The defaults describe the ordinary row so a test that cares about something else
 * overrides only that thing — and a deactivated account is always an explicit
 * choice, never a fixture accident.
 */
function account(overrides: Partial<UserSummary> = {}): UserSummary {
  return {
    id: "u-2",
    email: "colleague@example.test",
    role: "operator",
    tenant_id: "t-1",
    tenant_slug: "acme",
    is_active: true,
    ...overrides,
  };
}

/** The caller's own account, as the session reports it. */
const SELF = account({ id: ME_ID, email: "admin@example.test", role: "admin" });

function sessionFor(role: UserRole): Session {
  return {
    access_token: ACCESS,
    token_type: "bearer",
    expires_in: 900,
    expires_at: "2026-11-04T12:15:00Z",
    refresh_token: "refresh-1",
    refresh_expires_at: "2026-12-04T12:00:00Z",
    user: { ...SELF, role },
  };
}

function signedIn(role: UserRole = "admin") {
  setSession(sessionFor(role));
}

function renderScreen(locale: Locale = "en") {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
        <AuthProvider>
          <UsersPage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

/**
 * One card, found by the heading it renders.
 *
 * `Card` renders a `div.rounded-card`, and that class is the only structural handle
 * the component guarantees — the screen adds no test id of its own. If the wrapper
 * ever stops carrying it this throws loudly rather than letting a per-card
 * assertion quietly match the whole page.
 */
function cardTitled(title: string): HTMLElement {
  const root = screen.getByText(title).closest(".rounded-card");
  if (root === null) {
    throw new Error(`No card found titled "${title}".`);
  }
  return root as HTMLElement;
}

/** The card describing one account. */
function cardOf(row: UserSummary): HTMLElement {
  return cardTitled(row.email);
}

function createCard(): HTMLElement {
  return cardTitled(EN.users.createHeading);
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

/**
 * Every chip in `root` currently carrying `aria-current`, by attribute.
 *
 * The role chips show the current value by being `primary` and by setting
 * `aria-current`; the attribute is the part a test can read without asserting on
 * Tailwind classes. Same idiom the contract list's status filters are pinned with.
 */
function currentChips(root: HTMLElement): HTMLElement[] {
  return within(root)
    .getAllByRole("button")
    .filter((node) => node.getAttribute("aria-current") === "true");
}

/** The wire payload of the n-th `users.update` call. */
function updatePayload(index = 0): { role?: UserRole; is_active?: boolean } {
  const call = vi.mocked(users.update).mock.calls[index];
  if (call === undefined) {
    throw new Error(`users.update was called ${index + 1} times or fewer.`);
  }
  return call[2];
}

beforeEach(() => {
  setSession(null);
  vi.mocked(users.list).mockReset();
  vi.mocked(users.create).mockReset();
  vi.mocked(users.update).mockReset();
  vi.mocked(users.resetPassword).mockReset();

  vi.mocked(users.list).mockResolvedValue([SELF, account()]);
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("who may read the list", () => {
  it("asks a signed-out visitor to sign in and asks the route nothing", async () => {
    renderScreen();

    await screen.findByText(EN.users.signInRequired);

    // The message and the link are the whole screen: a refused read cannot be
    // reported to someone who has no session to refuse.
    expect(screen.getByRole("link", { name: EN.users.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );
    expect(users.list).not.toHaveBeenCalled();
  });

  it("tells an operator their role cannot manage accounts, without asking", async () => {
    signedIn("operator");
    renderScreen();

    await screen.findByText(EN.users.forbidden);

    // `enabled: isAdmin` is the point: a 403 that an operator can do nothing about
    // is not information, so the query is never fired.
    expect(screen.getByText(EN.users.forbiddenHint)).toBeTruthy();
    expect(users.list).not.toHaveBeenCalled();
    expect(screen.queryByText(EN.users.createHeading)).toBeNull();
  });

  it("asks an admin once and lists every account in the organisation", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(account().email);

    expect(users.list).toHaveBeenCalledTimes(1);
    expect(users.list).toHaveBeenCalledWith(ACCESS);
    expect(screen.getByText(SELF.email)).toBeTruthy();
  });
});

describe("the list", () => {
  it("shows each account's role by marking exactly one chip as current", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(account().email);

    const row = cardOf(account());
    const current = within(row).getAllByRole("button", { current: true });
    expect(current.map((node) => node.textContent)).toEqual([EN.users.roles.operator]);
  });

  it("badges a deactivated account so it is not mistaken for a working one", async () => {
    vi.mocked(users.list).mockResolvedValue([SELF, account({ is_active: false })]);
    signedIn();
    renderScreen();

    await screen.findByText(account().email);

    const row = cardOf(account({ is_active: false }));
    expect(within(row).getByText(EN.users.inactiveLabel)).toBeTruthy();
    expect(within(cardOf(SELF)).getByText(EN.users.activeLabel)).toBeTruthy();
  });

  it("states how many accounts there are", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.users.countLabel, { count: 2 }));
  });

  it("gives an empty organisation its own words instead of a count", async () => {
    vi.mocked(users.list).mockResolvedValue([]);
    signedIn();
    renderScreen();

    await screen.findByText(EN.users.empty);

    // An empty list is a state with its own words, not a "0 accounts" heading.
    expect(screen.getByText(EN.users.emptyHint)).toBeTruthy();
    expect(screen.queryByText(say(EN.users.countLabel, { count: 0 }))).toBeNull();
    expect(screen.queryByText(say(EN.users.countLabel, { count: 1 }))).toBeNull();
  });

  it("counts the caller's own row like any other", async () => {
    vi.mocked(users.list).mockResolvedValue([SELF]);
    signedIn();
    renderScreen();

    // One account is still one account: the count is the list's length, so a lone
    // caller reads "1 account" rather than looking like an empty organisation.
    await screen.findByText(say(EN.users.countLabel, { count: 1 }));
    expect(screen.queryByText(EN.users.empty)).toBeNull();
  });

  it("says it is loading until the answer arrives", async () => {
    const pending = deferred<UserSummary[]>();
    vi.mocked(users.list).mockReturnValue(pending.promise);
    signedIn();
    renderScreen();

    await screen.findByText(EN.users.loading);

    pending.resolve([SELF, account()]);
    await screen.findByText(account().email);
    expect(screen.queryByText(EN.users.loading)).toBeNull();
  });

  it("reads a refused read as its own sentence rather than a generic failure", async () => {
    vi.mocked(users.list).mockRejectedValue(new ApiError(403, "forbidden", "Nope."));
    signedIn();
    renderScreen();

    await screen.findByText(EN.users.forbidden);
  });

  it("tells an unreachable API apart from a refusal", async () => {
    vi.mocked(users.list).mockRejectedValue(new ApiError(0, "unreachable", "Down."));
    signedIn();
    renderScreen();

    await screen.findByText(EN.users.offline);
  });

  it("falls back to the generic sentence for a status it has no words for", async () => {
    vi.mocked(users.list).mockRejectedValue(new ApiError(500, "server", "Boom."));
    signedIn();
    renderScreen();

    await screen.findByText(EN.users.failed);
  });

  it("re-asks only the list when the operator retries", async () => {
    vi.mocked(users.list).mockRejectedValueOnce(new ApiError(500, "server", "Boom."));
    signedIn();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.users.retry }));

    await screen.findByText(account().email);
    // One failed read, one retry: nothing else on this page is re-fetched.
    expect(users.list).toHaveBeenCalledTimes(2);
  });
});

describe("creating an account", () => {
  it("refuses to submit until an email and a password are both typed", async () => {
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const submit = screen.getByRole<HTMLButtonElement>("button", {
      name: EN.users.createAction,
    });
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(EN.users.emailLabel), {
      target: { value: "new@example.test" },
    });
    // A password is not optional: the server would refuse, and a 422 for a field
    // the operator did not fill is a worse message than a disabled button.
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText(EN.users.passwordLabel), {
      target: { value: "correct horse battery staple" },
    });
    expect(submit.disabled).toBe(false);
  });

  it("keeps the button disabled for an email that is only whitespace", async () => {
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.change(screen.getByLabelText(EN.users.emailLabel), {
      target: { value: `${NBSP}${NBSP}` },
    });
    fireEvent.change(screen.getByLabelText(EN.users.passwordLabel), {
      target: { value: "correct horse battery staple" },
    });

    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: EN.users.createAction }).disabled,
    ).toBe(true);
  });

  it("sends the trimmed email, the typed password and the chosen role", async () => {
    const created = account({ id: "u-9", email: "new@example.test", role: "viewer" });
    vi.mocked(users.create).mockResolvedValue(created);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.change(screen.getByLabelText(EN.users.emailLabel), {
      target: { value: `${NBSP}  new@example.test  ${NBSP}` },
    });
    fireEvent.change(screen.getByLabelText(EN.users.passwordLabel), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(screen.getByRole("button", { name: EN.users.createAction }));

    await waitFor(() => {
      expect(users.create).toHaveBeenCalledTimes(1);
    });
    // The password is the password — never trimmed — and the role defaults to the
    // least privileged one offered.
    expect(users.create).toHaveBeenCalledWith(ACCESS, {
      email: "new@example.test",
      password: "correct horse battery staple",
      role: "viewer",
    });
  });

  it("says who was created, clears the form and re-asks the list", async () => {
    const created = account({ id: "u-9", email: "new@example.test", role: "viewer" });
    vi.mocked(users.create).mockResolvedValue(created);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.change(screen.getByLabelText(EN.users.emailLabel), {
      target: { value: "new@example.test" },
    });
    fireEvent.change(screen.getByLabelText(EN.users.passwordLabel), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(screen.getByRole("button", { name: EN.users.createAction }));

    await screen.findByText(say(EN.users.created, { email: created.email }));

    // The list is refetched rather than the row appended: the server decides what
    // exists, and a locally-inserted row would be a second opinion.
    await waitFor(() => {
      expect(users.list).toHaveBeenCalledTimes(2);
    });
    expect(screen.getByLabelText<HTMLInputElement>(EN.users.emailLabel).value).toBe("");
    expect(screen.getByLabelText<HTMLInputElement>(EN.users.passwordLabel).value).toBe("");
    // The chosen role falls back to the least privileged option, so a half-filled
    // form never leaves an admin selection behind for the next account.
    expect(currentChips(createCard()).map((node) => node.textContent)).toEqual([
      EN.users.roles.viewer,
    ]);
  });

  it("lets a short password reach the server and translates the 422", async () => {
    vi.mocked(users.create).mockRejectedValue(new ApiError(422, "validation", "Too short."));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.change(screen.getByLabelText(EN.users.emailLabel), {
      target: { value: "new@example.test" },
    });
    fireEvent.change(screen.getByLabelText(EN.users.passwordLabel), {
      target: { value: "short" },
    });

    // `MIN_PASSWORD_LENGTH` on this screen is a hint. The authority is a pydantic
    // `Field(min_length=...)`, so the client must not block below it — the day that
    // constant moves, a client-side gate would be wrong and silent.
    const submit = screen.getByRole<HTMLButtonElement>("button", {
      name: EN.users.createAction,
    });
    expect(submit.disabled).toBe(false);

    fireEvent.click(submit);

    await screen.findByText(EN.users.invalidInput);
    expect(within(createCard()).getByRole("alert").textContent).toBe(EN.users.invalidInput);
  });

  it("names the server's floor in the password hint", async () => {
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    expect(within(createCard()).getByText(say(EN.users.passwordHint, { min: 12 }))).toBeTruthy();
  });

  it("offers the three roles least privileged first", async () => {
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const chips = within(createCard()).getAllByRole("button", {
      name: new RegExp(`^(${Object.values(EN.users.roles).join("|")})$`),
    });
    expect(chips.map((node) => node.textContent)).toEqual([
      EN.users.roles.viewer,
      EN.users.roles.operator,
      EN.users.roles.admin,
    ]);
  });

  it("lets the operator pick the role the new account gets", async () => {
    const created = account({ id: "u-9", email: "new@example.test", role: "admin" });
    vi.mocked(users.create).mockResolvedValue(created);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.change(screen.getByLabelText(EN.users.emailLabel), {
      target: { value: "new@example.test" },
    });
    fireEvent.change(screen.getByLabelText(EN.users.passwordLabel), {
      target: { value: "correct horse battery staple" },
    });
    fireEvent.click(within(createCard()).getByRole("button", { name: EN.users.roles.admin }));
    fireEvent.click(screen.getByRole("button", { name: EN.users.createAction }));

    await waitFor(() => {
      expect(users.create).toHaveBeenCalledWith(ACCESS, {
        email: "new@example.test",
        password: "correct horse battery staple",
        role: "admin",
      });
    });
  });
});

describe("changing a role or an account's state", () => {
  it("sends only the changed field, never both", async () => {
    vi.mocked(users.update).mockResolvedValue(account({ role: "admin" }));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(within(cardOf(account())).getByRole("button", { name: EN.users.roles.admin }));

    await waitFor(() => {
      expect(users.update).toHaveBeenCalledTimes(1);
    });
    expect(users.update).toHaveBeenCalledWith(ACCESS, account().id, { role: "admin" });
    // Membership, not just value: an `is_active: undefined` would be a lie about
    // what the call asks for, and JSON would drop the key on the way out.
    expect(Object.keys(updatePayload())).toEqual(["role"]);
  });

  it("deactivates a working account", async () => {
    vi.mocked(users.update).mockResolvedValue(account({ is_active: false }));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(
      within(cardOf(account())).getByRole("button", { name: EN.users.deactivateAction }),
    );

    await waitFor(() => {
      expect(users.update).toHaveBeenCalledTimes(1);
    });
    expect(Object.keys(updatePayload())).toEqual(["is_active"]);
    expect(updatePayload().is_active).toBe(false);
  });

  it("reactivates a deactivated account", async () => {
    vi.mocked(users.list).mockResolvedValue([SELF, account({ is_active: false })]);
    vi.mocked(users.update).mockResolvedValue(account());
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(
      within(cardOf(account({ is_active: false }))).getByRole("button", {
        name: EN.users.reactivateAction,
      }),
    );

    await waitFor(() => {
      expect(updatePayload().is_active).toBe(true);
    });
  });

  it("re-asks the list after a change, because the server owns the new state", async () => {
    vi.mocked(users.update).mockResolvedValue(account({ role: "admin" }));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(within(cardOf(account())).getByRole("button", { name: EN.users.roles.admin }));

    await waitFor(() => {
      expect(users.list).toHaveBeenCalledTimes(2);
    });
  });

  it("offers no control at all on the caller's own row", async () => {
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const own = cardOf(SELF);
    expect(within(own).getByText(EN.users.selfNote)).toBeTruthy();
    // The server refuses a self change with 409; the screen stops offering an
    // action whose only outcome is a refusal.
    for (const chip of within(own).getAllByRole("button")) {
      expect((chip as HTMLButtonElement).disabled).toBe(true);
    }
    expect(within(own).queryByRole("button", { name: EN.users.deactivateAction })).toBeNull();
    expect(within(own).queryByRole("button", { name: EN.users.resetAction })).toBeNull();
  });

  it("says a deactivated account cannot be reset instead of offering it", async () => {
    vi.mocked(users.list).mockResolvedValue([SELF, account({ is_active: false })]);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const row = cardOf(account({ is_active: false }));
    expect(within(row).getByText(EN.users.resetInactiveHint)).toBeTruthy();
    expect(within(row).queryByRole("button", { name: EN.users.resetAction })).toBeNull();
  });

  it("disables the row being changed and no other", async () => {
    const pending = deferred<UserSummary>();
    vi.mocked(users.update).mockReturnValue(pending.promise);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(
      within(cardOf(account())).getByRole("button", { name: EN.users.deactivateAction }),
    );

    // A second click would race the first on the same row, so the row locks itself.
    await waitFor(() => {
      expect(within(cardOf(account())).getByText(EN.users.working)).toBeTruthy();
    });
    expect(cardOf(account()).getAttribute("aria-busy")).toBe("true");
    expect(cardOf(SELF).getAttribute("aria-busy")).toBe("false");

    pending.resolve(account({ is_active: false }));
    await waitFor(() => {
      expect(screen.queryByText(EN.users.working)).toBeNull();
    });
  });

  it("blames only the row that was refused", async () => {
    vi.mocked(users.update).mockRejectedValue(new ApiError(500, "server", "Boom."));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(within(cardOf(account())).getByRole("button", { name: EN.users.roles.admin }));

    await screen.findByText(EN.users.failed);

    // One shared message would blame every card for one refusal.
    expect(alertTexts()).toEqual([EN.users.failed]);
    expect(within(cardOf(account())).getByRole("alert").textContent).toBe(EN.users.failed);
    expect(within(cardOf(SELF)).queryByRole("alert")).toBeNull();
  });

  it("reads a 409 on a change as the server refusing it", async () => {
    vi.mocked(users.update).mockRejectedValue(new ApiError(409, "self_change", "No."));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    fireEvent.click(within(cardOf(account())).getByRole("button", { name: EN.users.roles.admin }));

    await screen.findByText(EN.users.conflict);
  });
});

describe("resetting a password", () => {
  it("asks before it resets, and asks the server nothing if cancelled", async () => {
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const row = cardOf(account());
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetAction }));

    // The confirmation is the point: this ends the target's sessions and destroys
    // the password they are using now, and there is nothing to undo it with.
    expect(within(row).getByText(EN.users.resetConfirm)).toBeTruthy();
    expect(within(row).getByText(EN.users.resetConfirmHint)).toBeTruthy();
    expect(users.resetPassword).not.toHaveBeenCalled();

    fireEvent.click(within(row).getByRole("button", { name: EN.users.cancel }));

    expect(within(row).queryByText(EN.users.resetConfirm)).toBeNull();
    expect(within(row).getByRole("button", { name: EN.users.resetAction })).toBeTruthy();
    expect(users.resetPassword).not.toHaveBeenCalled();
  });

  it("sends no body and shows the one copy of the value outside the row", async () => {
    const reset: PasswordReset = {
      user: account(),
      temporary_password: "temporary-horse-battery",
    };
    vi.mocked(users.resetPassword).mockResolvedValue(reset);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const row = cardOf(account());
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetAction }));
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetConfirmAction }));

    await screen.findByText(say(EN.users.resetSucceeded, { email: reset.user.email }));

    // The server generates the password, so the request carries nothing at all.
    expect(users.resetPassword).toHaveBeenCalledWith(ACCESS, account().id);

    const answer = cardTitled(say(EN.users.resetSucceeded, { email: reset.user.email }));
    expect(within(answer).getByText(reset.temporary_password)).toBeTruthy();
    expect(within(answer).getByText(EN.users.resetOnce)).toBeTruthy();
    // Page level on purpose: a row-level block would vanish with the row on the
    // next refetch, and this is the only copy that will ever exist.
    expect(row.contains(answer)).toBe(false);
    expect(within(row).queryByText(EN.users.resetConfirm)).toBeNull();
  });

  it("re-asks the list after a reset and keeps the value through the refetch", async () => {
    const reset: PasswordReset = { user: account(), temporary_password: "temporary-value" };
    vi.mocked(users.resetPassword).mockResolvedValue(reset);
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const row = cardOf(account());
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetAction }));
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetConfirmAction }));

    await waitFor(() => {
      expect(users.list).toHaveBeenCalledTimes(2);
    });
    await screen.findByText(reset.temporary_password);
  });

  it("blames the row whose reset was refused and shows no password", async () => {
    vi.mocked(users.resetPassword).mockRejectedValue(new ApiError(409, "inactive", "No."));
    signedIn();
    renderScreen();
    await screen.findByText(account().email);

    const row = cardOf(account());
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetAction }));
    fireEvent.click(within(row).getByRole("button", { name: EN.users.resetConfirmAction }));

    await waitFor(() => {
      expect(within(cardOf(account())).getByRole("alert").textContent).toBe(EN.users.conflict);
    });
    expect(within(cardOf(SELF)).queryByRole("alert")).toBeNull();
    expect(screen.queryByText(EN.users.resetOnce)).toBeNull();
  });
});

describe("the locale and the way back", () => {
  it("renders the translated chrome and links home inside the locale", async () => {
    signedIn();
    renderScreen("bn");

    await screen.findByText(account().email);

    expect(
      screen.getByRole("link", { name: BN.users.backToHome }).getAttribute("href"),
    ).toBe("/bn");
    expect(screen.getByText(BN.users.title)).toBeTruthy();
  });

  it("translates the role label but sends the wire value", async () => {
    vi.mocked(users.update).mockResolvedValue(account({ role: "admin" }));
    signedIn();
    renderScreen("bn");
    await screen.findByText(account().email);

    // The chips read "অপারেটর"/"অ্যাডমিন"; the route still receives `admin`, and
    // the current one is still the wire `operator`.
    const row = cardOf(account());
    expect(within(row).getByRole("button", { current: true }).textContent).toBe(
      BN.users.roles.operator,
    );

    fireEvent.click(within(row).getByRole("button", { name: BN.users.roles.admin }));

    await waitFor(() => {
      expect(updatePayload().role).toBe("admin");
    });
  });

  it("names the refused self change in the reader's language", async () => {
    vi.mocked(users.update).mockRejectedValue(new ApiError(409, "self_change", "No."));
    signedIn();
    renderScreen("bn");
    await screen.findByText(account().email);

    fireEvent.click(within(cardOf(account())).getByRole("button", { name: BN.users.roles.admin }));

    await screen.findByText(BN.users.conflict);
  });
});
