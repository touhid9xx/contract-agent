# Start smtp4dev (local SMTP capture server) — UI at http://localhost:5000
$ErrorActionPreference = "Stop"
$binary = ".\tools\smtp4dev.exe"
if (-not (Test-Path $binary)) {
    Write-Host "smtp4dev binary not found at $binary" -ForegroundColor Yellow
    Write-Host "   Download: https://github.com/rnwood/smtp4dev/releases" -ForegroundColor Yellow
    exit 1
}
Write-Host "Starting smtp4dev on :2525 (UI :5000)..." -ForegroundColor Cyan
& $binary --urls "http://localhost:5000" --smtpport 2525
