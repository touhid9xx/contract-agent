/**
 * The one place the browser talks HTTP.
 *
 * Every wire shape the UI consumes is declared here and every endpoint is a
 * typed function, so a route rename becomes a compile error instead of a 404 at
 * run time. Components never call `fetch` themselves — the error envelope is
 * parsed once, in this module, and surfaces as {@link ApiError}.
 *
 * The base path is relative on purpose. `next.config.ts` rewrites `/api/*` to
 * the FastAPI process, so the browser stays on a single origin: no CORS
 * preflight, and a first-party httpOnly cookie stays possible later without
 * changing this module (see D10-19).
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "/api/v1";

/** Message shown when the browser cannot reach the API at all. */
const UNREACHABLE =
  "The API is not reachable. Check that the backend is running, then try again.";

/** Wire values of `app.db.enums.UserRole`. */
export type UserRole = "admin" | "operator" | "viewer";

/**
 * `UserSummary` — the account fields the API is willing to return.
 *
 * `is_active` is present because the admin routes report a deactivation in the
 * same body shape they use for a promotion, so the client can tell the two
 * outcomes apart.
 */
export interface UserSummary {
  id: string;
  email: string;
  role: UserRole;
  tenant_id: string;
  tenant_slug: string;
  is_active: boolean;
}

/**
 * `SessionResponse` — one access token, one refresh token, and who they belong
 * to. Returned identically by `/auth/login` and `/auth/refresh`, which is what
 * lets a client handle admission and rotation with a single code path.
 */
export interface Session {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  expires_at: string;
  /** Exists in this response only; the server keeps just its SHA-256. */
  refresh_token: string;
  refresh_expires_at: string;
  user: UserSummary;
}

/** What `/auth/login` accepts. The tenant slug is required: login precedes a principal. */
export interface LoginCredentials {
  tenant: string;
  email: string;
  password: string;
}

/** The backend's single error envelope (`app.main.error_payload`). */
interface ErrorEnvelope {
  error?: {
    code?: string;
    message?: string;
    request_id?: string | null;
    /**
     * Structured extra context, when the route chose to send some.
     *
     * `app.main.http_exception_handler` passes a raised `{code, message,
     * details}` dict straight through, which is how a policy refusal can carry
     * the channels it *would* have allowed and a lost `version` race can carry
     * the row that won. Typed as `unknown` on purpose: only the caller that
     * knows which route it called can narrow it, and narrowing here would make
     * one route's shape look like every route's shape.
     */
    details?: unknown;
  };
}

/**
 * A failed call, carrying enough for the UI to react rather than just print.
 *
 * `status` is the HTTP status, or `0` when no response was received at all —
 * the case a user most needs explained, since "wrong password" and "backend is
 * down" must not look the same.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  /** Correlation id from the envelope, if the server sent one. */
  readonly requestId: string | null;
  /** The envelope's `details`, untouched. Narrow it where the route is known. */
  readonly details: unknown;

  constructor(
    status: number,
    code: string,
    message: string,
    requestId: string | null = null,
    details: unknown = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }

  /** The credential was not accepted, or is no longer valid. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** The request never reached a server. */
  get isOffline(): boolean {
    return this.status === 0;
  }

  /** Admission control refused the attempt and said when to come back. */
  get isRateLimited(): boolean {
    return this.status === 429;
  }

  /** The account is authenticated but not allowed to do this. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /**
   * Someone else wrote the row first.
   *
   * The review routes answer this when the `version` a reviewer loaded is no
   * longer the stored one, so the UI must reload rather than retry: applying both
   * decisions would let two reviewers silently overwrite each other.
   */
  get isConflict(): boolean {
    return this.status === 409;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** Serialised as JSON. `undefined` sends no body at all. */
  body?: unknown;
  /** Bearer token to attach. Omitted for the two unauthenticated routes. */
  accessToken?: string | null;
  signal?: AbortSignal;
}

/**
 * Turn a failed response into an {@link ApiError}.
 *
 * A body that is not the expected envelope (a proxy's HTML error page, say) is
 * not treated as a bug: the status is still true and is all the caller gets.
 */
