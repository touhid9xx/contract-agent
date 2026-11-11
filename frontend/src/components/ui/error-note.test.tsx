// @vitest-environment jsdom
/**
 * `ErrorNote` — a refused write, said in the screen's words, with the id under it.
 *
 * `trace-id.test.tsx` already pins the id component in isolation, and `api.test.ts`
 * pins that `traceId` is parsed. Neither can pin what went wrong between them: a card
 * was handed `describeFailure(cause)` — a finished sentence — so the `ApiError` was
 * gone before anything rendered. The rules here are the ones that only a component
 * taking a *cause* can hold:
 *
 *   * the sentence the screen passes comes out, and the id follows it;
 *   * only the sentence is the `role="alert"` — the id is a quiet line beneath it, so
 *     a screen reader is not told the id is part of the failure;
 *   * `describe` receives **the same cause** the id is read from, which is what makes
 *     "this sentence, that id" a structural guarantee rather than a coincidence;
 *   * a cause with no id still prints the sentence. This is where `ErrorNote` departs
 *     from `TraceId`: absence of an id must never swallow the message.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it } from "vitest";

import { ErrorNote } from "@/components/ui/error-note";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError } from "@/lib/api";

const EN = getMessages("en");
const BN = getMessages("bn");

/** A refusal as the catalogue says it, for the screens that map causes to prose. */
const SENTENCE = "The row changed while you were looking at it.";

function renderNote(
  cause: unknown,
  {
    describe: describeCause = () => SENTENCE,
    locale = "en" as Locale,
  }: {
    /** The screen's wording for the cause; the default ignores the cause entirely. */
    describe?: (cause: unknown) => string;
    locale?: Locale;
  } = {},
) {
  return render(
    <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
      <ErrorNote cause={cause} describe={describeCause} />
    </NextIntlClientProvider>,
  );
}

afterEach(cleanup);

describe("a refusal with a server id", () => {
  it("prints the screen's sentence and the id beneath it", () => {
    const { container } = renderNote(new ApiError(409, "version_conflict", "Stale.", null, null, "trace-9"));

    expect(container.textContent).toBe(`${SENTENCE}${EN.common.traceId.replace("{id}", "trace-9")}`);
  });

  it("announces the sentence as the alert, and not the id", () => {
    // One alert per refusal, and its text is exactly the sentence: a screen reader
    // should hear what failed, not a hexadecimal id trailing the sentence.
    renderNote(new ApiError(409, "version_conflict", "Stale.", null, null, "trace-9"));

    const alerts = screen.getAllByRole("alert");

    expect(alerts).toHaveLength(1);
    expect(screen.getByRole("alert").textContent).toBe(SENTENCE);
  });

  it("gives the id to the same cause it described", () => {
    // The point of a callback: the wording and the id are derived from one value, so
    // a sentence can never be printed beside another request's id.
    const cause = new ApiError(500, "server", "Boom.", null, null, "trace-9");
    let described: unknown = null;

    const { container } = renderNote(cause, {
      describe: (c) => {
        described = c;
        return SENTENCE;
      },
    });

    expect(described).toBe(cause);
    expect(container.textContent).toContain("trace-9");
  });

  it("prints the Bengali id sentence on a Bengali screen", () => {
    // The id is locale-free; only the words around it move. A locale that fell back
    // to English would still print an id, so the sentence is the assertion.
    const { container } = renderNote(
      new ApiError(500, "server", "Boom.", null, null, "trace-9"),
      { locale: "bn" },
    );

    expect(container.textContent).toContain(BN.common.traceId.replace("{id}", "trace-9"));
    expect(container.textContent).not.toContain(EN.common.traceId.replace("{id}", "trace-9"));
  });
});

describe("a refusal with nothing to quote", () => {
  it("still says what failed when the envelope carried an explicit null", () => {
    // `TraceId` would render nothing here; the sentence must not follow it. An
    // envelope built outside a request has a real `null` id, and a reader who cannot
    // quote an id still has to be told their write was refused.
    const { container } = renderNote(new ApiError(409, "version_conflict", "Stale.", null, null, null));

    expect(container.textContent).toBe(SENTENCE);
    expect(screen.getByRole("alert").textContent).toBe(SENTENCE);
  });

  it("still says what failed when the cause never reached a server", () => {
    const { container } = renderNote(new ApiError(0, "unreachable", "Down."));

    expect(container.textContent).toBe(SENTENCE);
  });

  it("still says what failed for a cause that is not our envelope", () => {
    // A mutation's `error` is `unknown`: a thrown `TypeError` reaches the same slot.
    const { container } = renderNote(new TypeError("x is not a function"));

    expect(container.textContent).toBe(SENTENCE);
  });
});
