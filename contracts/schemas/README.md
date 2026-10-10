# Contract Schemas

**Source of truth:** `src/contract_agent/schemas/` (Pydantic models).

**Generated artifacts (do not edit manually):**
- `contracts/schemas/*.json` — JSON Schema Draft 2020-12
- `frontend/src/lib/schemas/*.ts` — Zod schemas

**Regenerate:**

```powershell
# From repo root, venv activated
python -m scripts.export_schemas
cd frontend; npm run schemas:generate
