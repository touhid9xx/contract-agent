/**
 * The refusal vocabulary, pinned to the catalogues.
 *
 * Three screens translate a server refusal code into a sentence: the agent
 * decision screen, the notification queue, and the escalation queue. Each names
 * the catalogue key it wants, and until this file nothing checked those names:
 *
 *   * the maps were annotated `Record<string, string>`, which erases the literal
 *     type, so the compiler had only "some string" to look at;
 *   * the screens call `useTranslations()` with no namespace, so `t()` accepts
 *     any key at all;
 *   * `composed-keys.test.ts` could not see them either — it resolves the table
 *     reference inside a call like `t(\`ns.${TABLE[code]}\`)`, while these maps
 *     were read into a local (`const key = REFUSAL_KEYS[code]`) and then handed
 *     to `t(\`ns.${key}\`)` on a later line.
 *
 * So renaming `notifications.refusalContractClosed` in *both* catalogues left
 * every check in the repo green and would have put the literal string
 * `notifications.refusalContractClosed` on screen where an operator expects a
 * sentence — the failure this spec exists to prevent. The vocabulary itself
 * moved to `@/lib/refusals` for one reason: a node-environment test can import a
 * plain table, whereas reaching a `const` inside a `page.tsx` would mean parsing
 * screen source, which is the seam that has already broken twice here.
 *
 * Nothing below writes down a sentence or a key name. Every assertion compares
 * the tables against the catalogues, and the floors exist only so that reading
 * nothing cannot pass.
 */
import { describe, expect, it } from "vitest";

import { flatten } from "@/i18n/catalogue";
import bn from "@/i18n/messages/bn.json";
import en from "@/i18n/messages/en.json";
import { AGENT_REFUSALS, ESCALATION_REFUSALS, NOTIFICATION_REFUSALS } from "@/lib/refusals";

const TABLES: Record<string, Record<string, string>> = {
  AGENT_REFUSALS,
  ESCALATION_REFUSALS,
  NOTIFICATION_REFUSALS,
};

const flatEn = flatten(en);
const flatBn = flatten(bn);

/** Tables today carry 25 keys between them; the floor is a vacuity guard. */
const KEY_FLOOR = 24;

/** Every `[table, code, key]` triple, so a failure names the offender. */
function named(): { table: string; code: string; key: string }[] {
  return Object.entries(TABLES).flatMap(([table, entries]) =>
    Object.entries(entries).map(([code, key]) => ({ table, code, key })),
  );
}

/** A catalogue key whose last segment is named like a refusal sentence. */
function refusalNamed(key: string): boolean {
  const leaf = key.slice(key.lastIndexOf(".") + 1);
  return /^refus/i.test(leaf) || /Refusal$/.test(leaf);
}

describe("the refusal vocabulary", () => {
  it("reads the tables and both catalogues at all", () => {
    // Without these, every check below would pass on empty collections.
    expect(Object.keys(TABLES).length).toBeGreaterThanOrEqual(3);
    expect(flatEn.size).toBeGreaterThanOrEqual(200);
    expect(flatBn.size).toBe(flatEn.size);
    expect(named().length).toBeGreaterThanOrEqual(KEY_FLOOR);
    for (const [table, entries] of Object.entries(TABLES)) {
      expect(Object.keys(entries).length, `${table} is empty`).toBeGreaterThanOrEqual(2);
    }
  });

  it("names only sentences that exist in both catalogues", () => {
    const missingEn = named()
      .filter(({ key }) => !flatEn.has(key))
      .map(({ table, code, key }) => `${table}.${code} → ${key}`);
    const missingBn = named()
      .filter(({ key }) => !flatBn.has(key))
      .map(({ table, code, key }) => `${table}.${code} → ${key}`);
    // English and Bengali are checked apart because a key present in one and
    // absent from the other is a translation bug, not the same bug twice.
    expect(missingEn).toEqual([]);
    expect(missingBn).toEqual([]);
  });

  it("gives each code in a table a sentence of its own", () => {
    // These codes are translated separately because they name different,
    // actionable facts. Two codes sharing one sentence would mean the screen had
    // quietly started collapsing them back into one answer.
    for (const [table, entries] of Object.entries(TABLES)) {
      const sentences = Object.values(entries);
      const repeated = sentences.filter(
        (sentence) => sentences.indexOf(sentence) !== sentences.lastIndexOf(sentence),
      );
      expect([...new Set(repeated)], `${table} repeats a sentence`).toEqual([]);
    }
  });

  it("keeps every table inside its own screen's namespace", () => {
    // Derived from the table's own name rather than listed here: AGENT_REFUSALS
    // may only name `agent.…` sentences, NOTIFICATION_REFUSALS `notifications.…`,
    // and so on, so a key pasted into the wrong table shows up as a foreign
    // namespace instead of rendering a plausible sentence from another screen.
    for (const [table, entries] of Object.entries(TABLES)) {
      const namespace = table.split("_")[0]!.toLowerCase();
      const foreign = Object.entries(entries)
        .filter(([, key]) => !key.split(".")[0]!.startsWith(namespace))
        .map(([code, key]) => `${table}.${code} → ${key}`);
      expect(foreign).toEqual([]);
    }
  });

  it("reaches every catalogue key that is named like a refusal", () => {
    // The other direction: a sentence added to the catalogues and forgotten by
    // every screen. Checked against the English catalogue only because
    // `messages.test.ts` already pins en/bn key parity.
    const reached = new Set(named().map(({ key }) => key));
    const namedLikeRefusals = [...flatEn.keys()].filter(refusalNamed);
    expect(namedLikeRefusals.length).toBeGreaterThanOrEqual(20);
    const orphans = namedLikeRefusals.filter((key) => !reached.has(key));
    expect(orphans).toEqual([]);
  });
});
