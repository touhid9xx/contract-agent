/**
 * Axios API client with automatic JWT refresh.
 *
 * Interceptors:
 *   - Request: attach Bearer from auth store
 *   - Response: on 401, try refresh once, then retry original request
 *
 * Refresh rotation: backend issues a NEW refresh token on each refresh
 * (M5). We persist the new pair before retrying.
 *
 * Session cookie: on unrecoverable 401 (refresh failed or missing), we
 * clear the `ca_session` cookie so Next.js middleware stops letting the
 * user into /dashboard/*.
 */
import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const API_PREFIX = "/api/v1";

export const api: AxiosInstance = axios.create({
  baseURL: `${API_URL}${API_PREFIX}`,
  timeout: 15_000,
  headers: { "Content-Type": "application/json" },
});

// ============================================================
// SESSION COOKIE HELPERS
// ============================================================
/**
 * Clear the session presence cookie.
 *
 * Duplicated (small) from auth-store.ts to avoid a circular import:
 *   auth-store → api-client (imports `api`)
 *   api-client → auth-store (would import setSessionCookie)
 *
 * If the cookie name or attributes change, update BOTH places.
 * (Alternative: extract into `lib/session-cookie.ts` — deferred to M6.)
 */
function clearSessionCookie(): void {
  if (typeof document === "undefined") return;
  document.cookie = "ca_session=; path=/; max-age=0; SameSite=Lax";
}

// ============================================================
// TOKEN STORAGE — accessed via callback to avoid circular import
// ============================================================
type TokenGetter = () => { access: string | null; refresh: string | null };
type TokenSetter = (access: string, refresh: string) => void;
type TokenClearer = () => void;

let getTokens: TokenGetter = () => ({ access: null, refresh: null });
let setTokens: TokenSetter = () => {};
let clearTokens: TokenClearer = () => {};

export function configureAuthBridge(opts: {
  get: TokenGetter;
  set: TokenSetter;
  clear: TokenClearer;
}): void {
  getTokens = opts.get;
  setTokens = opts.set;
  clearTokens = opts.clear;
}

// ============================================================
// REQUEST INTERCEPTOR — attach Bearer
// ============================================================
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const { access } = getTokens();
  if (access) {
    config.headers.Authorization = `Bearer ${access}`;
  }
  return config;
});

// ============================================================
// RESPONSE INTERCEPTOR — 401 → refresh → retry (single-flight)
// ============================================================
let refreshPromise: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  const { refresh } = getTokens();
  if (!refresh) {
    throw new Error("No refresh token available");
  }

  // Use bare axios (not `api`) to avoid interceptor recursion
  const response = await axios.post(
    `${API_URL}${API_PREFIX}/auth/refresh`,
    { refresh_token: refresh },
    { headers: { "Content-Type": "application/json" }, timeout: 15_000 },
  );

  const { access_token, refresh_token } = response.data as {
    access_token: string;
    refresh_token: string;
  };
  setTokens(access_token, refresh_token);
  return access_token;
}

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as
      (InternalAxiosRequestConfig & { _retry?: boolean }) | undefined;

    const status = error.response?.status;
    const isRefreshCall = original?.url?.includes("/auth/refresh");
    const isLoginCall = original?.url?.includes("/auth/login");
    const isRegisterCall = original?.url?.includes("/auth/register");

    // Only retry on 401, once, and never for auth endpoints themselves
    if (
      status === 401 &&
      original &&
      !original._retry &&
      !isRefreshCall &&
      !isLoginCall &&
      !isRegisterCall
    ) {
      original._retry = true;
      try {
        // Single-flight: concurrent 401s share one refresh call
        if (!refreshPromise) {
          refreshPromise = refreshAccessToken().finally(() => {
            refreshPromise = null;
          });
        }
        const newAccess = await refreshPromise;
        original.headers.Authorization = `Bearer ${newAccess}`;
        return api(original);
      } catch {
        // Refresh failed → wipe everything (tokens + cookie) → redirect to login
        clearTokens();
        clearSessionCookie();
        if (typeof window !== "undefined") {
          const locale = window.location.pathname.split("/")[1] || "en";
          // eslint-disable-next-line @next/next/no-location-assign-relative-destination
          window.location.href = `/${locale}/login`;
        }
        return Promise.reject(error);
      }
    }

    return Promise.reject(error);
  },
);

// ============================================================
// ERROR NORMALIZATION
// ============================================================
export interface ApiError {
  status: number;
  code: string;
  detail: unknown;
  requestId?: string;
}

export function toApiError(err: unknown): ApiError {
  if (axios.isAxiosError(err)) {
    const body = err.response?.data as
      { error?: string; status?: number; detail?: unknown; request_id?: string } | undefined;
    return {
      status: err.response?.status ?? 0,
      code: body?.error ?? "network_error",
      detail: body?.detail ?? err.message,
      requestId: body?.request_id,
    };
  }
  return {
    status: 0,
    code: "unknown_error",
    detail: String(err),
  };
}
