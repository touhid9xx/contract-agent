/**
 * The catalogue reader the i18n specs share.
 *
 * Three specs ask different questions of the same two message files:
 * `messages.test.ts` asks whether the catalogues agree with each other,
 * `composed-keys.test.ts` whether a key a screen builds at its call site exists,
 * and `refusals.test.ts` whether a sentence a screen names for a server refusal
 * exists. Each begins with the same move — turn a nested catalogue into
 * `dotted.path → sentence` — and that helper was copied verbatim into the first
 * two before this module existed. A third copy would have made one behaviour
 * live in three places to fix, which is the defect this project keeps finding in
 * its own work.
 *
 * It sits in `src/i18n` rather than beside a spec because it is a statement
 * about the catalogues, not about one test. Nothing in the running app imports
 * it, so it enters no bundle.
 *
 * The one thing sharing it costs: a bug here would blind all three specs at
 * once, where three separate copies would fail differently. Each spec therefore
 * keeps a floor on how much it read, so a `flatten` that returned nothing shows
 * up as a failed floor rather than as three vacuous passes.
 */

/** `node.key.key` → value, for every leaf string in a catalogue. */
export function flatten(node: unknown, prefix = ""): Map<string, string> {
  const out = new Map<string, string>();
  if (node === null || typeof node !== "object") return out;
  for (const [key, value] of Object.entries(node as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") out.set(path, value);
    else for (const [nested, text] of flatten(value, path)) out.set(nested, text);
  }
  return out;
}
