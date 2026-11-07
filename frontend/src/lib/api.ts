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

  constructor(status: number, code: string, message: string, requestId: string | null = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
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

  try {
    const body = (await response.json()) as ErrorEnvelope;
    if (body.error) {
      code = body.error.code ?? code;
      message = body.error.message ?? message;
      requestId = body.error.request_id ?? null;
    }
  } catch {
    // Keep the status-derived message: the response was not our envelope.
  }

  return new ApiError(response.status, code, message, requestId);
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
