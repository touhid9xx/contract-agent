/**
 * The two catalogues, pinned against each other.
 *
 * `i18n/messages.ts` already forces the *shape*: `bn` is assigned to
 * `Record<Locale, Messages>` with no `as` cast, so a namespace or key added to
 * `en.json` and forgotten in `bn.json` is a compile error. That guard is real and
 * this file does not replace it — but it stops at the strings. A compiler cannot
 * see into a value, so these can still reach a Bengali screen while every English
 * one looks perfect:
 *
 *   - an argument renamed on one side (`{count}` → `{total}`), which renders the
 *     literal text `{total}` and drops the number;
 *   - a key that exists in `bn.json` and not in `en.json`, which `tsc` allows —
 *     missing keys are caught, extra ones are not;
 *   - an empty string, which renders as nothing at all rather than as a gap;
 *   - an English sentence pasted into `bn.json`, which passes every check above
 *     and renders fluent English on a Bengali screen.
 *
 * Every assertion below is relational: no key name, count or sentence is written
 * down here. That is deliberate — a test that listed the keys would be a third
 * copy of the catalogue, and the copy is what drifts. This asks only whether the
 * two files agree, which stays true as the catalogue grows.
 *
 * **The plural branch is unexercised by the catalogue and is here on purpose.**
 * Nothing in either file is an ICU plural or select today — all 43 braced
 * messages are plain `{name}` interpolation — so the category comparison can only
 * fire once one is written. The last test below runs the reader over a synthetic
 * plural so that tripwire is proven to work rather than merely intended.
 */
import { describe, expect, it } from "vitest";

import { flatten } from "@/i18n/catalogue";
import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";

/** Index one past the brace that closes the one at `open`. */
function closeOf(text: string, open: number): number {
  let depth = 0;
  for (let i = open; i < text.length; i += 1) {
    if (text[i] === "{") depth += 1;
    if (text[i] === "}") {
      depth -= 1;
      if (depth === 0) return i + 1;
    }
  }
  // An unbalanced message is a defect in the catalogue, not in the reader, so
  // fail loudly instead of silently reading a truncated argument list.
  throw new Error(`unbalanced braces in message: ${text}`);
}

/** Strip balanced brace bodies so only the plural categories remain visible. */
function withoutBodies(text: string): string {
  let stripped = text;
  while (/\{[^{}]*\}/.test(stripped)) stripped = stripped.replace(/\{[^{}]*\}/g, "");
  return stripped;
}

function categoriesOf(rest: string): string[] {
  return [...withoutBodies(rest).matchAll(/\b(zero|one|two|few|many|other)\b/g)].map(
    (match) => match[1] ?? "",
  );
}

interface Arg {
  /** `plural`, `select`, `number`, `date`… or null for a plain `{name}`. */
  type: string | null;
  /** For a plural or selectordinal argument, the CLDR categories it branches on. */
  categories: string[];
}

/** Every top-level ICU argument in a message, by name. */
function argsOf(text: string): Map<string, Arg> {
  const out = new Map<string, Arg>();
  let i = 0;
  while (i < text.length) {
    if (text[i] !== "{") {
      i += 1;
      continue;
    }
    const end = closeOf(text, i);
    const body = text.slice(i + 1, end - 1);
    const comma = body.indexOf(",");
    const name = (comma === -1 ? body : body.slice(0, comma)).trim();
    const rest = comma === -1 ? "" : body.slice(comma + 1).trim();
    const type = rest === "" ? null : (rest.split(",")[0] ?? "").trim();
    const branches = type === "plural" || type === "selectordinal";
    out.set(name, { type, categories: branches ? categoriesOf(rest) : [] });
    i = end;
  }
  return out;
}

/** One message's arguments as plain data, so two locales can be compared by value. */
function shapeOf(text: string): Record<string, Arg> {
  return Object.fromEntries(argsOf(text));
}

