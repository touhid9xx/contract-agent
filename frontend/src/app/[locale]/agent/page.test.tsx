// @vitest-environment jsdom
/**
 * The agent decision screen — the fourth screen under test, and the second whose
 * state lives in the address.
 *
 * The three screens before it each proved one thing: `login` that a session can be
 * obtained, `review` that a queue can be worked, `escalations` that a filter can
 * live in the URL. None of them could reach the rules *this* screen carries, because
 * no other screen makes them:
 *
 *   * a **re-run appends and a correction edits**, so the two actions are offered on
 *     different rows and report their outcome differently;
 *   * the correction routes are addressed by **contract**, and resolve "the current
 *     recommendation" server-side, so the screen must offer them only on a
 *     contract's newest row — offering one on a superseded row would send a
 *     `version` the server has already moved past;
 *   * a refusal can carry the channels that *would* have worked, and the screen
 *     must render them in the reader's language without ever computing the
 *     permitted set itself.
 *
 * The harness is the one `frontend/src/app/[locale]/escalations/page.test.tsx`
 * established, with no new dependency and no invented provider:
 *
 *   * a real `QueryClientProvider` with a fresh client per test, `retry: false`
 *     because these tests ask *what the screen asked and how often*, and a retry
 *     policy would answer that question in the screen's place;
 *   * the real `NextIntlClientProvider` and the real catalogue, so every assertion
 *     is against the sentence a user reads;
 *   * the real `AuthProvider` and the real session store — role gating is read from
 *     the session, not passed in as a prop;
 *   * only `agent.decisions`, `agent.decide`, `agent.override` and
 *     `agent.flagReview` are replaced, with `importOriginal` keeping the real
 *     `ApiError`, because the screen branches on `instanceof` and on `cause.code`;
 *   * `next/navigation` is the second double: there is no Next runtime here, so
 *     `useSearchParams` reads a mutable address this test writes, and the router is
 *     an object of spies. The screen must derive what it asks the server from the
 *     address alone, which is exactly what that double lets us see.
 *
 * What is pinned:
 *
 *   * a signed-out visitor is asked to sign in and the API is asked nothing,
 *   * a viewer reads decisions and is offered no way to change one,
 *   * the unscoped read sends **no** contract filter, and a scoped address narrows
 *     that same read,
 *   * an address naming something that cannot be a contract performs no read at
 *     all, rather than falling through to the tenant-wide history,
 *   * the scope sentence quotes the server's echo, so a narrower list is never
 *     claimed on the strength of the address alone,
 *   * the filter navigates with `replace` and a trimmed id, and an id that cannot
 *     be a contract is never navigated,
 *   * a re-run calls the appending route and reloads the list; a refusal from it is
 *     reported on the row and still reloads, because the list has gone stale,
 *   * an override echoes the `version` it was shown, trims a reason that says
 *     something and omits one that does not,
 *   * `CHANNEL_NOT_PERMITTED` names the permitted channels in the reader's language,
 *     and degrades to a generic sentence when the envelope carries no usable list,
 *   * a flag echoes the version the same way,
 *   * corrections are offered on a contract's newest row only, and an older row
 *     says so,
 *   * a row states what was overridden, by whom, and how many re-runs preceded it,
 *   * "the API is down" never looks like "nothing has been decided", and a scoped
 *     empty means something different from an unscoped one.
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
    agent: {
      decisions: vi.fn(),
      decide: vi.fn(),
      override: vi.fn(),
      flagReview: vi.fn(),
    },
  };
});

import AgentPage from "@/app/[locale]/agent/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { agent, ApiError } from "@/lib/api";
import type { AgentDecision, AgentDecisionList, Session, UserRole } from "@/lib/api";
import { setSession } from "@/lib/session";

const EN = getMessages("en");
const ACCESS = "access-1";

/** Substitution for a plain (non-plural) ICU string. */
function say(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => String(values[key]));
}

const BASE_ROW: AgentDecision = {
  id: 5,
  contract_id: 7,
  channel: "email",
  confidence: 0.92,
  reasoning: "The customer consented to email and to no other channel.",
  needs_review: false,
  prompt_version: "deterministic-v1",
  llm_model: "deterministic",
  correlation_id: "corr-1",
  created_at: "2026-11-04T12:00:00Z",
  version: 3,
  original_channel: null,
  overridden_by: null,
  rerun_count: 0,
};

