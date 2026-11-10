// @vitest-environment jsdom
/**
 * The contract detail screen — the seventh screen under test, and the first one
 * that reads **five routes at once**.
 *
 * The six screens before it each proved one seam: `login` that a session can be
 * obtained, `review` that a queue can be worked, `escalations` that a filter can
 * live in the address, `agent` that a refusal can carry the answer, `notifications`
 * that one action may ask before it acts, `contracts` that a form can own two
 * transports and a card a version. This screen carries rules none of them reach:
 *
 *   * **the route segment is a string that must not be coerced.** `"12abc"` is an
 *     invalid id, not 12, and `""` is an invalid id, not 0. `Number.parseInt` reads
 *     the first as twelve and `Number` as `NaN`; `Number` reads the empty segment as
 *     zero. The digit test refuses all three before any of them can be an id.
 *   * **five reads fail independently.** A 403 on one of them may not blank the
 *     other four: they are separate queries for exactly that reason.
 *   * **`may_contact` decides and the flags only explain.** The screen states the
 *     server's contactability answer; it never re-derives one from the four
 *     booleans, which would be a second contact policy free to disagree.
 *   * **counts come from the responses' own `count`.** Nothing on this page sums
 *     `items.length` and calls it the total.
 *   * **nothing shows a value.** The fields route withholds every extracted value
 *     and this card says so rather than looking broken.
 *   * **the three related links carry the contract.** `withContractScope` spells
 *     `?contract=` so a link and the screen that parses it cannot disagree.
 *
 * The harness is the one `frontend/src/app/[locale]/agent/page.test.tsx`
 * established: `next/navigation` is doubled (there is no Next runtime here and the
 * page needs `useParams`), `@/lib/api` is doubled through `importOriginal` so the
 * real `ApiError` survives `instanceof`, and everything else is real:
 *
 *   * a real `QueryClientProvider` with a fresh client per test and `retry: false`,
 *     because these tests ask *what the screen asked and how often*;
 *   * the real `NextIntlClientProvider` and the real catalogue, so every assertion
 *     is against the sentence a user reads — including the field labels, which are
 *     read from the **review** namespace on purpose;
 *   * the real `AuthProvider` and the real session store, so the signed-out state
 *     is produced by the session, not passed in as a prop.
 *
 * What is pinned:
 *
 *   * a valid segment is the id every one of the five routes is asked for,
 *   * a malformed segment asks nothing at all and claims no failure,
 *   * a signed-out visitor is asked to sign in and the client is asked nothing,
 *   * a `404` reads as "not in this organisation" and a `403` as "your role cannot",
 *     and the two are told apart from an unreachable API and from a plain failure,
 *   * a refusal is not retried — each route is asked exactly once,
 *   * the term joins two dates, prints one alone, and says so when there is none,
 *   * calendar dates are formatted on the contract's clock (UTC), instants on the
 *     reader's,
 *   * the fingerprint is truncated and the full hash never appears,
 *   * contactability is the server's answer, even when a flag disagrees with it,
 *   * an empty field list is not a clean contract,
 *   * a field row shows its review state, confidence and provenance and never a value,
 *   * each related read states its own count and its own latest row,
 *   * one refused read leaves the other two answering,
 *   * a retry re-asks only the read that failed,
 *   * the three related links carry `?contract=` and the current locale.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

const { address } = vi.hoisted(() => ({
  /** The route segment under test. Written per test, read on every render. */
  address: { current: "7" },
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: address.current }),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    contracts: { get: vi.fn(), fields: vi.fn() },
    agent: { decisions: vi.fn() },
    notifications: { list: vi.fn() },
    escalations: { queue: vi.fn() },
  };
});

