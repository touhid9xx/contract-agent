import axios, {
  type AxiosError,
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from "axios";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

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
    const original = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    // M6 will implement real refresh rotation
    if (error.response?.status === 401 && !original._retry && !isRefreshing) {
      original._retry = true;
      isRefreshing = true;
      try {
        // TODO(M6): POST /api/v1/auth/refresh with refresh_token cookie
        // const { data } = await axios.post(`${API_URL}/api/v1/auth/refresh`, {}, { withCredentials: true })
        // window.localStorage.setItem("access_token", data.access_token)
        // return api(original)
        throw new Error("Refresh not implemented until Milestone 6");
      } catch {
        if (typeof window !== "undefined") {
          window.localStorage.removeItem("access_token");
          window.location.href = "/en/login";
        }
        return Promise.reject(error);
      } finally {
        isRefreshing = false;
      }
    }

    return Promise.reject(error);
  },
);
