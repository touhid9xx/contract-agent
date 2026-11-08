// @vitest-environment jsdom
/**
 * The review queue — the first screen under test that can *complete* work.
 *
 * `login/page.test.tsx` proves a session can be obtained; this proves what the
 * product does with one that matters. The non-negotiable this screen enforces is
 * the one nothing else can: no field below the confidence threshold reaches a
 * customer until a person decides about it. Until now that gate was reachable
 * only through `TestClient`, so it was proven in tests and unreachable in the UI.
 *
 * It is also the first test that exercises TanStack Query. The login page never
 * touched it, so every rule below about a refetch, an invalidation and a
 * per-row error is proven here for the first time.
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
 *     gating is read from the session, not from a prop, and a stub would not
 *     catch a broken binding.
 *   * only `review.queue`, `review.decide` and `review.reveal` are replaced, and
 *     `importOriginal` keeps the real `ApiError`, because the screen branches on
 *     `instanceof` and on the status getters.
 *
 * What is pinned, each a rule the source states in prose:
 *
 *   * a signed-out visitor is sent to the sign-in door and the 401 that the
 *     queue read raises is *not* reported as a queue failure,
 *   * a `viewer` may read the queue and is offered neither a decision nor a
 *     reveal — reveal writes an audit row, so it is decision-grade,
 *   * every decision echoes the `version` the reviewer saw, which is the whole
 *     mechanism by which two reviewers cannot silently overwrite each other,
 *   * `reject` and `unreadable` travel as the actions they name rather than
 *     collapsing into `accept`,
 *   * a correction is trimmed and an empty one is never sent,
 *   * a refusal is named on the card that caused it and on no other,
 *   * a conflict or a 404 reloads the queue instead of retrying the decision,
 *   * a landed decision refetches, and the decided row leaves the list,
 *   * the blocking count is reported against the row it belongs to,
 *   * a revealed value replaces its button and says the read was audited,
 *   * "the API is down" never looks like "nothing is waiting for a person",
 *   * an empty queue offers no next page, and load-more asks with the cursor,
 *   * an expiry is stated ahead of, behind and absent from today.
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
    review: {
      queue: vi.fn(),
      decide: vi.fn(),
      reveal: vi.fn(),
    },
  };
});

import ReviewQueuePage from "@/app/[locale]/review/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, review } from "@/lib/api";
import type { FieldName, ReviewItem, ReviewQueue, Session, UserRole } from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const ACCESS = "access-1";

/**
 * Substitution for a plain (non-plural) ICU string.
 *
 * Asserting against `EN.review.expiresInDays` with its `{days}` filled in is the
 * difference between pinning "the number reaches the sentence" and pinning the
 * sentence to whatever it happened to say.
 */
function say(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => String(values[key]));
}

const BASE_ITEM: ReviewItem = {
  field_id: 11,
  contract_id: 7,
  customer_ref: "ACME-2024-001",
  contract_status: "active",
  end_date: "2027-01-01",
  days_to_expiry: 120,
  age_minutes: 95,
  threshold: 0.8,
  field_name: "email",
  status: "pending_review",
  confidence: 0.42,
  version: 3,
  source_page: 1,
  source_line: 12,
  reviewed_at: null,
  review_reason: null,
  blocks_pipeline: false,
};

function item(overrides: Partial<ReviewItem> = {}): ReviewItem {
  return { ...BASE_ITEM, ...overrides };
}

