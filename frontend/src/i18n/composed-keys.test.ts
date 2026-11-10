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
 * **The five sites with no map in between.** `review.fields.${item.field_name}`,
 * `review.statuses.${item.status}`, `review.contractStatuses.${item.contract_status}`,
 * `escalations.statuses.${row.status}` and `escalations.contractStatuses.${reply.data.contract_status}`
 * interpolate a raw wire value, so there is no literal here to read. `D10-65`
 * left those five measured by hand — a snapshot, not a guard — and `D10-66`
 * recorded that no case drove the report below. Both are now read from the tree:
 * the members come from `backend/app/db/enums.py`, the frontend's own vocabulary
 * from the `Wire values of `app.db.enums.…`` docstrings in `lib/api.ts`, and
 * every member is asked to resolve under its prefix in both catalogues. A fifth
 * `EscalationStatus` on the server is now a failure here in three places.
 *
 * Like its neighbours, every assertion is relational: no screen, map, key name or
 * count is written down below. The floors in the first test exist only so this
 * cannot pass by parsing nothing.
 *
 * **What this does not cover, stated rather than implied.** The wire-value half
 * parses the server's enum *source text*: an enum assembled at runtime, or a
 * value outside any `StrEnum`, is invisible to it. It covers the four enums the
 * five sites draw on, not every wire value in the API. And copy remains
 * unchecked, exactly as `D10-64` said: a fluent Bengali sentence that says the
 * wrong thing passes this file and its neighbour.
 */
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { flatten } from "@/i18n/catalogue";
import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";

const SRC = dirname(dirname(fileURLToPath(import.meta.url)));
/** The repo root, one level above this app — the server's enums live there. */
const ROOT = dirname(dirname(SRC));
const ENUMS = join(ROOT, "backend", "app", "db", "enums.py");

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

/**
 * `t(\`review.fields.${item.field_name}\`)` → prefix `review.fields`.
 *
 * The same call shape as `compositions` above, so the two are told apart by what
 * sits inside the interpolation: an identifier followed by `[` indexes a table
 * (read there), anything else is a raw wire value (read here). Without that split
 * every table site would also be reported as a wire value with no enum.
 */
