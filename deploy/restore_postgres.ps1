param(
    [Parameter(Mandatory = $true)]
    [string]$InputFile,

    [string]$Container = "vastrabook-postgres-1",
    [string]$DbName = $env:DB_NAME,
    [string]$DbUser = $env:DB_USER,
    [switch]$Yes
)

if (-not $DbName) { $DbName = "vastrabook" }
if (-not $DbUser) { $DbUser = "vastrabook" }

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI is required to restore a backup."
}

$resolvedInput = (Resolve-Path -LiteralPath $InputFile -ErrorAction Stop).Path
$containerPath = "/tmp/vastrabook-restore.dump"

if (-not $Yes) {
    $confirmation = Read-Host "This will overwrite database '$DbName'. Type RESTORE to continue"
    if ($confirmation -ne "RESTORE") {
        Write-Host "Restore cancelled."
        exit 1
    }
}

docker cp $resolvedInput "${Container}:$containerPath"
if ($LASTEXITCODE -ne 0) {
    throw "docker cp failed while copying '$resolvedInput' into '$Container'."
}

docker exec $Container pg_restore -U $DbUser -d $DbName --clean --if-exists --no-owner $containerPath
if ($LASTEXITCODE -ne 0) {
    throw "pg_restore failed for database '$DbName' in container '$Container'."
}

docker exec $Container rm -f $containerPath | Out-Null

Write-Host "Restore completed into database '$DbName'."
