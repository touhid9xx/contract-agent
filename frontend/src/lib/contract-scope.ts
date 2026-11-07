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
 * Parse `?contract=` into a scope.
 *
 * Hand-checked rather than `Number(raw)`, for the reason `D10-34` records against
 * the route segment: `Number("12abc")` is `12` and `Number("")` is `0`, so a
 * malformed address would quietly become a real filter on a contract nobody named.
 * The server's path parameter is an `int` and its query parameters are `gt=0`, so
 * the same rule holds here: whole digits, no leading zero, greater than zero.
 */
export function readContractScope(params: ReadableParams): ContractScope {
  const raw = params.get(CONTRACT_PARAM);
  if (raw === null || raw === "") return { kind: "none" };
  if (!/^[1-9][0-9]*$/.test(raw)) return { kind: "invalid", raw };
  const id = Number(raw);
  // A digit string can still exceed `Number.MAX_SAFE_INTEGER`, where ids stop
  // comparing reliably; such an id is malformed rather than merely absent.
  if (!Number.isSafeInteger(id)) return { kind: "invalid", raw };
  return { kind: "contract", id };
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
 * way back out is `readContractScope`, and the two agree because they test the
 * same shape.
 */
export function contractIdFromInput(raw: string): number | undefined {
  const trimmed = raw.trim();
  if (!/^[1-9][0-9]*$/.test(trimmed)) return undefined;
  const id = Number(trimmed);
  return Number.isSafeInteger(id) ? id : undefined;
}

/** True when a typed id is usable or empty — used to disable a submit button. */
export function inputNamesAContract(raw: string): boolean {
  return raw.trim() === "" || contractIdFromInput(raw) !== undefined;
}
