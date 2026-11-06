"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { auth } from "@/lib/api";
import type { LoginCredentials, UserSummary } from "@/lib/api";
import { authorized, getServerSession, getSession, setSession, subscribe } from "@/lib/session";

/** What `<AuthProvider>` hands to the tree. */
export interface AuthContextValue {
  /** The signed-in account, or `null`. */
  user: UserSummary | null;
  /** True once a session exists. */
  isAuthenticated: boolean;
  /** Exchange credentials for a session. Throws `ApiError` on refusal. */
  signIn: (credentials: LoginCredentials) => Promise<UserSummary>;
  /** End this session and forget it locally. */
  signOut: () => Promise<void>;
  /** End every session of the account — the remedy for a suspected leak. */
  signOutEverywhere: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Session state for the tree, backed by the module-level store.
 *
 * The tokens are *not* in React state. `useSyncExternalStore` reads the store
 * directly, which matters for two reasons: an in-flight request can read the
 * current token without waiting for a render, and there is one copy of the
 * truth rather than a component copy that can drift from it.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const session = useSyncExternalStore(subscribe, getSession, getServerSession);

  const signIn = useCallback(async (credentials: LoginCredentials) => {
    // Nothing is stored until the server has accepted the credentials, so a
    // failed attempt leaves an existing session (if any) untouched.
    const next = await auth.login(credentials);
    setSession(next);
    return next.user;
  }, []);

  const signOut = useCallback(async () => {
    // Read the token before clearing: signing out of the UI must never depend on
    // the server answering, and the token is needed to tell the server.
    const refreshToken = getSession()?.refresh_token;
    setSession(null);

    if (!refreshToken) {
      return;
    }

    try {
      await auth.logout(refreshToken);
    } catch {
      // The local session is already gone and the token expires on its own, so a
      // failed revocation is not something the user can act on. Swallowing it
      // keeps "Sign out" from ever appearing broken.
    }
  }, []);

  const signOutEverywhere = useCallback(async () => {
    try {
      // `authorized` rotates first if the access token has expired — the moment
      // a user most wants this button is the moment they suspect the session is
      // compromised, so an expired token must not block it.
      await authorized((accessToken) => auth.logoutAll(accessToken));
    } finally {
      // The epoch bump on the server has already invalidated every token,
      // including these, whatever happened to the response.
      setSession(null);
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user: session?.user ?? null,
      isAuthenticated: session !== null,
      signIn,
      signOut,
      signOutEverywhere,
    }),
    [session, signIn, signOut, signOutEverywhere],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Read the session. Throws if used outside `<AuthProvider>`, which is a bug. */
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth must be used inside <AuthProvider>");
  }
  return value;
}
