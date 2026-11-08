// @vitest-environment jsdom
/**
 * The notification queue — the fifth screen under test, and the third and last
 * whose scope lives in the address.
 *
 * Each screen before it proved one thing: `login` that a session can be obtained,
 * `review` that a queue can be worked, `escalations` that a filter can live in the
 * URL, `agent` that a refusal can carry the answer. None of them could reach the
 * rules *this* screen carries, because no other screen makes them:
 *
 *   * **one action asks first and the other does not**. `/dispatch` mails a real
 *     customer, so it takes a confirmation step; `/retry` only requeues a parked
 *     row and would train people to click through dialogs if it asked too. The two
 *     are distinguished by *what the row's own status permits*, which is why the
 *     controls are per row rather than one toolbar of everything.
 *   * **a dispatch has three outcomes behind one `200`**, and the quiet-hours
 *     deferral is the one that reads as an error if the sentence is ordered wrongly:
 *     the system protected the customer, it did not fail.
 *   * **a refusal is committed before it is reported**, so a `409` or a `404` also
 *     means "the list you are reading is stale" and must reload.
 *
 * The harness is the one `frontend/src/app/[locale]/agent/page.test.tsx` established,
 * with no new dependency and no invented provider:
 *
 *   * a real `QueryClientProvider` with a fresh client per test, `retry: false`
 *     because these tests ask *what the screen asked and how often*, and a retry
 *     policy would answer that question in the screen's place;
 *   * the real `NextIntlClientProvider` and the real catalogue, so every assertion
 *     is against the sentence a user reads;
 *   * the real `AuthProvider` and the real session store — role gating is read from
 *     the session, not passed in as a prop;
 *   * only `notifications.list`, `notifications.dispatch` and `notifications.retry`
 *     are replaced, with `importOriginal` keeping the real `ApiError`, because the
 *     screen branches on `instanceof` and on `cause.code`;
 *   * `next/navigation` is the second double: there is no Next runtime here, so
 *     `useSearchParams` reads a mutable address this test writes, and the router is
 *     an object of spies. The screen must derive what it asks the server from the
 *     address alone, which is exactly what that double lets us see.
 *
 * What is pinned:
 *
 *   * a signed-out visitor is asked to sign in and the API is asked nothing,
 *   * a viewer reads the queue and is offered no button that contacts anyone,
 *   * the unscoped read sends an **empty** filter object, not a partial one,
 *   * a scoped address narrows that same read, starts the box from the address and
 *     quotes the **server's echo** rather than its own query string,
 *   * an address naming something that cannot be a contract performs no read at
 *     all, and shows no loading card either,
 *   * filtering navigates with `replace` and a trimmed id; an id that cannot be a
 *     contract is never navigated, and an emptied box means "no filter",
 *   * the status and channel chips re-ask the server, one current chip per group,
 *   * **sending asks first** — one click calls nothing — while **requeueing does
 *     not**, and each row offers only the action its status permits,
 *   * a landed send reports *what happened*: a deferral is not a failure even when
 *     the envelope also says `sent`, and a recorded transport failure names its
 *     error,
 *   * a refusal is reported on the row that was clicked only, and reloads when the
 *     server changed the row (`409`/`404`) but not when it did not,
 *   * an unrecognised refusal code still says "that did not work",
 *   * "the API is down" never looks like "nothing is scheduled", and a scoped empty
 *     means something different from an unscoped one,
 *   * a row states what it knows and stays silent about what never happened.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

const { router, address } = vi.hoisted(() => ({
  router: {
    push: vi.fn(),
    replace: vi.fn(),
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  },
  /** The query string under test. Written per test, read on every render. */
  address: { current: "" },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => router,
  useSearchParams: () => new URLSearchParams(address.current),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    notifications: {
      list: vi.fn(),
      dispatch: vi.fn(),
      retry: vi.fn(),
    },
  };
});

import NotificationsPage from "@/app/[locale]/notifications/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, notifications } from "@/lib/api";
import type {
  DispatchOutcome,
  NotificationList,
  NotificationRow,
  Session,
  UserRole,
} from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const ACCESS = "access-1";

/** Substitution for a plain (non-plural) ICU string. */
function say(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => String(values[key]));
}

/**
 * A schedulable row.
 *
 * `scheduled_date` is a calendar date in the contract's own time zone and
 * `scheduled_at` an instant, so the fixture keeps the two shapes distinct: the
 * deferred case below relies on them being different columns.
 */
