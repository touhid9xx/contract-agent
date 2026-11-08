/**
 * The client session, pinned where a compiler cannot reach.
 *
 * `session.ts` is the one place a single-use refresh token is redeemed, and
 * `D10-17` made that a correctness rule rather than an optimisation: two
 * redemptions of one token are indistinguishable from theft, and the backend
 * answers by revoking the whole family — so the failure mode of a bug here is
 * "the user is logged out", not "the user waits longer". The properties worth a
 * test are therefore not "does it hand back a token" but:
 *
 *   * the server snapshot never claims a session the server cannot have,
 *   * concurrent refreshes share exactly one redemption,
 *   * a refused rotation ends the session instead of leaving a dead one on screen,
 *   * `authorized` retries exactly once — never zero times, never twice.
 *
 * Only `auth.refresh` is replaced, and `importOriginal` is used so `ApiError`
 * stays the real class: `authorized` branches on `instanceof`, which a
 * hand-rolled double could not satisfy honestly.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, auth: { ...actual.auth, refresh: vi.fn() } };
});

import { ApiError, auth } from "@/lib/api";
import type { Session, UserSummary } from "@/lib/api";

import {
  authorized,
  getServerSession,
  getSession,
  refreshSession,
  setSession,
  subscribe,
} from "./session";

const USER: UserSummary = {
  id: "9f1c0a4e-0000-4000-8000-000000000001",
  email: "operator@example.test",
  role: "operator",
  tenant_id: "9f1c0a4e-0000-4000-8000-000000000002",
  tenant_slug: "acme",
  is_active: true,
};

/** A login response, shaped like `/auth/login` and `/auth/refresh`. */
function sessionWith(access: string, refresh: string): Session {
  return {
    access_token: access,
    token_type: "bearer",
    expires_in: 900,
    expires_at: "2026-03-01T12:15:00Z",
    refresh_token: refresh,
    refresh_expires_at: "2026-03-08T12:00:00Z",
    user: USER,
  };
}

const FIRST = sessionWith("access-1", "refresh-1");
const SECOND = sessionWith("access-2", "refresh-2");

/** The one seam replaced above, typed as the mock it is. */
const refresh = vi.mocked(auth.refresh);

/** A refusal shaped the way `request()` builds one. */
function refused(): ApiError {
  return new ApiError(401, "unauthorized", "Invalid or expired refresh token");
}

beforeEach(() => {
  setSession(null);
  refresh.mockReset();
});

describe("the session store", () => {
  it("starts signed out, with nobody to notify", () => {
    expect(getSession()).toBeNull();
  });

  it("replaces the session wholesale rather than merging into it", () => {
    setSession(FIRST);
    setSession(SECOND);
    expect(getSession()).toBe(SECOND);
  });

  it("treats null as a sign-out", () => {
    setSession(FIRST);
    setSession(null);
    expect(getSession()).toBeNull();
  });

  it("notifies each subscriber once per change, and stops on unsubscribe", () => {
    const listener = vi.fn();
    const off = subscribe(listener);

    setSession(FIRST);
    expect(listener).toHaveBeenCalledTimes(1);

    off();
    setSession(SECOND);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("never lets the server snapshot claim the live session", () => {
    // Hydration compares the server's render with the first client render; a
    // server that reported the token would mismatch the markup it just sent.
    setSession(FIRST);
    expect(getServerSession()).toBeNull();
    expect(getSession()).toBe(FIRST);
  });
});

describe("refreshSession", () => {
  it("asks nobody when signed out", async () => {
    await expect(refreshSession()).resolves.toBeNull();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("redeems the current refresh token and stores its successor", async () => {
    setSession(FIRST);
    refresh.mockResolvedValue(SECOND);

    await expect(refreshSession()).resolves.toBe(SECOND);
    expect(refresh).toHaveBeenCalledWith("refresh-1");
    expect(getSession()).toBe(SECOND);
  });

  it("shares one redemption between concurrent callers", async () => {
    // The whole point: two 401s that each redeemed would spend the token twice
    // and sign the user out of every session in the family.
    setSession(FIRST);
    refresh.mockResolvedValue(SECOND);

    const [first, second] = await Promise.all([refreshSession(), refreshSession()]);

    expect(refresh).toHaveBeenCalledTimes(1);
    expect(first).toBe(SECOND);
    expect(second).toBe(SECOND);
  });

  it("starts a fresh redemption once the previous one has settled", async () => {
    setSession(SECOND);
    refresh.mockResolvedValue(FIRST);

    await refreshSession();
    await refreshSession();

    expect(refresh).toHaveBeenCalledTimes(2);
  });

  it("ends the session when the server refuses the rotation", async () => {
    setSession(FIRST);
    refresh.mockRejectedValue(refused());

    await expect(refreshSession()).resolves.toBeNull();
    expect(getSession()).toBeNull();
  });

  it("tells subscribers about that sign-out, not only about successes", async () => {
    setSession(FIRST);
    const listener = vi.fn();
    const off = subscribe(listener);
    refresh.mockRejectedValue(refused());

    await refreshSession();

    expect(listener).toHaveBeenCalledTimes(1);
    off();
  });
});

describe("authorized", () => {
  it("refuses to call anything when signed out, and says so as a 401", async () => {
    const call = vi.fn();

    await expect(authorized(call)).rejects.toMatchObject({ status: 401 });
    expect(call).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });

  it("hands the current access token to a call that succeeds first time", async () => {
    setSession(FIRST);
    const call = vi.fn().mockResolvedValue("ok");

    await expect(authorized(call)).resolves.toBe("ok");
    expect(call).toHaveBeenCalledWith("access-1");
    expect(refresh).not.toHaveBeenCalled();
  });

  it("rotates once, then retries with the token it was given", async () => {
    setSession(FIRST);
    refresh.mockResolvedValue(SECOND);
    const call = vi.fn().mockRejectedValueOnce(refused()).mockResolvedValueOnce("ok");

    await expect(authorized(call)).resolves.toBe("ok");

    expect(call).toHaveBeenNthCalledWith(1, "access-1");
    expect(call).toHaveBeenNthCalledWith(2, "access-2");
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("surfaces the original refusal when the rotation fails", async () => {
    setSession(FIRST);
    refresh.mockRejectedValue(refused());
    const call = vi.fn().mockRejectedValue(refused());

    await expect(authorized(call)).rejects.toMatchObject({ status: 401 });
    // No second attempt: the session is over, so a retry could only be a
    // slower way to reach the same refusal.
    expect(call).toHaveBeenCalledTimes(1);
    expect(getSession()).toBeNull();
  });

  it("retries once and not twice when the retry is refused too", async () => {
    setSession(FIRST);
    refresh.mockResolvedValue(SECOND);
    const call = vi.fn().mockRejectedValue(refused());

    await expect(authorized(call)).rejects.toMatchObject({ status: 401 });
    expect(call).toHaveBeenCalledTimes(2);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("passes a non-401 ApiError straight through, keeping the session", async () => {
    setSession(FIRST);
    const call = vi.fn().mockRejectedValue(new ApiError(500, "server_error", "boom"));

    await expect(authorized(call)).rejects.toMatchObject({ status: 500 });
    expect(refresh).not.toHaveBeenCalled();
    expect(getSession()).toBe(FIRST);
  });

  it("passes an error that is not an ApiError through untouched", async () => {
    setSession(FIRST);
    const boom = new Error("not from the API");
    const call = vi.fn().mockRejectedValue(boom);

    await expect(authorized(call)).rejects.toBe(boom);
    expect(refresh).not.toHaveBeenCalled();
  });
});
