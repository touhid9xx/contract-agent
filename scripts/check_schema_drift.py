"""Export Pydantic schemas → JSON Schema artifacts.

Usage:
    python -m scripts.export_schemas            # Write JSON schemas
    python -m scripts.export_schemas --check    # Check for drift (CI)

Exit codes:
    0 — success (or no drift in --check mode)
    1 — drift detected or export failed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Import your Pydantic models here as they land (M5+):
# from contract_agent.schemas.user import UserCreate, UserRead
# from contract_agent.schemas.contract import ContractRead
# etc.

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "contracts" / "schemas"

# Registry: {schema_name: PydanticModelClass}
# Empty until M5 — add models as they are created.
SCHEMA_REGISTRY: dict[str, Any] = {}


def _dump_schema(model: type) -> dict[str, Any]:
    """Pydantic v2 → JSON Schema dict."""
    return model.model_json_schema()  # type: ignore[attr-defined]


def _format_json(data: dict[str, Any]) -> str:
    """Deterministic JSON formatting for stable diffs."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _export_all(write: bool, check: bool) -> int:
    """Core logic — write JSON schemas OR verify no drift."""
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)

    if not SCHEMA_REGISTRY:
        if check:
            print("⚠️  No schemas registered yet (expected until M5)")
            return 0
        print("⚠️  No schemas registered yet (expected until M5)")
        return 0

    drift = False
    for name, model in sorted(SCHEMA_REGISTRY.items()):
        target = SCHEMAS_DIR / f"{name}.json"
        fresh = _format_json(_dump_schema(model))

        if check:
            if not target.exists():
                print(f"❌ {name}: schema file missing ({target})")
                drift = True
                continue
            existing = target.read_text(encoding="utf-8")
            if existing != fresh:
                print(f"❌ {name}: schema drifted from committed version")
                drift = True
            else:
                print(f"✅ {name}: no drift")
        elif write:
            target.write_text(fresh, encoding="utf-8")
            print(f"✅ {name}: written to {target.relative_to(ROOT)}")

    return 1 if drift else 0


def main(check: bool = False) -> int:
    """Public entry point.

    Args:
        check: If True, only verify drift (no writes). CI uses this.

    Returns:
        0 on success, 1 on drift/error.
    """
    return _export_all(write=not check, check=check)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Export or verify Pydantic → JSON Schema.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check for drift without writing (exits 1 on drift)",
    )
    args = parser.parse_args()
    return main(check=args.check)


if __name__ == "__main__":
    sys.exit(_cli())
