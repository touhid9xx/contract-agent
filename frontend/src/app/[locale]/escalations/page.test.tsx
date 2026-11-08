// @vitest-environment jsdom
/**
 * The escalation queue — the third screen under test, and the first whose *state
 * lives in the address* rather than in `useState`.
 *
 * `login/page.test.tsx` proves a session can be obtained and `review/page.test.tsx`
 * proves a queue can be worked. Neither could reach the rule this screen carries:
 * `contract-scope.test.ts` parses `?contract=` correctly and proves nothing about
 * whether a *page* obeys the parser. `D10-36` moved both filters into the query
 * string on the promise that the back button, a bookmark and a shared link all
 * keep working; until now that promise was prose.
 *
 * The harness adds no dependency and invents no provider:
 *
 *   * `QueryClientProvider` is real, with a client built per test rather than
 *     reused, so one test's cache cannot answer another's question.
 *   * `retry: false` is the one deviation from the app's provider (`retry: 1`)
 *     and it is deliberate: these tests ask *how often the screen asked*, and a
 *     retry policy would answer that question in its place.
 *   * the real `NextIntlClientProvider` and the real message catalogue, so every
 *     assertion is against the string a user reads.
 *   * the real `AuthProvider` and the real session store — the screen's role
 *     gating is read from the session, not from a prop.
 *   * only `escalations.queue`, `escalations.update` and `escalations.recordResponse`
 *     are replaced, and `importOriginal` keeps the real `ApiError`, because the
 *     screen branches on `instanceof` and on `cause.code`.
 *   * `next/navigation` is the second double, and it is the interesting one: there
 *     is no Next runtime here, so `useSearchParams` reads a mutable address this
 *     test writes. That is not a weakness for these assertions — the screen must
 *     derive what it asks the server from the address alone, and a real router
 *     would only add a rendering pass to the same question.
 *
 * What is pinned, each a rule the source states in prose:
 *
 *   * the working queue asks for `open` + `in_progress` and sends **no**
 *     `contractId` key at all, rather than an `undefined` one,
 *   * a scoped address narrows that same read and says which contract it is,
 *   * an address naming something that cannot be a contract performs **no read** —
 *     falling through to the tenant-wide queue would answer a question nobody
 *     asked,
 *   * filters change by navigation (`replace`, so toggling twice leaves one history
 *     entry) and each navigation carries the *other* filter rather than dropping it,
 *   * a typed id is submitted trimmed and its unusable form is never navigated,
 *   * claim sends no status, because the server derives the transition,
 *   * resolve echoes the `version` it was shown and omits an empty note rather than
 *     storing a note that says nothing,
 *   * resolving is not responding: the reply route is called with a channel, and
 *     the outcome is reported on the page because the card usually disappears,
 *   * the two `409` codes this route returns are named separately,
 *   * lateness is the server's `overdue` flag, never recomputed from the deadline,
 *   * "the API is down" never looks like "nothing is late", and a scoped empty means
 *     something different from an unscoped one.
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
    escalations: {
      queue: vi.fn(),
      update: vi.fn(),
      recordResponse: vi.fn(),
    },
  };
});

import EscalationsPage from "@/app/[locale]/escalations/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, escalations } from "@/lib/api";
import type {
  Escalation,
  EscalationQueue,
  EscalationStatus,
  RecordedResponse,
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

const BASE_ROW: Escalation = {
  id: 31,
  contract_id: 7,
  status: "open",
  assigned_to: null,
  sla_deadline: "2026-11-06T09:00:00Z",
  resolved_at: null,
  resolution_note: null,
  overdue: false,
  version: 4,
};

function row(overrides: Partial<Escalation> = {}): Escalation {
  return { ...BASE_ROW, ...overrides };
}

function queue(rows: Escalation[], overrides: Partial<EscalationQueue> = {}): EscalationQueue {
  return {
    items: rows,
    count: rows.length,
    overdue: rows.filter((entry) => entry.overdue).length,
    sla_hours: 48,
    contract_id: null,
    ...overrides,
  };
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
 * An update invalidates the queue, so "one read" is not a given: a single
 * `mockResolvedValue` would let the second read return the first answer by
 * accident and hide the refetch.
 */
