import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/lib/stores/auth-store";

// Mock the api client module
vi.mock("@/lib/api/client", () => ({
  api: {
    post: vi.fn(),
    get: vi.fn(),
  },
  configureAuthBridge: vi.fn(),
}));

import { api } from "@/lib/api/client";

describe("auth store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      user: null,
      access: null,
      refresh: null,
      hydrated: true,
    });
  });

  it("setTokens updates access + refresh", () => {
    useAuthStore.getState().setTokens("a1", "r1");
    const s = useAuthStore.getState();
    expect(s.access).toBe("a1");
    expect(s.refresh).toBe("r1");
  });

  it("clear wipes all state", () => {
    useAuthStore.getState().setTokens("a1", "r1");
    useAuthStore.getState().clear();
    const s = useAuthStore.getState();
    expect(s.access).toBeNull();
    expect(s.refresh).toBeNull();
    expect(s.user).toBeNull();
  });

  it("login sets tokens + fetches user", async () => {
    (api.post as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: { access_token: "a1", refresh_token: "r1" },
    });
    (api.get as ReturnType<typeof vi.fn>).mockResolvedValueOnce({
      data: {
        id: "u1",
        tenant_id: "t1",
        email: "a@b.com",
        role: "admin",
        is_active: true,
        full_name: "Alice",
        created_at: "2025-01-01T00:00:00Z",
        updated_at: "2025-01-01T00:00:00Z",
      },
    });

    await useAuthStore.getState().login("a@b.com", "Sup3rSecret!Pass");
    const s = useAuthStore.getState();
    expect(s.access).toBe("a1");
    expect(s.user?.email).toBe("a@b.com");
  });

  it("logout clears state even if API fails", async () => {
    (api.post as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new Error("network"));
    useAuthStore.getState().setTokens("a1", "r1");
    await useAuthStore.getState().logout();
    expect(useAuthStore.getState().access).toBeNull();
  });
});
