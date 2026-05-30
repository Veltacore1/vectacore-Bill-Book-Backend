param(
    [string]$Container = "vastrabook-postgres-1",
    [string]$DbName = $env:DB_NAME,
    [string]$DbUser = $env:DB_USER,
    [string]$OutputDir = (Join-Path $PSScriptRoot "backups")
)

if (-not $DbName) { $DbName = "vastrabook" }
if (-not $DbUser) { $DbUser = "vastrabook" }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is required to create a backup."
}

if (-not (Test-Path -LiteralPath $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$fileName = "vastrabook-$timestamp.dump"
$outputPath = Join-Path $OutputDir $fileName
$containerPath = "/tmp/$fileName"

docker exec $Container pg_dump -U $DbUser -d $DbName -Fc -f $containerPath
if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed for database '$DbName' in container '$Container'."
}

docker cp "${Container}:$containerPath" $outputPath
if ($LASTEXITCODE -ne 0) {
    throw "docker cp failed while copying the backup out of '$Container'."
}

docker exec $Container rm -f $containerPath | Out-Null

Write-Host "Backup created: $outputPath"