/**
 * One Bengali code point — the block `bn.json` is written in.
 *
 * Used as a presence test, never as a correctness one: this can tell that a
 * value was *written* in Bengali, not that the Bengali says what the English
 * says. A bad translation passes; a missing one does not.
 */
const BENGALI = /[\u0980-\u09FF]/;

/**
 * Whether a message is prose rather than a token.
 *
 * A run of three lowercase Latin letters is what separates `"Escalations"` from
 * `"API"`, `"{id}"` and `"N/A"` — the values that are legitimately identical in
 * both locales and must not be flagged. Widening this to any Latin letter would
 * catch all three and the rule would be deleted within a week.
 */
const PROSE = /[a-z]{3}/;

const flatEn = flatten(en);
const flatBn = flatten(bn);

describe("the message catalogues", () => {
  it("was actually read, so the assertions below are not vacuous", () => {
    // A floor, not a count to maintain: it fails if flattening returns nothing.
    expect(flatEn.size).toBeGreaterThan(300);
    expect(flatBn.size).toBeGreaterThan(300);
  });

  it("carries the same keys in both locales", () => {
    const missingFromBn = [...flatEn.keys()].filter((key) => !flatBn.has(key));
    const missingFromEn = [...flatBn.keys()].filter((key) => !flatEn.has(key));
    expect({
      "only in en.json": missingFromBn,
      "only in bn.json": missingFromEn,
    }).toEqual({ "only in en.json": [], "only in bn.json": [] });
  });

  it("uses the same ICU arguments, of the same kind, in both locales", () => {
    const drift: Record<string, { en: Record<string, Arg>; bn: Record<string, Arg> }> = {};
    for (const [key, english] of flatEn) {
      const bengali = flatBn.get(key);
      if (bengali === undefined) continue; // key parity is the test above
      const left = shapeOf(english);
      const right = shapeOf(bengali);
      if (JSON.stringify(left) !== JSON.stringify(right)) {
        drift[key] = { en: left, bn: right };
      }
    }
    expect(drift).toEqual({});
  });

  it("leaves no message empty in either locale", () => {
    const empty = [
      ...[...flatEn].filter(([, text]) => text.trim() === "").map(([key]) => `en.json: ${key}`),
      ...[...flatBn].filter(([, text]) => text.trim() === "").map(([key]) => `bn.json: ${key}`),
    ];
    expect(empty).toEqual([]);
  });

  it("translates its prose into Bengali rather than copying the English", () => {
    // The failure this catches is the ordinary one: a new key is added to both
    // files in one commit and the `bn.json` half is the English sentence, so it
    // typechecks, has the right arguments, is not empty — and is not translated.
    // Nothing else in the suite can see that, because every other check compares
    // the files to each other.
    const copied = [...flatEn]
      .filter(([key, english]) => {
        const bengali = flatBn.get(key);
        return bengali !== undefined && PROSE.test(english) && !BENGALI.test(bengali);
      })
      .map(([key, english]) => `${key}: ${JSON.stringify(english)}`);
    expect(copied).toEqual([]);
  });

  it("never carries Bengali prose in the English catalogue", () => {
    // The same mistake in the other direction: translating `en.json`'s value and
    // leaving the English one replaced, which reads as fluent Bengali to a
    // Bengali reviewer and as nonsense on the English screens.
    const leaked = [...flatEn]
      .filter(([, english]) => BENGALI.test(english))
      .map(([key]) => key);
    expect(leaked).toEqual([]);
  });

  it("reads a plural message, which the catalogue has none of yet", () => {
    // Proves the comparison above would notice a plural that drifted, before the
    // first plural is ever written. Without this the plural branch is untested
    // code guarding nothing.
    expect(shapeOf("{count, plural, one {# day} other {# days}}")).toEqual({
      count: { type: "plural", categories: ["one", "other"] },
    });
    expect(shapeOf("Oldest {minutes} min")).toEqual({ minutes: { type: null, categories: [] } });
    expect(shapeOf("Threshold {threshold}")).toEqual({ threshold: { type: null, categories: [] } });
  });
});
