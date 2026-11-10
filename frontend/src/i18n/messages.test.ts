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
 *   - an empty string, which renders as nothing at all rather than as a gap.
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

import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";

/** `node.key.key` → value, for every leaf string in a catalogue. */
function flatten(node: unknown, prefix = ""): Map<string, string> {
  const out = new Map<string, string>();
  if (node === null || typeof node !== "object") return out;
  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") out.set(path, value);
    else for (const [nested, text] of flatten(value, path)) out.set(nested, text);
  }
  return out;
}

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