function queueAnswers(first: EscalationQueue, ...rest: EscalationQueue[]) {
  const answers = [first, ...rest];
  const queueMock = vi.mocked(escalations.queue);
  let served = 0;
  queueMock.mockImplementation(async () => {
    const answer = answers[Math.min(served, answers.length - 1)] ?? first;
    served += 1;
    return answer;
  });
  return queueMock;
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
          <EscalationsPage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
  return { ...view, form: view.container.querySelector("form")! as HTMLFormElement };
}

/** The card's own heading text, which is also how these tests find a row. */
function titleOf(entry: Escalation): string {
  return `${say(EN.escalations.contractLabel, { id: entry.contract_id })} · ${
    EN.escalations.statuses[entry.status as EscalationStatus]
  }`;
}

/**
 * A row's card.
 *
 * The page marks each card with `aria-busy`, so that attribute — a real thing
 * the component sets, rather than a class chosen for the test — is the handle.
 */
function card(entry: Escalation): HTMLElement {
  const heading = screen.getByText(titleOf(entry));
  const root = heading.closest("[aria-busy]");
  if (root === null) {
    throw new Error(`No escalation card found for "${titleOf(entry)}".`);
  }
  return root as HTMLElement;
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

beforeEach(() => {
  setSession(null);
  address.current = "";
  vi.mocked(escalations.queue).mockReset();
  vi.mocked(escalations.update).mockReset();
  vi.mocked(escalations.recordResponse).mockReset();
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

    expect(screen.getByText(EN.escalations.signInRequired)).toBeDefined();
    // `authorized` refuses before a request is built, so nothing leaves the tab.
    expect(escalations.queue).not.toHaveBeenCalled();
    // A 401 is not a queue failure: reporting it would say the queue is broken.
    expect(alertTexts()).toStrictEqual([]);
    expect(screen.getByRole("link", { name: EN.escalations.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );
  });

  it("lets a viewer read the queue but offers no way to work it", async () => {
    setSession(sessionFor("viewer"));
    queueAnswers(queue([row()]));
    renderScreen();

    expect(await screen.findByText(titleOf(row()))).toBeDefined();
    expect(screen.getByText(EN.escalations.viewerNote)).toBeDefined();

    for (const label of [EN.escalations.claim, EN.escalations.resolve]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
    // Recording a reply closes the escalation server-side; a viewer writing one
    // would be resolving a row through the back door.
    for (const label of [EN.escalations.channelEmail, EN.escalations.channelSms]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
  });
});

describe("the address is the state", () => {
  it("asks for the working queue and sends no contract filter at all", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row()]));
    renderScreen();

    await screen.findByText(titleOf(row()));
    // `toStrictEqual`, not a subset match: an `undefined` `contractId` that is
    // *present* would still be a filter the client put in the address bar.
    expect(vi.mocked(escalations.queue).mock.calls[0]![1]).toStrictEqual({
      statuses: ["open", "in_progress"],
    });
    expect(screen.getByText(EN.escalations.scopeAllContracts)).toBeDefined();
  });

  it("narrows the same read to one contract and says which one", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    queueAnswers(queue([row({ contract_id: 12 })], { contract_id: 12 }));
    renderScreen();

    expect(await screen.findByText(titleOf(row({ contract_id: 12 })))).toBeDefined();
    expect(vi.mocked(escalations.queue).mock.calls[0]![1]).toStrictEqual({
      statuses: ["open", "in_progress"],
      contractId: 12,
    });
    // The header reads the server's echo rather than the address: the sentence
    // must not claim a narrower list than the one on screen.
    expect(screen.getByText(say(EN.escalations.scopeContract, { id: 12 }))).toBeDefined();
    expect(screen.getByText(say(EN.escalations.slaLabel, { hours: 48 }))).toBeDefined();
  });

  it("refuses to read anything when the address names an impossible contract", () => {
    setSession(sessionFor("operator"));
    address.current = "contract=012";
    renderScreen();

    // The most damaging confusion this screen could make: showing the tenant's
    // whole queue behind a filter that never ran, which looks like an answer.
    expect(escalations.queue).not.toHaveBeenCalled();
    expect(alertTexts()).toStrictEqual([EN.escalations.contractInvalid]);
    expect(screen.getByText(EN.escalations.contractInvalidHint)).toBeDefined();
    expect(screen.queryByText(EN.escalations.scopeAllContracts)).toBeNull();
  });

  it("shows resolved rows when the address asks for them", async () => {
    setSession(sessionFor("operator"));
    address.current = "resolved=1";
    queueAnswers(queue([row({ status: "resolved", resolved_at: "2026-11-05T10:00:00Z" })]));
    renderScreen();

    await screen.findByText(titleOf(row({ status: "resolved" })));
    // No status filter at all is the server's "everything" — the toggle must not
    // send the working statuses and then hope the server disagrees.
    expect(vi.mocked(escalations.queue).mock.calls[0]![1]).toStrictEqual({ statuses: undefined });
    expect(screen.getByRole("button", { name: EN.escalations.hideResolved })).toBeDefined();
  });

  it("filters by navigating, carrying the other filter with it", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    queueAnswers(queue([row({ contract_id: 12 })], { contract_id: 12 }));
    const { form } = renderScreen();

    await screen.findByText(titleOf(row({ contract_id: 12 })));

    fireEvent.change(screen.getByLabelText(EN.escalations.contractFilterLabel), {
      target: { value: " 12 " },
    });
    fireEvent.submit(form);

    await waitFor(() => {
      // Trimmed, and `replace` rather than `push`: toggling a filter twice must
      // not leave two history entries for one question.
      expect(router.replace).toHaveBeenCalledWith("/en/escalations?contract=12");
    });
    expect(router.push).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: EN.escalations.showResolved }));

    await waitFor(() => {
      // The scope survives the toggle; a filter that silently evaporates is the
      // exact failure `D10-35` left these links plain to avoid.
      expect(router.replace).toHaveBeenLastCalledWith("/en/escalations?contract=12&resolved=1");
    });
  });

  it("will not navigate on an id that cannot be a contract", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row()]));
    const { form } = renderScreen();

    await screen.findByText(titleOf(row()));
    fireEvent.change(screen.getByLabelText(EN.escalations.contractFilterLabel), {
      target: { value: "12abc" },
    });

    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: EN.escalations.contractApply }).disabled,
    ).toBe(true);
    fireEvent.submit(form);

    // The screen says why and stays put; it does not hand the router a value the
    // reader would then reject as malformed.
    expect(alertTexts()).toStrictEqual([EN.escalations.contractInvalid]);
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("clears the filter by navigating back to the unfiltered address", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12&resolved=1";
    queueAnswers(queue([row({ contract_id: 12 })], { contract_id: 12 }));
    renderScreen();

    await screen.findByText(titleOf(row({ contract_id: 12 })));
    fireEvent.click(screen.getByRole("button", { name: EN.escalations.contractClear }));

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en/escalations?resolved=1");
    });
  });

  it("submits nothing at all when the filter is empty", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row()]));
    const { form } = renderScreen();

    await screen.findByText(titleOf(row()));
    fireEvent.submit(form);

    // An empty box is "no filter", and "no filter" is already the address: there
    // is nothing to navigate to and the reader is not sent to `?contract=`.
    expect(router.replace).not.toHaveBeenCalled();
    expect(alertTexts()).toStrictEqual([]);
  });
});