const BASE_ROW: NotificationRow = {
  id: 41,
  contract_id: 7,
  channel: "email",
  template: "T-60",
  status: "pending",
  scheduled_date: "2026-11-04T00:00:00Z",
  scheduled_at: null,
  sent_at: null,
  attempts: 0,
  last_error: null,
  provider_message_id: null,
  idempotency_key: "7:T-60:2026-11-04",
};

function row(overrides: Partial<NotificationRow> = {}): NotificationRow {
  return { ...BASE_ROW, ...overrides };
}

function listing(
  rows: NotificationRow[],
  overrides: Partial<NotificationList> = {},
): NotificationList {
  return { items: rows, count: rows.length, contract_id: null, ...overrides };
}

function outcome(overrides: Partial<DispatchOutcome> = {}): DispatchOutcome {
  return { notification: row(), sent: true, deferred_until: null, error: null, ...overrides };
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

/**
 * Answer successive queue reads in order, repeating the last one.
 *
 * A landed send, requeue or refusal invalidates the list, so "one read" is not a
 * given: a single `mockResolvedValue` would let the second read return the first
 * answer by accident and hide the reload the screen promises.
 */
function readAnswers(first: NotificationList, ...rest: NotificationList[]) {
  const queue = [first, ...rest];
  const readMock = vi.mocked(notifications.list);
  let served = 0;
  readMock.mockImplementation(async () => {
    const answer = queue[Math.min(served, queue.length - 1)] ?? first;
    served += 1;
    return answer;
  });
  return readMock;
}

function renderScreen(locale: Locale = "en") {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });
  const view = render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
        <AuthProvider>
          <NotificationsPage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
  return { ...view, form: view.container.querySelector("form")! as HTMLFormElement };
}

/** The card's own heading text, which is also how these tests find a row. */
function titleOf(entry: NotificationRow): string {
  return `${say(EN.notifications.contractLabel, { id: entry.contract_id })} · ${entry.template}`;
}

/**
 * A row's card.
 *
 * The page marks each card with `aria-busy`, so that attribute — a real thing the
 * component sets, rather than a class chosen for the test — is the handle.
 */