function decision(overrides: Partial<AgentDecision> = {}): AgentDecision {
  return { ...BASE_ROW, ...overrides };
}

function listing(
  rows: AgentDecision[],
  overrides: Partial<AgentDecisionList> = {},
): AgentDecisionList {
  return { items: rows, count: rows.length, contract_id: null, ...overrides };
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
 * Answer successive decision reads in order, repeating the last one.
 *
 * A landed re-run or correction invalidates the list, so "one read" is not a given:
 * a single `mockResolvedValue` would let the second read return the first answer by
 * accident and hide the reload the screen promises.
 */
function readAnswers(first: AgentDecisionList, ...rest: AgentDecisionList[]) {
  const queue = [first, ...rest];
  const readMock = vi.mocked(agent.decisions);
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
          <AgentPage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
  return { ...view, form: view.container.querySelector("form")! as HTMLFormElement };
}

/** The card's own heading text, which is also how these tests find a row. */
function titleOf(entry: AgentDecision): string {
  return `${say(EN.agent.contractLabel, { id: entry.contract_id })} · ${
    EN.agent[`channel${entry.channel === "email" ? "Email" : entry.channel === "sms" ? "Sms" : "Voice"}`]
  }`;
}

/**
 * A row's card.
 *
 * The page marks each card with `aria-busy`, so that attribute — a real thing the
 * component sets, rather than a class chosen for the test — is the handle.
 */
function card(entry: AgentDecision): HTMLElement {
  const heading = screen.getByText(titleOf(entry));
  const root = heading.closest("[aria-busy]");
  if (root === null) {
    throw new Error(`No decision card found for "${titleOf(entry)}".`);
  }
  return root as HTMLElement;
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

/** Type into the contract filter. */
function typeFilter(value: string) {
  fireEvent.change(screen.getByLabelText(EN.agent.filterLabel), { target: { value } });
}

beforeEach(() => {
  setSession(null);
  address.current = "";
  vi.mocked(agent.decisions).mockReset();
  vi.mocked(agent.decide).mockReset();
  vi.mocked(agent.override).mockReset();
  vi.mocked(agent.flagReview).mockReset();
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

    expect(screen.getByText(EN.agent.signInRequired)).toBeDefined();
    // `authorized` refuses before a request is built, so nothing leaves the tab.
    expect(agent.decisions).not.toHaveBeenCalled();
    // A 401 is not a decision failure: reporting it would say the list is broken.
    expect(alertTexts()).toStrictEqual([]);
    expect(screen.getByRole("link", { name: EN.agent.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );
  });

  it("lets a viewer read the decisions but offers no way to change one", async () => {
    setSession(sessionFor("viewer"));
    readAnswers(listing([decision()]));
    renderScreen();

    expect(await screen.findByText(titleOf(decision()))).toBeDefined();
    expect(screen.getByText(EN.agent.viewerNote)).toBeDefined();

    for (const label of [EN.agent.rerun, EN.agent.override, EN.agent.flag]) {
      expect(within(card(decision())).queryByRole("button", { name: label })).toBeNull();
    }
  });
});

describe("the address is the state", () => {
  it("asks for the tenant's recent decisions and sends no contract filter", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    renderScreen();

    await screen.findByText(titleOf(decision()));
    // `undefined` rather than a partial filter object: the client's own signature
    // turns it into `/agent/decisions` with no query string at all.
    expect(vi.mocked(agent.decisions).mock.calls[0]![1]).toBeUndefined();
    expect(screen.getByText(EN.agent.scopeRecent)).toBeDefined();
    expect(screen.getByText(say(EN.agent.countLabel, { count: 1 }))).toBeDefined();
  });

  it("narrows the same read to one contract, and starts the box from the address", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    readAnswers(listing([decision({ contract_id: 12 })], { contract_id: 12 }));
    renderScreen();

    expect(await screen.findByText(titleOf(decision({ contract_id: 12 })))).toBeDefined();
    expect(vi.mocked(agent.decisions).mock.calls[0]![1]).toBe(12);
    expect(screen.getByText(say(EN.agent.scopeContract, { id: 12 }))).toBeDefined();
    // A box still holding the previous id would contradict the list under it.
    expect(screen.getByLabelText<HTMLInputElement>(EN.agent.filterLabel).value).toBe("12");
  });

  it("quotes the server's echo in the scope sentence, not the address", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    // The route echoes the filter it applied; here it applied none. The sentence
    // must not claim a narrower list than the one on screen.
    readAnswers(listing([decision()], { contract_id: null }));
    renderScreen();

    await screen.findByText(titleOf(decision()));
    expect(screen.getByText(EN.agent.scopeRecent)).toBeDefined();
    expect(screen.queryByText(say(EN.agent.scopeContract, { id: 12 }))).toBeNull();
  });

  it("refuses to read anything when the address names an impossible contract", () => {
    setSession(sessionFor("operator"));
    address.current = "contract=012";
    renderScreen();

    // The most damaging confusion this screen could make: showing the tenant's whole
    // history behind a filter that never ran, which looks like an answer.
    expect(agent.decisions).not.toHaveBeenCalled();
    expect(alertTexts()).toStrictEqual([EN.agent.filterInvalid]);
    expect(screen.getByText(EN.agent.filterInvalidHint)).toBeDefined();
    expect(screen.queryByText(EN.agent.scopeRecent)).toBeNull();
  });

  it("filters by replacing the address with the trimmed id", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    const { form } = renderScreen();

    await screen.findByText(titleOf(decision()));
    typeFilter(" 12 ");
    fireEvent.submit(form);

    await waitFor(() => {
      // `replace`, not `push`: a half-typed id followed by a few corrections is not
      // several pages an operator wants to step back through.
      expect(router.replace).toHaveBeenCalledWith("/en/agent?contract=12");
    });
    expect(router.push).not.toHaveBeenCalled();
  });

  it("will not navigate on an id that cannot be a contract", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    const { form } = renderScreen();

    await screen.findByText(titleOf(decision()));
    typeFilter("12abc");

    expect(screen.getByRole<HTMLButtonElement>("button", { name: EN.agent.filterApply }).disabled)
      .toBe(true);
    fireEvent.submit(form);

    // The screen says why and stays put; it does not hand the router a value the
    // reader's own parser would then reject as malformed.
    expect(alertTexts()).toStrictEqual([EN.agent.filterInvalid]);
    expect(router.replace).not.toHaveBeenCalled();
  });

  it("treats an emptied box as no filter at all", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    readAnswers(listing([decision({ contract_id: 12 })], { contract_id: 12 }));
    const { form } = renderScreen();

    await screen.findByText(titleOf(decision({ contract_id: 12 })));
    typeFilter("");
    fireEvent.submit(form);

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en/agent");
    });
  });

  it("clears the filter by navigating back to the unscoped address", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    readAnswers(listing([decision({ contract_id: 12 })], { contract_id: 12 }));
    renderScreen();

    await screen.findByText(titleOf(decision({ contract_id: 12 })));
    fireEvent.click(screen.getByRole("button", { name: EN.agent.filterClear }));

    await waitFor(() => {
      expect(router.replace).toHaveBeenCalledWith("/en/agent");
    });
  });
});

