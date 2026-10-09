# Auto-fix lint + format (writes files)
$ErrorActionPreference = "Stop"

Write-Host "[1/2] ruff check --fix..." -ForegroundColor Cyan
ruff check --fix src tests scripts

Write-Host "[2/2] ruff format..." -ForegroundColor Cyan
ruff format src tests scripts

Write-Host "OK - Formatting done" -ForegroundColor Green