function card(entry: NotificationRow): HTMLElement {
  const heading = screen.getByText(titleOf(entry));
  const root = heading.closest("[aria-busy]");
  if (root === null) {
    throw new Error(`No notification card found for "${titleOf(entry)}".`);
  }
  return root as HTMLElement;
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

/** The page-level outcome sentence a landed mutation writes. */
async function statusText(): Promise<string> {
  return (await screen.findByRole("status")).textContent ?? "";
}

/** Type into the contract filter. */
function typeFilter(value: string) {
  fireEvent.change(screen.getByLabelText(EN.notifications.contractFilterLabel), {
    target: { value },
  });
}

beforeEach(() => {
  setSession(null);
  address.current = "";
  vi.mocked(notifications.list).mockReset();
  vi.mocked(notifications.dispatch).mockReset();
  vi.mocked(notifications.retry).mockReset();
  router.replace.mockReset();
  router.push.mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
  address.current = "";
});

describe("who may see what", () => {
  it("asks a signed-out visitor to sign in, and asks the API nothing", () => {
    renderScreen();

    expect(screen.getByText(EN.notifications.signInRequired)).toBeDefined();
    // `authorized` refuses before a request is built, so nothing leaves the tab.
    expect(notifications.list).not.toHaveBeenCalled();
    // A 401 is not a queue failure: reporting it would say the queue is broken.
    expect(alertTexts()).toStrictEqual([]);
    expect(screen.getByRole("link", { name: EN.notifications.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );
  });

  it("lets a viewer read the queue but offers no button that contacts anyone", async () => {
    const waiting = row({ id: 41, status: "pending" });
    const parked = row({ id: 42, template: "T-14", status: "failed", last_error: "relay refused" });
    setSession(sessionFor("viewer"));
    readAnswers(listing([waiting, parked]));
    renderScreen();

    await screen.findByText(titleOf(waiting));
    expect(screen.getByText(EN.notifications.viewerNote)).toBeDefined();

    // Both actions send or spend on the customer's behalf, so a read-only role gets
    // neither — not a disabled one, which would invite the click anyway.
    expect(within(card(waiting)).queryByRole("button", { name: EN.notifications.sendNow })).toBeNull();
    expect(
      within(card(parked)).queryByRole("button", { name: EN.notifications.retryAction }),
    ).toBeNull();
  });
});

describe("the address is the state", () => {
  it("asks for the tenant's queue and sends no filter at all", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([row()]));
    renderScreen();

    await screen.findByText(titleOf(row()));
    // `{}` rather than `undefined` or a partial object: the client builds the query
    // string from the keys it finds, so an empty object is the absence of a filter
    // and a `contractId: undefined` would be a filter the server could act on.
    expect(vi.mocked(notifications.list).mock.calls[0]).toStrictEqual([ACCESS, {}]);
    expect(screen.getByText(EN.notifications.scopeAllContracts)).toBeDefined();
    expect(screen.getByText(say(EN.notifications.countLabel, { count: 1 }))).toBeDefined();
  });

  it("narrows the same read to one contract, and starts the box from the address", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    const scoped = row({ contract_id: 12 });
    readAnswers(listing([scoped], { contract_id: 12 }));
    renderScreen();

    await screen.findByText(titleOf(scoped));
    expect(vi.mocked(notifications.list).mock.calls[0]![1]).toStrictEqual({ contractId: 12 });
    expect(screen.getByText(say(EN.notifications.scopeContract, { id: 12 }))).toBeDefined();
    // A box still holding the previous id would contradict the list under it.
    expect(
      screen.getByLabelText<HTMLInputElement>(EN.notifications.contractFilterLabel).value,
    ).toBe("12");
  });

  it("quotes the server's echo in the scope sentence, not the address", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    // The route echoes the filter it applied; here it applied none. The sentence
    // must not claim a narrower queue than the one on screen.
    readAnswers(listing([row()], { contract_id: null }));
    renderScreen();

    await screen.findByText(titleOf(row()));
    expect(screen.getByText(EN.notifications.scopeAllContracts)).toBeDefined();
    expect(screen.queryByText(say(EN.notifications.scopeContract, { id: 12 }))).toBeNull();
  });

  it("refuses to read anything when the address names an impossible contract", () => {
    setSession(sessionFor("operator"));
    address.current = "contract=012";
    renderScreen();

    // The most damaging confusion this screen could make: showing the tenant's whole
    // queue behind a filter that never ran, which looks like an answer.
    expect(notifications.list).not.toHaveBeenCalled();
    expect(alertTexts()).toStrictEqual([EN.notifications.contractInvalid]);
    expect(screen.getByText(EN.notifications.contractInvalidHint)).toBeDefined();
    // Nor a spinner: "still loading" would promise rows that no read will produce.
    expect(screen.queryByText(EN.notifications.loading)).toBeNull();
    expect(screen.queryByText(EN.notifications.scopeAllContracts)).toBeNull();
    // The way out is offered twice — by the filter form and by the refusal itself —
    // and both point at the unscoped address, so either click is the escape.
    const escapes = screen.getAllByRole("button", { name: EN.notifications.contractClear });
    fireEvent.click(escapes[escapes.length - 1]!);
    expect(router.replace).toHaveBeenCalledWith("/en/notifications");
  });

  it("filters by replacing the address with the trimmed id", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([row()]));
    const { form } = renderScreen();

    await screen.findByText(titleOf(row()));
    typeFilter(" 12 ");
    fireEvent.submit(form);

    await waitFor(() => {
      // `replace`, not `push`: a half-typed id followed by a few corrections is not
      // several pages an operator wants to step back through.
      expect(router.replace).toHaveBeenCalledWith("/en/notifications?contract=12");
    });
    expect(router.push).not.toHaveBeenCalled();
  });

  it("will not navigate on an id that cannot be a contract", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([row()]));
    const { form } = renderScreen();

    await screen.findByText(titleOf(row()));
    typeFilter("12abc");

    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: EN.notifications.contractApply })
        .disabled,
    ).toBe(true);
    fireEvent.submit(form);

    // The screen says why and stays put; it does not hand the router a value the
    // reader's own parser would then reject as malformed.
    expect(alertTexts()).toStrictEqual([EN.notifications.contractInvalid]);
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("treats an emptied box as no filter at all", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    const scoped = row({ contract_id: 12 });
    readAnswers(listing([scoped], { contract_id: 12 }));
    const { form } = renderScreen();

    await screen.findByText(titleOf(scoped));
    typeFilter("");
    fireEvent.submit(form);

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en/notifications");
    });
  });
});

