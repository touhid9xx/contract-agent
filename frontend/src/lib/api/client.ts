import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

// Navigate to a relative URL. Unlike next/navigation's router, we
// intentionally want a FULL page reload here (see the 401 handler below).
// This is an exception to @next/next/no-location-assign-relative-destination.
function hardNavigateToLogin(): void {
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.href = "/en/login";
}

export const api: AxiosInstance = axios.create({
  baseURL: API_URL,
  timeout: 15_000,
  headers: { "Content-Type": "application/json" },
});

// ---------- Request interceptor: attach Bearer token ----------
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  if (typeof window !== "undefined") {
    const token = window.localStorage.getItem("access_token");
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

// ---------- Response interceptor: 401 → refresh → retry (stub) ----------
let isRefreshing = false;

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const original = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean;
    };

    // M6 will implement real refresh rotation
    if (error.response?.status === 401 && !original._retry && !isRefreshing) {
      original._retry = true;
      isRefreshing = true;
      try {
        // TODO(M6): POST /api/v1/auth/refresh with refresh_token cookie
        // const { data } = await axios.post(
        //   `${API_URL}/api/v1/auth/refresh`,
        //   {},
        //   { withCredentials: true },
        // );
        // window.localStorage.setItem("access_token", data.access_token);
        // return api(original);
        throw new Error("Refresh not implemented until Milestone 6");
      } catch {
        if (typeof window !== "undefined") {
          // Clear all auth state. A hard reload is INTENTIONAL here:
          // - React Query cache reset
          // - Zustand auth store reset
          // - No stale in-memory state carried across sessions
          // Using useRouter().push() would preserve stale state — wrong for auth expiry.
          window.localStorage.removeItem("access_token");
          hardNavigateToLogin();
        }
        return Promise.reject(error);
      } finally {
        isRefreshing = false;
      }
    }

    return Promise.reject(error);
  },
);
