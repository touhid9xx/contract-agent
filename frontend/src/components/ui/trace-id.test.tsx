// @vitest-environment jsdom
/**
 * `TraceId` — the correlation id the client has been parsing since `D10-72` and
 * that no screen showed.
 *
 * `api.test.ts` already pins that `failure()` reads `trace_id` into `ApiError.traceId`
 * (`D10-72`). What it could not pin is that the value ever reaches a person, so the
 * rules here are the ones only a rendered component has:
 *
 *   * a populated envelope prints the id — the whole point, and the only case where
 *     the component renders anything at all;
 *   * an envelope carrying `trace_id: null` prints **nothing**. The server binds a
 *     trace id on every request and leaves it null only outside one (CLI, worker
 *     ticks), so `null` is a real value, not a missing one — and "Trace id: null" is
 *     worse than silence because it looks quotable;
 *   * a failure that never reached the server (offline, `status === 0`) prints
 *     nothing, since there is no server line for the id to name;
 *   * an error that is not our envelope prints nothing. `unknown` is the prop type
 *     precisely because a screen's failure slot can hold anything, from a `TypeError`
 *     thrown in render to a rejected `fetch`;
 *   * the sentence comes from the catalogue, so the Bengali page gets Bengali prose.
 *
 * The absence cases are what a presence-only test would miss: with the `instanceof`
 * guard or the null check deleted, "renders the id" stays green while every other
 * failure grows a bogus line.
 */
import { cleanup, render } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import { afterEach, describe, expect, it } from "vitest";

import { TraceId } from "@/components/ui/trace-id";
import type { Locale } from "@/i18n/config";
import { getMessages } from "@/i18n/messages";
import { ApiError } from "@/lib/api";

const EN = getMessages("en");
const BN = getMessages("bn");

/** Stand-in for ICU substitution; the catalogue string is a plain `{id}` slot. */
function say(template: string, id: string): string {
  return template.replace("{id}", id);
}

/** Render and return the text the component contributed to the document. */
function rendered(error: unknown, locale: Locale = "en"): string {
  const { container } = render(
    <NextIntlClientProvider locale={locale} messages={getMessages(locale)}>
      <TraceId error={error} />
    </NextIntlClientProvider>,
  );
  return container.textContent ?? "";
}

afterEach(cleanup);

describe("an envelope that carries an id", () => {
  it("prints it in the sentence the catalogue gives", () => {
    expect(rendered(new ApiError(500, "server", "Boom.", null, null, "trace-9"))).toBe(
      say(EN.common.traceId, "trace-9"),
    );
  });

  it("prints the Bengali sentence on a Bengali page", () => {
    // The id itself is locale-free; only the words around it move. A locale that
    // fell back to English would still print an id, so the sentence is the assertion.
    const bengali = rendered(new ApiError(500, "server", "Boom.", null, null, "trace-9"), "bn");

    expect(bengali).toBe(say(BN.common.traceId, "trace-9"));
    expect(bengali).not.toBe(say(EN.common.traceId, "trace-9"));
  });
});

describe("a failure with nothing worth quoting", () => {
  it("says nothing when the envelope carried an explicit null", () => {
    // `trace_id` is null for an envelope built outside a request — a real answer,
    // not an absent field, and the one a naive `${error.traceId}` would print.
    expect(rendered(new ApiError(500, "server", "Boom.", "req-1", null, null))).toBe("");
  });

  it("says nothing for a failure that never reached a server", () => {
    // Offline: no response, therefore no `x-trace-id` for the id to name.
    expect(rendered(new ApiError(0, "unreachable", "Down."))).toBe("");
  });

  it("says nothing for an error that is not our envelope", () => {
    // A thrown `TypeError` in render or a raw `fetch` rejection lands in the same
    // slot as an `ApiError`, so the guard has to be structural, not just null-aware.
    expect(rendered(new TypeError("x is not a function"))).toBe("");
    expect(rendered(undefined)).toBe("");
    expect(rendered("trace-9")).toBe("");
  });

  it("says nothing for an object that merely looks like one", () => {
    // `instanceof` rather than duck typing: a plain object with the right shape is
    // not proof that the client's envelope parser produced it.
    expect(rendered({ traceId: "trace-9" })).toBe("");
  });
});