import ContractDetailPage from "@/app/[locale]/contracts/[id]/page";
import { AuthProvider } from "@/components/auth-provider";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { agent, ApiError, contracts, escalations, notifications } from "@/lib/api";
import type {
  AgentDecision,
  AgentDecisionList,
  ContractField,
  ContractFields,
  ContractListItem,
  EscalationQueue,
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

/** The same formatters the screen builds, so a formatting change is not a failure. */
const DATE_FMT = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" });
const INSTANT_FMT = new Intl.DateTimeFormat("en", { dateStyle: "medium", timeStyle: "short" });

function dateText(value: string): string {
  return DATE_FMT.format(new Date(value));
}

/**
 * A contract with nothing switched on.
 *
 * The consent flags and `may_contact` all default to `false` because that is the
 * fail-closed state a freshly ingested row is really in — this page describes rows
 * in it, so the fixture starts there rather than at a convenient truth.
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

const BASE_FIELD: ContractField = {
  field_name: "email",
  status: "approved",
  confidence: 0.9,
  version: 1,
  source_page: null,
  source_line: null,
  reviewed_at: null,
  review_reason: null,
  blocks_pipeline: false,
};

function field(overrides: Partial<ContractField> = {}): ContractField {
  return { ...BASE_FIELD, ...overrides };
}

/** The fields answer. `blocking` is the server's count and is never derived here. */
function fieldPage(items: ContractField[] = [], blocking = 0): ContractFields {
  return { contract_id: 7, fields: items, blocking };
}

const BASE_DECISION: AgentDecision = {
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
  return { ...BASE_DECISION, ...overrides };
}

/** `count` is deliberately allowed to exceed `items.length`: it is the total. */
function decisionPage(items: AgentDecision[] = [], count = items.length): AgentDecisionList {
  return { items, count, contract_id: 7 };
}

const BASE_REMINDER: NotificationRow = {
  id: 11,
  contract_id: 7,
  channel: "email",
  template: "T-30",
  status: "pending",
  scheduled_date: "2027-01-15",
  scheduled_at: null,
  sent_at: null,
  attempts: 0,
  last_error: null,
  provider_message_id: null,
  idempotency_key: "slot-1",
};

function reminder(overrides: Partial<NotificationRow> = {}): NotificationRow {
  return { ...BASE_REMINDER, ...overrides };
}

function reminderPage(items: NotificationRow[] = [], count = items.length): NotificationList {
  return { items, count, contract_id: 7 };
}

/** `overdue` and `sla_hours` are the server's numbers, echoed rather than computed. */
function escalationPage(count = 0, overdue = 0, slaHours = 48): EscalationQueue {
  return { items: [], count, overdue, sla_hours: slaHours, contract_id: 7 };
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

function signedIn(role: UserRole = "viewer") {
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
          <ContractDetailPage />
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

function fieldsCard(): HTMLElement {
  return cardTitled(EN.contracts.fieldsHeading);
}

function relatedCard(): HTMLElement {
  return cardTitled(EN.contracts.detailRelatedHeading);
}

function alertTexts(): (string | null)[] {
  return screen.queryAllByRole("alert").map((node) => node.textContent);
}

/** The three "Open" links, in the order the card renders them. */
function openHrefs(): string[] {
  return screen
    .queryAllByRole("link", { name: EN.contracts.detailOpen })
    .map((node) => node.getAttribute("href") ?? "");
}

/**
 * The same three links found by the parameter they carry, not by their label.
 *
 * The label is translated, so asserting it would tie a locale test to English.
 * The parameter is the contract this page has with the screens it links to, so it
 * is the right handle for a test about the locale in the path.
 */
function scopedHrefs(): string[] {
  return Array.from(document.querySelectorAll("a[href*='?contract=7']")).map(
    (node) => node.getAttribute("href") ?? "",
  );
}

beforeEach(() => {
  address.current = "7";
  setSession(null);
  vi.mocked(contracts.get).mockReset();
  vi.mocked(contracts.fields).mockReset();
  vi.mocked(agent.decisions).mockReset();
  vi.mocked(notifications.list).mockReset();
  vi.mocked(escalations.queue).mockReset();

  vi.mocked(contracts.get).mockResolvedValue(row());
  vi.mocked(contracts.fields).mockResolvedValue(fieldPage());
  vi.mocked(agent.decisions).mockResolvedValue(decisionPage());
  vi.mocked(notifications.list).mockResolvedValue(reminderPage());
  vi.mocked(escalations.queue).mockResolvedValue(escalationPage());
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("the address", () => {
  it("asks all five routes for the id in the segment", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));

    // Every read carries the same id, and each goes to its own route: the page
    // composes four existing screens' reads rather than inventing a sixth endpoint.
    expect(contracts.get).toHaveBeenCalledWith(ACCESS, 7);
    expect(contracts.fields).toHaveBeenCalledWith(ACCESS, 7);
    expect(agent.decisions).toHaveBeenCalledWith(ACCESS, 7);
    expect(notifications.list).toHaveBeenCalledWith(ACCESS, { contractId: 7 });
    expect(escalations.queue).toHaveBeenCalledWith(ACCESS, { contractId: 7 });
  });

  it("asks nothing and claims nothing for a segment with a tail", async () => {
    // `"12abc"` is a tail that coerces: `Number.parseInt` reads it as 12, the id of
    // a contract nobody named, while `Number` gives `NaN`. The whole-digit shape test
    // is what refuses the string, so no read is ever enabled with either number.
    address.current = "12abc";
    signedIn();
    renderScreen();

    await waitFor(() => {
      expect(contracts.get).not.toHaveBeenCalled();
    });
    expect(contracts.fields).not.toHaveBeenCalled();
    expect(agent.decisions).not.toHaveBeenCalled();
    expect(notifications.list).not.toHaveBeenCalled();
    expect(escalations.queue).not.toHaveBeenCalled();
    // A disabled read is pending, not failed: nothing is reported as broken.
    expect(alertTexts()).toStrictEqual([]);
  });

  it("refuses zero, which is a shape the server can never answer", async () => {
    // `^\\d+$` matches "0"; the `> 0` clause is what rejects it, because the
    // server's path parameter is an `int` and its ids start at one.
    address.current = "0";
    signedIn();
    renderScreen();

    await waitFor(() => {
      expect(contracts.get).not.toHaveBeenCalled();
    });
    expect(alertTexts()).toStrictEqual([]);
  });

  it("refuses the empty segment rather than reading it as zero", async () => {
    address.current = "";
    signedIn();
    renderScreen();

    await waitFor(() => {
      expect(contracts.get).not.toHaveBeenCalled();
    });
  });

  it("refuses a digit string past the safe integer range", async () => {
    // It matches the shape, but ids past `MAX_SAFE_INTEGER` stop comparing
    // reliably — an id that cannot be compared is not an id.
    address.current = "99999999999999999999";
    signedIn();
    renderScreen();

    await waitFor(() => {
      expect(contracts.get).not.toHaveBeenCalled();
    });
  });

  it("reads a leading zero as the same contract", async () => {
    // Pinned as it is: this screen's guard is `^\\d+$`, so "07" is 7 — unlike
    // `contract-scope.ts`, whose `^[1-9][0-9]*$` refuses it. The divergence is
    // recorded in D10-55 rather than quietly reconciled here.
    address.current = "07";
    signedIn();
    renderScreen();

    await waitFor(() => {
      expect(contracts.get).toHaveBeenCalledWith(ACCESS, 7);
    });
  });
});

describe("who may read", () => {
  it("asks a signed-out visitor to sign in, and asks the API nothing", async () => {
    renderScreen();

    expect(screen.getByText(EN.contracts.signInRequired)).toBeDefined();
    expect(screen.getByRole("link", { name: EN.contracts.signIn }).getAttribute("href")).toBe(
      "/en/login",
    );

    // Every read is `enabled: isAuthenticated`, so nothing is even built.
    await waitFor(() => {
      expect(contracts.get).not.toHaveBeenCalled();
    });
    expect(contracts.fields).not.toHaveBeenCalled();
    expect(alertTexts()).toStrictEqual([]);
  });

  it("lets every role read, because the route does", async () => {
    signedIn("viewer");
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    expect(contracts.get).toHaveBeenCalledTimes(1);
  });
});

describe("the header", () => {
  it("names the contract, its reference and its state", async () => {
    vi.mocked(contracts.get).mockResolvedValue(
      row({ id: 7, customer_ref: "ACME-9", status: "reminder_sent" }),
    );
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    // The reference appears twice — the header and the facts list — on purpose.
    expect(screen.getAllByText("ACME-9").length).toBe(2);
    expect(screen.getByText(EN.contracts.statuses.reminder_sent)).toBeDefined();
  });

  it("colours only the two states that need a person", async () => {
    vi.mocked(contracts.get).mockResolvedValue(row({ status: "expired" }));
    signedIn();
    renderScreen();

    const chip = await screen.findByText(EN.contracts.statuses.expired);
    expect(chip.className).toContain("text-danger");
  });

  it("shows a loading line until the row arrives, and never a stale one", async () => {
    let release: (value: ContractListItem) => void = () => {};
    vi.mocked(contracts.get).mockImplementation(
      () =>
        new Promise<ContractListItem>((resolve) => {
          release = resolve;
        }),
    );
    signedIn();
    renderScreen();

    expect(screen.getByText(EN.contracts.loading)).toBeDefined();
    expect(screen.queryByText(say(EN.contracts.contractLabel, { id: 7 }))).toBeNull();

    release(row());
    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
  });
});

describe("the facts", () => {
  it("joins a term's two calendar dates on the contract's clock", async () => {
    vi.mocked(contracts.get).mockResolvedValue(
      row({ start_date: "2026-01-02", end_date: "2026-12-31" }),
    );
    signedIn();
    renderScreen();

    const expected = `${dateText("2026-01-02")} – ${dateText("2026-12-31")}`;
    expect(await screen.findByText(expected)).toBeDefined();
    expect(screen.queryByText(EN.contracts.noDates)).toBeNull();
  });

  it("prints the one date a half-term has", async () => {
    vi.mocked(contracts.get).mockResolvedValue(row({ end_date: "2026-12-31" }));
    signedIn();
    renderScreen();

    expect(await screen.findByText(dateText("2026-12-31"))).toBeDefined();
  });

  it("says the term is unrecorded rather than showing an empty line", async () => {
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.noDates)).toBeDefined();
  });

  it("states the source, the risk, the document and the ingestion instant", async () => {
    vi.mocked(contracts.get).mockResolvedValue(
      row({ source: "pdf", risk: "high", has_document: true }),
    );
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.sources.pdf)).toBeDefined();
    expect(screen.getByText(EN.contracts.risks.high)).toBeDefined();
    expect(screen.getByText(EN.contracts.documentStored)).toBeDefined();
    // An instant keeps the reader's zone, which is why it has its own formatter.
    expect(screen.getByText(INSTANT_FMT.format(new Date("2026-10-01T09:15:00Z")))).toBeDefined();
  });

  it("says a metadata-only contract has no document", async () => {
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.documentMissing)).toBeDefined();
  });

  it("truncates the fingerprint, and the full hash appears nowhere", async () => {
    const sha = "abcdef0123456789" + "f".repeat(48);
    vi.mocked(contracts.get).mockResolvedValue(row({ sha256: sha }));
    signedIn();
    renderScreen();

    expect(await screen.findByText("abcdef0123456789…")).toBeDefined();
    // A dedupe key, not a value to copy out: the 64-character form must not leak.
    expect(screen.queryByText(sha)).toBeNull();
  });

  it("omits the fingerprint line when the row has none", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    expect(screen.queryByText(EN.contracts.shaLabel)).toBeNull();
  });

  it("states the server's contactability answer with the channels it allows", async () => {
    vi.mocked(contracts.get).mockResolvedValue(
      row({ may_contact: true, consent_email: true, consent_channels: ["email", "sms"] }),
    );
    signedIn();
    renderScreen();

    const expected = say(EN.contracts.contactPermitted, {
      channels: `${EN.contracts.channels.email}, ${EN.contracts.channels.sms}`,
    });
    expect(await screen.findByText(expected)).toBeDefined();
  });

  it("does not re-derive contactability from the flags", async () => {
    // `may_contact: false` with `consent_email: true` is the disagreement that
    // matters: a screen reading the flags would say "Allowed on email" here.
    vi.mocked(contracts.get).mockResolvedValue(
      row({ may_contact: false, consent_email: true, consent_channels: ["email"] }),
    );
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.contactNoChannels)).toBeDefined();
    expect(screen.queryByText(say(EN.contracts.contactPermitted, { channels: "email" }))).toBeNull();
  });

  it("distinguishes an opt-out from having consented to nothing", async () => {
    vi.mocked(contracts.get).mockResolvedValue(row({ do_not_contact: true }));
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.contactOptedOut)).toBeDefined();
    expect(screen.queryByText(EN.contracts.contactNoChannels)).toBeNull();
  });
});

