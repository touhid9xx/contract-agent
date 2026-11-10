/**
 * The refusal vocabulary — one place, fully qualified.
 *
 * A server refusal is a `409` carrying a machine-readable `code`
 * (`ApiError.code`). Three screens turn those codes into sentences, and each of
 * them names the message key it wants. Before this module those names lived
 * inside the screens as `Record<string, string>` maps and inline `if` branches,
 * and *nothing* checked them against the catalogues: `Record<string, string>`
 * erases the literal type, `useTranslations()` with no namespace accepts any
 * string, and `composed-keys.test.ts` cannot see them either — its parser looks
 * for `t(\`ns.${TABLE[…]}​\`)`, while the screens wrote `t(\`agent.${key}\`)`
 * against a *variable*, which is not a table lookup.
 *
 * The consequence was that renaming `notifications.refusalContractClosed` in
 * both catalogues — a change the parity test and the type checker both allow —
 * left every test green and every operator reading the literal text
 * `notifications.refusalContractClosed` where a sentence belongs.
 *
 * So the keys are collected here, written out in full rather than appended to a
 * namespace at the call site, and `refusals.test.ts` asserts every one of them
 * resolves in both catalogues.
 *
 * **What this cannot pin.** The *codes* — `CONTRACT_CLOSED`, `NOT_YET_DUE` and
 * the rest — are the server's vocabulary, declared as `REFUSAL_*` constants in
 * `app/workers/dispatch.py`, `app/workers/decision.py`, `app/workers/escalation.py`
 * and `app/api/v1/notifications.py`. Restating them as a TypeScript union would
 * be a fourth copy of the same fact with nothing to verify it against Python, so
 * this module deliberately does not: a code the server stops sending leaves a
 * table entry unused, and a code it starts sending falls through to the screens'
 * generic sentence. Both are survivable, which is why the codes are not pinned
 * and the sentences are.
 *
 * A table is `as const satisfies Record<string, string>` for the reason the
 * status maps are: the shape is checked, and the values stay literal strings
 * that a reader (and the spec) can enumerate.
 */

/**
 * The agent screen's refusals.
 *
 * `app.workers.decision` gives each refusal its own code rather than one `409`
 * precisely so this screen can tell them apart — "the customer consented to
 * nothing" and "the pipeline is blocked further back" call for different next
 * actions.
 */
export const AGENT_REFUSALS = {
  PIPELINE_BLOCKED: "agent.refusePipelineBlocked",
  NO_CONSENT: "agent.refuseNoConsent",
  CONTACT_REFUSED: "agent.refuseContactRefused",
  CONTRACT_CLOSED: "agent.refuseContractClosed",
  NO_CONTACT_VALUE: "agent.refuseNoContactValue",
  /**
   * Carries `details.permitted` — the channels consent *would* have allowed.
   * The screen composes this sentence with `agent.permittedChannels` when the
   * server sent any, and returns it alone when it did not.
   */
  CHANNEL_NOT_PERMITTED: "agent.refuseChannelNotPermitted",
  /**
   * Raised by the notification routes, not by the decision worker, but it is
   * the same screen that reports it: correcting a decision requires one to
   * exist, so "there is no current recommendation" is a refusal about a button
   * on this screen.
   */
  NO_DECISION: "agent.noDecision",
} as const satisfies Record<string, string>;

/**
 * The notification queue's refusals.
 *
 * The queue has two buttons — dispatch and requeue — and these are the codes
 * they can provoke, plus three that planning raises earlier (`NOT_IN_WINDOW`,
 * `MISSING_END_DATE`, `NEEDS_REVIEW`). The over-coverage is deliberate: a
 * sentence that never renders costs nothing, and the alternative is a vague
 * "that did not work" the day one of them becomes reachable.
 *
 * Codes this route family raises that are *not* here — `NOTIFICATION_EXISTS`,
 * `DECISION_MISMATCH`, `TENANT_MISMATCH`, and `NO_DECISION` (which the agent
 * table above does translate, for the button that provokes it) — come from
 * paths no button on *this* screen reaches, so they fall through to the generic
 * failure here rather than being translated for a click that cannot happen.
 */
export const NOTIFICATION_REFUSALS = {
  CONTRACT_CLOSED: "notifications.refusalContractClosed",
  CONTACT_REFUSED: "notifications.refusalContactRefused",
  NO_CONSENT: "notifications.refusalNoConsent",
  CHANNEL_NOT_CONSENTED: "notifications.refusalChannelNotConsented",
  PIPELINE_BLOCKED: "notifications.refusalPipelineBlocked",
  NOT_PENDING: "notifications.refusalNotPending",
  NOT_YET_DUE: "notifications.refusalNotYetDue",
  NOT_IN_WINDOW: "notifications.refusalNotInWindow",
  MISSING_END_DATE: "notifications.refusalMissingEndDate",
  UNKNOWN_TIMEZONE: "notifications.refusalUnknownTimezone",
  NEEDS_REVIEW: "notifications.refusalNeedsReview",
  ATTEMPTS_EXHAUSTED: "notifications.refusalAttemptsExhausted",
  SENDER_UNAVAILABLE: "notifications.refusalSenderUnavailable",
  RECIPIENT_UNAVAILABLE: "notifications.refusalRecipientUnavailable",
  NOT_FAILED: "notifications.refusalNotFailed",
  CONCURRENT_UPDATE: "notifications.refusalConcurrentUpdate",
} as const satisfies Record<string, string>;

/**
 * The escalation queue's refusals.
 *
 * Two 409 codes that are different facts — the row was resolved under you, or
 * you asked for a move the queue does not have — so they are named separately
 * rather than sharing one "conflict" sentence.
 */
export const ESCALATION_REFUSALS = {
  ESCALATION_CLOSED: "escalations.closedRefusal",
  INVALID_TRANSITION: "escalations.invalidTransition",
} as const satisfies Record<string, string>;

/**
 * The message key for a refusal code, or `undefined` if this table has none.
 *
 * `undefined` rather than a fallback sentence, because the fallback differs per
 * screen: each one has its own `forbidden` / `offline` / `failed` ladder to walk
 * first, and a table that answered for those codes would hide them.
 *
 * The `typeof` guard is not decoration. These tables are object literals, so
 * `table["constructor"]` and `table["toString"]` reach `Object.prototype` at
 * runtime — a refusal code is attacker-influenced data on the wire — and
 * returning a function where a key belongs would render as a garbage sentence
 * rather than falling through to the generic one.
 */
export function refusalKey(table: Record<string, string>, code: string): string | undefined {
  const found = table[code];
  return typeof found === "string" ? found : undefined;
}
