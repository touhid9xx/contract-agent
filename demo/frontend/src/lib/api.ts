const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

export class ApiError extends Error {
  status: number;
  code: string;
  current?: unknown;

  constructor(status: number, code: string, message: string, current?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.current = current;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "The mocked demo API is not reachable. Is the demo backend running?");
  }

  if (!response.ok) {
    let code = "ERROR";
    let message = `Request failed with status ${response.status}.`;
    let current: unknown;
    try {
      const body = await response.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        current = body.error.current;
      }
    } catch {
      // keep the fallback message
    }
    throw new ApiError(response.status, code, message, current);
  }

  return (await response.json()) as T;
}

export const api = {
  me: () => request<import("./types").CurrentUser>("/me"),
  dashboard: () => request<import("./types").DashboardData>("/dashboard"),
  contracts: (params: Record<string, string | number | undefined>) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== "" && value !== "all") search.set(key, String(value));
    });
    return request<import("./types").ContractListResponse>(`/contracts?${search.toString()}`);
  },
  contract: (id: number | string) => request<import("./types").ContractDetail>(`/contracts/${id}`),
  addNote: (id: number | string, text: string) =>
    request<{ event: import("./types").TimelineEvent }>(`/contracts/${id}/note`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  escalate: (id: number | string) =>
    request<{ escalation: import("./types").Escalation; created: boolean }>(`/contracts/${id}/escalate`, {
      method: "POST",
    }),
  markResponded: (id: number | string) =>
    request<{ contract: import("./types").Contract }>(`/contracts/${id}/mark-responded`, {
      method: "POST",
      body: JSON.stringify({ response_type: "manual", note: "recorded from demo UI" }),
    }),
  reviewQueue: () => request<import("./types").ReviewQueueResponse>("/review/queue"),
  reviewField: (
    fieldId: string,
    body: { action: "accept" | "reject" | "mark_unreadable" | "edit"; value?: string; version: number },
  ) =>
    request<{ item: import("./types").ReviewItem; contract: import("./types").Contract }>(`/review/${fieldId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  decision: (contractId: number) =>
    request<{ decision: import("./types").AgentDecision | null; contract: import("./types").Contract }>(
      `/agent/decisions?contract_id=${contractId}`,
    ),
  rerunAgent: (contractId: number) =>
    request<{ decision: import("./types").AgentDecision }>(`/agent/decisions/${contractId}/rerun`, { method: "POST" }),
  overrideChannel: (contractId: number, channel: import("./types").Channel, version: number) =>
    request<{ decision: import("./types").AgentDecision }>(`/agent/decisions/${contractId}/override`, {
      method: "POST",
      body: JSON.stringify({ channel, version }),
    }),
  flagDecision: (contractId: number, version: number) =>
    request<{ decision: import("./types").AgentDecision }>(`/agent/decisions/${contractId}/flag-review`, {
      method: "POST",
      body: JSON.stringify({ version }),
    }),
  notifications: (params: Record<string, string | undefined> = {}) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value && value !== "all") search.set(key, value);
    });
    return request<{ items: import("./types").Notification[]; count: number }>(`/notifications?${search.toString()}`);
  },
  retryNotification: (id: string) =>
    request<{ notification: import("./types").Notification }>(`/notifications/${id}/retry`, { method: "POST" }),
  escalations: () => request<import("./types").EscalationResponse>("/escalations"),
  assignEscalation: (id: number, assignee: string) =>
    request<{ escalation: import("./types").Escalation }>(`/escalations/${id}/assign`, {
      method: "POST",
      body: JSON.stringify({ assignee }),
    }),
  resolveEscalation: (id: number, resolution = "resolved from demo UI") =>
    request<{ escalation: import("./types").Escalation }>(`/escalations/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({ resolution }),
    }),
};
