# Run full test suite with coverage
$ErrorActionPreference = "Stop"
Write-Host "Running pytest..." -ForegroundColor Cyan
pytest @args

Write-Host "Tests passed" -ForegroundColor Green
