param(
    [switch]$SkipSeed
)

$ErrorActionPreference = "Stop"
$Backend = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path (Split-Path -Parent $Backend) "frontend"

Write-Host "Checking Postgres on localhost:5434..."
$pgReady = (Test-NetConnection -ComputerName localhost -Port 5434 -WarningAction SilentlyContinue).TcpTestSucceeded
if (-not $pgReady) {
    Write-Host "Starting Postgres container..."
    docker run -d --name vastrabook-pg `
        -e POSTGRES_DB=csm_silks -e POSTGRES_USER=csm_user -e POSTGRES_PASSWORD=csm_password `
        -p 5434:5432 postgres:16-alpine 2>$null
    if ($LASTEXITCODE -ne 0) {
        docker start vastrabook-pg | Out-Null
    }
    Start-Sleep -Seconds 4
}

Write-Host "Backend: migrate + seed + runserver on :8001"
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$Backend'; py -3.12 manage.py migrate --noinput; if (-not `$SkipSeed) { py -3.12 manage.py seed_demo_data }; py -3.12 manage.py runserver 127.0.0.1:8001"
)

if (Test-Path $Frontend) {
    Write-Host "Frontend: Vite on :5174"
    Start-Process powershell -ArgumentList @(
        "-NoExit", "-Command",
        "cd '$Frontend'; `$env:VITE_API_URL='http://127.0.0.1:8001/api/v1'; npm run dev"
    )
    Write-Host "Open http://127.0.0.1:5174 when both windows show ready."
} else {
    Write-Host "Frontend not found at $Frontend — clone vectacore-Bill-Book-Frontend as a sibling folder."
    Write-Host "Backend API: http://127.0.0.1:8001/api/v1"
}
