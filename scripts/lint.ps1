# Lint + format check (no writes)
$ErrorActionPreference = "Stop"

Write-Host "[1/4] Running ruff check..." -ForegroundColor Cyan
ruff check src tests scripts

Write-Host "[2/4] Running ruff format --check..." -ForegroundColor Cyan
ruff format --check src tests scripts

Write-Host "[3/4] Running mypy..." -ForegroundColor Cyan
mypy src

Write-Host "[4/4] Running pytest with coverage..." -ForegroundColor Cyan
pytest

Write-Host "OK - Lint passed" -ForegroundColor Green
