// @vitest-environment jsdom
/**
 * The contract listing — the sixth screen under test, and the first one that owns
 * a **write onto a row it also lists**.
 *
 * The five screens before it each proved something the others could not: `login`
 * that a session can be obtained, `review` that a queue can be worked, `escalations`
 * that a filter can live in the URL, `agent` that a refusal can carry the answer,
 * `notifications` that one action may ask before it acts. This screen carries rules
 * none of them can reach:
 *
 *   * **one form, two routes.** `POST /contracts` and `POST /contracts/document`
 *     take the same four content fields and differ only in transport, so attaching
 *     a file is what switches routes. Nothing on screen says "upload": the operator
 *     attaches a document, and the transport is the screen's problem.
 *   * **an empty optional field is omitted, not sent empty.** `""` is not a date
 *     and the route answers it 422, so "not recorded" is an absent key. That is a
 *     one-character bug with a 422 on the far side, which is why the payload's
 *     *shape* is what these tests assert rather than its values.
 *   * **a consent write carries the version the row was drawn with**, never a copy
 *     the draft kept: the token means "the state I was looking at when I decided".
 *   * **a no-op save is a visible success.** A submission identical to what is
 *     stored answers the version it was given rather than a higher one, and that
 *     difference is the only way to tell an operator their click changed nothing
 *     because the flags already matched — a reassuring answer, not a failure.
 *   * **the page appends.** Paging passes the opaque `next_cursor` straight back,
 *     so a scroll touches each contract once even while new rows arrive underneath.
 *
 * The harness is the one `frontend/src/app/[locale]/notifications/page.test.tsx`
 * established, minus one double: **there is no `next/navigation` mock here**, because
 * this screen reads nothing out of the address — the sixth screen is also the first
 * whose state is entirely its own. What remains is real:
 *
 *   * a real `QueryClientProvider` with a fresh client per test and `retry: false`,
 *     because these tests ask *what the screen asked and how often*;
 *   * the real `NextIntlClientProvider` and the real catalogue, so every assertion
 *     is against the sentence a user reads;
 *   * the real `AuthProvider` and the real session store — role gating is read from
 *     the session, not passed in as a prop;
 *   * the real `session.authorized`, so a signed-out read is refused *before* a
 *     request is built, which is what makes "nothing leaves the tab" checkable;
 *   * only `contracts.list` / `ingest` / `ingestDocument` / `updateConsent` are
 *     replaced, with `importOriginal` keeping the real `ApiError`, because the
 *     screen branches on `instanceof` and on status predicates.
 *
 * What is pinned:
 *
 *   * a signed-out visitor is asked to sign in and the client is asked nothing,
 *   * a viewer may read the list but is offered neither the form nor the consent
 *     write, and is told why in a sentence rather than shown a disabled button,
 *   * the first read sends **no status key at all** — absence, not `undefined`,
 *   * a status chip re-asks the server and marks exactly one chip current,
 *   * paging passes the cursor back and **appends**; a `null` cursor offers nothing,
 *   * the form cannot be submitted with a blank reference, trims the one it gets,
 *     and omits dates that were never filled in rather than sending `""`,
 *   * staging a file switches the route and carries the identical metadata,
 *   * a landed ingestion clears the form, names the row it created and reloads,
 *   * every refusal a person can provoke is reported in its own words — including
 *     the two that say *why* in deployment terms (`413`, `503`),
 *   * consent goes out as the **complete** state with the row's version,
 *   * a recorded change and a no-op read differently, and editing hides either,
 *   * a row's refusal stays on the row that was clicked and a `409` reloads,
 *   * a card states contactability the server derived rather than re-deriving it,
 *   * "the API is down" never looks like "no contract matches this filter".
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
    contracts: {
      list: vi.fn(),
      ingest: vi.fn(),
      ingestDocument: vi.fn(),
      updateConsent: vi.fn(),
    },
  };
});

import ContractsPage from "@/app/[locale]/contracts/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError, contracts } from "@/lib/api";
import type {
  ConsentState,
  ContractIngestRequest,
  ContractList,
  ContractListItem,
  ContractSummary,
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

/** The same formatter the screen builds, so a formatting change is not a test failure. */
const DATE_FMT = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" });