describe("re-running appends", () => {
  it("calls the appending route, reloads the list and says so", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision({ version: 3 })]), listing([decision({ id: 6, version: 1 })]));
    vi.mocked(agent.decide).mockResolvedValue(decision({ id: 6, version: 1 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.rerun }));

    await waitFor(() => {
      expect(agent.decide).toHaveBeenCalledWith(ACCESS, 7);
    });
    // Appending means the row on screen is history now, so the list is re-read
    // rather than patched: only the server knows what the new newest row says.
    await waitFor(() => {
      expect(agent.decisions).toHaveBeenCalledTimes(2);
    });
    expect((await screen.findByRole("status")).textContent).toBe(EN.agent.rerunDone);
  });

  it("names a blocked pipeline on the row, and reloads anyway", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision({ version: 3 })]), listing([decision({ version: 3 })]));
    vi.mocked(agent.decide).mockRejectedValue(
      new ApiError(409, "PIPELINE_BLOCKED", "A field is still awaiting review."),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.rerun }));

    await waitFor(() => {
      expect(within(card(decision())).getByRole("alert").textContent).toBe(
        EN.agent.refusePipelineBlocked,
      );
    });
    // The contract moved out of a decidable state under the operator, so the list
    // they are reading no longer describes the server.
    await waitFor(() => {
      expect(agent.decisions).toHaveBeenCalledTimes(2);
    });
  });
});