describe("the extracted fields", () => {
  it("calls an unread contract unread, not clean", async () => {
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.fieldsEmpty)).toBeDefined();
    expect(screen.getByText(EN.contracts.fieldsEmptyHint)).toBeDefined();
    // The distinction this card exists for: no rows is not "nothing blocks".
    expect(screen.queryByText(EN.contracts.fieldsClear)).toBeNull();
  });

  it("states that no field blocks when the server's count is zero", async () => {
    vi.mocked(contracts.fields).mockResolvedValue(fieldPage([field()], 0));
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.fieldsClear)).toBeDefined();
    expect(screen.queryByText(EN.contracts.fieldsEmpty)).toBeNull();
  });

  it("reports the blocking count the server sent, not the rows it can see", async () => {
    // Two rows, three blocking: the count is an answer, not arithmetic.
    vi.mocked(contracts.fields).mockResolvedValue(
      fieldPage([field({ field_name: "email" }), field({ field_name: "phone" })], 3),
    );
    signedIn();
    renderScreen();

    expect(await screen.findByText(say(EN.contracts.fieldsBlocking, { count: 3 }))).toBeDefined();
  });

  it("labels a field from the review namespace, with its state, score and provenance", async () => {
    vi.mocked(contracts.fields).mockResolvedValue(
      fieldPage(
        [
          field({
            field_name: "email",
            status: "pending_review",
            confidence: 0.876,
            blocks_pipeline: true,
            source_page: 2,
            source_line: 17,
            review_reason: "The scan cut the address in half.",
          }),
        ],
        1,
      ),
    );
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.fieldsBlocking, { count: 1 }));
    const card = within(fieldsCard());

    // One vocabulary for a field's name and its state, shared with the queue that
    // decides it: two wordings is how a screen calls a rejected field "decided".
    expect(card.getByText(EN.review.fields.email)).toBeDefined();
    expect(card.getByText(EN.review.statuses.pending_review)).toBeDefined();
    expect(card.getByText(EN.contracts.fieldStatusColumn)).toBeDefined();
    expect(card.getByText(EN.contracts.fieldConfidenceColumn)).toBeDefined();
    expect(card.getByText("0.88")).toBeDefined();
    expect(card.getByText(EN.contracts.fieldBlocks)).toBeDefined();
    expect(card.getByText(say(EN.review.sourcePageLine, { page: 2, line: 17 }))).toBeDefined();
    expect(card.getByText("The scan cut the address in half.")).toBeDefined();
  });

  it("says a field clears the pipeline when the model says it does", async () => {
    vi.mocked(contracts.fields).mockResolvedValue(fieldPage([field({ blocks_pipeline: false })], 0));
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.fieldDoesNotBlock)).toBeDefined();
    expect(screen.queryByText(EN.contracts.fieldBlocks)).toBeNull();
  });

  it("shows a page without a line, and no line at all without a page", async () => {
    vi.mocked(contracts.fields).mockResolvedValue(
      fieldPage([field({ field_name: "phone", source_page: 4, source_line: null })], 0),
    );
    signedIn();
    renderScreen();

    // The card cannot be scoped until the row it hangs off has arrived.
    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(fieldsCard());
    expect(await card.findByText(say(EN.review.sourcePageOnly, { page: 4 }))).toBeDefined();
    expect(card.queryByText(EN.contracts.fieldSourceColumn)).not.toBeNull();
  });

  it("hides the provenance and reason rows when the server sent neither", async () => {
    vi.mocked(contracts.fields).mockResolvedValue(fieldPage([field()], 0));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(fieldsCard());
    expect(card.queryByText(EN.contracts.fieldSourceColumn)).toBeNull();
    expect(card.queryByText(EN.contracts.fieldReasonColumn)).toBeNull();
  });

  it("says values are not shown here, so the card does not look broken", async () => {
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.fieldsHint)).toBeDefined();
  });
});