function dateText(value: string): string {
  return DATE_FMT.format(new Date(value));
}

/**
 * A contract with nothing switched on.
 *
 * The consent flags all default to `false` and `may_contact` to `false` because
 * that is the fail-closed state a freshly ingested row is really in — the listing
 * shows rows in it, so the fixture starts there rather than at a convenient truth.
 */
const BASE_ROW: ContractListItem = {
  id: 7,
  customer_ref: "ACME-1",
  status: "active",
  source: "api",
  risk: "low",
  start_date: null,
  end_date: null,
  timezone: "Asia/Dhaka",
  sha256: null,
  has_document: false,
  consent_email: false,
  consent_sms: false,
  consent_voice: false,
  do_not_contact: false,
  consent_channels: [],
  may_contact: false,
  version: 7,
  created_at: "2026-10-01T09:15:00Z",
};

function row(overrides: Partial<ContractListItem> = {}): ContractListItem {
  return { ...BASE_ROW, ...overrides };
}

function page(items: ContractListItem[], overrides: Partial<ContractList> = {}): ContractList {
  return { items, count: items.length, status: null, next_cursor: null, ...overrides };
}

function consentState(versions: Partial<ConsentState> = {}): ConsentState {
  return {
    contract_id: 7,
    consent_email: true,
    consent_sms: false,
    consent_voice: false,
    do_not_contact: false,
    consent_channels: ["email"],
    may_contact: true,
    consent_updated_at: "2026-10-02T08:00:00Z",
    version: 8,
    ...versions,
  };
}

