/**
 * The HTTP client's own contract, pinned where a compiler cannot reach.
 *
 * `request()` is the only place this UI turns bytes into a caller's answer, and
 * every screen's failure handling rests on four decisions it makes once:
 *
 *   * what a **refusal** looks like — an `ApiError` carrying the server's own
 *     code, message, request id and `details`, all of which screens branch on;
 *   * what a **non-answer** looks like — status `0`, distinct from a 401,
 *     because "the backend is down" and "wrong password" must never share a
 *     message (that distinction is the module's stated reason for existing);
 *   * what a **204** means — nothing to parse, not a parse failure;
 *   * what a **200 with an unreadable body** means — a refusal, not an empty
 *     success, since a screen that rendered `undefined` as data would hide it.
 *
 * `request` and `failure` are private, so every case here goes through an
 * exported endpoint and asserts on the real call. That is deliberate: the
 * subject is the seam between a screen and the wire, so a test that reached
 * past the endpoint would be testing a function no screen ever calls.
 *
 * Only the global `fetch` is replaced. `ApiError`, the envelope parsing and the
 * header/body decisions are the real code under test — a hand-rolled double of
 * any of them would pin the double rather than the module.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, auth, contracts, escalations, notifications, review } from "@/lib/api";

/** The one seam replaced: no test here talks to a network. */
const fetchMock = vi.fn<typeof fetch>();

/** A response the way the FastAPI process sends one. */
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The backend's error envelope (`app.main.error_payload`). */
function envelope(body: unknown, status: number): Response {
  return jsonResponse({ error: body }, status);
}

/** The URL `fetch` was first asked for. */
function requestedUrl(call = 0): string {
  return String(fetchMock.mock.calls[call]?.[0]);
}

/** The init `fetch` was first given. */
function requestedInit(call = 0): RequestInit {
  return fetchMock.mock.calls[call]?.[1] ?? {};
}

/** Headers as the module builds them: a plain object literal, not a `Headers`. */
function requestedHeaders(call = 0): Record<string, string> {
  return (requestedInit(call).headers ?? {}) as Record<string, string>;
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a call that succeeds", () => {
  it("returns the parsed body rather than the response", async () => {
    const user = { id: "u-1", email: "operator@example.test", role: "operator" };
    fetchMock.mockResolvedValue(jsonResponse(user));

    await expect(auth.me("access-1")).resolves.toEqual(user);
  });

  it("asks the versioned prefix, so no caller has to know the base path", async () => {
    // The prefix is relative on purpose (`next.config.ts` rewrites it), which is
    // what keeps the browser on one origin and CORS out of the picture.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await auth.me("access-1");

    expect(requestedUrl()).toBe("/api/v1/auth/me");
    expect(requestedInit().method).toBe("GET");
  });

  it("never lets an intermediary answer from cache", async () => {
    // A cached 401 or a stale review queue is indistinguishable from a live one,
    // and the queue's whole purpose is to be current.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await auth.me("access-1");

    expect(requestedInit().cache).toBe("no-store");
  });

  it("attaches the bearer token to an authenticated call", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await auth.me("access-1");

    expect(requestedHeaders().Authorization).toBe("Bearer access-1");
  });

  it("sends no token on login, because there is no principal yet", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await auth.login({ tenant: "acme", email: "a@b.test", password: "secret" });

    expect(requestedHeaders()).not.toHaveProperty("Authorization");
  });

  it("sends no body and no content type on a bare GET", async () => {
    // `Content-Type` without a body is a lie a proxy may act on, and the header
    // is spread on the body rather than always set for exactly this reason.
    fetchMock.mockResolvedValue(jsonResponse({}));

    await contracts.get("access-1", 7);

    expect(requestedInit().body).toBeUndefined();
    expect(requestedHeaders()).not.toHaveProperty("Content-Type");
  });

  it("serialises a JSON body under its own media type", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await auth.login({ tenant: "acme", email: "a@b.test", password: "secret" });

    expect(requestedHeaders()["Content-Type"]).toBe("application/json");
    expect(JSON.parse(String(requestedInit().body))).toEqual({
      tenant: "acme",
      email: "a@b.test",
      password: "secret",
    });
  });

  it("reads a 204 as 'nothing to return', not as an unreadable body", async () => {
    // `POST /auth/logout` answers 204 by design: it succeeds whether or not the
    // token existed, so there is nothing to parse and nothing to tell apart.
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));

    await expect(auth.logout("refresh-1")).resolves.toBeUndefined();
  });
});

describe("a document upload", () => {
  it("sends the bytes verbatim under the route's own media type", async () => {
    // The route parses the body as opaque bytes; announcing `application/json`
    // over a PDF would hand FastAPI a decode error instead of a document.
    fetchMock.mockResolvedValue(jsonResponse({ id: 1 }));
    const pdf = new Blob(["%PDF-1.4"], { type: "application/pdf" });

    await contracts.ingestDocument("access-1", { customer_ref: "ACME-1" }, pdf);

    expect(requestedInit().body).toBe(pdf);
    expect(requestedHeaders()["Content-Type"]).toBe("application/octet-stream");
  });

  it("carries the metadata as query parameters, not as a body", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 1 }));
    const pdf = new Blob(["%PDF-1.4"]);

    await contracts.ingestDocument(
      "access-1",
      {
        customer_ref: "ACME 1/2026",
        start_date: "2026-01-01",
        end_date: "2026-12-31",
        timezone: "Europe/London",
      },
      pdf,
    );

    const url = new URL(requestedUrl(), "http://localhost");
    expect(url.pathname).toBe("/api/v1/contracts/document");
    expect(url.searchParams.get("customer_ref")).toBe("ACME 1/2026");
    expect(url.searchParams.get("start_date")).toBe("2026-01-01");
    expect(url.searchParams.get("end_date")).toBe("2026-12-31");
    expect(url.searchParams.get("timezone")).toBe("Europe/London");
  });
});