describe("claiming and resolving", () => {
  it("claims by assigning the signed-in operator, and sends no status", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ version: 4 })]));
    vi.mocked(escalations.update).mockResolvedValue(row({ assigned_to: "u-1", version: 5 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.claim }));

    await waitFor(() => {
      // No `status`: the server moves an `open` row to `in_progress` when an
      // assignee is set, and duplicating that rule here would let them disagree.
      expect(escalations.update).toHaveBeenCalledWith(ACCESS, 31, {
        assigned_to: "u-1",
        version: 4,
      });
    });
  });

  it("resolves with the version it was shown and omits an empty note", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ version: 4 })]));
    vi.mocked(escalations.update).mockResolvedValue(row({ status: "resolved", version: 5 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.resolve }));
    fireEvent.change(screen.getByLabelText(EN.escalations.resolveLabel), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: EN.escalations.resolveSubmit }));

    await waitFor(() => {
      expect(escalations.update).toHaveBeenCalledTimes(1);
    });
    // `toStrictEqual` on the payload: an omitted note and a note of `undefined`
    // are different requests, and only one of them is what this screen means.
    expect(vi.mocked(escalations.update).mock.calls[0]![2]).toStrictEqual({
      status: "resolved",
      version: 4,
    });
  });

  it("trims a note that says something", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ version: 4 })]));
    vi.mocked(escalations.update).mockResolvedValue(row({ status: "resolved", version: 5 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.resolve }));
    fireEvent.change(screen.getByLabelText(EN.escalations.resolveLabel), {
      target: { value: "  Called back, signing next week  " },
    });
    fireEvent.click(screen.getByRole("button", { name: EN.escalations.resolveSubmit }));

    await waitFor(() => {
      expect(escalations.update).toHaveBeenCalledWith(ACCESS, 31, {
        status: "resolved",
        version: 4,
        resolution_note: "Called back, signing next week",
      });
    });
  });

  it("cancels the note without sending anything", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row()]));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.resolve }));
    fireEvent.change(screen.getByLabelText(EN.escalations.resolveLabel), {
      target: { value: "half a thought" },
    });
    fireEvent.click(screen.getByRole("button", { name: EN.escalations.cancel }));

    expect(escalations.update).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(EN.escalations.resolveLabel)).toBeNull();
  });

  it("reloads the queue after a landed change instead of splicing the row", async () => {
    setSession(sessionFor("operator"));
    // The server answers the second read without the row: it was resolved, so it
    // is no longer part of the working queue.
    queueAnswers(queue([row()]), queue([]));
    vi.mocked(escalations.update).mockResolvedValue(row({ status: "resolved", version: 5 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.claim }));

    await waitFor(() => {
      expect(escalations.queue).toHaveBeenCalledTimes(2);
    });
    expect(screen.queryByText(titleOf(row()))).toBeNull();
    expect(screen.getByText(EN.escalations.empty)).toBeDefined();
  });

  it("names the two refusals apart, on the row that caused it and no other", async () => {
    const first = row({ id: 31, contract_id: 7 });
    const second = row({ id: 32, contract_id: 8, version: 1 });
    setSession(sessionFor("operator"));
    queueAnswers(queue([first, second]));
    vi.mocked(escalations.update).mockRejectedValue(
      new ApiError(409, "ESCALATION_CLOSED", "Already resolved."),
    );
    renderScreen();

    await screen.findByText(titleOf(second));
    fireEvent.click(within(card(first)).getByRole("button", { name: EN.escalations.claim }));

    await waitFor(() => {
      expect(within(card(first)).getByRole("alert").textContent).toBe(
        EN.escalations.closedRefusal,
      );
    });
    // "Someone closed this under you" and "that is not a move this queue allows"
    // are different facts about different rows; one shared sentence would be a
    // lie about one of them.
    expect(within(card(first)).queryByText(EN.escalations.invalidTransition)).toBeNull();
    expect(within(card(second)).queryByRole("alert")).toBeNull();
  });

  it("says a hand-set status is not a move the queue allows", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row()]));
    vi.mocked(escalations.update).mockRejectedValue(
      new ApiError(409, "INVALID_TRANSITION", "Not a legal transition."),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.claim }));

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.escalations.invalidTransition]);
    });
  });
});