function summary(overrides: Partial<ContractSummary> = {}): ContractSummary {
  return {
    id: 51,
    customer_ref: "ACME-1",
    status: "active",
    source: "api",
    risk: "low",
    start_date: null,
    end_date: null,
    timezone: "UTC",
    sha256: "a".repeat(64),
    version: 1,
    created_at: "2026-10-02T08:00:00Z",
    outbox_event_id: "0f8f8f8f-1111-4222-8333-444455556666",
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
 * Answer successive list reads in order, repeating the last one.
 *
 * Every landed write — an ingestion, a consent save, a conflict — invalidates the
 * list, so "one read" is not a given: a single `mockResolvedValue` would let the
 * second read return the first answer by accident and hide the reload the screen
 * promises.
 */
function readAnswers(first: ContractList, ...rest: ContractList[]) {
  const queue = [first, ...rest];
  const readMock = vi.mocked(contracts.list);
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
  return render(
    <QueryClientProvider client={client}>
      <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
        <AuthProvider>
          <ContractsPage />
        </AuthProvider>
      </NextIntlClientProvider>
    </QueryClientProvider>,
  );
}

/** The heading a card is found by, which is also the link to the contract page. */
function titleOf(entry: ContractListItem): string {
  return say(EN.contracts.contractLabel, { id: entry.id });
}

/**
 * One contract's card.
 *
 * `Card` renders a `div.rounded-card`, and that class is the only structural
 * handle the component guarantees — the screen adds no `aria-busy` or test id of
 * its own. If the wrapper ever stops carrying it this throws loudly rather than
 * letting a per-row assertion quietly match the whole page.
 */
function card(entry: ContractListItem): HTMLElement {
  const root = screen.getByText(titleOf(entry)).closest(".rounded-card");
  if (root === null) {
    throw new Error(`No contract card found for "${titleOf(entry)}".`);
  }
  return root as HTMLElement;
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

function statusTexts(): (string | null)[] {
  return screen.queryAllByRole("status").map((node) => node.textContent);
}

/** Every chip currently carrying `aria-current`. */
function currentChips(): string[] {
  return screen
    .getAllByRole("button")
    .filter((node) => node.getAttribute("aria-current") === "true")
    .map((node) => node.textContent ?? "");
}

function typeInto(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function addButton(): HTMLElement {
  return screen.getByRole("button", { name: EN.contracts.ingestAction });
}

beforeEach(() => {
  setSession(null);
  vi.mocked(contracts.list).mockReset();
  vi.mocked(contracts.ingest).mockReset();
  vi.mocked(contracts.ingestDocument).mockReset();
  vi.mocked(contracts.updateConsent).mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("who may see what", () => {
  it("asks a signed-out visitor to sign in, and asks the API nothing", async () => {
    renderScreen();

    expect(screen.getByText(EN.contracts.signInRequired)).toBeDefined();
    expect(screen.getByRole("link", { name: EN.contracts.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );

    // The query still runs — this screen gates the *view*, not the read, unlike the
    // agent and escalation screens which use `enabled`. `authorized` refuses before a
    // request is built, so the observable promise still holds: nothing leaves the tab,
    // and a 401 is not reported as a list failure.
    await waitFor(() => {
      expect(contracts.list).not.toHaveBeenCalled();
    });
    expect(alertTexts()).toStrictEqual([]);
  });

  it("lets a viewer read the list but offers neither write, and says why", async () => {
    setSession(sessionFor("viewer"));
    readAnswers(page([row()]));
    renderScreen();

    await screen.findByText(titleOf(row()));

    // Both write routes are `require_roles(ADMIN, OPERATOR)`, so a viewer is answered
    // 403. The form is replaced by a sentence rather than shown disabled: an action
    // whose only outcome is a refusal is noise.
    expect(screen.getByText(EN.contracts.ingestViewerNote)).toBeDefined();
    expect(screen.queryByText(EN.contracts.ingestHeading)).toBeNull();
    expect(screen.queryByRole("button", { name: EN.contracts.ingestAction })).toBeNull();

    // The consent *read* is not withheld — only the writing is.
    expect(within(card(row())).getByText(EN.contracts.consentViewerNote)).toBeDefined();
    expect(
      within(card(row())).queryByRole("button", { name: EN.contracts.consentAction }),
    ).toBeNull();
  });

  it("offers an operator the form and the consent write", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([row()]));
    renderScreen();

    expect(await screen.findByText(EN.contracts.ingestHeading)).toBeDefined();
    expect(screen.queryByText(EN.contracts.ingestViewerNote)).toBeNull();

    await screen.findByText(titleOf(row()));
    expect(
      within(card(row())).getByRole("button", { name: EN.contracts.consentAction }),
    ).toBeDefined();
    expect(within(card(row())).queryByText(EN.contracts.consentViewerNote)).toBeNull();
  });
});

describe("the read", () => {
  it("asks for one page and sends no status key at all", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([row(), row({ id: 9, customer_ref: "ACME-2" })]));
    renderScreen();

    await screen.findByText(titleOf(row()));

    // `{}` rather than `undefined` or `{ status: null }`: the client builds the query
    // string from what it is given, so an explicit `null` would be neither a filter
    // nor an absence. Asserting the *keys* is what distinguishes those two.
    const options = vi.mocked(contracts.list).mock.calls[0]?.[1] ?? {};
    expect(Object.keys(options)).toStrictEqual(["cursor"]);
    expect(vi.mocked(contracts.list).mock.calls[0]?.[0]).toBe(ACCESS);

    expect(screen.getByText(say(EN.contracts.countLabel, { count: 2 }))).toBeDefined();
    expect(currentChips()).toStrictEqual([EN.contracts.filterAll]);
  });

  it("re-asks the server for the status it was given, and marks one chip current", async () => {
    setSession(sessionFor("operator"));
    const readMock = readAnswers(page([row()]), page([row({ status: "escalated" })]));
    renderScreen();

    await screen.findByText(titleOf(row()));

    fireEvent.click(screen.getByRole("button", { name: EN.contracts.statuses.escalated }));

    await waitFor(() => {
      expect(readMock).toHaveBeenCalledTimes(2);
    });
    // A filter is the server's question to answer: the screen narrows the request
    // rather than the answer it already holds.
    expect(vi.mocked(contracts.list).mock.calls[1]?.[1]).toStrictEqual({
      cursor: undefined,
      status: "escalated",
    });
    expect(currentChips()).toStrictEqual([EN.contracts.statuses.escalated]);
  });

  it("follows the cursor and appends rather than replacing the page", async () => {
    const first = row();
    const second = row({ id: 9, customer_ref: "ACME-2" });
    setSession(sessionFor("operator"));
    const readMock = readAnswers(
      page([first], { next_cursor: 7 }),
      page([second], { next_cursor: null }),
    );
    renderScreen();

    await screen.findByText(titleOf(first));
    fireEvent.click(screen.getByRole("button", { name: EN.contracts.loadMore }));

    await screen.findByText(titleOf(second));
    // The opaque cursor goes straight back: it is a position, not a page number.
    expect(vi.mocked(contracts.list).mock.calls[1]?.[1]).toStrictEqual({ cursor: 7 });
    // Appended, not replaced — the first page is still on screen.
    expect(screen.getByText(titleOf(first))).toBeDefined();
    expect(screen.getByText(say(EN.contracts.countLabel, { count: 2 }))).toBeDefined();
    expect(readMock).toHaveBeenCalledTimes(2);
  });

  it("offers nothing more when the server says the page was complete", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([row()], { next_cursor: null }));
    renderScreen();

    await screen.findByText(titleOf(row()));
    // `null` is a completeness claim about the database, not an inference from a
    // short page, so there is no second page to offer.
    expect(screen.queryByRole("button", { name: EN.contracts.loadMore })).toBeNull();
  });
});

describe("adding a contract", () => {
  it("refuses to submit a reference that is empty or only space", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([]));
    renderScreen();

    await screen.findByText(EN.contracts.ingestHeading);
    expect(addButton().hasAttribute("disabled")).toBe(true);

    typeInto(EN.contracts.ingestRefLabel, "   ");
    // Trimmed to nothing is still nothing: the button stays disabled rather than
    // sending a blank reference the server would have to refuse.
    expect(addButton().hasAttribute("disabled")).toBe(true);

    typeInto(EN.contracts.ingestRefLabel, "ACME-1");
    expect(addButton().hasAttribute("disabled")).toBe(false);
    expect(contracts.ingest).not.toHaveBeenCalled();
  });

  it("trims the reference, omits untouched dates, and sends the zone it assumed", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([]));
    vi.mocked(contracts.ingest).mockResolvedValue(summary());
    renderScreen();

    await screen.findByText(EN.contracts.ingestHeading);
    typeInto(EN.contracts.ingestRefLabel, "  ACME-1  ");
    fireEvent.click(addButton());

    await waitFor(() => {
      expect(contracts.ingest).toHaveBeenCalledTimes(1);
    });

    // Strict on the payload's *shape*: `""` is not a date and the route answers a
    // blank optional field with 422, so "not recorded" has to be an absent key.
    const payload = vi.mocked(contracts.ingest).mock.calls[0]?.[1] as ContractIngestRequest;
    expect(payload).toStrictEqual({ customer_ref: "ACME-1", timezone: "UTC" });
    expect(Object.keys(payload)).toStrictEqual(["customer_ref", "timezone"]);
  });

  it("sends the dates it was given, as calendar dates", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([]));
    vi.mocked(contracts.ingest).mockResolvedValue(summary());
    renderScreen();

    await screen.findByText(EN.contracts.ingestHeading);
    typeInto(EN.contracts.ingestRefLabel, "ACME-1");
    typeInto(EN.contracts.ingestStartLabel, "2026-01-02");
    typeInto(EN.contracts.ingestEndLabel, "2027-01-02");
    fireEvent.click(addButton());

    await waitFor(() => {
      expect(contracts.ingest).toHaveBeenCalledTimes(1);
    });
    // The browser does not check an inverted term or a malformed zone; the route does,
    // once, for both ingestion paths. So the screen forwards what was typed verbatim.
    expect(vi.mocked(contracts.ingest).mock.calls[0]?.[1]).toStrictEqual({
      customer_ref: "ACME-1",
      start_date: "2026-01-02",
      end_date: "2027-01-02",
      timezone: "UTC",
    });
  });

  it("switches route when a document is staged, and sends the same metadata", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([]));
    vi.mocked(contracts.ingestDocument).mockResolvedValue(summary({ source: "pdf" }));
    renderScreen();

    await screen.findByText(EN.contracts.ingestHeading);
    typeInto(EN.contracts.ingestRefLabel, "ACME-1");

    const file = new File(["%PDF-1.7"], "contract.pdf", { type: "application/pdf" });
    fireEvent.change(screen.getByLabelText(EN.contracts.ingestDocumentLabel), {
      target: { files: [file] },
    });
    fireEvent.click(addButton());

    await waitFor(() => {
      expect(contracts.ingestDocument).toHaveBeenCalledTimes(1);
    });
    // Attaching a file is what switches the endpoint: the operator thinks about the
    // contract, not about which route a document triggers.
    expect(contracts.ingest).not.toHaveBeenCalled();

    const call = vi.mocked(contracts.ingestDocument).mock.calls[0];
    expect(call?.[0]).toBe(ACCESS);
    // Identical metadata either way — that is the whole reason there is one form.
    expect(call?.[1]).toStrictEqual({ customer_ref: "ACME-1", timezone: "UTC" });
    expect(call?.[2]).toBe(file);
  });

  it("clears the form once the row is recorded, and names it", async () => {
    setSession(sessionFor("operator"));
    const readMock = readAnswers(page([]), page([row({ id: 51 })]));
    vi.mocked(contracts.ingest).mockResolvedValue(summary());
    renderScreen();

    await screen.findByText(EN.contracts.ingestHeading);
    typeInto(EN.contracts.ingestRefLabel, "ACME-1");
    typeInto(EN.contracts.ingestEndLabel, "2027-01-02");
    fireEvent.click(addButton());

    expect(
      await screen.findByText(say(EN.contracts.ingested, { id: 51, ref: "ACME-1" })),
    ).toBeDefined();
    // One live region, two sentences, and the second one is load-bearing: a contract
    // that has been added has *no fields yet*, because extraction is a command, not
    // a consequence of ingestion. Said together, so a screen reader hears both.
    expect(statusTexts()).toHaveLength(1);
    expect(statusTexts()[0]).toContain(EN.contracts.ingestedNote);

    // The form is ready for the next contract: reference and dates cleared, and the
    // button disabled again because there is nothing to submit.
    const ref = screen.getByLabelText(EN.contracts.ingestRefLabel) as HTMLInputElement;
    const end = screen.getByLabelText(EN.contracts.ingestEndLabel) as HTMLInputElement;
    expect(ref.value).toBe("");
    expect(end.value).toBe("");
    expect((screen.getByLabelText(EN.contracts.ingestTimezoneLabel) as HTMLInputElement).value).toBe(
      "UTC",
    );
    expect(addButton().hasAttribute("disabled")).toBe(true);

    // Every status filter is its own cache entry, so the whole prefix is reloaded
    // rather than just the entry that happens to be on screen.
    await waitFor(() => {
      expect(readMock).toHaveBeenCalledTimes(2);
    });
  });

  /** The four answers a person can provoke, plus the one that is a deployment state. */
  const REFUSALS: [number, string][] = [
    [403, EN.contracts.ingestForbidden],
    [409, EN.contracts.ingestConflict],
    [413, EN.contracts.ingestTooLarge],
    [422, EN.contracts.ingestInvalid],
    [503, EN.contracts.ingestUnavailable],
  ];

  for (const [status, sentence] of REFUSALS) {
    it(`reports a ${status} ingestion refusal in its own words`, async () => {
      setSession(sessionFor("operator"));
      readAnswers(page([]));
      vi.mocked(contracts.ingest).mockRejectedValue(
        new ApiError(status, "REFUSED", "The server said no."),
      );
      renderScreen();

      await screen.findByText(EN.contracts.ingestHeading);
      typeInto(EN.contracts.ingestRefLabel, "ACME-1");
      fireEvent.click(addButton());

      // Mapped by status, never by matching the route's prose: the bodies carry a
      // human `detail` string and this would break on the first rewording. `503` is
      // the one that is not about what was typed — it says the deployment cannot
      // seal a document at all.
      await waitFor(() => {
        expect(alertTexts()).toStrictEqual([sentence]);
      });
      // Nothing was created, so the form keeps what was typed.
      expect((screen.getByLabelText(EN.contracts.ingestRefLabel) as HTMLInputElement).value).toBe(
        "ACME-1",
      );
    });
  }

  it("tells an unreachable API apart from a refused one", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([]));
    vi.mocked(contracts.ingest).mockRejectedValue(
      new ApiError(0, "network_error", "The API is not reachable."),
    );
    renderScreen();

    await screen.findByText(EN.contracts.ingestHeading);
    typeInto(EN.contracts.ingestRefLabel, "ACME-1");
    fireEvent.click(addButton());

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.contracts.offline]);
    });
  });
});

