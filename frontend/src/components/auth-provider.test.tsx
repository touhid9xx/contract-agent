// @vitest-environment jsdom
/**
 * The React binding on the session store, pinned where a compiler cannot reach.
 *
 * `session.test.ts` proves the store is correct; it cannot prove that a
 * component *sees* it. `auth-provider.tsx` is the only place React reads those
 * tokens, and it does so through `useSyncExternalStore` rather than `useState`
 * on purpose — one copy of the truth, read by an in-flight request without
 * waiting for a render. Every claim below is a rule the source states in a
 * comment and nothing checked:
 *
 *   * nothing is stored until the server has accepted the credentials, so a
 *     refused sign-in cannot destroy a session that was already working,
 *   * signing out clears the local session *before* the server answers — "Sign
 *     out" must not look broken because the API is down,
 *   * a refused revocation still leaves the user signed out, not half-signed-in,
 *   * `signOutEverywhere` clears the session in a `finally`, because the epoch
 *     bump has already invalidated every token whatever the response said,
 *   * a session replaced anywhere in the app re-renders the tree — the whole
 *     point of subscribing to a store instead of copying it into state.
 *
 * Only `auth` is replaced, and `importOriginal` keeps the real `ApiError`, since
 * `authorized` branches on `instanceof` and a hand-rolled double could not
 * satisfy that honestly. The session module itself is the real one.
 */
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    auth: {
      ...actual.auth,
      login: vi.fn(),
      logout: vi.fn(),
      logoutAll: vi.fn(),
      refresh: vi.fn(),
    },
  };
});

import { AuthProvider, useAuth } from "@/components/auth-provider";
import { ApiError, auth } from "@/lib/api";
import type { LoginCredentials, Session, UserSummary } from "@/lib/api";
import { getSession, setSession } from "@/lib/session";

const USER: UserSummary = {
  id: "u-1",
  email: "operator@example.test",
  role: "operator",
  tenant_id: "t-1",
  tenant_slug: "acme",
  is_active: true,
};

function sessionWith(accessToken: string, refreshToken: string): Session {
  return {
    access_token: accessToken,
    token_type: "bearer",
    expires_in: 900,
    expires_at: "2026-11-04T12:15:00Z",
    refresh_token: refreshToken,
    refresh_expires_at: "2026-12-04T12:00:00Z",
    user: USER,
  };
}

const SESSION = sessionWith("access-1", "refresh-1");

const CREDENTIALS: LoginCredentials = {
  tenant: "acme",
  email: "operator@example.test",
  password: "correct horse battery staple",
};