describe("the chips re-ask the server", () => {
  it("narrows by status and leaves exactly one chip current", async () => {
    setSession(sessionFor("operator"));
    const parked = row({ status: "failed", last_error: "relay refused" });
    readAnswers(listing([row()]), listing([parked]));
    renderScreen();

    await screen.findByText(titleOf(row()));
    fireEvent.click(screen.getByRole("button", { name: EN.notifications.statuses.failed }));

    await waitFor(() => {
      expect(vi.mocked(notifications.list).mock.calls[1]![1]).toStrictEqual({ status: "failed" });
    });
    // One current chip *per group*: `All` must lose it at status while keeping it at
    // channel, which is why this counts the buttons carrying `aria-current` rather
    // than asking for one of them by name — both groups render a button called "All".
    const current = screen
      .getAllByRole("button")
      .filter((node) => node.getAttribute("aria-current") === "true");
    expect(current.map((node) => node.textContent)).toStrictEqual([
      EN.notifications.statuses.failed,
      EN.notifications.filterAll,
    ]);
  });

  it("narrows by channel, and combines with the status already chosen", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([row()]));
    renderScreen();

    await screen.findByText(titleOf(row()));
    fireEvent.click(screen.getByRole("button", { name: EN.notifications.channels.sms }));

    await waitFor(() => {
      expect(vi.mocked(notifications.list).mock.calls[1]![1]).toStrictEqual({ channel: "sms" });
    });

    fireEvent.click(screen.getByRole("button", { name: EN.notifications.statuses.delivered }));

    // The two filters are independent state and travel together: a screen that kept
    // only the last click would answer a different question after the second chip.
    await waitFor(() => {
      expect(vi.mocked(notifications.list).mock.calls[2]![1]).toStrictEqual({
        status: "delivered",
        channel: "sms",
      });
    });
  });
});

describe("sending asks first; requeueing does not", () => {
  it("offers the two actions by status, and a landed row gets neither", async () => {
    const waiting = row({ id: 41, status: "pending" });
    const parked = row({ id: 42, template: "T-14", status: "failed" });
    const landed = row({
      id: 43,
      template: "T-7",
      status: "delivered",
      sent_at: "2026-11-04T09:00:00Z",
    });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting, parked, landed]));
    renderScreen();

    await screen.findByText(titleOf(landed));

    expect(within(card(waiting)).getByRole("button", { name: EN.notifications.sendNow })).toBeDefined();
    expect(
      within(card(waiting)).queryByRole("button", { name: EN.notifications.retryAction }),
    ).toBeNull();

    expect(
      within(card(parked)).getByRole("button", { name: EN.notifications.retryAction }),
    ).toBeDefined();
    // Sending a `failed` row again is not the retry path — the budget is spent and
    // the policy refuses it, so the button is not offered.
    expect(within(card(parked)).queryByRole("button", { name: EN.notifications.sendNow })).toBeNull();

    // The system having worked leaves nothing to do on the row at all.
    for (const label of [EN.notifications.sendNow, EN.notifications.retryAction]) {
      expect(within(card(landed)).queryByRole("button", { name: label })).toBeNull();
    }
  });

  it("calls nothing on the first click, and names the consequence before sending", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]));
    vi.mocked(notifications.dispatch).mockResolvedValue(outcome({ notification: waiting }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));

    // `/dispatch` mails a customer immediately, and a single mis-click cannot be
    // undone: the first click only asks.
    expect(notifications.dispatch).not.toHaveBeenCalled();
    const root = card(waiting);
    expect(within(root).getByText(EN.notifications.sendConfirm)).toBeDefined();
    expect(within(root).getByText(EN.notifications.sendConfirmHint)).toBeDefined();

    fireEvent.click(within(root).getByRole("button", { name: EN.notifications.confirmSend }));

    await waitFor(() => {
      expect(notifications.dispatch).toHaveBeenCalledWith(ACCESS, 41);
    });
  });

  it("cancels the confirmation without sending anything", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(waiting)).getByRole("button", { name: EN.notifications.cancel }));

    expect(notifications.dispatch).not.toHaveBeenCalled();
    const root = card(waiting);
    expect(within(root).queryByText(EN.notifications.sendConfirm)).toBeNull();
    // Back to the offer, so the row is not left in a state with no way forward.
    expect(within(root).getByRole("button", { name: EN.notifications.sendNow })).toBeDefined();
  });

  it("requeues on the first click and reloads the queue", async () => {
    const parked = row({ id: 42, status: "failed", attempts: 3, last_error: "relay refused" });
    setSession(sessionFor("operator"));
    readAnswers(listing([parked]));
    vi.mocked(notifications.retry).mockResolvedValue(row({ id: 42, status: "pending", attempts: 0 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.retryAction }));

    await waitFor(() => {
      expect(notifications.retry).toHaveBeenCalledWith(ACCESS, 42);
    });
    // Nothing is sent: the scheduler still has to find the row due. Asking first
    // here would teach operators to dismiss the dialog that does matter.
    expect(screen.queryByText(EN.notifications.sendConfirm)).toBeNull();
    expect(await statusText()).toBe(EN.notifications.retrySucceeded);
    // The row is `pending` again, so the page the operator is reading has changed.
    await waitFor(() => {
      expect(notifications.list).toHaveBeenCalledTimes(2);
    });
  });
});

