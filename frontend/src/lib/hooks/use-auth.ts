"use client";

import { useCallback } from "react";
import { useRouter } from "@/i18n/navigation";
import { useAuthStore } from "@/lib/stores/auth-store";

export function useAuth() {
  const router = useRouter();
  const user = useAuthStore((s) => s.user);
  const hydrated = useAuthStore((s) => s.hydrated);
  const login = useAuthStore((s) => s.login);
  const register = useAuthStore((s) => s.register);
  const logoutStore = useAuthStore((s) => s.logout);

  const isAuthenticated = !!user;

  const doLogin = useCallback(
    async (email: string, password: string) => {
      await login(email, password);
      router.push("/dashboard");
    },
    [login, router],
  );

  const doRegister = useCallback(
    async (input: { email: string; password: string; fullName?: string }) => {
      await register(input);
      router.push("/dashboard");
    },
    [register, router],
  );

  const doLogout = useCallback(async () => {
    await logoutStore();
    router.push("/login");
  }, [logoutStore, router]);

  return {
    user,
    hydrated,
    isAuthenticated,
    login: doLogin,
    register: doRegister,
    logout: doLogout,
  };
}