/** A promise the test releases by hand, so "before the server answers" is testable. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function wrapper({ children }: { children: ReactNode }) {
  return <AuthProvider>{children}</AuthProvider>;
}

beforeEach(() => {
  setSession(null);
  vi.mocked(auth.login).mockReset();
  vi.mocked(auth.logout).mockReset();
  vi.mocked(auth.logoutAll).mockReset();
  vi.mocked(auth.refresh).mockReset();
});

afterEach(() => {
  cleanup();
  setSession(null);
});

describe("useAuth outside the provider", () => {
  it("throws instead of handing back a null session", () => {
    // A silent `null` would let a screen render as signed-out forever with no
    // hint that the provider was never mounted.
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      expect(() => renderHook(() => useAuth())).toThrow(
        /useAuth must be used inside <AuthProvider>/,
      );
    } finally {
      consoleError.mockRestore();
    }
  });
});

describe("with no session", () => {
  it("reports signed out and no user", () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    expect(result.current.user).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });
});

describe("signIn", () => {
  it("stores nothing while the server is still deciding", async () => {
    const server = deferred<Session>();
    vi.mocked(auth.login).mockReturnValue(server.promise);

    const { result } = renderHook(() => useAuth(), { wrapper });

    let outcome!: Promise<UserSummary>;
    act(() => {
      outcome = result.current.signIn(CREDENTIALS);
    });

    expect(getSession()).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);

    server.resolve(SESSION);
    await act(async () => {
      await outcome;
    });

    expect(getSession()).toEqual(SESSION);
    expect(result.current.isAuthenticated).toBe(true);
  });

  it("stores the session and reports the user it belongs to", async () => {
    vi.mocked(auth.login).mockResolvedValue(SESSION);
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.signIn(CREDENTIALS);
    });

    expect(auth.login).toHaveBeenCalledWith(CREDENTIALS);
    expect(result.current.user).toEqual(USER);
    expect(result.current.isAuthenticated).toBe(true);
  });

  it("leaves a working session untouched when the server refuses", async () => {
    setSession(sessionWith("access-old", "refresh-old"));
    vi.mocked(auth.login).mockRejectedValue(
      new ApiError(401, "unauthorized", "Invalid tenant, email or password."),
    );

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await expect(result.current.signIn(CREDENTIALS)).rejects.toBeInstanceOf(ApiError);
    });

    // The pre-existing session is still the truth: a typo in the sign-in field
    // must not sign out a user who was already signed in.
    expect(getSession()?.access_token).toBe("access-old");
    expect(result.current.isAuthenticated).toBe(true);
  });
});

describe("the tree follows the store", () => {
  it("re-renders when a session is set outside the provider", () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    expect(result.current.isAuthenticated).toBe(false);

    act(() => {
      setSession(SESSION);
    });

    expect(result.current.user).toEqual(USER);
    expect(result.current.isAuthenticated).toBe(true);
  });
});

describe("signOut", () => {
  it("clears the local session before the server answers", async () => {
    const server = deferred<void>();
    vi.mocked(auth.logout).mockReturnValue(server.promise);
    setSession(SESSION);

    const { result } = renderHook(() => useAuth(), { wrapper });

    let outcome!: Promise<void>;
    act(() => {
      outcome = result.current.signOut();
    });

    // The important half of "Sign out must never depend on the server": the UI
    // is already signed out while the revocation is still in flight.
    expect(getSession()).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
    expect(auth.logout).toHaveBeenCalledWith("refresh-1");

    server.resolve();
    await act(async () => {
      await outcome;
    });
  });

  it("never calls the server when there is no session to revoke", async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.signOut();
    });

    expect(auth.logout).not.toHaveBeenCalled();
    expect(getSession()).toBeNull();
  });

  it("stays signed out when the revocation is refused", async () => {
    vi.mocked(auth.logout).mockRejectedValue(
      new ApiError(401, "unauthorized", "Refresh token is unknown."),
    );
    setSession(SESSION);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.signOut();
    });

    // Swallowed on purpose: the local session is gone and the token expires on
    // its own, so this is not something the user could act on.
    expect(getSession()).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });
});

describe("signOutEverywhere", () => {
  it("hands the access token to the server and ends the session", async () => {
    vi.mocked(auth.logoutAll).mockResolvedValue(undefined);
    setSession(SESSION);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await result.current.signOutEverywhere();
    });

    expect(auth.logoutAll).toHaveBeenCalledWith("access-1");
    expect(getSession()).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("ends the session even when the call fails", async () => {
    vi.mocked(auth.logoutAll).mockRejectedValue(
      new ApiError(403, "forbidden", "You do not have permission."),
    );
    setSession(SESSION);

    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await expect(result.current.signOutEverywhere()).rejects.toBeInstanceOf(ApiError);
    });

    // The epoch bump on the server has already invalidated every token,
    // including these, whatever happened to the response.
    expect(getSession()).toBeNull();
    expect(result.current.isAuthenticated).toBe(false);
  });

  it("refuses to call the server when nobody is signed in", async () => {
    const { result } = renderHook(() => useAuth(), { wrapper });

    await act(async () => {
      await expect(result.current.signOutEverywhere()).rejects.toBeInstanceOf(ApiError);
    });

    expect(auth.logoutAll).not.toHaveBeenCalled();
  });
});
