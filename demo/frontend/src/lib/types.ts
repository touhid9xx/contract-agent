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

export interface AgentDecision {
  contract_id: number;
  channel: Channel;
  confidence: number;
  reasoning: string;
  needs_review: boolean;
  prompt_version: string;
  llm_model: string;
  temperature: number;
  tokens_in: number;
  tokens_out: number;
  latency_ms: number;
  schema_repairs: number;
  mlflow_run_id: string;
  injection_tags_present: boolean;
  created_at: string;
  input_signals: Record<string, string | number | boolean>;
  original_channel?: Channel;
  overridden_by?: string;
  rerun_count?: number;
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