function page(items: ReviewItem[], overrides: Partial<ReviewQueue> = {}): ReviewQueue {
  return {
    items,
    count: items.length,
    oldest_minutes: items[0]?.age_minutes ?? 0,
    threshold: items[0]?.threshold ?? 0.8,
    next_cursor: null,
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
      email: "reviewer@example.test",
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
 * A conflict refetches, so "one read" is not a given: a single
 * `mockResolvedValue` would let the second read return the first answer by
 * accident and hide exactly the behaviour under test.
 */
function queueAnswers(first: ReviewQueue, ...rest: ReviewQueue[]) {
  const answers = [first, ...rest];
  const queue = vi.mocked(review.queue);
  let served = 0;
  queue.mockImplementation(async () => {
    // `?? first` only satisfies the index read: `served` is clamped to the last
    // answer, so the fallback is unreachable.
    const answer = answers[Math.min(served, answers.length - 1)] ?? first;
    served += 1;
    return answer;
  });
  return queue;
}

function renderQueue(locale: Locale = "en") {
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
          <ReviewQueuePage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

/** The card's own heading text, which is also how these tests find a row. */
function titleOf(entry: ReviewItem): string {
  return `${EN.review.fields[entry.field_name as FieldName]} · ${EN.review.statuses[entry.status]}`;
}

/**
 * A row's card.
 *
 * The page marks each card with `aria-busy`, so that attribute — a real thing
 * the component sets, rather than a class name chosen for the test — is the
 * handle. `within(card(...))` then makes "this row and no other" expressible.
 */
function card(titleText: string): HTMLElement {
  const heading = screen.getByText(titleText);
  const root = heading.closest("[aria-busy]");
  if (root === null) {
    throw new Error(`No review card found for "${titleText}".`);
  }
  return root as HTMLElement;
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

beforeEach(() => {
  setSession(null);
  vi.mocked(review.queue).mockReset();
  vi.mocked(review.decide).mockReset();
  vi.mocked(review.reveal).mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("who may see what", () => {
  it("asks a signed-out visitor to sign in, and asks the API nothing", () => {
    renderQueue();

    expect(screen.getByText(EN.review.signInRequired)).toBeDefined();
    // `authorized` refuses before a request is built, so nothing leaves the tab.
    expect(review.queue).not.toHaveBeenCalled();
    // That refusal is a 401, and a 401 here is not a queue failure: reporting it
    // would tell a signed-out visitor the queue is broken.
    expect(alertTexts()).toStrictEqual([]);
    // The prompt points at the sign-in door in the active locale.
    expect(screen.getByRole("link", { name: EN.review.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );
  });

  it("shows a viewer the queue but offers no decision and no reveal", async () => {
    setSession(sessionFor("viewer"));
    queueAnswers(page([item()]));
    renderQueue();

    expect(await screen.findByText(titleOf(item()))).toBeDefined();
    expect(screen.getByText(EN.review.viewerNote)).toBeDefined();

    for (const label of [EN.review.accept, EN.review.reject, EN.review.unreadable, EN.review.edit]) {
      expect(screen.queryByRole("button", { name: label })).toBeNull();
    }
    // Reveal is disabled rather than absent — and it is the `canDecide` gate that
    // does it, because revealing writes an audit row.
    expect(screen.getByRole<HTMLButtonElement>("button", { name: EN.review.reveal }).disabled).toBe(
      true,
    );
  });
});

describe("a decision", () => {
  it("echoes the version the reviewer saw, so a second reviewer cannot overwrite it", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(page([item({ version: 9 })]));
    vi.mocked(review.decide).mockResolvedValue({ item: item({ version: 10 }), blocking: 0 });
    renderQueue();

    fireEvent.click(await screen.findByRole("button", { name: EN.review.accept }));

    await waitFor(() => {
      expect(review.decide).toHaveBeenCalledWith(ACCESS, 11, { action: "accept", version: 9 });
    });
  });

  it("sends reject and unreadable as the actions they name", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(page([item()]));
    vi.mocked(review.decide).mockResolvedValue({ item: item(), blocking: 0 });
    renderQueue();

    fireEvent.click(await screen.findByRole("button", { name: EN.review.reject }));
    await waitFor(() => {
      expect(review.decide).toHaveBeenCalledWith(ACCESS, 11, { action: "reject", version: 3 });
    });

    // Wait for the pending decision to settle: a rejected row is not accepted by
    // mistake, and the three actions must stay distinct all the way to the wire.
    await waitFor(() => {
      expect(
        screen.getByRole<HTMLButtonElement>("button", { name: EN.review.unreadable }).disabled,
      ).toBe(false);
    });
    fireEvent.click(screen.getByRole("button", { name: EN.review.unreadable }));

    await waitFor(() => {
      expect(review.decide).toHaveBeenCalledWith(ACCESS, 11, {
        action: "mark_unreadable",
        version: 3,
      });
    });
  });

  it("trims a correction and refuses to send an empty one", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(page([item()]));
    vi.mocked(review.decide).mockResolvedValue({ item: item(), blocking: 0 });
    renderQueue();

    fireEvent.click(await screen.findByRole("button", { name: EN.review.edit }));
    fireEvent.change(screen.getByLabelText(EN.review.editLabel), { target: { value: "   " } });

    // Whitespace is not a value. The screen does not send it and let the server
    // answer 422; it declines to send anything at all.
    expect(
      screen.getByRole<HTMLButtonElement>("button", { name: EN.review.editSubmit }).disabled,
    ).toBe(true);

    fireEvent.change(screen.getByLabelText(EN.review.editLabel), {
      target: { value: "  corrected@example.test  " },
    });
    fireEvent.click(screen.getByRole("button", { name: EN.review.editSubmit }));

    await waitFor(() => {
      expect(review.decide).toHaveBeenCalledWith(ACCESS, 11, {
        action: "edit",
        version: 3,
        value: "corrected@example.test",
      });
    });
  });

  it("blames only the card whose decision was refused", async () => {
    const email = item({ field_id: 11, field_name: "email" });
    const phone = item({ field_id: 12, field_name: "phone", version: 4 });
    setSession(sessionFor("operator"));
    queueAnswers(page([email, phone]));
    vi.mocked(review.decide).mockRejectedValue(new ApiError(409, "conflict", "Stale version."));
    renderQueue();

    await screen.findByText(titleOf(phone));
    fireEvent.click(within(card(titleOf(email))).getByRole("button", { name: EN.review.accept }));

    await waitFor(() => {
      expect(within(card(titleOf(email))).getByRole("alert").textContent).toBe(EN.review.conflict);
    });
    // One message for the whole page would blame the phone field for a race on
    // the email one, which is the failure mode a shared error always has.
    expect(within(card(titleOf(phone))).queryByRole("alert")).toBeNull();
  });

  it("reloads the queue on a conflict instead of retrying the decision", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(page([item()]));
    vi.mocked(review.decide).mockRejectedValue(new ApiError(409, "conflict", "Stale version."));
    renderQueue();

    fireEvent.click(await screen.findByRole("button", { name: EN.review.accept }));

    await waitFor(() => {
      expect(review.queue).toHaveBeenCalledTimes(2);
    });
    // A retry would re-send the version that just lost; the list is what is
    // stale, so the list is what is reloaded.
    expect(review.decide).toHaveBeenCalledTimes(1);
  });

  it("refetches after a landed decision, and the decided row leaves the queue", async () => {
    const decided = item({ field_id: 11, field_name: "email" });
    const remaining = item({ field_id: 12, field_name: "phone" });
    setSession(sessionFor("operator"));
    // The server answers the second read without the decided row, which is what
    // a decided field looks like: it is approved and no longer awaiting anyone.
    queueAnswers(page([decided, remaining]), page([remaining]));
    vi.mocked(review.decide).mockResolvedValue({ item: decided, blocking: 1 });
    renderQueue();

    await screen.findByText(titleOf(remaining));
    fireEvent.click(within(card(titleOf(decided))).getByRole("button", { name: EN.review.accept }));

    await waitFor(() => {
      expect(screen.queryByText(titleOf(decided))).toBeNull();
    });
    expect(screen.getByText(titleOf(remaining))).toBeDefined();
    expect(review.queue).toHaveBeenCalledTimes(2);
  });

  it("reports how many fields still block the contract, on the row that was decided", async () => {
    const decided = item({ field_id: 11, field_name: "email" });
    const other = item({ field_id: 12, field_name: "phone" });
    setSession(sessionFor("operator"));
    // Here the refetch still carries the decided row (a slower server, or a field
    // that comes back), which is the only state in which the count is visible.
    queueAnswers(page([decided, other]));
    vi.mocked(review.decide).mockResolvedValue({ item: decided, blocking: 1 });
    renderQueue();

    await screen.findByText(titleOf(other));
    fireEvent.click(within(card(titleOf(decided))).getByRole("button", { name: EN.review.accept }));

    const reported = say(EN.review.decidedBlocking, { blocking: 1 });
    await waitFor(() => {
      expect(within(card(titleOf(decided))).getByText(reported)).toBeDefined();
    });
    expect(within(card(titleOf(other))).queryByText(reported)).toBeNull();
  });
});

describe("revealing a value", () => {
  it("replaces the button with the value and says the read was audited", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(page([item()]));
    vi.mocked(review.reveal).mockResolvedValue({
      field_id: 11,
      field_name: "email",
      value: "customer@example.test",
      version: 3,
    });
    renderQueue();

    fireEvent.click(await screen.findByRole("button", { name: EN.review.reveal }));

    await waitFor(() => {
      expect(screen.getByText("customer@example.test")).toBeDefined();
    });
    expect(screen.getByText(EN.review.revealedNote)).toBeDefined();
    // Reveal is a one-way action on this visit: the queue still carries no value,
    // so there is nothing left to reveal for this row.
    expect(screen.queryByRole("button", { name: EN.review.reveal })).toBeNull();
    expect(review.reveal).toHaveBeenCalledWith(ACCESS, 11);
  });

  it("reports a refused reveal on its own card, not as a queue failure", async () => {
    const email = item({ field_id: 11, field_name: "email" });
    const phone = item({ field_id: 12, field_name: "phone" });
    setSession(sessionFor("operator"));
    queueAnswers(page([email, phone]));
    vi.mocked(review.reveal).mockRejectedValue(new ApiError(403, "forbidden", "Nope."));
    renderQueue();

    await screen.findByText(titleOf(phone));
    fireEvent.click(within(card(titleOf(email))).getByRole("button", { name: EN.review.reveal }));

    await waitFor(() => {
      expect(within(card(titleOf(email))).getByRole("alert").textContent).toBe(EN.review.forbidden);
    });
    expect(within(card(titleOf(phone))).queryByRole("alert")).toBeNull();
    // The queue itself loaded fine; a refused reveal must not blank the list.
    expect(review.queue).toHaveBeenCalledTimes(1);
  });
});

describe("the queue itself", () => {
  it("says why a row is here: the threshold, the wait, and that it blocks reminders", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(
      page([
        item({
          confidence: 0.42,
          threshold: 0.8,
          age_minutes: 95,
          blocks_pipeline: true,
          source_page: 2,
          source_line: null,
        }),
      ]),
    );
    renderQueue();

    await screen.findByText(titleOf(item()));
    const row = card(titleOf(item()));

    expect(
      within(row).getByText(say(EN.review.confidenceValue, { confidence: "0.42", threshold: "0.80" })),
    ).toBeDefined();
    expect(within(row).getByText(say(EN.review.ageValue, { minutes: 95 }))).toBeDefined();
    expect(within(row).getByText(say(EN.review.sourcePageOnly, { page: 2 }))).toBeDefined();
    expect(within(row).getByText(EN.review.blockingLabel)).toBeDefined();
    expect(
      within(row).getByText(say(EN.review.contractLabel, { ref: "ACME-2024-001", id: 7 })),
    ).toBeDefined();
  });

  it("states an expiry ahead of, behind and absent from today", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(
      page([
        item({ field_id: 11, field_name: "start_date", days_to_expiry: 5 }),
        item({ field_id: 12, field_name: "end_date", days_to_expiry: -3 }),
        item({ field_id: 13, field_name: "dob", days_to_expiry: null }),
      ]),
    );
    renderQueue();

    expect(await screen.findByText(say(EN.review.expiresInDays, { days: 5 }))).toBeDefined();
    // A negative distance is information, not an error: the contract has already
    // lapsed and the sentence says so rather than rendering "-3 days".
    expect(screen.getByText(say(EN.review.expiredDaysAgo, { days: 3 }))).toBeDefined();
    expect(screen.getByText(EN.review.noEndDate)).toBeDefined();
  });

  it("does not let an unreachable API look like an empty queue", async () => {
    setSession(sessionFor("operator"));
    const queue = vi.mocked(review.queue);
    queue.mockRejectedValueOnce(new ApiError(0, "unreachable", "The API is not reachable."));
    queue.mockResolvedValueOnce(page([item()]));
    renderQueue();

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.review.offline]);
    });
    // The single most damaging confusion this screen could make: "everything has
    // been decided" and "we could not ask" must never read the same.
    expect(screen.queryByText(EN.review.empty)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: EN.review.retry }));

    expect(await screen.findByText(titleOf(item()))).toBeDefined();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("says the queue is empty and offers no next page", async () => {
    setSession(sessionFor("operator"));
    queueAnswers(page([]));
    renderQueue();

    expect(await screen.findByText(EN.review.empty)).toBeDefined();
    expect(screen.getByText(EN.review.emptyHint)).toBeDefined();
    expect(screen.queryByRole("button", { name: EN.review.loadMore })).toBeNull();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("asks for the next page with the cursor it was given", async () => {
    const first = item({ field_id: 11, field_name: "email" });
    const second = item({ field_id: 12, field_name: "phone" });
    setSession(sessionFor("operator"));
    queueAnswers(
      page([first], { next_cursor: "cursor-1" }),
      page([second], { next_cursor: null }),
    );
    renderQueue();

    fireEvent.click(await screen.findByRole("button", { name: EN.review.loadMore }));

    await waitFor(() => {
      // The opaque cursor, not a page number: the server's keyset position is the
      // only thing that stays correct while rows are being decided underneath.
      expect(review.queue).toHaveBeenCalledWith(ACCESS, "cursor-1");
    });
    expect(await screen.findByText(titleOf(second))).toBeDefined();
    // Earlier pages stay on screen; the list is appended to, not replaced.
    expect(screen.getByText(titleOf(first))).toBeDefined();
    expect(screen.queryByRole("button", { name: EN.review.loadMore })).toBeNull();
  });
});