describe("a call the server refuses", () => {
  it("surfaces the envelope's code, message and request id", async () => {
    fetchMock.mockResolvedValue(
      envelope(
        { code: "unauthorized", message: "Invalid credentials", request_id: "req-9" },
        401,
      ),
    );

    const error = await auth.me("access-1").catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 401,
      code: "unauthorized",
      message: "Invalid credentials",
      requestId: "req-9",
    });
    expect((error as ApiError).isUnauthorized).toBe(true);
  });

  it("keeps `details` untouched, because only the calling route knows its shape", async () => {
    // A consent 409 carries the state that won the race, and a decision refusal
    // carries the channels the policy would still allow. Narrowing here would
    // make one route's shape look like every route's shape.
    const current = { consent_email: true, version: 4 };
    fetchMock.mockResolvedValue(
      envelope({ code: "VERSION_CONFLICT", message: "stale", details: { current } }, 409),
    );

    const error = (await contracts
      .updateConsent("access-1", 3, {
        consent_email: false,
        consent_sms: false,
        consent_voice: false,
        do_not_contact: true,
        version: 3,
      })
      .catch((cause: unknown) => cause)) as ApiError;

    expect(error.details).toEqual({ current });
    expect(error.isConflict).toBe(true);
  });

  it("falls back to the status when the body is not our envelope", async () => {
    // A proxy's HTML error page is not a bug in this module: the status is still
    // true, and it is all the caller gets.
    fetchMock.mockResolvedValue(new Response("<html>Bad Gateway</html>", { status: 502 }));

    const error = (await auth.me("access-1").catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(502);
    expect(error.code).toBe("request_failed");
    expect(error.message).toContain("502");
    expect(error.requestId).toBeNull();
  });

  it("treats a 200 with an unreadable body as a refusal, not an empty success", async () => {
    // The alternative — resolving `undefined` — would let a screen render
    // nothing and call it a contract with no fields.
    fetchMock.mockResolvedValue(new Response("<html>maintenance</html>", { status: 200 }));

    const error = (await contracts.get("access-1", 12).catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(200);
    expect(error.code).toBe("invalid_response");
  });
});

describe("a call that never reached a server", () => {
  it("answers status 0, which is how 'down' stays distinct from 'wrong password'", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));

    const error = (await auth.login({
      tenant: "acme",
      email: "a@b.test",
      password: "secret",
    }).catch((cause: unknown) => cause)) as ApiError;

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(0);
    expect(error.code).toBe("network_error");
    expect(error.isOffline).toBe(true);
    expect(error.isUnauthorized).toBe(false);
  });

  it("lets a caller's own cancellation through unchanged", async () => {
    // An abort is the caller asking to stop, so it must not be reported as a
    // failure worth showing anyone — and must stay `instanceof DOMException` for
    // the `AbortSignal` plumbing that already catches it.
    const aborted = new DOMException("aborted", "AbortError");
    fetchMock.mockRejectedValue(aborted);

    await expect(auth.me("access-1")).rejects.toBe(aborted);
  });
});

describe("the query strings", () => {
  it("sends no query at all when nothing narrows the read", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], count: 0 }));

    await contracts.list("access-1");

    expect(requestedUrl()).toBe("/api/v1/contracts");
  });

  it("carries the status filter and the keyset cursor together", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], count: 0 }));

    await contracts.list("access-1", { status: "expired", cursor: 42 });

    const url = new URL(requestedUrl(), "http://localhost");
    expect(url.searchParams.get("status")).toBe("expired");
    expect(url.searchParams.get("cursor")).toBe("42");
  });

  it("percent-encodes an opaque cursor instead of trusting it", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], count: 0 }));

    await review.queue("access-1", "0.42:7/8+9");

    const url = new URL(requestedUrl(), "http://localhost");
    expect(url.searchParams.get("cursor")).toBe("0.42:7/8+9");
  });

  it("drops status and channel when a contract is chosen, because the route ignores them", async () => {
    // A query string that describes a filter the answer ignored is worse than a
    // shorter one: the screen would claim to be showing one thing while showing
    // another.
    fetchMock.mockResolvedValue(jsonResponse({ items: [], count: 0 }));

    await notifications.list("access-1", { contractId: 5, status: "sent", channel: "email" });

    const url = new URL(requestedUrl(), "http://localhost");
    expect(url.searchParams.get("contract_id")).toBe("5");
    expect(url.searchParams.has("status")).toBe(false);
    expect(url.searchParams.has("channel")).toBe(false);
  });

  it("repeats the status key for a multi-status escalation read", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ items: [], count: 0 }));

    await escalations.queue("access-1", { statuses: ["open", "sla_breached"], contractId: 5 });

    const url = new URL(requestedUrl(), "http://localhost");
    expect(url.searchParams.getAll("status")).toEqual(["open", "sla_breached"]);
    expect(url.searchParams.get("contract_id")).toBe("5");
  });
});