async function failure(response: Response): Promise<ApiError> {
  let code = "request_failed";
  let message = `The server answered with status ${response.status}.`;
  let requestId: string | null = null;
  let details: unknown = null;

  try {
    const body = (await response.json()) as ErrorEnvelope;
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      requestId = body.error.request_id ?? null;
      details = body.error.details ?? null;
    }
  } catch {
    // Keep the status-derived message: the response was not our envelope.
  }

  return new ApiError(response.status, code, message, requestId, details);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, accessToken, signal } = options;

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      cache: "no-store",
      signal,
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(accessToken ? { Authorization: `Bearer ${accessToken}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    // A caller-cancelled request is not a failure worth showing anyone.
    if (cause instanceof DOMException && cause.name === "AbortError") {
      throw cause;
    }
    throw new ApiError(0, "network_error", UNREACHABLE);
  }

  // `POST /auth/logout` answers 204 by design: it succeeds whether or not the
  // token existed, so there is nothing to parse and nothing to tell apart.
  if (response.status === 204) {
    return undefined as unknown as T;
  }

  if (!response.ok) {
    throw await failure(response);
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(
      response.status,
      "invalid_response",
      "The server answered with a body this UI cannot read.",
    );
  }
}

/**
 * The authentication surface.
 *
 * `login` and `refresh` are the only two calls that carry no bearer token: at
 * login there is no principal yet, and on refresh the refresh token *is* the
 * credential.
 */
export const auth = {
  login: (credentials: LoginCredentials) =>
    request<Session>("/auth/login", { method: "POST", body: credentials }),

  refresh: (refreshToken: string) =>
    request<Session>("/auth/refresh", {
      method: "POST",
      body: { refresh_token: refreshToken },
    }),

  /** Ends the session this refresh token belongs to. Always answers 204. */
  logout: (refreshToken: string) =>
    request<void>("/auth/logout", {
      method: "POST",
      body: { refresh_token: refreshToken },
    }),

  /** Ends every session of the caller's account, including the current one. */
  logoutAll: (accessToken: string) =>
    request<void>("/auth/logout-all", { method: "POST", accessToken }),

  me: (accessToken: string) => request<UserSummary>("/auth/me", { accessToken }),
};

/** Wire values of `app.db.enums.FieldName`. */
export type FieldName =
  | "email"
  | "phone"
  | "address"
  | "dob"
  | "start_date"
  | "end_date"
  | "contract_value";

/** Wire values of `app.db.enums.FieldStatus`. */
export type FieldStatus =
  | "auto_accepted"
  | "pending_review"
  | "approved"
  | "rejected"
  | "unreadable";

/** Wire values of `app.db.enums.ContractStatus`. */
export type ContractStatus =
  | "active"
  | "reminder_sent"
  | "responded"
  | "renewed"
  | "expired"
  | "escalated";

/**
 * One field awaiting a person (`app.api.v1.review.ReviewItem`).
 *
 * There is no `value` here and there is no field to add one to: the queue route
 * withholds every extracted value, and `review.reveal` is the audited call that
 * shows exactly one. A client that invents a looser shape would leak PII through
 * whichever screen reused it.
 */
export interface ReviewItem {
  field_id: number;
  contract_id: number;
  customer_ref: string;
  contract_status: ContractStatus;
  /** `YYYY-MM-DD`, or `null` when the contract carries no end date. */
  end_date: string | null;
  /** Negative once the contract has already expired — that is information, not an error. */
  days_to_expiry: number | null;
  age_minutes: number;
  /** The threshold this field fell below, so a reviewer sees *why* it is here. */
  threshold: number;
  field_name: FieldName;
  status: FieldStatus;
  confidence: number;
  /** Echo this back with a decision; the server refuses a stale one with 409. */
  version: number;
  source_page: number | null;
  source_line: number | null;
  reviewed_at: string | null;
  review_reason: string | null;
  blocks_pipeline: boolean;
}

/** One page of the review queue, lowest confidence first. */
export interface ReviewQueue {
  items: ReviewItem[];
  /** How many items this page carries, not how many the queue holds. */
  count: number;
  /** Age of the oldest item *on this page*. */
  oldest_minutes: number;
  threshold: number;
  /** Opaque: pass back as `cursor`. `null` means this was the last page. */
  next_cursor: string | null;
}

/** The four decisions `PATCH /review/{field_id}` accepts. */
export type ReviewAction = "accept" | "reject" | "mark_unreadable" | "edit";

/**
 * One reviewer decision.
 *
 * `value` is only legal with `edit` — the server rejects it with any other action
 * rather than ignoring it, because a caller that sends it believes it is being
 * stored.
 */
export interface ReviewDecision {
  action: ReviewAction;
  version: number;
  value?: string;
  reason?: string;
}

/** The decided field, plus how many fields still block its contract. */
export interface ReviewDecisionResponse {
  item: ReviewItem;
  /** Zero means the contract's pipeline may proceed. */
  blocking: number;
}

/** One revealed value — returned by `review.reveal` and nowhere else. */
export interface FieldValue {
  field_id: number;
  field_name: FieldName;
  value: string;
  version: number;
}

/**
 * The human-review surface (F8).
 *
 * Reading the queue needs any authenticated role; deciding and revealing are
 * `OPERATOR`/`ADMIN` only, so a viewer gets a 403 from the two write calls rather
 * than from the queue. `reveal` is a `POST` because it writes an audit row — a
 * reveal that leaves no trace is unauditable.
 */
export const review = {
  queue: (accessToken: string, cursor?: string) =>
    request<ReviewQueue>(
      cursor === undefined
        ? "/review/queue"
        : `/review/queue?cursor=${encodeURIComponent(cursor)}`,
      { accessToken },
    ),

  decide: (accessToken: string, fieldId: number, decision: ReviewDecision) =>
    request<ReviewDecisionResponse>(`/review/${fieldId}`, {
      method: "PATCH",
      body: decision,
      accessToken,
    }),

  reveal: (accessToken: string, fieldId: number) =>
    request<FieldValue>(`/review/${fieldId}/reveal`, { method: "POST", accessToken }),
};

/** Wire values of `app.db.enums.EscalationStatus`. */
export type EscalationStatus = "open" | "in_progress" | "resolved" | "sla_breached";

/** Wire values of `app.db.enums.ResponseType`. */
export type ResponseType = "reply" | "click" | "call_answered" | "manual";

/** The channels a customer can reply on (`app.db.enums.Channel`). */
export type ContactChannel = "email" | "sms" | "voice";

/**
 * One queue item (`app.api.v1.escalations.EscalationView`).
 *
 * `overdue` is a *server* answer, computed from `sla_deadline` and the clock when
 * the queue was read rather than read from a column — `sla_breached` is derived,
 * and the update route refuses an operator who tries to set it by hand. A client
 * that recomputed it from `sla_deadline` would drift from the row it displays.
 */
export interface Escalation {
  id: number;
  contract_id: number;
  status: EscalationStatus;
  assigned_to: string | null;
  /** When this row becomes late. UTC, so the browser can localise it. */
  sla_deadline: string;
  resolved_at: string | null;
  resolution_note: string | null;
  overdue: boolean;
  /** Echo this back with an update; the server refuses a stale one with 409. */
  version: number;
}

/** The queue: the rows, and the two numbers its header reports. */
export interface EscalationQueue {
  items: Escalation[];
  /** How many rows this response carries, not how many the tenant has. */
  count: number;
  /** How many of `items` have passed their deadline. */
  overdue: number;
  /** The tenant's configured SLA, echoed so the UI never hard-codes "48". */
  sla_hours: number;
}

/**
 * The two changes an operator makes, and the `version` they were looking at.
 *
 * `resolution_note` is only legal alongside `status: "resolved"` — the server
 * rejects it otherwise rather than ignoring it, because a note attached to an
 * open row is a note nothing reads.
 */
export interface EscalationUpdate {
  assigned_to?: string | null;
  status?: EscalationStatus;
  resolution_note?: string;
  version: number;
}

/** That a customer answered, plus the two side effects it caused. */
export interface RecordedResponse {
  response: {
    id: number;
    contract_id: number;
    channel: ContactChannel;
    response_type: ResponseType;
    received_at: string;
  };
  contract_id: number;
  /** Unchanged when the contract had already closed: a late reply is still a reply. */
  contract_status: ContractStatus;
  /** Set when this reply closed the contract's open escalation. */
  resolved_escalation_id: number | null;
}

/**
 * The escalation surface (gap 15).
 *
 * Reading needs any authenticated role — knowing what is late is not a write —
 * while claiming, resolving and recording a reply are `OPERATOR`/`ADMIN` only.
 *
 * Two actions that look alike are kept apart on purpose: `update` closes a
 * *queue item*, and `recordResponse` records that the *customer answered*. Only
 * the second moves a contract to `responded`; a hand-resolved escalation leaves
 * the contract `escalated`, because nobody has heard from them.
 */
export const escalations = {
  /** The queue, most urgent first. Omitted filters return resolved rows too. */
  queue: (accessToken: string, statuses?: EscalationStatus[]) =>
    request<EscalationQueue>(
      statuses === undefined || statuses.length === 0
        ? "/escalations"
        : `/escalations?${statuses.map((value) => `status=${value}`).join("&")}`,
      { accessToken },
    ),

  update: (accessToken: string, escalationId: number, update: EscalationUpdate) =>
    request<Escalation>(`/escalations/${escalationId}`, {
      method: "PATCH",
      body: update,
      accessToken,
    }),

  /**
   * Record that the customer replied. The reply body is never sent: this route
   * stores *that* they answered, on which channel and when — nothing more.
   */
  recordResponse: (accessToken: string, contractId: number, channel: ContactChannel) =>
    request<RecordedResponse>(`/contracts/${contractId}/response`, {
      method: "POST",
      body: { channel },
      accessToken,
    }),
};

/**
 * One stored channel recommendation (`app.api.v1.agent.AgentDecisionView`).
 *
 * Nothing here is a customer value — the reasoning is a fixed sentence built from
 * channel names — which is why reading a decision needs no reveal step while
 * reading the field it rests on does. `original_channel` is filled in once, on the
 * first human override, and never rewritten: a row overridden twice still reports
 * what the *policy* said rather than the previous operator's choice.
 */
export interface AgentDecision {
  id: number;
  contract_id: number;
  channel: ContactChannel;
  confidence: number;
  reasoning: string;
  /** Read by `dispatch`, which refuses to send a flagged decision. */
  needs_review: boolean;
  /** Which policy produced this row, so an old decision can be told apart. */
  prompt_version: string;
  /** `deterministic` in this slice: no model was consulted (D9-02). */
  llm_model: string;
  correlation_id: string;
  created_at: string;
  /** Echo this back with a correction; the server refuses a stale one with 409. */
  version: number;
  /** The policy's answer, once a person has replaced it. `null` = nobody has. */
  original_channel: ContactChannel | null;
  overridden_by: string | null;
  /** How many recommendations already existed when this one was appended. */
  rerun_count: number;
}

/** A bounded page of decisions, newest first. */
export interface AgentDecisionList {
  items: AgentDecision[];
  count: number;
  /** Echo of the filter; `null` means this is the tenant's recent decisions. */
  contract_id: number | null;
}

/** A correction, built from the `version` the operator was looking at. */
export interface DecisionOverrideRequest {
  channel: ContactChannel;
  version: number;
  reason?: string;
}

/** A flag, built from the `version` the operator was looking at. */
export interface FlagReviewRequest {
  version: number;
  reason?: string;
}

/**
 * The agent-decision surface (F9's human half, gap 19).
 *
 * Reading needs any authenticated role — a decision carries no customer value —
 * while deciding, overriding and flagging are `OPERATOR`/`ADMIN` only.
 *
 * `decide` **appends** and the two corrections **edit the newest row in place**:
 * "a person disagrees with this recommendation" is a fact about *that*
 * recommendation, so it must not destroy the history it corrects. That asymmetry
 * is also why the corrections are addressed by contract id rather than decision
 * id — the server resolves "the current recommendation" itself, and a caller
 * holding an older row's version is refused with `409`.
 */
export const agent = {
  /** The tenant's recent decisions, or one contract's history. */
  decisions: (accessToken: string, contractId?: number) =>
    request<AgentDecisionList>(
      contractId === undefined
        ? "/agent/decisions"
        : `/agent/decisions?contract_id=${contractId}`,
      { accessToken },
    ),

  /** Run the policy for one contract. Always appends; never an update. */
  decide: (accessToken: string, contractId: number) =>
    request<AgentDecision>("/agent/decisions", {
      method: "POST",
      body: { contract_id: contractId },
      accessToken,
    }),

  /** Replace the channel, choosing only among those the policy still permits. */
  override: (accessToken: string, contractId: number, payload: DecisionOverrideRequest) =>
    request<AgentDecision>(`/agent/decisions/${contractId}/override`, {
      method: "POST",
      body: payload,
      accessToken,
    }),

  /** Take the current recommendation out of the send path without deleting it. */
  flagReview: (accessToken: string, contractId: number, payload: FlagReviewRequest) =>
    request<AgentDecision>(`/agent/decisions/${contractId}/flag-review`, {
      method: "POST",
      body: payload,
      accessToken,
    }),
};

/** Wire values of `app.db.enums.ContractSource`. */
export type ContractSource = "pdf" | "api";

/** Wire values of `app.db.enums.RiskLevel`. */
export type RiskLevel = "low" | "medium" | "high";

/**
 * One stored contract (`app.api.v1.contracts.ContractListItem`).
 *
 * The route withholds two columns and this shape is the reason: `storage_path` is
 * an absolute path inside the API container and `created_by` is an internal user
 * id, so neither is modelled here. `has_document` answers the one question a
 * reader has about the stored file, and modelling a `path` field would invite a
 * screen to render it.
 *
 * `may_contact` and `consent_channels` are computed by the model, not by this
 * client: a UI that re-derived them from the four booleans would be a second
 * implementation of the contact policy, free to disagree with the one the
 * decision worker enforces.
 */
export interface ContractListItem {
  id: number;
  customer_ref: string;
  status: ContractStatus;
  source: ContractSource;
  risk: RiskLevel;
  /** `YYYY-MM-DD`, or `null` when the contract carries no term. */
  start_date: string | null;
  end_date: string | null;
  /** The customer's wall clock, which is what reminder milestones are measured on. */
  timezone: string;
  /** Per-tenant dedupe key: the metadata fingerprint, or the document's hash. */
  sha256: string | null;
  has_document: boolean;
  consent_email: boolean;
  consent_sms: boolean;
  consent_voice: boolean;
  do_not_contact: boolean;
  consent_channels: ContactChannel[];
  may_contact: boolean;
  version: number;
  created_at: string;
}

/** One keyset page of contracts, newest first. */
export interface ContractList {
  items: ContractListItem[];
  /** How many rows this page carries, not how many the tenant holds. */
  count: number;
  /** Echo of the filter; `null` means every status. */
  status: ContractStatus | null;
  /** Pass back as `cursor`. `null` is a completeness claim, not "looked full". */
  next_cursor: number | null;
}

/**
 * The contract surface (the read half of the aggregate root).
 *
 * Reading needs any authenticated role: nothing in the payload is a customer
 * value — `customer_ref` is the pseudonymous label, and identity lives in the
 * encrypted `ExtractedField` rows this route never touches. There is no write
 * function here yet: ingestion is a route without a screen.
 */
export const contracts = {
  /**
   * One page, newest first.
   *
   * The cursor is `Contract.id`, which is insertion order — so a scroll touches
   * each contract once even while new rows arrive underneath it. It is a
   * position, not an authorisation: the server always ANDs it with the tenant
   * predicate, so a guessed id narrows this tenant's own rows.
   */
  list: (accessToken: string, options: { status?: ContractStatus; cursor?: number } = {}) => {
    const params = new URLSearchParams();
    if (options.status !== undefined) params.set("status", options.status);
    if (options.cursor !== undefined) params.set("cursor", String(options.cursor));
    const query = params.toString();
    return request<ContractList>(query === "" ? "/contracts" : `/contracts?${query}`, {
      accessToken,
    });
  },
};

/**
 * Liveness probe.
 *
 * Built on `fetch` directly rather than on {@link request} because `/health`
 * lives outside the versioned API prefix — an orchestrator must not have to
 * track an API version to ask whether the process is up — so `next.config.ts`
 * proxies that one path separately.
 */
export async function probeHealth(signal?: AbortSignal): Promise<boolean> {
  try {
    const response = await fetch("/health", { cache: "no-store", signal });
    return response.ok;
  } catch {
    return false;
  }
}
