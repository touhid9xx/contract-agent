/**
 * Reading a contract out of an address.
 *
 * Three screens — agent decisions, notifications and escalations — can each answer
 * for one contract, and a contract's own page links to all three. The scope lives
 * in the query string rather than in `useState` because a link has to be able to
 * carry it: local state survives a click but not a reload, a bookmark, the back
 * button, or the copy-paste that sends the address to a colleague. A link whose
 * filter silently evaporates is worse than no link, which is why `D10-35` left the
 * three links plain until this existed.
 *
 * The parameter is `contract`, not the wire's `contract_id`: this is the address
 * bar's vocabulary, and a screen is free to name its scope without mirroring the
 * parameter name of a route that may never see it (the agent screen's own route
 * calls it `contract_id`; that is the route's business, not the reader's).
 */

/** The one place the parameter's name is spelled. */
export const CONTRACT_PARAM = "contract";

/** Set to `1` to include a contract's closed rows; absent means the working queue. */
export const RESOLVED_PARAM = "resolved";

/**
 * What an address says about a contract.
 *
 * Three outcomes rather than `number | undefined`, because "the address does not
 * name a contract" and "the address names something that cannot be one" call for
 * two different screens: the first shows the tenant-wide list, the second must not
 * — showing everything behind a malformed filter is the same lie as ignoring a
 * filter, only louder.
 */
export type ContractScope =
  | { kind: "none" }
  | { kind: "contract"; id: number }
  | { kind: "invalid"; raw: string };

/** The part of a `URLSearchParams` this module needs, so it is callable from a test. */
type ReadableParams = { get(name: string): string | null };

/**
 * What an id may be spelled like — the one rule both ways in and out obey.
 *
 * Hand-checked rather than `Number(raw)`, for the reason `D10-34` records against
 * the route segment: `Number("")` is `0`, and a spaced, exponent or hex spelling of
 * a real id survives it — `Number(" 12")`, `Number("1e3")` and `Number("0x10")` are
 * 12, 1000 and 16 — so a malformed address would quietly become a real filter on a
 * contract nobody named. What `Number` cannot rescue comes back `NaN`, which is not
 * an id either: `"12abc"` is `NaN`, not 12. Reading that one as twelve is
 * `Number.parseInt`'s doing, and this module does neither: it tests the digits first,
 * and `Number.isSafeInteger` refuses a digit string past the range where ids stop
 * comparing reliably.
 *
 * **A leading zero is not one of those traps, and this rule now says so (`D10-76`).**
 * It is the only spelling that cannot change the value it names: the tail of
 * `"12abc"`, the space in `" 12"`, the exponent in `"1e3"` and the base in `"0x10"`
 * each stand for a *different* contract than the digits in front of them, while
 * padding a number with zeros stands for the same one. The server has always agreed —
 * `GET /contracts/01` answers **200**, measured in `D10-59` — and the contract detail
 * page has always read `/contracts/07` as contract 7, so refusing it here left one
 * client holding two grammars for one id. Both accept it now, and the parse still
 * returns a `number`, so a padded address is canonicalised before anything reaches
 * the wire; only an address someone typed can carry the padding at all, because
 * `withContractScope` takes a number.
 *
 * What stays refused is every spelling that could mean something else: `"0"` and
 * `"00"` (no contract has id zero — the route answers 404 rather than naming a
 * scope), a sign, a space, a fraction, a power of ten, a tail, and anything past
 * `Number.MAX_SAFE_INTEGER`.
 */
const ID_SHAPE = /^0*[1-9][0-9]*$/;

/** `ID_SHAPE` applied to text already known to be a whole address value. */
function readId(raw: string): number | undefined {
  if (!ID_SHAPE.test(raw)) return undefined;
  const id = Number(raw);
  return Number.isSafeInteger(id) ? id : undefined;
}

/**
 * Parse `?contract=` into a scope.
 *
 * The rule itself lives in `ID_SHAPE` above, so the query string and the typed box
 * cannot drift apart: they are not two tests that agree, they are one function
 * called twice.
 */
export function readContractScope(params: ReadableParams): ContractScope {
  const raw = params.get(CONTRACT_PARAM);
  if (raw === null || raw === "") return { kind: "none" };
  const id = readId(raw);
  return id === undefined ? { kind: "invalid", raw } : { kind: "contract", id };
}

/**
 * Whether the address asks for a contract's closed rows as well.
 *
 * Only `1` means yes. Anything else — `0`, `true`, a typo — is the working queue:
 * a value that is not the one this module writes is not a filter someone asked
 * for, and treating `resolved=false` as "yes" would be the surprise.
 */
export function readResolved(params: ReadableParams): boolean {
  return params.get(RESOLVED_PARAM) === "1";
}

/** `base` with this contract named, or `base` untouched when there is no scope. */
export function withContractScope(base: string, id: number | undefined): string {
  return id === undefined ? base : `${base}?${CONTRACT_PARAM}=${id}`;
}

/** `base`, which may already carry `?contract=`, plus the resolved flag. */
export function withResolved(base: string, resolved: boolean): string {
  if (!resolved) return base;
  return `${base}${base.includes("?") ? "&" : "?"}${RESOLVED_PARAM}=1`;
}

/**
 * A typed id → the id to put in the address, or `undefined` for "no filter".
 *
 * The screens accept a hand-typed id as well as a link, so this is the single
 * place that decides what a usable id looks like on the way *in*; parsing on the
 * way back out is `readContractScope`, and the two agree because they call the
 * same `readId` — the only difference left is the `trim()`, which belongs to a
 * draft in a box and not to an address someone may have pasted.
 */
export function contractIdFromInput(raw: string): number | undefined {
  return readId(raw.trim());
}

/** True when a typed id is usable or empty — used to disable a submit button. */
export function inputNamesAContract(raw: string): boolean {
  return raw.trim() === "" || contractIdFromInput(raw) !== undefined;
}
