/**
 * The keys the screens *compose*, checked to exist.
 *
 * `messages.test.ts` pins the two catalogues against each other, and
 * `i18n/messages.ts` pins their shape. Neither can see the other half of the
 * seam: a key built at the call site, like
 *
 *     t(`contracts.${STATUS_KEYS[row.status]}`)
 *
 * is a plain string to the compiler. Rename `contracts.statuses.renewed` in
 * *both* catalogues and every check above still passes — the map still
 * typechecks, the two files still agree, and the contract screen renders the
 * literal text `contracts.statuses.renewed` where a word belongs.
 *
 * So this reads the map literals out of the screen sources and asks whether the
 * key each one composes actually exists. The maps are `as const satisfies
 * Record<SomeEnum, string>`, which is what makes the values enumerable, and the
 * `satisfies` clause is also what proves they are exhaustive — a new enum member
 * with no entry is a type error in the screen, not a test failure here.
 *
 * Like its neighbour, every assertion is relational: no screen, map, key name or
 * count is written down below. The floors in the first test exist only so this
 * cannot pass by parsing nothing.
 *
 * **What this does not cover, stated rather than implied.** Five sites compose a
 * key from a raw wire value with no map in between — `review.fields.${item.field_name}`,
 * `review.statuses.${item.status}`, `review.contractStatuses.${item.contract_status}`,
 * `escalations.statuses.${row.status}` and
 * `escalations.contractStatuses.${reply.data.contract_status}`. There is no
 * literal to read there, so this file cannot see them; they were measured once
 * by hand instead — all five prefixes fully present in both catalogues, and the
 * four enum unions they draw on matching `app/db/enums.py` member for member
 * (7, 5, 6, 4). Adding a fifth member to `EscalationStatus` on the server would
 * be caught by nothing in this suite.
 */
import { readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";

const SRC = dirname(dirname(fileURLToPath(import.meta.url)));

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

/** Every `page.tsx` under `src/app`, as a path relative to `src`. */
function screens(dir = join(SRC, "app")): string[] {
  const found: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) found.push(...screens(path));
    else if (entry.name === "page.tsx") found.push(path);
  }
  return found;
}

interface Table {
  union: string;
  /** Wire value → the namespace-relative key the screen appends to. */
  keys: Record<string, string>;
}

/**
 * The `as const satisfies Record<…, string>` tables declared in one file.
 *
 * The terminator is deliberately `Record<[^>]+>` rather than `Record<\w+>`:
 * `CONSENT_LABELS` satisfies `Record<(typeof CONSENT_FLAGS)[number], string>`,
 * and a narrower pattern skips past it into the *next* declaration and parses
 * both tables as one. The `const`-inside-body guard below fails loudly if that
 * ever happens again rather than quietly merging two tables.
 */
function tablesIn(source: string): Map<string, Table> {
  const out = new Map<string, Table>();
  const declaration =
    /^const (\w+)\b[^=]*= \{([\s\S]*?)^\} as const satisfies Record<([^>]+), string>;/gm;
  for (const match of source.matchAll(declaration)) {
    const [, name = "", body = "", union = ""] = match;
    if (/^const /m.test(body)) throw new Error(`${name}: table body ran past its declaration`);
    const keys: Record<string, string> = {};
    for (const pair of body.matchAll(/^\s*(\w+):\s*"([^"]+)"/gm)) {
      const [, wire = "", relative = ""] = pair;
      if (relative) keys[wire] = relative;
    }
    if (Object.keys(keys).length > 0) out.set(name, { union, keys });
  }
  return out;
}

/** `t(\`contracts.${STATUS_KEYS[row.status]}\`)` → namespace plus table name. */
function compositions(source: string): { namespace: string; table: string }[] {
  const out: { namespace: string; table: string }[] = [];
  for (const match of source.matchAll(/t\(`([A-Za-z]+)\.\$\{(\w+)\[/g)) {
    const [, namespace = "", table = ""] = match;
    out.push({ namespace, table });
  }
  return out;
}

const flatEn = flatten(en);
const flatBn = flatten(bn);

describe("keys the screens compose", () => {
  const found = screens().map((path) => {
    const source = readFileSync(path, "utf8");
    return {
      path: path.slice(SRC.length + 1),
      tables: tablesIn(source),
      sites: compositions(source),
    };
  });

  /**
   * Every site paired with the table its call site indexes.
   *
   * The table's *name* is kept alongside its lookup deliberately: spreading the
   * site would let the resolved table overwrite the name, and the failure report
   * below would then name no table at all — `no table undefined`, the one word
   * the reader needs.
   */
  const paired = found.flatMap((file) =>
    file.sites.map((site) => ({
      path: file.path,
      namespace: site.namespace,
      name: site.table,
      table: file.tables.get(site.table),
    })),
  );

  it("found the screens and their compositions, so the checks below are not vacuous", () => {
    // Floors, not counts to maintain: they fail if parsing stops finding things.
    // 25 sites × the sizes of the tables they index is 96 keys today.
    expect(found.length).toBeGreaterThanOrEqual(8);
    expect(paired.length).toBeGreaterThanOrEqual(20);
    expect(paired.filter((site) => site.table !== undefined).length).toBeGreaterThanOrEqual(20);
  });

  it("resolves every composed key in both catalogues", () => {
    const unresolved = paired
      .filter((site) => site.table === undefined)
      .map((site) => `${site.path}: no table ${site.name} for t(\`${site.namespace}.…\`)`);
    const missing: string[] = [];
    let checked = 0;
    for (const site of paired) {
      if (site.table === undefined) continue;
      for (const [wire, relative] of Object.entries(site.table.keys)) {
        const key = `${site.namespace}.${relative}`;
        checked += 1;
        if (!flatEn.has(key)) {
          missing.push(`${site.path} ${wire} → en.json has no ${key}`);
        }
        if (!flatBn.has(key)) {
          missing.push(`${site.path} ${wire} → bn.json has no ${key}`);
        }
      }
    }
    // A composition whose table is not in its own file cannot be read here, and
    // silently skipping it would let the guard shrink to nothing.
    expect(unresolved).toEqual([]);
    expect(checked).toBeGreaterThanOrEqual(90);
    expect(missing).toEqual([]);
  });
});