function wirePrefixes(source: string): string[] {
  const out: string[] = [];
  for (const match of source.matchAll(/t\(`([A-Za-z]+(?:\.[A-Za-z]+)*)\.\$\{([^{}]*)\}/g)) {
    const [, prefix = "", expression = ""] = match;
    if (/^\w+\[/.test(expression)) continue;
    out.push(prefix);
  }
  return out;
}

/**
 * The `StrEnum` member values declared on the server, by class name.
 *
 * Split on `^class ` rather than scanned, so a member can never be attributed to
 * the class above it. Members are `NAME = "value"` lines; docstrings and the
 * `enum_column` helper do not match that shape.
 */
function enumsIn(source: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  for (const block of source.split(/^class /m).slice(1)) {
    const name = /^(\w+)\(StrEnum\):/.exec(block);
    if (name === null || name[1] === undefined) continue;
    const values = [...block.matchAll(/^[ \t]+[A-Z][A-Z0-9_]* = "([^"]+)"/gm)].map(
      (match) => match[1] ?? "",
    );
    if (values.length > 0) out.set(name[1], values);
  }
  return out;
}

/**
 * The frontend unions that declare a server enum's wire values, by enum name.
 *
 * The anchor is the docstring the unions already carried — ``Wire values of
 * `app.db.enums.FieldName` `` — which asserted a correspondence nothing read, the
 * shape `D10-59` warns about. Making it the anchor turns the claim into the link:
 * rewording the docstring stops the union being found, and the lookup below fails
 * loudly rather than skipping the enum it was supposed to check.
 */
function unionsIn(source: string): Map<string, string[]> {
  const out = new Map<string, string[]>();
  const declaration =
    /\/\*\* Wire values of `app\.db\.enums\.(\w+)`\. \*\/\nexport type \w+ =\n?([^;]+);/g;
  for (const match of source.matchAll(declaration)) {
    const [, enumName = "", body = ""] = match;
    const values = [...body.matchAll(/"([^"]+)"/g)].map((pair) => pair[1] ?? "");
    if (values.length > 0) out.set(enumName, values);
  }
  return out;
}

/**
 * The catalogue prefix each table-less composition writes into, and the server
 * enum its interpolation draws on.
 *
 * The prefix is the one fact neither side carries: `t(\`review.fields.${item.field_name}\`)`
 * names no type, and a catalogue is JSON. So it is written here once — and
 * checked in both directions below, so it can neither miss a site nor outlive one.
 */
const WIRE_PREFIXES = new Map<string, string>([
  ["review.fields", "FieldName"],
  ["review.statuses", "FieldStatus"],
  ["review.contractStatuses", "ContractStatus"],
  ["escalations.statuses", "EscalationStatus"],
  ["escalations.contractStatuses", "ContractStatus"],
]);

/**
 * Table-less compositions that interpolate something which is not a wire value,
 * with the reason each is exempt. Exemptions are checked both ways for the same
 * reason `screens.test.ts` checks `UNTESTED` both ways: a stale one silently
 * widens the hole it documents.
 */
const NOT_A_WIRE_VALUE = new Map<string, string>([
  ["common", "`common.${candidate}` renders a theme name from the local `THEMES` list"],
]);

interface Site {
  path: string;
  namespace: string;
  /** The table's *name*, kept beside the lookup rather than overwritten by it. */
  name: string;
  table: Table | undefined;
}

/**
 * Each composition paired with the table its call site indexes.
 *
 * The name is kept in a field of its own deliberately: spreading the site —
 * `{ ...site, table: file.tables.get(site.table) }` — lets the resolved table
 * overwrite the site's table name, and the report below then names no table at
 * all, printing `no table undefined`. That was `D10-66`, and the comment is here
 * because spreading the site is the thing that will be written again.
 */
function pair(files: {
  path: string;
  tables: Map<string, Table>;
  sites: { namespace: string; table: string }[];
}[]): Site[] {
  return files.flatMap((file) =>
    file.sites.map((site) => ({
      path: file.path,
      namespace: site.namespace,
      name: site.table,
      table: file.tables.get(site.table),
    })),
  );
}

/** The compositions a reader has to be told about, as sentences naming the table. */
function unresolvedReport(paired: Site[]): string[] {
  return paired
    .filter((site) => site.table === undefined)
    .map((site) => `${site.path}: no table ${site.name} for t(\`${site.namespace}.…\`)`);
}

const flatEn = flatten(en);
const flatBn = flatten(bn);

const found = screens().map((path) => {
  const source = readFileSync(path, "utf8");
  return {
    path: path.slice(SRC.length + 1),
    tables: tablesIn(source),
    sites: compositions(source),
    wire: wirePrefixes(source),
  };
});

describe("keys the screens compose", () => {
  const paired = pair(found);

  it("found the screens and their compositions, so the checks below are not vacuous", () => {
    // Floors, not counts to maintain: they fail if parsing stops finding things.
    // 25 sites × the sizes of the tables they index is 96 keys today.
    expect(found.length).toBeGreaterThanOrEqual(8);
    expect(paired.length).toBeGreaterThanOrEqual(20);
    expect(paired.filter((site) => site.table !== undefined).length).toBeGreaterThanOrEqual(20);
  });

  it("names the missing table when a composition's table is not in its file", () => {
    // `D10-66`: this branch runs only once something else has already broken, so
    // no screen can drive it, and it had never been run. It is driven here through
    // the same `pair()` the case below uses. Put the spread back and it reads
    // `no table undefined` — losing the one word the message exists to print.
    const orphaned = pair([
      {
        path: "app/[locale]/somewhere/page.tsx",
        tables: new Map(),
        sites: [{ namespace: "contracts", table: "STATUS_KEYZ" }],
      },
    ]);
    expect(unresolvedReport(orphaned)).toEqual([
      "app/[locale]/somewhere/page.tsx: no table STATUS_KEYZ for t(`contracts.…`)",
    ]);
  });

  it("resolves every composed key in both catalogues", () => {
    const unresolved = unresolvedReport(paired);
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

describe("the wire values the table-less sites interpolate", () => {
  // Read once: an absent file must fail as a named assertion, not as an ENOENT
  // from somewhere inside a regex helper. It is present in CI — the frontend job
  // checks out the whole repo and runs `npm test` from `frontend/` — and a guard
  // that skips when its source is missing is not a guard.
  const enumSource = existsSync(ENUMS) ? readFileSync(ENUMS, "utf8") : "";
  const unions = unionsIn(readFileSync(join(SRC, "lib", "api.ts"), "utf8"));

  it("can see the server's enums and the frontend's unions, so the checks below are not vacuous", () => {
    expect(existsSync(ENUMS)).toBe(true);
    expect(enumsIn(enumSource).size).toBeGreaterThanOrEqual(4);
    expect(unions.size).toBeGreaterThanOrEqual(4);
  });

  it("has a prefix or an exemption for every table-less composition, and none to spare", () => {
    const stated = new Set([...WIRE_PREFIXES.keys(), ...NOT_A_WIRE_VALUE.keys()]);
    const composed = [...new Set(found.flatMap((file) => file.wire))];
    // A site nothing lists is a site no guard reads — which is the defect itself.
    expect(composed.filter((prefix) => !stated.has(prefix))).toEqual([]);
    // And a list entry no screen composes any more is a hole left open in prose.
    const seen = new Set(composed);
    expect([...stated].filter((prefix) => !seen.has(prefix))).toEqual([]);
    expect(NOT_A_WIRE_VALUE.size).toBeGreaterThanOrEqual(1);
  });

  it("resolves every member of each enum under the prefix that interpolates it", () => {
    const enums = enumsIn(enumSource);
    const problems: string[] = [];
    for (const [prefix, enumName] of WIRE_PREFIXES) {
      const members = enums.get(enumName);
      if (members === undefined) {
        problems.push(`${prefix}: no StrEnum ${enumName} in backend/app/db/enums.py`);
        continue;
      }
      const union = unions.get(enumName);
      if (union === undefined) {
        problems.push(`${prefix}: no \`Wire values of \`app.db.enums.${enumName}\`\` in api.ts`);
        continue;
      }
      // The fronts and the server are one vocabulary or the screen lies about it:
      // a member the server added renders as a raw wire value, and a member only
      // the frontend knows is a value it can never receive.
      const frontend = [...union].sort().join(", ");
      const server = [...members].sort().join(", ");
      if (frontend !== server) {
        problems.push(`${prefix}: ${enumName} is [${frontend}] in api.ts, [${server}] on the server`);
      }
      for (const member of members) {
        const key = `${prefix}.${member}`;
        if (!flatEn.has(key)) problems.push(`${prefix}: en.json has no ${key}`);
        if (!flatBn.has(key)) problems.push(`${prefix}: bn.json has no ${key}`);
      }
    }
    expect(problems).toEqual([]);
  });
});
