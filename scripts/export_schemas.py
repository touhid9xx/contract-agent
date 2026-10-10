"""Export Pydantic schemas → JSON Schema (Draft 2020-12)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

OUT_DIR = Path("contracts/schemas")

# Register Pydantic models here as milestones add them.
# M5: UserRegister, UserLogin, UserRead, TokenPair
try:
    from contract_agent.schemas.user import (
        TokenPair,
        UserLogin,
        UserRead,
        UserRegister,
    )

    SCHEMAS: dict[str, type[BaseModel]] = {
        "user_register": UserRegister,
        "user_login": UserLogin,
        "user_read": UserRead,
        "token_pair": TokenPair,
    }
except ImportError:  # schemas not yet present
    SCHEMAS = {}


def _build_error_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ErrorResponse",
        "type": "object",
        "required": ["error", "status", "detail", "path", "request_id"],
        "properties": {
            "error": {"type": "string"},
            "status": {"type": "integer"},
            "detail": {},
            "path": {"type": "string"},
            "request_id": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    }


def _build_health_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "HealthResponse",
        "type": "object",
        "required": ["status", "service", "env", "timestamp"],
        "properties": {
            "status": {"type": "string"},
            "service": {"type": "string"},
            "env": {"type": "string"},
            "timestamp": {"type": "string", "format": "date-time"},
        },
        "additionalProperties": False,
    }


def _dump_model(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
    return schema


def main(*, check: bool = False) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_schemas: dict[str, dict[str, Any]] = {
        "error.json": _build_error_schema(),
        "health.json": _build_health_schema(),
    }
    for name, model in SCHEMAS.items():
        all_schemas[f"{name}.json"] = _dump_model(model)

    drift = False
    written = 0

    for filename, schema in sorted(all_schemas.items()):
        path = OUT_DIR / filename
        new_content = json.dumps(schema, indent=2, sort_keys=True) + "\n"

        if path.exists():
            if path.read_text(encoding="utf-8") != new_content:
                if check:
                    print(f"❌ DRIFT: {filename}")
                    drift = True
                else:
                    path.write_text(new_content, encoding="utf-8")
                    print(f"📝 Updated: {filename}")
                    written += 1
            else:
                print(f"✅ Unchanged: {filename}")
        else:
            if check:
                print(f"❌ MISSING: {filename}")
                drift = True
            else:
                path.write_text(new_content, encoding="utf-8")
                print(f"📝 Created: {filename}")
                written += 1

    existing = {p.name for p in OUT_DIR.glob("*.json")}
    for orphan in sorted(existing - set(all_schemas)):
        if check:
            print(f"❌ ORPHAN: {orphan}")
            drift = True
        else:
            (OUT_DIR / orphan).unlink()
            print(f"🗑️  Deleted orphan: {orphan}")
            written += 1

    if check and drift:
        print("\n❌ Schema drift detected.")
        return 1

    if not check:
        print(f"\n✅ Exported {len(all_schemas)} schemas ({written} changed)")
    else:
        print(f"\n✅ No schema drift ({len(all_schemas)} verified)")
    return 0


if __name__ == "__main__":
    sys.exit(main(check="--check" in sys.argv))