describe("what a dispatch attempt reports", () => {
  it("says it was sent, and reloads because the row moved", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]), listing([row({ id: 41, status: "sent" })]));
    vi.mocked(notifications.dispatch).mockResolvedValue(outcome({ sent: true }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(waiting)).getByRole("button", { name: EN.notifications.confirmSend }));

    expect(await statusText()).toBe(EN.notifications.sendSucceeded);
    await waitFor(() => {
      expect(notifications.list).toHaveBeenCalledTimes(2);
    });
  });

  it("reads a quiet-hours deferral as the system working, not as a failed send", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]));
    // A deferral is a success with a different meaning, and the envelope can still
    // say `sent: true` for a row whose attempt was moved. Ordering the branches the
    // other way round would report every deferral as delivered.
    vi.mocked(notifications.dispatch).mockResolvedValue(
      outcome({ sent: true, deferred_until: "2026-11-05T09:30:00Z" }),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(waiting)).getByRole("button", { name: EN.notifications.confirmSend }));

    const said = await statusText();
    expect(said).not.toBe(EN.notifications.sendSucceeded);
    expect(said.startsWith("Deferred to ")).toBe(true);
    expect(said).toContain("— outside the customer's contact window.");
  });

  it("names the recorded transport failure instead of a generic sentence", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]));
    vi.mocked(notifications.dispatch).mockResolvedValue(
      outcome({ sent: false, error: "550 relay refused" }),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(waiting)).getByRole("button", { name: EN.notifications.confirmSend }));

    // `sent: false` is not an error envelope — a transport failure is a *recorded
    // attempt* — so the row's own words are what the operator needs.
    expect(await statusText()).toBe(
      say(EN.notifications.sendAttemptFailed, { error: "550 relay refused" }),
    );
  });

  it("falls back to a generic sentence when the answer says nothing", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]));
    vi.mocked(notifications.dispatch).mockResolvedValue(
      outcome({ sent: false, deferred_until: null, error: null }),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(waiting)).getByRole("button", { name: EN.notifications.confirmSend }));

    expect(await statusText()).toBe(EN.notifications.failed);
  });
});

describe("refusals", () => {
  it("names the refusal on the row it happened to, and reloads because the row moved", async () => {
    const first = row({ id: 41, status: "pending" });
    const second = row({ id: 44, contract_id: 9, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([first, second]));
    vi.mocked(notifications.dispatch).mockRejectedValue(
      new ApiError(409, "CONTRACT_CLOSED", "The contract is closed."),
    );
    renderScreen();

    await screen.findByText(titleOf(second));
    fireEvent.click(within(card(first)).getByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(first)).getByRole("button", { name: EN.notifications.confirmSend }));

    await waitFor(() => {
      expect(within(card(first)).getByRole("alert").textContent).toBe(
        EN.notifications.refusalContractClosed,
      );
    });
    // One shared message would blame every reminder on the page for one refusal.
    expect(within(card(second)).queryByRole("alert")).toBeNull();
    // A refusal can park the row before answering, so the list is stale either way.
    await waitFor(() => {
      expect(notifications.list).toHaveBeenCalledTimes(2);
    });
  });

  it("does not reload for a failure that changed nothing, and still says something", async () => {
    const waiting = row({ id: 41, status: "pending" });
    setSession(sessionFor("operator"));
    readAnswers(listing([waiting]));
    // A code this screen has never heard of: the map is looked up with a fallback
    // rather than trusted to be exhaustive, and a 500 moved no row.
    vi.mocked(notifications.dispatch).mockRejectedValue(
      new ApiError(500, "SOMETHING_NEW", "Unexpected."),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.notifications.sendNow }));
    fireEvent.click(within(card(waiting)).getByRole("button", { name: EN.notifications.confirmSend }));

    await waitFor(() => {
      expect(within(card(waiting)).getByRole("alert").textContent).toBe(EN.notifications.failed);
    });
    expect(notifications.list).toHaveBeenCalledTimes(1);
  });

  it("translates a role refusal and an unreachable API on the queue itself", async () => {
    setSession(sessionFor("operator"));
    const readMock = vi.mocked(notifications.list);
    readMock.mockRejectedValueOnce(new ApiError(403, "FORBIDDEN", "Nope."));
    readMock.mockResolvedValueOnce(listing([row()]));
    renderScreen();

    expect(await screen.findByRole("alert")).toBeDefined();
    expect(alertTexts()).toStrictEqual([EN.notifications.forbidden]);

    fireEvent.click(screen.getByRole("button", { name: EN.notifications.retry }));

    expect(await screen.findByText(titleOf(row()))).toBeDefined();
    expect(alertTexts()).toStrictEqual([]);
  });
});

