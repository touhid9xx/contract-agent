# Alembic helper — usage: .\scripts\migrate.ps1 upgrade head
# Examples:
#   .\scripts\migrate.ps1                    # upgrade head (default)
#   .\scripts\migrate.ps1 upgrade head
#   .\scripts\migrate.ps1 downgrade -1
#   .\scripts\migrate.ps1 revision -m "add_users"
#   .\scripts\migrate.ps1 current

param(
    [Parameter(Position=0)][string]$Command = "upgrade",
    [Parameter(Position=1)][string]$Arg = "head"
)

$ErrorActionPreference = "Stop"

Write-Host "alembic $Command $Arg" -ForegroundColor Cyan

if ($Arg -and $Arg -ne "") {
    alembic $Command $Arg
} else {
    alembic $Command
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "Migration FAILED (exit $LASTEXITCODE)" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "OK - Migration done" -ForegroundColor Green
