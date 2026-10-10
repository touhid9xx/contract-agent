"""Fail if backend schemas drift from committed JSON Schema artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.export_schemas import main as export_main

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "contracts" / "schemas"
ZOD_DIR = ROOT / "frontend" / "src" / "lib" / "schemas"


def _check_backend_drift() -> int:
    print("=" * 60)
    print("Step 1: Backend schema drift")
    print("=" * 60)
    return export_main(check=True)


def _check_frontend_zod_exists() -> int:
    print()
    print("=" * 60)
    print("Step 2: Frontend Zod coverage")
    print("=" * 60)

    if not SCHEMAS_DIR.exists():
        print(f"❌ {SCHEMAS_DIR} missing")
        return 1

    if not ZOD_DIR.exists():
        print(f"⚠️  {ZOD_DIR} not yet created (expected until Zod generation is wired)")
        return 0

    missing: list[str] = []
    for json_file in sorted(SCHEMAS_DIR.glob("*.json")):
        zod_file = ZOD_DIR / f"{json_file.stem}.ts"
        if zod_file.exists():
            print(f"✅ {json_file.stem}")
        else:
            print(f"❌ {json_file.stem} — missing {zod_file.name}")
            missing.append(json_file.stem)

    if missing:
        print(f"\n❌ {len(missing)} schema(s) missing Zod")
        return 1

    print("\n✅ All schemas have Zod counterparts")
    return 0


def main() -> int:
    return max(_check_backend_drift(), _check_frontend_zod_exists())


if __name__ == "__main__":
    sys.exit(main())
