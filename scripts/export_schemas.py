"""Export Pydantic schemas → JSON Schema for frontend Zod sync.

Usage:
    python -m scripts.export_schemas

Why?
    - Frontend Zod schemas MUST mirror backend Pydantic (CI enforces in M3)
    - Single source of truth = backend Pydantic
"""

from __future__ import annotations

import json
from pathlib import Path

OUT_DIR = Path("contracts/schemas")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Error shape — mirrored on frontend as Zod
    error_schema = {
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
    }

    health_schema = {
        "title": "HealthResponse",
        "type": "object",
        "required": ["status", "service", "env", "timestamp"],
        "properties": {
            "status": {"type": "string"},
            "service": {"type": "string"},
            "env": {"type": "string"},
            "timestamp": {"type": "string", "format": "date-time"},
        },
    }

    (OUT_DIR / "error.json").write_text(json.dumps(error_schema, indent=2), encoding="utf-8")
    (OUT_DIR / "health.json").write_text(json.dumps(health_schema, indent=2), encoding="utf-8")

    print(f"✅ Exported schemas to {OUT_DIR}")


if __name__ == "__main__":
    main()
