/**
 * Auth state — tokens, user, hydration.
 *
 * Why Zustand?
 *   - Tiny (~1KB), no providers
 *   - Selector-based re-renders
 *   - persist middleware for localStorage
 *
 * Why not store server data here?
 *   - Contract list, notifications, etc. go through TanStack Query
 *   - This store owns ONLY auth: {user, access, refresh, hydrated}
 *
 * Why a session cookie?
 *   - localStorage is invisible to Next.js middleware (server-side)
 *   - A non-httpOnly cookie `ca_session=1` acts as a presence flag
 *   - Middleware reads it to redirect unauthenticated users away from
 *     /dashboard/* without needing access to localStorage
 *   - Real auth check still happens server-side on the API (JWT validation)
 */
"use client";

import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import { api, configureAuthBridge } from "@/lib/api/client";
import type { User } from "@/lib/schemas/auth";
import { userSchema } from "@/lib/schemas/auth";

// ============================================================
// SESSION COOKIE BRIDGE
// ============================================================
/**
 * Set or clear a non-sensitive presence cookie.
 *
 * NOT httpOnly (by design — JS must set/clear it).
 * NOT the auth itself — the JWT lives in localStorage.
 * Purpose: let Next.js middleware (server-side) detect "is user logged in?"
 *          so it can redirect /dashboard/* → /login when appropriate.
 *
 * Security note: cookie is only a hint. Middleware does NOT trust it for
 * authorization. Real authorization = server-side JWT validation on the API.
 */
function setSessionCookie(on: boolean): void {
  if (typeof document === "undefined") return;
  if (on) {
    // Non-httpOnly hint cookie — expires with refresh (7 days)
    document.cookie = `ca_session=1; path=/; max-age=${7 * 24 * 3600}; SameSite=Lax`;
  } else {
    document.cookie = "ca_session=; path=/; max-age=0; SameSite=Lax";
  }
}

interface AuthState {
  // --- State ---
  user: User | null;
  access: string | null;
  refresh: string | null;
  hydrated: boolean;

  // --- Actions ---
  setTokens: (access: string, refresh: string) => void;
  setUser: (user: User | null) => void;
  clear: () => void;
  setHydrated: () => void;

  // --- Async ---
  login: (email: string, password: string) => Promise<void>;
  register: (input: { email: string; password: string; fullName?: string }) => Promise<void>;
  fetchMe: () => Promise<User>;
  logout: () => Promise<void>;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      access: null,
      refresh: null,
      hydrated: false,

      setTokens: (access, refresh) => {
        set({ access, refresh });
        setSessionCookie(true);
      },

      setUser: (user) => set({ user }),

      clear: () => {
        set({ user: null, access: null, refresh: null });
        setSessionCookie(false);
      },

      setHydrated: () => set({ hydrated: true }),

      async login(email, password) {
        const { data } = await api.post("/auth/login", { email, password });
        set({
          access: data.access_token,
          refresh: data.refresh_token,
        });
        setSessionCookie(true);
        await get().fetchMe();
      },

      async register({ email, password, fullName }) {
        const { data } = await api.post("/auth/register", {
          email,
          password,
          full_name: fullName || null,
        });
        set({
          access: data.access_token,
          refresh: data.refresh_token,
        });
        setSessionCookie(true);
        await get().fetchMe();
      },

      async fetchMe() {
        const { data } = await api.get("/auth/me");
        const parsed = userSchema.parse(data);
        set({ user: parsed });
        return parsed;
      },

      async logout() {
        try {
          await api.post("/auth/logout");
        } catch {
          // Ignore — we clear locally regardless
        }
        set({ user: null, access: null, refresh: null });
        setSessionCookie(false);
      },
    }),
    {
      name: "contract-agent-auth",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        access: state.access,
        refresh: state.refresh,
        user: state.user,
      }),
      onRehydrateStorage: () => (state) => {
        state?.setHydrated();
        // On rehydrate, if we have an access token in storage, ensure the
        // session cookie is present too. (Covers the case where user closed
        // the browser but localStorage persisted.)
        if (state?.access) {
          setSessionCookie(true);
        }
      },
    },
  ),
);

// ============================================================
// BRIDGE — connect store ↔ axios client (avoids circular imports)
// ============================================================
configureAuthBridge({
  get: () => {
    const { access, refresh } = useAuthStore.getState();
    return { access, refresh };
  },
  set: (access, refresh) => useAuthStore.getState().setTokens(access, refresh),
  clear: () => useAuthStore.getState().clear(),
});