describe("the related reads", () => {
  it("states each response's own count", async () => {
    // One row carried, twelve recorded: the card must say twelve.
    vi.mocked(agent.decisions).mockResolvedValue(decisionPage([decision()], 12));
    vi.mocked(notifications.list).mockResolvedValue(reminderPage([reminder()], 3));
    vi.mocked(escalations.queue).mockResolvedValue(escalationPage(2, 1, 48));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(relatedCard());
    expect(
      await card.findByText(say(EN.contracts.detailDecisionsCount, { count: 12 })),
    ).toBeDefined();
    expect(card.getByText(say(EN.contracts.detailNotificationsCount, { count: 3 }))).toBeDefined();
    expect(card.getByText(say(EN.contracts.detailEscalationsCount, { count: 2 }))).toBeDefined();
  });

  it("names the newest decision's channel in the shared vocabulary", async () => {
    vi.mocked(agent.decisions).mockResolvedValue(decisionPage([decision({ channel: "sms" })], 1));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const expected = say(EN.contracts.detailLatestDecision, {
      channel: EN.contracts.channels.sms,
    });
    expect(await within(relatedCard()).findByText(expected)).toBeDefined();
  });

  it("prints the newest reminder's slot exactly as the server sent it", async () => {
    // A calendar date on the contract's clock: formatting it here would move it.
    vi.mocked(notifications.list).mockResolvedValue(
      reminderPage([reminder({ scheduled_date: "2027-01-15" })], 1),
    );
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const expected = say(EN.contracts.detailLatestReminder, { date: "2027-01-15" });
    expect(await within(relatedCard()).findByText(expected)).toBeDefined();
    expect(screen.queryByText(dateText("2027-01-15"))).toBeNull();
  });

  it("reports lateness with the server's overdue count and SLA", async () => {
    vi.mocked(escalations.queue).mockResolvedValue(escalationPage(4, 3, 72));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const expected = say(EN.contracts.detailEscalationOverdue, { count: 3, hours: 72 });
    expect(await within(relatedCard()).findByText(expected)).toBeDefined();
  });

  it("states a zero count and offers no latest row when there is none", async () => {
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(relatedCard());
    expect(
      await card.findByText(say(EN.contracts.detailDecisionsCount, { count: 0 })),
    ).toBeDefined();
    expect(card.queryByText(/Newest recommendation/)).toBeNull();
  });

  it("lets one refused read leave the other two answering", async () => {
    // Five separate queries exist for exactly this: a 403 on the decisions route
    // must not blank the reminders and escalations counts.
    vi.mocked(agent.decisions).mockRejectedValue(
      new ApiError(403, "forbidden", "Your role cannot read this contract."),
    );
    vi.mocked(notifications.list).mockResolvedValue(reminderPage([reminder()], 3));
    vi.mocked(escalations.queue).mockResolvedValue(escalationPage(2, 1, 48));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(relatedCard());
    expect(await card.findByText(EN.contracts.detailForbidden)).toBeDefined();
    expect(card.getByText(say(EN.contracts.detailNotificationsCount, { count: 3 }))).toBeDefined();
    expect(card.getByText(say(EN.contracts.detailEscalationsCount, { count: 2 }))).toBeDefined();
  });

  it("re-asks only the read that failed", async () => {
    vi.mocked(agent.decisions)
      .mockRejectedValueOnce(new ApiError(0, "unreachable", "The API is not reachable."))
      .mockResolvedValueOnce(decisionPage([decision()], 1));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(relatedCard());
    const retry = await card.findByRole("button", { name: EN.contracts.retry });
    fireEvent.click(retry);

    expect(
      await card.findByText(say(EN.contracts.detailDecisionsCount, { count: 1 })),
    ).toBeDefined();
    expect(agent.decisions).toHaveBeenCalledTimes(2);
    expect(notifications.list).toHaveBeenCalledTimes(1);
    expect(escalations.queue).toHaveBeenCalledTimes(1);
  });

  it("carries the contract into all three links", async () => {
    // A link that ignores the filter looks like a filter and shows everything,
    // which is worse than no link — so the parameter is the one the screens parse.
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    expect(openHrefs()).toStrictEqual([
      "/en/agent?contract=7",
      "/en/notifications?contract=7",
      "/en/escalations?contract=7",
    ]);
  });

  it("carries the reader's locale into all three links", async () => {
    signedIn();
    renderScreen("bn");

    await waitFor(() => {
      expect(scopedHrefs().length).toBe(3);
    });
    expect(scopedHrefs()).toStrictEqual([
      "/bn/agent?contract=7",
      "/bn/notifications?contract=7",
      "/bn/escalations?contract=7",
    ]);
  });
});

