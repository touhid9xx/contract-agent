# Start StrikeMQ (local Kafka-protocol broker) — single binary
# Download from https://strikemq.io (or your preferred build) and put in tools\
$ErrorActionPreference = "Stop"
$binary = ".\tools\strikemq.exe"
if (-not (Test-Path $binary)) {
    Write-Host " StrikeMQ binary not found at $binary" -ForegroundColor Yellow
    Write-Host "   Download it and place at tools\strikemq.exe" -ForegroundColor Yellow
    Write-Host "   Meanwhile, Kafka from docker-compose works too (M4)." -ForegroundColor Yellow
    exit 1
}
Write-Host "Starting StrikeMQ on :9092..." -ForegroundColor Cyan
& $binary --port 9092 --data-dir .\strikemq-data
