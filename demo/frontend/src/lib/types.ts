export type Channel = "email" | "sms" | "voice";
export type ContractStatus = "active" | "reminder_sent" | "responded" | "renewed" | "expired" | "escalated" | "review";
export type RiskLevel = "low" | "medium" | "high";

export interface Contract {
  id: number;
  customer_name: string;
  tenant_id: string;
  end_date: string;
  days_to_expiry: number;
  status: ContractStatus;
  channel: Channel | null;
  risk: RiskLevel;
  confidence: number;
  uploaded_at: string;
  contract_value_bucket: string;
  pipeline_blocked: boolean;
  values_redacted: {
    email: string;
    phone: string;
    dob: string;
    address: string;
  };
}

export interface ContractListResponse {
  items: Contract[];
  total: number;
  page: number;
  page_size: number;
  page_count: number;
  mocked: boolean;
  note?: string;
}

export interface ExtractedField {
  id: string;
  contract_id: number;
  field: string;
  value: string;
  confidence: number;
  source: string;
  status: "approved" | "pending_review" | "rejected" | "unreadable";
  version: number;
  conflict?: string;
  reviewed_by?: string;
  reviewed_at?: string;
  original_value?: string;
}

export interface ReviewItem extends ExtractedField {
  customer_name: string;
  end_date: string;
  days_to_expiry: number;
  contract_status: ContractStatus;
  pipeline_blocked: boolean;
  age_minutes: number;
  threshold: number;
}

export interface ReviewQueueResponse {
  items: ReviewItem[];
  count: number;
  oldest_minutes: number;
  threshold: number;
  mocked: boolean;
}

/**
 * Mirrors `AgentDecisionView` in `backend/app/api/v1/agent.py` field for field.
 *
 * That list is deliberately narrower than the `agent_decisions` table: the table
 * also carries model-provenance columns (temperature, token counts, latency,
 * schema repairs, mlflow run id), and the API does not expose them. Keeping this
 * type on the wire shape rather than the row shape is what lets this screen
 * survive the Phase-1 swap to the real backend.
 */
export interface AgentDecision {
  id: number;
  contract_id: number;
  correlation_id: string;
  channel: Channel;
  confidence: number;
  reasoning: string;
  needs_review: boolean;
  prompt_version: string;
  llm_model: string;
  created_at: string;
  /** Send back on override / flag-review; a stale value is a 409. */
  version: number;
  /** The channel the *policy* chose, once a person overrode it. Set once, never rewritten. */
  original_channel: Channel | null;
  overridden_by: string | null;
  /** How many recommendations already existed for this contract when this row was appended. */
  rerun_count: number;
}

export interface TimelineEvent {
  id: string;
  contract_id: number;
  at: string;
  type: string;
  title: string;
  summary: string;
  payload: Record<string, unknown>;
}

export interface Notification {
  id: string;
  contract_id: number;
  customer_name: string;
  channel: Channel;
  status: "sent" | "failed" | "answered" | "machine" | "delivered";
  sent_at: string;
  template: string;
  idempotency_key: string;
  retry_count: number;
  retry_max: number;
}

export interface Escalation {
  id: number;
  contract_id: number;
  customer_name: string;
  raised_at: string;
  sla_deadline: string;
  status: "open" | "in_progress" | "resolved" | "sla_breached";
  assigned_to: string | null;
  resolved_at?: string;
  resolved_by?: string;
  resolution?: string;
  last_activity: string;
  note: string;
}

export interface EscalationResponse {
  breached: Escalation[];
  open: Escalation[];
  resolved: Escalation[];
  sla_hours: number;
  mocked: boolean;
}

export interface ContractDetail {
  contract: Contract;
  fields: ExtractedField[];
  agent_decision: AgentDecision | null;
  timeline: TimelineEvent[];
  notifications: Notification[];
  review_items: ReviewItem[];
  escalation: Escalation | null;
  mocked: boolean;
}

export interface DashboardData {
  today: string;
  counts: {
    active_contracts: number;
    expiring_in_30_days: number;
    awaiting_review: number;
    escalations: number;
  };
  reminder_windows: { milestone: string; contracts: number; capacity: number }[];
  recent_activity: {
    id: string;
    at: string;
    kind: string;
    tone: "info" | "warn";
    contract_id: number;
    text: string;
    detail: string;
  }[];
  channel_mix: { channel: Channel; percent: number }[];
  mocked: boolean;
}

export interface CurrentUser {
  name: string;
  display_name: string;
  role: "admin" | "operator" | "viewer";
  tenant_id: string;
  tenant_name: string;
  mocked: boolean;
}