describe("consent", () => {
  it("sends all four flags with the version the row was drawn with", async () => {
    const entry = row({ version: 7 });
    setSession(sessionFor("operator"));
    readAnswers(page([row()]), page([row()]));
    vi.mocked(contracts.updateConsent).mockResolvedValue(consentState({ version: 8 }));
    renderScreen();

    await screen.findByText(titleOf(entry));
    // Switching one flag on is the whole interaction: the other three travel as they
    // stand, because `PUT` replaces the state and a body carrying one flag cannot
    // express a withdrawal.
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentEmailLabel }));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));

    await waitFor(() => {
      expect(contracts.updateConsent).toHaveBeenCalledTimes(1);
    });
    const call = vi.mocked(contracts.updateConsent).mock.calls[0];
    expect(call?.[0]).toBe(ACCESS);
    expect(call?.[1]).toBe(7);
    // The version is the row's, not a copy the draft kept: the token means "the state
    // I was looking at when I decided", and a draft copy would age differently.
    expect(call?.[2]).toStrictEqual({
      consent_email: true,
      consent_sms: false,
      consent_voice: false,
      do_not_contact: false,
      version: 7,
    });
  });

  it("reports a recorded change as a change", async () => {
    const entry = row({ version: 7 });
    setSession(sessionFor("operator"));
    readAnswers(page([entry]), page([entry]));
    vi.mocked(contracts.updateConsent).mockResolvedValue(consentState({ version: 8 }));
    renderScreen();

    await screen.findByText(titleOf(entry));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentEmailLabel }));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));

    await waitFor(() => {
      expect(statusTexts()).toStrictEqual([EN.contracts.consentSaved]);
    });
  });

  it("tells a save that wrote nothing apart from a change", async () => {
    const entry = row({ version: 7 });
    setSession(sessionFor("operator"));
    readAnswers(page([entry]), page([entry]));
    // The route answers the version it was given when the stored flags already
    // matched. That difference is the only signal, and it is a reassuring answer.
    vi.mocked(contracts.updateConsent).mockResolvedValue(consentState({ version: 7 }));
    renderScreen();

    await screen.findByText(titleOf(entry));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));

    await waitFor(() => {
      expect(statusTexts()).toStrictEqual([EN.contracts.consentUnchanged]);
    });
  });

  it("hides the answer as soon as the operator edits again", async () => {
    const entry = row({ version: 7 });
    setSession(sessionFor("operator"));
    readAnswers(page([entry]), page([entry]));
    vi.mocked(contracts.updateConsent).mockResolvedValue(consentState({ version: 8 }));
    renderScreen();

    await screen.findByText(titleOf(entry));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));
    await waitFor(() => {
      expect(statusTexts()).toStrictEqual([EN.contracts.consentSaved]);
    });

    // The sentence describes a request. Leaving it beside a draft that has moved
    // would caption a state that no longer exists with the result of an earlier one.
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentSmsLabel }));
    expect(statusTexts()).toStrictEqual([]);
  });

  it("reloads the list on a conflict, because the stored flags moved", async () => {
    const entry = row({ version: 7 });
    setSession(sessionFor("operator"));
    const readMock = readAnswers(page([entry]), page([entry]), page([entry]));
    vi.mocked(contracts.updateConsent).mockRejectedValue(
      new ApiError(409, "VERSION_CONFLICT", "Someone else was first."),
    );
    renderScreen();

    await screen.findByText(titleOf(entry));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));

    await waitFor(() => {
      expect(within(card(entry)).getByRole("alert").textContent).toBe(
        EN.contracts.consentConflict,
      );
    });
    // Showing the old flags beside "try again" would invite a re-submission of a
    // picture that is already out of date, so the row is reloaded first.
    await waitFor(() => {
      expect(readMock).toHaveBeenCalledTimes(2);
    });
  });

  it("names a role refusal and a contract in another tenant apart", async () => {
    setSession(sessionFor("operator"));
    const entry = row({ version: 7 });
    const readMock = readAnswers(page([entry]), page([entry]));
    vi.mocked(contracts.updateConsent).mockRejectedValueOnce(
      new ApiError(403, "FORBIDDEN", "Nope."),
    );
    renderScreen();

    await screen.findByText(titleOf(entry));
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));
    await waitFor(() => {
      expect(within(card(entry)).getByRole("alert").textContent).toBe(
        EN.contracts.consentForbidden,
      );
    });

    // A 404 is not a 403: the route scopes by tenant and cannot be used to learn
    // which ids exist elsewhere, so the two answers must not read the same.
    vi.mocked(contracts.updateConsent).mockRejectedValueOnce(
      new ApiError(404, "NOT_FOUND", "No such contract."),
    );
    fireEvent.click(within(card(entry)).getByRole("button", { name: EN.contracts.consentAction }));
    await waitFor(() => {
      expect(within(card(entry)).getByRole("alert").textContent).toBe(
        EN.contracts.consentNotFound,
      );
    });
    // Neither a 403 nor a 404 moved the row, so neither reloads.
    expect(readMock).toHaveBeenCalledTimes(1);
  });

  it("keeps one row's refusal off every other row", async () => {
    const first = row({ id: 7 });
    const second = row({ id: 9, customer_ref: "ACME-2" });
    setSession(sessionFor("operator"));
    readAnswers(page([first, second]));
    vi.mocked(contracts.updateConsent).mockRejectedValue(
      new ApiError(403, "FORBIDDEN", "Nope."),
    );
    renderScreen();

    await screen.findByText(titleOf(second));
    fireEvent.click(within(card(first)).getByRole("button", { name: EN.contracts.consentAction }));

    await waitFor(() => {
      expect(within(card(first)).getByRole("alert").textContent).toBe(
        EN.contracts.consentForbidden,
      );
    });
    // One shared message would blame every storage contract for one refusal.
    expect(within(card(second)).queryByRole("alert")).toBeNull();
  });
});