describe("refusals", () => {
  it("reads a 404 as a contract this organisation does not hold", async () => {
    vi.mocked(contracts.get).mockRejectedValue(
      new ApiError(404, "not_found", "Contract not found."),
    );
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.detailNotFound)).toBeDefined();
    // Nothing below the header can be drawn without a row.
    expect(screen.queryByText(EN.contracts.fieldsHeading)).toBeNull();
  });

  it("tells a 403 apart from a 404", async () => {
    vi.mocked(contracts.get).mockRejectedValue(new ApiError(403, "forbidden", "Nope."));
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.detailForbidden)).toBeDefined();
    expect(screen.queryByText(EN.contracts.detailNotFound)).toBeNull();
  });

  it("tells an unreachable API apart from a refusal", async () => {
    vi.mocked(contracts.get).mockRejectedValue(
      new ApiError(0, "unreachable", "The API is not reachable."),
    );
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.offline)).toBeDefined();
    expect(screen.queryByText(EN.contracts.detailNotFound)).toBeNull();
  });

  it("falls back to a plain failure for anything else", async () => {
    vi.mocked(contracts.get).mockRejectedValue(new ApiError(500, "server", "Boom."));
    signedIn();
    renderScreen();

    expect(await screen.findByText(EN.contracts.failed)).toBeDefined();
  });

  it("does not retry a refusal — each route is asked exactly once", async () => {
    // A 403 and a 404 are answers, not transient failures: retrying repeats the
    // refusal and delays the sentence the reader needs.
    vi.mocked(contracts.fields).mockRejectedValue(new ApiError(500, "server", "Boom."));
    vi.mocked(agent.decisions).mockRejectedValue(new ApiError(500, "server", "Boom."));
    vi.mocked(notifications.list).mockRejectedValue(new ApiError(500, "server", "Boom."));
    vi.mocked(escalations.queue).mockRejectedValue(new ApiError(500, "server", "Boom."));
    signedIn();
    renderScreen();

    await waitFor(() => {
      expect(alertTexts().length).toBe(4);
    });
    expect(contracts.get).toHaveBeenCalledTimes(1);
    expect(contracts.fields).toHaveBeenCalledTimes(1);
    expect(agent.decisions).toHaveBeenCalledTimes(1);
    expect(notifications.list).toHaveBeenCalledTimes(1);
    expect(escalations.queue).toHaveBeenCalledTimes(1);
  });

  it("re-asks the contract when the reader tries again, and draws it", async () => {
    vi.mocked(contracts.get).mockClear();
    vi.mocked(contracts.get)
      .mockRejectedValueOnce(new ApiError(0, "unreachable", "The API is not reachable."))
      .mockResolvedValueOnce(row({ customer_ref: "ACME-2" }));
    signedIn();
    renderScreen();

    fireEvent.click(await screen.findByRole("button", { name: EN.contracts.retry }));

    expect(await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }))).toBeDefined();
    expect(screen.getAllByText("ACME-2").length).toBe(2);
    expect(contracts.get).toHaveBeenCalledTimes(2);
    expect(screen.queryByText(EN.contracts.offline)).toBeNull();
  });

  it("keeps a field refusal inside the fields card", async () => {
    vi.mocked(contracts.fields).mockRejectedValue(new ApiError(500, "server", "Boom."));
    signedIn();
    renderScreen();

    await screen.findByText(say(EN.contracts.contractLabel, { id: 7 }));
    const card = within(fieldsCard());
    expect(await card.findByText(EN.contracts.failed)).toBeDefined();
    // The row and the other reads are unaffected.
    expect(screen.getByText(say(EN.contracts.contractLabel, { id: 7 }))).toBeDefined();
    expect(
      screen.getByText(say(EN.contracts.detailDecisionsCount, { count: 0 })),
    ).toBeDefined();
  });
});