describe("the queue itself", () => {
  it("does not let an unreachable API look like an empty queue", async () => {
    setSession(sessionFor("operator"));
    const readMock = vi.mocked(notifications.list);
    readMock.mockRejectedValueOnce(new ApiError(0, "unreachable", "The API is not reachable."));
    readMock.mockResolvedValueOnce(listing([row()]));
    renderScreen();

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.notifications.offline]);
    });
    // "Nothing is scheduled" and "we could not ask" must never read the same.
    expect(screen.queryByText(EN.notifications.empty)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: EN.notifications.retry }));

    expect(await screen.findByText(titleOf(row()))).toBeDefined();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("keeps a scoped empty apart from an empty queue", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    readAnswers(listing([], { contract_id: 12 }));
    renderScreen();

    expect(await screen.findByText(EN.notifications.emptyForContract)).toBeDefined();
    // "This contract has no reminder" is a good answer about one contract and must
    // not read like nothing having been scheduled for anyone.
    expect(screen.queryByText(EN.notifications.empty)).toBeNull();
    expect(screen.getByText(EN.notifications.emptyHint)).toBeDefined();
  });
});

describe("what a row states", () => {
  it("reports the parked failure, the attempts and the provider id", async () => {
    const parked = row({
      id: 42,
      status: "failed",
      template: "T-14",
      channel: "sms",
      attempts: 3,
      last_error: "550 relay refused",
      provider_message_id: "prov-abc-123",
    });
    setSession(sessionFor("operator"));
    readAnswers(listing([parked]));
    renderScreen();

    const root = await waitFor(() => card(parked));
    // The milestone is rendered as stored: it is configured, not a closed enum, so a
    // message-key map here would silently miss the day the schedule changes.
    expect(within(root).getByText("T-14")).toBeDefined();
    expect(within(root).getByText(EN.notifications.channels.sms)).toBeDefined();
    expect(within(root).getByText("3")).toBeDefined();
    expect(within(root).getByText("550 relay refused")).toBeDefined();
    expect(within(root).getByText("prov-abc-123")).toBeDefined();
    // Never sent, so the `Sent` row says so rather than being blank.
    expect(within(root).getByText(EN.notifications.notSent)).toBeDefined();
  });

  it("says nothing about a deferral or a failure that never happened", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([row()]));
    renderScreen();

    const root = await waitFor(() => card(row()));
    expect(within(root).queryByText(EN.notifications.queuedAtLabel)).toBeNull();
    expect(within(root).queryByText(EN.notifications.lastErrorLabel)).toBeNull();
    expect(within(root).queryByText(EN.notifications.providerLabel)).toBeNull();
  });

  it("shows the instant quiet hours deferred a row to", async () => {
    const deferred = row({ status: "pending", scheduled_at: "2026-11-05T09:30:00Z" });
    setSession(sessionFor("operator"));
    readAnswers(listing([deferred]));
    renderScreen();

    const root = await waitFor(() => card(deferred));
    // `scheduled_at` is an instant and `scheduled_date` a calendar date: only a
    // deferred row has the first, and only then is the label worth showing.
    expect(within(root).getByText(EN.notifications.queuedAtLabel)).toBeDefined();
  });
});