describe("what a card states", () => {
  it("states contactability the server derived, in all three shapes", async () => {
    const permitted = row({
      id: 7,
      may_contact: true,
      consent_email: true,
      consent_sms: true,
      consent_channels: ["email", "sms"],
    });
    const optedOut = row({ id: 9, customer_ref: "ACME-2", may_contact: false, do_not_contact: true });
    const silent = row({ id: 11, customer_ref: "ACME-3", may_contact: false });
    setSession(sessionFor("viewer"));
    readAnswers(page([permitted, optedOut, silent]));
    renderScreen();

    await screen.findByText(titleOf(permitted));

    const channels = [EN.contracts.channels.email, EN.contracts.channels.sms].join(", ");
    expect(
      within(card(permitted)).getByText(say(EN.contracts.contactPermitted, { channels })),
    ).toBeDefined();
    expect(within(card(optedOut)).getByText(EN.contracts.contactOptedOut)).toBeDefined();
    // Two rows with every flag off read the same way: "no channel consented" is the
    // flags' answer, and `may_contact` is the model's. The screen shows both truths
    // and computes neither.
    expect(within(card(silent)).getByText(EN.contracts.contactNoChannels)).toBeDefined();
  });

  it("shows the term it has, and says so when it has none", async () => {
    const bare = row({ id: 7 });
    const bounded = row({ id: 9, customer_ref: "ACME-2", start_date: "2026-01-02", end_date: "2027-01-02" });
    setSession(sessionFor("viewer"));
    readAnswers(page([bare, bounded]));
    renderScreen();

    await screen.findByText(titleOf(bare));
    expect(within(card(bare)).getByText(EN.contracts.noDates)).toBeDefined();
    // Calendar dates are formatted in UTC on purpose: `2026-01-02` is a day, not an
    // instant, and formatting it in the reader's zone could render 1 January.
    expect(
      within(card(bounded)).getByText(
        `${dateText("2026-01-02")} – ${dateText("2027-01-02")}`,
      ),
    ).toBeDefined();
  });

  it("truncates the fingerprint, and omits the row when there is none", async () => {
    const fingerprinted = row({ id: 7, sha256: "abcdef0123456789" + "0".repeat(48) });
    const metadataOnly = row({ id: 9, customer_ref: "ACME-2", sha256: null });
    setSession(sessionFor("viewer"));
    readAnswers(page([fingerprinted, metadataOnly]));
    renderScreen();

    await screen.findByText(titleOf(fingerprinted));
    // 64 hex characters are a dedupe key, not a value anyone compares by eye.
    expect(within(card(fingerprinted)).getByText("abcdef0123456789…")).toBeDefined();
    expect(within(card(fingerprinted)).getByText(EN.contracts.shaLabel)).toBeDefined();
    expect(within(card(metadataOnly)).queryByText(EN.contracts.shaLabel)).toBeNull();

    // `has_document` is the one question a reader has about the stored file.
    expect(within(card(metadataOnly)).getByText(EN.contracts.documentMissing)).toBeDefined();
  });

  it("links each card to the contract's own page", async () => {
    setSession(sessionFor("viewer"));
    readAnswers(page([row({ id: 7 })]));
    renderScreen();

    const link = await screen.findByRole("link", { name: titleOf(row({ id: 7 })) });
    expect(link.getAttribute("href")).toBe("/en/contracts/7");
  });
});

