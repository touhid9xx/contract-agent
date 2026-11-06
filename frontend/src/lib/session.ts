/**
 * The client session: two tokens, held in memory and nowhere else.
 *
 * Deliberately not `localStorage`, not `sessionStorage`, not a readable cookie.
 * A token in web storage is readable by any script on the origin, so a single
 * cross-site-scripting hole anywhere in the app — or in any dependency it loads
 * — exfiltrates a long-lived refresh token, not just a fifteen-minute access
 * token. Keeping both in a module variable means a reload signs the user out
 * and a stolen DOM snapshot is worthless.
 *
 * The cost is real and is the reason this is a decision rather than a detail:
 * a refresh token the page cannot persist cannot survive a reload, so the user
 * re-authenticates each time they open the app. The alternative — an httpOnly
 * cookie set by the API — is the right end state and is recorded as the next
 * step in D10-19; it needs a `Set-Cookie` on login/refresh plus a CSRF story,
 * which is a backend slice, not a frontend one.
 *
 * This module is framework-free on purpose. The React binding in
 * `components/auth-provider.tsx` subscribes to it with `useSyncExternalStore`,
 * so there is exactly one copy of the tokens and no chance of component state
 * and module state disagreeing about whether the user is signed in.
 */

import { ApiError, auth } from "@/lib/api";
import type { Session } from "@/lib/api";

type Listener = () => void;

const listeners = new Set<Listener>();

let session: Session | null = null;

/** Subscribe to session changes. Returns the unsubscribe function. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** The current session, or `null` when signed out. */
export function getSession(): Session | null {
  return session;
}

/**
 * Server snapshot for `useSyncExternalStore`.
 *
 * The server has no session and must not pretend to: returning the real value
 * here would make the first client render disagree with the server's markup.
 */
export function getServerSession(): Session | null {
  return null;
}

/** Replace the session. `null` signs out. */
export function setSession(next: Session | null): void {
  session = next;
  for (const listener of listeners) {
    listener();
  }
}

/**
 * The rotation currently in flight, if any.
 *
 * Refresh tokens are single-use: redemption spends the presented token and
 * mints a successor in the same family, and a second redemption of the spent
 * token is treated as theft — the backend revokes the **entire family**, which
 * signs the legitimate client out as well.
 *
 * That makes concurrent refreshes a correctness problem, not a performance one.
 * Two requests that both see a 401 would otherwise both redeem the same token
 * and guarantee a logout. Sharing one promise means the second caller waits for
 * the first rotation and reuses its result.
 */
let inFlight: Promise<Session | null> | null = null;

/**
 * Rotate the refresh token, at most once at a time.
 *
 * Resolves with the new session, or `null` when the session is over for good
 * (expired, revoked, or replayed). The store is updated either way, so every
 * subscriber — including a component that never called this — sees the truth.
 */
export function refreshSession(): Promise<Session | null> {
  if (inFlight) {
    return inFlight;
  }

  const refreshToken = session?.refresh_token;
  if (!refreshToken) {
    return Promise.resolve(null);
  }

  inFlight = auth
    .refresh(refreshToken)
    .then((next) => {
      setSession(next);
      return next;
    })
    .catch(() => {
      // Any refusal means the family is gone; a dead session must not linger in
      // the UI looking valid. The next action will ask the user to sign in.
      setSession(null);
      return null;
    })
    .finally(() => {
      inFlight = null;
    });

  return inFlight;
}

/**
 * Run an authenticated call, rotating the session once if the token expired.
 *
 * One retry, not a loop: the second 401 is a real refusal (the account was
 * deactivated, or the epoch moved under us) and retrying it would only be a
 * slower way to fail.
 */
export async function authorized<T>(call: (accessToken: string) => Promise<T>): Promise<T> {
  const active = session;
  if (!active) {
    throw new ApiError(401, "unauthorized", "You are not signed in.");
  }

  try {
    return await call(active.access_token);
  } catch (error) {
    if (!(error instanceof ApiError) || !error.isUnauthorized) {
      throw error;
    }

    const renewed = await refreshSession();
    if (!renewed) {
      throw error;
    }
    return await call(renewed.access_token);
  }
}
