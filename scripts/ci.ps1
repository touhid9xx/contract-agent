# Local mirror of GitHub Actions CI — run before pushing.
# Usage: .\scripts\ci.ps1
#
# Prerequisites:
#   - Backend venv activated (`.\venv\Scripts\Activate.ps1`)
#   - Docker MySQL running (`docker compose up -d mysql`)
#   - Frontend Playwright browsers installed once (`cd frontend; npx playwright install chromium`)

$ErrorActionPreference = "Stop"

function Section($name) {
    Write-Host ""
    Write-Host ("=" * 60) -ForegroundColor Cyan
    Write-Host "  $name" -ForegroundColor Cyan
    Write-Host ("=" * 60) -ForegroundColor Cyan
}

function Invoke-Checked {
    param(
        [string]$Name,
        [scriptblock]$Command
    )
    $global:LASTEXITCODE = 0
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Check '$Name' failed with exit code $LASTEXITCODE"
    }
}

$start = Get-Date
$failures = @()

# ---------- Preflight ----------
Section "PREFLIGHT"
$mysqlRunning = $false
try {
    $mysqlPs = docker compose ps mysql --format json 2>$null
    if ($mysqlPs -match '"State":"running"') {
        Write-Host "  MySQL container is running" -ForegroundColor Green
        $mysqlRunning = $true
    } else {
        Write-Host "  WARNING: MySQL container is NOT running. Integration tests will fail." -ForegroundColor Yellow
        Write-Host "     Run: docker compose up -d mysql" -ForegroundColor Yellow
    }
} catch {
    Write-Host "  WARNING: docker not found - assuming MySQL not available" -ForegroundColor Yellow
}

# ---------- Backend ----------
Section "BACKEND: ruff check"
try {
    Invoke-Checked "ruff check" { ruff check src tests scripts }
} catch {
    $failures += "ruff check"
}

Section "BACKEND: ruff format check"
try {
    Invoke-Checked "ruff format" { ruff format --check src tests scripts }
} catch {
    $failures += "ruff format"
}

Section "BACKEND: mypy"
try {
    Invoke-Checked "mypy" { mypy src }
} catch {
    $failures += "mypy"
}

Section "BACKEND: bandit"
try {
    Invoke-Checked "bandit" { bandit -r src -c pyproject.toml --severity-level=medium }
} catch {
    $failures += "bandit"
}

Section "BACKEND: pytest (unit + integration)"
try {
    Invoke-Checked "pytest" { pytest -q -m "integration or not integration" }
} catch {
    $failures += "pytest"
}

if ($mysqlRunning) {
    Section "BACKEND: alembic round-trip"
    if (Test-Path alembic.ini) {
        try {
            Invoke-Checked "alembic upgrade" { alembic upgrade head }
            Invoke-Checked "alembic downgrade" { alembic downgrade -1 }
            Invoke-Checked "alembic upgrade" { alembic upgrade head }
        } catch {
            $failures += "alembic round-trip"
        }
    } else {
        Write-Host "  WARNING: alembic.ini not found - skipping" -ForegroundColor Yellow
    }
}

Section "BACKEND: schema drift"
try {
    Invoke-Checked "schema drift" { python -m scripts.check_schema_drift }
} catch {
    $failures += "schema drift"
}

# ---------- Frontend ----------
Push-Location frontend
try {
    Section "FRONTEND: eslint"
    try {
        Invoke-Checked "eslint" { npx eslint . --max-warnings=0 }
    } catch {
        $failures += "eslint"
    }

    Section "FRONTEND: tsc"
    try {
        Invoke-Checked "tsc" { npx tsc --noEmit }
    } catch {
        $failures += "tsc"
    }

    Section "FRONTEND: prettier"
    try {
        Invoke-Checked "prettier" { npx prettier --check . }
    } catch {
        $failures += "prettier"
    }

    Section "FRONTEND: vitest"
    try {
        Invoke-Checked "vitest" { npm run test:unit:coverage }
    } catch {
        $failures += "vitest"
    }

    Section "FRONTEND: build"
    try {
        Invoke-Checked "build" { npm run build }
    } catch {
        $failures += "build"
    }

    Section "FRONTEND: playwright (e2e + a11y)"
    try {
        Invoke-Checked "playwright" { npx playwright test }
    } catch {
        $failures += "playwright"
    }
} finally {
    Pop-Location
}

# ---------- Summary ----------
$elapsed = (Get-Date) - $start
Write-Host ""
Write-Host ("=" * 60) -ForegroundColor Cyan
if ($failures.Count -eq 0) {
    Write-Host "OK - ALL CI CHECKS PASSED in $($elapsed.TotalSeconds.ToString('F1'))s" -ForegroundColor Green
} else {
    Write-Host "FAIL - CI FAILED - $($failures.Count) check(s) failed:" -ForegroundColor Red
    $failures | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
    Write-Host ("=" * 60) -ForegroundColor Cyan
    exit 1
}
Write-Host ("=" * 60) -ForegroundColor Cyan
