# Remove build/cache artifacts
$ErrorActionPreference = "Continue"
Write-Host "Cleaning..." -ForegroundColor Cyan

Get-ChildItem -Recurse -Force -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Force -Directory -Filter ".pytest_cache" | Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Force -Directory -Filter ".mypy_cache" | Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Force -Directory -Filter ".ruff_cache" | Remove-Item -Recurse -Force
Get-ChildItem -Recurse -Force -Directory -Filter "htmlcov" | Remove-Item -Recurse -Force

if (Test-Path ".coverage") { Remove-Item ".coverage" -Force }
if (Test-Path "coverage.xml") { Remove-Item "coverage.xml" -Force }

Write-Host "Clean done" -ForegroundColor Green
