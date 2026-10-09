# Alembic helper — usage: .\scripts\migrate.ps1 upgrade head
$ErrorActionPreference = "Stop"
param(
    [Parameter(Position=0)][string]$Command = "upgrade",
    [Parameter(Position=1)][string]$Arg = "head"
)
Write-Host "alembic $Command $Arg" -ForegroundColor Cyan
alembic $Command $Arg
Write-Host "Migration done" -ForegroundColor Green
