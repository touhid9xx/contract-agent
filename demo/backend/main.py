"""MOCKED Phase-0 demo backend for the Contract-Expiry Notification Agent.

Everything in this service is fake: JSON fixtures in ``demo/fixtures/`` are
loaded into memory at start-up, all state is process-local, and no database,
Kafka, Ollama, SMTP, SMS or voice provider is contacted. The purpose is to let
a human walk the finished product before any real foundation is built.

The API intentionally mirrors the shape of the future /api/v1 contract so the
demo UI survives the Phase-1 replacement (see DEMO.md screen -> milestone map).
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# Fixed "now" so the demo story reads exactly like the sketches.
DEMO_NOW = datetime(2025, 6, 14, 9, 26, 0, tzinfo=timezone.utc)


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _read_all() -> dict[str, Any]:
    contracts = load_fixture("contracts.json")
    fields = load_fixture("extracted_fields.json")
    decisions = load_fixture("agent_decisions.json")
    notifications = load_fixture("notifications.json")
    escalations = load_fixture("escalations.json")
    timeline = load_fixture("timeline.json")
    dashboard = load_fixture("dashboard.json")
    users = load_fixture("users.json")
    return {
        "today": contracts["today"],
        "mocked_total": contracts["total"],
        "contracts": contracts["contracts"],
        "fields": fields["fields"],
        "decisions": decisions["decisions"],
        "notifications": notifications["notifications"],
        "escalations": escalations["escalations"],
        "timeline": timeline["timeline"],
        "dashboard": dashboard,
        "users": users,
    }


STATE: dict[str, Any] = copy.deepcopy(_read_all())
EVENT_SEQ = 9000

app = FastAPI(
    title="Contract-Expiry Notification Agent — MOCKED demo API",
    version="0.0.0-demo",
    description="Phase 0 mocked API. Every response is fixture data.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def mark_mocked(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    response.headers["X-Mocked"] = "true"
    return response


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _find_contract(contract_id: int) -> dict[str, Any]:
    for contract in STATE["contracts"]:
        if contract["id"] == contract_id:
            return contract
    raise HTTPException(status_code=404, detail={"code": "CONTRACT_NOT_FOUND", "message": f"Contract {contract_id} not found."})


def _find_field(field_id: str) -> dict[str, Any]:
    for field in STATE["fields"]:
        if field["id"] == field_id:
            return field
    raise HTTPException(status_code=404, detail={"code": "FIELD_NOT_FOUND", "message": f"Field {field_id} not found."})


def _fields_for(contract_id: int) -> list[dict[str, Any]]:
    return [f for f in STATE["fields"] if f["contract_id"] == contract_id]


def _pending_fields(contract_id: int) -> list[dict[str, Any]]:
    return [f for f in _fields_for(contract_id) if f["status"] == "pending_review"]


def _timeline_for(contract_id: int) -> list[dict[str, Any]]:
    return list(STATE["timeline"].get(str(contract_id), []))


def _decision_for(contract_id: int) -> dict[str, Any] | None:
    for decision in STATE["decisions"]:
        if decision["contract_id"] == contract_id:
            return decision
    return None


def _escalation_for(contract_id: int) -> dict[str, Any] | None:
    for escalation in STATE["escalations"]:
        if escalation["contract_id"] == contract_id and escalation["status"] in ("open", "sla_breached", "in_progress"):
            return escalation
    return None


def _append_event(contract_id: int, event_type: str, title: str, summary: str, payload: dict[str, Any]) -> dict[str, Any]:
    global EVENT_SEQ
    EVENT_SEQ += 1
    event = {
        "id": f"ev-live-{EVENT_SEQ}",
        "contract_id": contract_id,
        "at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": event_type,
        "title": title,
        "summary": summary,
        "payload": payload,
    }
    STATE["timeline"].setdefault(str(contract_id), []).append(event)
    return event


def _refresh_pipeline_block(contract: dict[str, Any]) -> None:
    blocked = bool(_pending_fields(contract["id"]))
    contract["pipeline_blocked"] = blocked
    if not blocked and contract["status"] == "review":
        contract["status"] = "active"


def _review_item(field: dict[str, Any]) -> dict[str, Any]:
    contract = _find_contract(field["contract_id"])
    age_minutes = max(1, int((DEMO_NOW - datetime.fromisoformat(contract["uploaded_at"].replace("Z", "+00:00"))).total_seconds() // 60))
    return {
        **field,
        "customer_name": contract["customer_name"],
        "end_date": contract["end_date"],
        "days_to_expiry": contract["days_to_expiry"],
        "contract_status": contract["status"],
        "pipeline_blocked": contract["pipeline_blocked"],
        "age_minutes": age_minutes,
        "threshold": 0.80,
    }


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class ReviewUpdate(BaseModel):
    action: Literal["accept", "reject", "mark_unreadable", "edit"]
    value: str | None = Field(default=None, max_length=200)
    version: int = Field(ge=1)


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)


class ChannelIn(BaseModel):
    """Body of an operator override, matching ``DecisionOverrideRequest``.

    ``version`` is required rather than optional so the demo client has to send the
    generation it rendered, exactly as the real route demands. Without it the demo
    could never show the 409 the real API returns.
    """

    channel: Literal["email", "sms", "voice"]
    version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class FlagReviewIn(BaseModel):
    """Body of a flag-review, matching ``FlagReviewRequest`` (no channel: unrepresentable)."""

    version: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=500)


class AssignIn(BaseModel):
    assignee: str = Field(min_length=1, max_length=60)


class ResolveIn(BaseModel):
    resolution: str = Field(default="resolved", min_length=1, max_length=200)


class RespondIn(BaseModel):
    response_type: Literal["reply", "click", "call_answered", "manual"] = "manual"
    note: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "mocked": True, "mode": "phase-0-demo"}


@app.get("/api/v1/health")
async def api_health() -> dict[str, Any]:
    return {"status": "ok", "mocked": True, "fixtures": len(STATE["contracts"])}


# --------------------------------------------------------------------------- #
# identity
# --------------------------------------------------------------------------- #
@app.get("/api/v1/me")
async def me() -> dict[str, Any]:
    return {**STATE["users"]["current_user"], "mocked": True}


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
@app.get("/api/v1/dashboard")
async def dashboard() -> dict[str, Any]:
    await asyncio.sleep(0.18)
    pending = [f for f in STATE["fields"] if f["status"] == "pending_review"]
    open_escalations = [e for e in STATE["escalations"] if e["status"] in ("open", "sla_breached", "in_progress")]
    return {
        **STATE["dashboard"],
        "counts": {
            **STATE["dashboard"]["counts"],
            "awaiting_review": len(pending),
            "escalations": len(open_escalations),
        },
        "mocked": True,
    }


# --------------------------------------------------------------------------- #
# contracts
# --------------------------------------------------------------------------- #
@app.get("/api/v1/contracts")
async def list_contracts(
    status: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    risk: str | None = Query(default=None),
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=6, ge=1, le=50),
) -> dict[str, Any]:
    await asyncio.sleep(0.22)
    items = list(STATE["contracts"])
    if status:
        items = [c for c in items if c["status"] == status]
    if channel:
        items = [c for c in items if c["channel"] == channel]
    if risk:
        items = [c for c in items if c["risk"] == risk]
    if q:
        needle = q.lower().strip()
        items = [c for c in items if needle in c["customer_name"].lower() or needle in str(c["id"])]

    filtered = bool(status or channel or risk or q)
    total = len(items) if filtered else STATE["mocked_total"]
    start = (page - 1) * page_size
    return {
        "items": items[start : start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "page_count": max(1, -(-total // page_size)) if filtered else 128,
        "mocked": True,
        "note": "Fixture rows only — 1,284 is a mocked portfolio count.",
    }


@app.get("/api/v1/contracts/{contract_id}")
async def contract_detail(contract_id: int) -> dict[str, Any]:
    await asyncio.sleep(0.25)
    contract = _find_contract(contract_id)
    escalation = _escalation_for(contract_id)
    notifications = [n for n in STATE["notifications"] if n["contract_id"] == contract_id]
    return {
        "contract": contract,
        "fields": _fields_for(contract_id),
        "agent_decision": _decision_for(contract_id),
        "timeline": _timeline_for(contract_id),
        "notifications": notifications,
        "review_items": [_review_item(f) for f in _pending_fields(contract_id)],
        "escalation": escalation,
        "mocked": True,
    }


@app.post("/api/v1/contracts/{contract_id}/note")
async def add_note(contract_id: int, body: NoteIn) -> dict[str, Any]:
    await asyncio.sleep(0.3)
    contract = _find_contract(contract_id)
    actor = STATE["users"]["current_user"]["name"]
    event = _append_event(
        contract["id"],
        "audit_log",
        "note.added",
        body.text.strip(),
        {"note": body.text.strip(), "actor": actor, "channel": "internal"},
    )
    return {"event": event, "mocked": True}


@app.post("/api/v1/contracts/{contract_id}/escalate")
async def escalate(contract_id: int) -> dict[str, Any]:
    await asyncio.sleep(0.4)
    contract = _find_contract(contract_id)
    existing = _escalation_for(contract_id)
    if existing:
        return {"escalation": existing, "created": False, "mocked": True}
    now = datetime.now(tz=timezone.utc)
    escalation = {
        "id": contract_id,
        "contract_id": contract_id,
        "customer_name": contract["customer_name"],
        "raised_at": now.isoformat().replace("+00:00", "Z"),
        "sla_deadline": (now + timedelta(hours=48)).isoformat().replace("+00:00", "Z"),
        "status": "open",
        "assigned_to": None,
        "last_activity": "escalated manually from contract detail",
        "note": "Raised manually by an operator (MOCKED).",
    }
    STATE["escalations"].insert(0, escalation)
    contract["status"] = "escalated"
    _append_event(
        contract_id,
        "escalation.triggered",
        "escalation.triggered",
        "escalated manually to human operator",
        {"reason": "manual", "sla_deadline": escalation["sla_deadline"]},
    )
    return {"escalation": escalation, "created": True, "mocked": True}


@app.post("/api/v1/contracts/{contract_id}/mark-responded")
async def mark_responded(contract_id: int, body: RespondIn) -> dict[str, Any]:
    await asyncio.sleep(0.35)
    contract = _find_contract(contract_id)
    contract["status"] = "responded"
    event = _append_event(
        contract_id,
        "customer_responses",
        "customer.responded",
        f"response_type={body.response_type} · recorded manually",
        {"channel": contract["channel"] or "unknown", "response_type": body.response_type, "meaningful_response": True, "note": body.note},
    )
    return {"contract": contract, "event": event, "mocked": True}


# --------------------------------------------------------------------------- #
# review queue
# --------------------------------------------------------------------------- #
@app.get("/api/v1/review/queue")
async def review_queue() -> dict[str, Any]:
    await asyncio.sleep(0.2)
    items = [_review_item(f) for f in STATE["fields"] if f["status"] == "pending_review"]
    items.sort(key=lambda item: item["confidence"])
    return {
        "items": items,
        "count": len(items),
        "oldest_minutes": max((item["age_minutes"] for item in items), default=0),
        "threshold": 0.80,
        "mocked": True,
    }


@app.patch("/api/v1/review/{field_id}")
async def review_field(field_id: str, body: ReviewUpdate) -> dict[str, Any]:
    await asyncio.sleep(0.45)
    field = _find_field(field_id)
    if body.action == "edit" and not (body.value or "").strip():
        raise HTTPException(status_code=422, detail={"code": "VALUE_REQUIRED", "message": "An edited value is required."})
    if field["version"] != body.version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "STALE_VERSION",
                "message": "This field was updated by someone else. Reload to see the latest value.",
                "current": _review_item(field),
            },
        )
    previous = field["value"]
    if body.action == "accept":
        field["status"] = "approved"
    elif body.action == "edit":
        field["value"] = (body.value or "").strip()
        field["status"] = "approved"
    elif body.action == "reject":
        field["status"] = "rejected"
    else:
        field["status"] = "unreadable"

    field["version"] += 1
    # The real column is a GUID (`ExtractedField.reviewed_by`), not an email: identity
    # is resolved through the user table, and the demo stores the same shape.
    field["reviewed_by"] = STATE["users"]["current_user"]["id"]
    field["reviewed_at"] = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    contract = _find_contract(field["contract_id"])
    _refresh_pipeline_block(contract)
    _append_event(
        contract["id"],
        "review.resolved",
        "review.resolved",
        f"{field['field']} {body.action} by {STATE['users']['current_user']['display_name']}",
        {
            "field": field["field"],
            "action": body.action,
            "previous_value": previous,
            "new_value": field["value"],
            "reviewer": field["reviewed_by"],
            "version": field["version"],
            "pipeline_blocked": contract["pipeline_blocked"],
        },
    )
    return {"item": _review_item(field), "contract": contract, "mocked": True}


# --------------------------------------------------------------------------- #
# agent decisions
# --------------------------------------------------------------------------- #
@app.get("/api/v1/agent/decisions")
async def get_decision(contract_id: int = Query(...)) -> dict[str, Any]:
    await asyncio.sleep(0.2)
    contract = _find_contract(contract_id)
    return {"decision": _decision_for(contract_id), "contract": contract, "mocked": True}


@app.post("/api/v1/agent/decisions/{contract_id}/rerun")
async def rerun_agent(contract_id: int) -> dict[str, Any]:
    await asyncio.sleep(1.6)  # simulated Ollama latency
    contract = _find_contract(contract_id)
    current = _decision_for(contract_id)
    if current is None:
        raise HTTPException(status_code=409, detail={"code": "PIPELINE_BLOCKED", "message": "No decision exists; the contract is still awaiting review."})
    # A re-run **appends**, it never edits, which is what the real route does: the
    # previous recommendation stays readable as history instead of being overwritten.
    decision = copy.deepcopy(current)
    decision["id"] = max((row["id"] for row in STATE["decisions"]), default=0) + 1
    decision["correlation_id"] = str(uuid.uuid4())
    decision["created_at"] = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    decision["version"] = 1
    decision["original_channel"] = None
    decision["overridden_by"] = None
    decision["needs_review"] = False
    decision["rerun_count"] = current.get("rerun_count", 0) + 1
    STATE["decisions"].insert(0, decision)
    _append_event(
        contract_id,
        "agent_decisions",
        "agent_decisions",
        f"channel={decision['channel']} conf={decision['confidence']:.2f} (re-run)",
        {"channel": decision["channel"], "confidence": decision["confidence"], "prompt_version": decision["prompt_version"], "llm_model": decision["llm_model"], "rerun_count": decision["rerun_count"], "rerun": True},
    )
    return {"decision": decision, "contract": contract, "mocked": True}


@app.post("/api/v1/agent/decisions/{contract_id}/override")
async def override_channel(contract_id: int, body: ChannelIn) -> dict[str, Any]:
    await asyncio.sleep(0.4)
    decision = _decision_for(contract_id)
    if decision is None:
        raise HTTPException(status_code=409, detail={"code": "NO_DECISION", "message": "There is no agent decision to override."})
    if body.version != decision["version"]:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONCURRENT_UPDATE", "message": "This decision changed since you loaded it.", "decision": decision},
        )
    previous = decision["channel"]
    decision["channel"] = body.channel
    decision["version"] += 1
    # Set once and never rewritten, so a second override still reports the channel the
    # *policy* chose rather than the previous operator's choice.
    if decision["original_channel"] is None:
        decision["original_channel"] = previous
    decision["overridden_by"] = STATE["users"]["current_user"]["id"]
    _append_event(
        contract_id,
        "audit_log",
        "agent_decision.overridden",
        f"channel {previous} → {body.channel} by operator",
        {"previous_channel": previous, "new_channel": body.channel, "original_channel": decision["original_channel"], "actor": decision["overridden_by"]},
    )
    return {"decision": decision, "mocked": True}


@app.post("/api/v1/agent/decisions/{contract_id}/flag-review")
async def flag_for_review(contract_id: int, body: FlagReviewIn) -> dict[str, Any]:
    await asyncio.sleep(0.3)
    decision = _decision_for(contract_id)
    if decision is None:
        raise HTTPException(status_code=409, detail={"code": "NO_DECISION", "message": "There is no agent decision to flag."})
    if body.version != decision["version"]:
        raise HTTPException(
            status_code=409,
            detail={"code": "CONCURRENT_UPDATE", "message": "This decision changed since you loaded it.", "decision": decision},
        )
    decision["needs_review"] = True
    decision["version"] += 1
    _append_event(contract_id, "audit_log", "agent_decision.flagged", "decision flagged for human review", {"needs_review": True})
    return {"decision": decision, "mocked": True}


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
@app.get("/api/v1/notifications")
async def list_notifications(
    channel: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    await asyncio.sleep(0.2)
    items = list(STATE["notifications"])
    if channel:
        items = [n for n in items if n["channel"] == channel]
    if status:
        items = [n for n in items if n["status"] == status]
    items.sort(key=lambda n: n["sent_at"], reverse=True)
    return {"items": items, "count": len(items), "mocked": True}


@app.post("/api/v1/notifications/{notification_id}/retry")
async def retry_notification(notification_id: str) -> dict[str, Any]:
    await asyncio.sleep(0.6)
    for notification in STATE["notifications"]:
        if notification["id"] == notification_id:
            if notification["retry_count"] >= notification["retry_max"]:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "RETRY_LIMIT_REACHED", "message": "Retry limit reached — this notification now needs human attention."},
                )
            notification["retry_count"] += 1
            notification["status"] = "sent"
            notification["sent_at"] = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
            _append_event(
                notification["contract_id"],
                "notifications",
                "notification.retried",
                f"{notification['channel']} retry {notification['retry_count']}/{notification['retry_max']}",
                {"channel": notification["channel"], "status": "sent", "idempotency_key": notification["idempotency_key"], "retry_count": notification["retry_count"]},
            )
            return {"notification": notification, "mocked": True}
    raise HTTPException(status_code=404, detail={"code": "NOTIFICATION_NOT_FOUND", "message": "Notification not found."})


# --------------------------------------------------------------------------- #
# escalations
# --------------------------------------------------------------------------- #
@app.get("/api/v1/escalations")
async def list_escalations() -> dict[str, Any]:
    await asyncio.sleep(0.2)

    def bucket(statuses: set[str], resolved: bool = False) -> list[dict[str, Any]]:
        return [e for e in STATE["escalations"] if (e["status"] in statuses if not resolved else e["status"] == "resolved")]

    return {
        "breached": bucket({"sla_breached"}),
        "open": bucket({"open", "in_progress"}),
        "resolved": bucket(set(), resolved=True),
        "mocked": True,
        "sla_hours": 48,
    }


@app.post("/api/v1/escalations/{escalation_id}/assign")
async def assign_escalation(escalation_id: int, body: AssignIn) -> dict[str, Any]:
    await asyncio.sleep(0.3)
    for escalation in STATE["escalations"]:
        if escalation["id"] == escalation_id:
            escalation["assigned_to"] = body.assignee
            if escalation["status"] == "sla_breached":
                escalation["status"] = "in_progress"
            return {"escalation": escalation, "mocked": True}
    raise HTTPException(status_code=404, detail={"code": "ESCALATION_NOT_FOUND", "message": "Escalation not found."})


@app.post("/api/v1/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: int, body: ResolveIn) -> dict[str, Any]:
    await asyncio.sleep(0.35)
    for escalation in STATE["escalations"]:
        if escalation["id"] == escalation_id:
            escalation["status"] = "resolved"
            escalation["resolved_at"] = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
            escalation["resolved_by"] = STATE["users"]["current_user"]["name"]
            escalation["resolution"] = body.resolution
            _append_event(
                escalation["contract_id"],
                "escalation.resolved",
                "escalation.resolved",
                f"resolved: {body.resolution}",
                {"resolution": body.resolution, "resolved_by": escalation["resolved_by"]},
            )
            return {"escalation": escalation, "mocked": True}
    raise HTTPException(status_code=404, detail={"code": "ESCALATION_NOT_FOUND", "message": "Escalation not found."})


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if not isinstance(detail, dict):
        detail = {"code": "ERROR", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail, "mocked": True})


if __name__ == "__main__":  # pragma: no cover - manual run helper
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