describe("overriding corrects in place", () => {
  it("echoes the version it was shown and omits an empty reason", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision({ version: 3 })]));
    vi.mocked(agent.override).mockResolvedValue(
      decision({ channel: "sms", original_channel: "email", version: 4 }),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.override }));
    fireEvent.change(within(card(decision())).getByLabelText(EN.agent.reasonLabel), {
      target: { value: "   " },
    });
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.channelSms }));

    await waitFor(() => {
      expect(agent.override).toHaveBeenCalledTimes(1);
    });
    // `toStrictEqual` on the payload: an omitted reason and a reason of `undefined`
    // are different requests, and only one of them is what this screen means.
    expect(vi.mocked(agent.override).mock.calls[0]!.slice(0, 2)).toStrictEqual([ACCESS, 7]);
    expect(vi.mocked(agent.override).mock.calls[0]![2]).toStrictEqual({
      channel: "sms",
      version: 3,
    });
  });

  it("trims a reason that says something", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision({ version: 9 })]));
    vi.mocked(agent.override).mockResolvedValue(decision({ channel: "voice", version: 10 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.override }));
    fireEvent.change(within(card(decision())).getByLabelText(EN.agent.reasonLabel), {
      target: { value: "  The phone number is the one they answer  " },
    });
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.channelVoice }));

    await waitFor(() => {
      expect(agent.override).toHaveBeenCalledWith(ACCESS, 7, {
        channel: "voice",
        version: 9,
        reason: "The phone number is the one they answer",
      });
    });
  });

  it("cancels the override without sending anything", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    renderScreen();

    await screen.findByText(titleOf(decision()));
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.override }));
    fireEvent.change(within(card(decision())).getByLabelText(EN.agent.reasonLabel), {
      target: { value: "half a thought" },
    });
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.cancel }));

    expect(agent.override).not.toHaveBeenCalled();
    expect(within(card(decision())).queryByLabelText(EN.agent.reasonLabel)).toBeNull();
  });

  it("names the channels the policy would have allowed", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    vi.mocked(agent.override).mockRejectedValue(
      new ApiError(409, "CHANNEL_NOT_PERMITTED", "Not consented.", null, {
        permitted: ["sms", "email"],
      }),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.override }));
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.channelVoice }));

    await waitFor(() => {
      expect(within(card(decision())).getByRole("alert").textContent).toBe(
        `${EN.agent.refuseChannelNotPermitted} ${say(EN.agent.permittedChannels, {
          channels: `${EN.agent.channelSms}, ${EN.agent.channelEmail}`,
        })}`,
      );
    });
  });

  it("falls back to the generic sentence when the envelope carries no usable list", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    // A shape the route never sends, plus one channel a `Channel` may not be: the
    // permission list is narrowed, not trusted.
    vi.mocked(agent.override).mockRejectedValue(
      new ApiError(409, "CHANNEL_NOT_PERMITTED", "Not consented.", null, { permitted: ["fax"] }),
    );
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.override }));
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.channelVoice }));

    await waitFor(() => {
      expect(within(card(decision())).getByRole("alert").textContent).toBe(
        EN.agent.refuseChannelNotPermitted,
      );
    });
  });
});

describe("flagging takes the row out of the send path", () => {
  it("echoes the version it was shown and omits an empty reason", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision({ version: 4 })]));
    vi.mocked(agent.flagReview).mockResolvedValue(decision({ needs_review: true, version: 5 }));
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.agent.flag }));
    fireEvent.click(within(card(decision())).getByRole("button", { name: EN.agent.flagSubmit }));

    await waitFor(() => {
      expect(agent.flagReview).toHaveBeenCalledTimes(1);
    });
    expect(vi.mocked(agent.flagReview).mock.calls[0]!.slice(0, 2)).toStrictEqual([ACCESS, 7]);
    expect(vi.mocked(agent.flagReview).mock.calls[0]![2]).toStrictEqual({ version: 4 });
  });
});