describe("resolve is not respond", () => {
  it("records a reply on the channel that was clicked, never a body", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ contract_id: 7 })]));
    const recorded: RecordedResponse = {
      response: {
        id: 5,
        contract_id: 7,
        channel: "email",
        response_type: "reply",
        received_at: "2026-11-04T12:00:00Z",
      },
      contract_id: 7,
      contract_status: "responded",
      resolved_escalation_id: 31,
    };
    vi.mocked(escalations.recordResponse).mockResolvedValue(recorded);
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.channelEmail }));

    await waitFor(() => {
      expect(escalations.recordResponse).toHaveBeenCalledWith(ACCESS, 7, "email");
    });
    // The reply is a different route from the operator's own close: only this one
    // moves the contract, so the update route must not also have been called.
    expect(escalations.update).not.toHaveBeenCalled();
  });

  it("reports a recorded reply on the page, because the card usually leaves", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ contract_id: 7 })]), queue([]));
    vi.mocked(escalations.recordResponse).mockResolvedValue({
      response: {
        id: 5,
        contract_id: 7,
        channel: "sms",
        response_type: "reply",
        received_at: "2026-11-04T12:00:00Z",
      },
      contract_id: 7,
      contract_status: "responded",
      resolved_escalation_id: 31,
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.channelSms }));

    const status = await screen.findByRole("status");
    expect(status.textContent).toContain(
      say(EN.escalations.replyRecorded, {
        status: EN.escalations.contractStatuses.responded,
      }),
    );
    expect(status.textContent).toContain(EN.escalations.replyClosedEscalation);
    // The contract answered, so the open row is gone and the message outlived it.
    expect(await screen.findByText(EN.escalations.empty)).toBeDefined();
  });

  it("says so when the reply closed nothing", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ contract_id: 7 })]));
    vi.mocked(escalations.recordResponse).mockResolvedValue({
      response: {
        id: 5,
        contract_id: 7,
        channel: "voice",
        response_type: "call_answered",
        received_at: "2026-11-04T12:00:00Z",
      },
      contract_id: 7,
      // A late reply is still a reply: the contract had already closed.
      contract_status: "renewed",
      resolved_escalation_id: null,
    });
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.channelVoice }));

    const status = await screen.findByRole("status");
    expect(status.textContent).toContain(EN.escalations.replyNoEscalation);
    expect(status.textContent).not.toContain(EN.escalations.replyClosedEscalation);
  });

  it("reports a refused reply without blanking the queue", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(queue([row({ contract_id: 7 })]));
    vi.mocked(escalations.recordResponse).mockRejectedValue(
      new ApiError(403, "forbidden", "Nope."),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.escalations.channelEmail }));

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.escalations.forbidden]);
    });
    expect(screen.getByText(titleOf(row({ contract_id: 7 })))).toBeDefined();
  });
});