describe("failure and emptiness", () => {
  it("does not let an unreachable API look like an empty list", async () => {
    setSession(sessionFor("operator"));
    const readMock = vi.mocked(contracts.list);
    readMock.mockRejectedValueOnce(new ApiError(0, "network_error", "The API is not reachable."));
    readMock.mockResolvedValueOnce(page([row()]));
    renderScreen();

    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.contracts.offline]);
    });
    // "No contract matches this filter" and "we could not ask" must never read the same.
    expect(screen.queryByText(EN.contracts.empty)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: EN.contracts.retry }));

    expect(await screen.findByText(titleOf(row()))).toBeDefined();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("says the list is empty when it is, and shows no count", async () => {
    setSession(sessionFor("operator"));
    readAnswers(page([]));
    renderScreen();

    expect(await screen.findByText(EN.contracts.empty)).toBeDefined();
    expect(screen.getByText(EN.contracts.emptyHint)).toBeDefined();
    // "0 in view" over an empty card is noise, and the sentence already says it.
    expect(screen.queryByText(say(EN.contracts.countLabel, { count: 0 }))).toBeNull();
  });

  it("translates a role refusal on the list itself", async () => {
    setSession(sessionFor("viewer"));
    const readMock = vi.mocked(contracts.list);
    readMock.mockRejectedValueOnce(new ApiError(403, "FORBIDDEN", "Nope."));
    readMock.mockResolvedValueOnce(page([row()]));
    renderScreen();

    // Reading needs any authenticated role, so this should be unreachable — it is
    // translated anyway, because a UI that assumes it cannot happen turns a policy
    // change into an untranslated envelope.
    await waitFor(() => {
      expect(alertTexts()).toStrictEqual([EN.contracts.forbidden]);
    });

    fireEvent.click(screen.getByRole("button", { name: EN.contracts.retry }));
    expect(await screen.findByText(titleOf(row()))).toBeDefined();
  });
});