describe("corrections are offered where they apply", () => {
  it("offers them on a contract's newest row only, and says so on the older ones", async () => {
    // Newest first, as the route orders them: the first row seen for a contract is
    // that contract's current recommendation.
    const newest = decision({ id: 5, contract_id: 7, channel: "email", version: 3 });
    const older = decision({ id: 3, contract_id: 7, channel: "sms", version: 1 });
    setSession(sessionFor("operator"));
    readAnswers(listing([newest, older]));
    renderScreen();

    await screen.findByText(titleOf(older));
    for (const label of [EN.agent.rerun, EN.agent.override, EN.agent.flag]) {
      expect(within(card(newest)).getByRole("button", { name: label })).toBeDefined();
      // Every click on a superseded row would be a `409`: the routes resolve "the
      // current recommendation" themselves.
      expect(within(card(older)).queryByRole("button", { name: label })).toBeNull();
    }
    expect(within(card(older)).getByText(EN.agent.olderRowNote)).toBeDefined();
  });
});

describe("what a row states", () => {
  it("reports the override, who made it, and how many re-runs preceded it", async () => {
    const corrected = decision({
      id: 9,
      contract_id: 7,
      channel: "email",
      original_channel: "sms",
      overridden_by: "abcdef12-3456-7890",
      rerun_count: 2,
      needs_review: true,
    });
    setSession(sessionFor("operator"));
    readAnswers(listing([corrected]));
    renderScreen();

    const root = await waitFor(() => card(corrected));
    expect(
      within(root).getByText(
        say(EN.agent.overriddenBadge, {
          from: EN.agent.channelSms,
          to: EN.agent.channelEmail,
        }),
      ),
    ).toBeDefined();
    // Shortened, not parsed: the screen shows a handle a person can compare, and
    // the full id stays in the audit row where it means something.
    expect(within(root).getByText("abcdef12")).toBeDefined();
    expect(within(root).getByText(EN.agent.rerunCountLabel)).toBeDefined();
    expect(within(root).getByText(EN.agent.flaggedBadge)).toBeDefined();
  });

  it("says nothing about an override that never happened", async () => {
    setSession(sessionFor("operator"));
    readAnswers(listing([decision()]));
    renderScreen();

    const root = await waitFor(() => card(decision()));
    expect(within(root).queryByText(EN.agent.rerunCountLabel)).toBeNull();
    expect(within(root).queryByText(EN.agent.overriddenByLabel)).toBeNull();
    expect(within(root).queryByText(EN.agent.flaggedBadge)).toBeNull();
    // The policy and the model are stated on every row: a recommendation with no
    // provenance cannot be argued with.
    expect(within(root).getByText("deterministic-v1")).toBeDefined();
    expect(within(root).getByText("deterministic")).toBeDefined();
  });
});

describe("the list itself", () => {
  it("does not let an unreachable API look like an empty history", async () => {
    setSession(sessionFor("operator"));
    const readMock = vi.mocked(agent.decisions);
    readMock.mockRejectedValueOnce(new ApiError(0, "unreachable", "The API is not reachable."));
    readMock.mockResolvedValueOnce(listing([decision()]));
    renderScreen();

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.agent.offline]);
    });
    // "Nothing has been decided" and "we could not ask" must never read the same.
    expect(screen.queryByText(EN.agent.empty)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: EN.agent.retry }));

    expect(await screen.findByText(titleOf(decision()))).toBeDefined();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("keeps a scoped empty apart from an empty history", async () => {
    setSession(sessionFor("operator"));
    address.current = "contract=12";
    readAnswers(listing([], { contract_id: 12 }));
    renderScreen();

    expect(await screen.findByText(EN.agent.emptyForContract)).toBeDefined();
    // "This contract has no recommendation" is a good answer about one contract and
    // must not read like the policy never having run.
    expect(screen.queryByText(EN.agent.empty)).toBeNull();
    expect(screen.getByText(EN.agent.emptyHint)).toBeDefined();
  });
});