describe("the queue itself", () => {
  it("takes lateness from the server rather than recomputing it", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(
      queue([
        // Both deadlines are in the past: only the server's flag may decide
        // which one wears the badge, because it used the same clock that wrote
        // `sla_deadline` and this browser's is not that clock.
        row({ id: 31, contract_id: 7, overdue: true, sla_deadline: "2020-01-01T09:00:00Z" }),
        row({ id: 32, contract_id: 8, overdue: false, sla_deadline: "2020-01-01T09:00:00Z" }),
      ]),
    );
    renderScreen();

    await screen.findByText(titleOf(row({ id: 32, contract_id: 8 })));
    expect(within(card(row({ id: 31, contract_id: 7 }))).getByText(EN.escalations.overdueBadge))
      .toBeDefined();
    expect(
      within(card(row({ id: 32, contract_id: 8 }))).queryByText(EN.escalations.overdueBadge),
    ).toBeNull();
    expect(screen.getByText(say(EN.escalations.overdueLabel, { overdue: 1 }))).toBeDefined();
  });

  it("shows who holds a row, and only calls the signed-in operator 'you'", async () => {
    const mine = row({ id: 31, contract_id: 7, status: "in_progress", assigned_to: "u-1" });
    const theirs = row({ id: 32, contract_id: 8, status: "in_progress", assigned_to: "u-9" });
    const nobody = row({ id: 33, contract_id: 9 });
    setSession(sessionFor("operator"));
    queueAnswers(queue([mine, theirs, nobody]));
    renderScreen();

    await screen.findByText(titleOf(nobody));
    expect(within(card(mine)).getByText(EN.escalations.you)).toBeDefined();
    expect(within(card(theirs)).getByText("u-9".slice(0, 8))).toBeDefined();
    expect(within(card(nobody)).getByText(EN.escalations.unassigned)).toBeDefined();
    // A row already in the operator's own hands offers no Claim button: the
    // action would be a no-op that reads as progress.
    expect(within(card(mine)).queryByRole("button", { name: EN.escalations.claim })).toBeNull();
  });

  it("does not let an unreachable API look like an empty queue", async () => {
    setSession(sessionFor("operator"));
    const queueMock = vi.mocked(escalations.queue);
    queueMock.mockRejectedValueOnce(new ApiError(0, "unreachable", "The API is not reachable."));
    queueMock.mockResolvedValueOnce(queue([row()]));
    renderScreen();

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.escalations.offline]);
    });
    // "Nothing is late" and "we could not ask" must never read the same.
    expect(screen.queryByText(EN.escalations.empty)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: EN.escalations.retry }));

    expect(await screen.findByText(titleOf(row()))).toBeDefined();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("keeps a scoped empty apart from an empty queue", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    queueAnswers(queue([], { contract_id: 12 }));
    renderScreen();

    expect(await screen.findByText(EN.escalations.emptyForContract)).toBeDefined();
    // "This contract has nothing waiting" is a good answer about one contract and
    // must not read like the queue itself being clear.
    expect(screen.queryByText(EN.escalations.empty)).toBeNull();
    expect(screen.getByText(EN.escalations.emptyHint)).toBeDefined();
  });
});
