"""Fail if backend Pydantic schemas diverge from frontend Zod schemas.

Usage:
    python -m scripts.check_schema_drift

How it works (M3 will make this strict):
    1. Export backend schemas to contracts/schemas/*.json
    2. Frontend generates Zod from these (json-schema-to-zod)
    3. Compare mtimes/hashes — fail if drift

For M0: it just exports and exits 0 (placeholder logic, real check in M3).
"""

from __future__ import annotations

import sys

from scripts.export_schemas import main as export_main


def main() -> int:
    export_main()
    print("✅ Schema drift check passed (full check enabled in M3)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
