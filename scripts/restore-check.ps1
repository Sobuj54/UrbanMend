param(
    [Parameter(Mandatory = $true)] [string]$DumpPath,
    [string]$MediaDirectory,
    [string]$DatabaseContainer = "urbenmend-db-1",
    [string]$DatabaseName = "urbenmend_restore_check",
    [string]$DatabaseUser = "urbenmend",
    [string]$StorageContainer = "urbenmend-storage-1",
    [string]$RestoreBucket = "urbenmend-restore-check",
    [string]$StorageAccessKey = "urbenmend",
    [string]$StorageSecretKey = "urbenmend-local-dev"
)

$ErrorActionPreference = "Stop"
$resolvedDump = [System.IO.Path]::GetFullPath($DumpPath)
if (-not (Test-Path -LiteralPath $resolvedDump -PathType Leaf)) {
    throw "Dump file does not exist: $resolvedDump"
}
$remoteDump = "/tmp/$([System.IO.Path]::GetFileName($resolvedDump))"

docker exec $DatabaseContainer dropdb --if-exists -U $DatabaseUser $DatabaseName
if ($LASTEXITCODE -ne 0) { throw "failed to remove restore-check database" }
docker exec $DatabaseContainer createdb -U $DatabaseUser $DatabaseName
if ($LASTEXITCODE -ne 0) { throw "failed to create restore-check database" }
docker cp $resolvedDump "$DatabaseContainer`:$remoteDump"
if ($LASTEXITCODE -ne 0) { throw "failed to copy database dump" }
docker exec $DatabaseContainer pg_restore --exit-on-error -U $DatabaseUser -d $DatabaseName $remoteDump
if ($LASTEXITCODE -ne 0) { throw "pg_restore failed" }

$tableCount = docker exec $DatabaseContainer psql -At -U $DatabaseUser -d $DatabaseName -c "select count(*) from information_schema.tables where table_schema = 'public'"
if ($LASTEXITCODE -ne 0 -or [int]$tableCount.Trim() -lt 1) { throw "restore validation found no public tables" }
Write-Host "Restore check passed: $tableCount public tables"

if ($MediaDirectory) {
    $resolvedMedia = [System.IO.Path]::GetFullPath($MediaDirectory)
    if (-not (Test-Path -LiteralPath $resolvedMedia -PathType Container)) {
        throw "Media backup directory does not exist: $resolvedMedia"
    }
    $remoteMedia = "/tmp/restore-media"
    docker cp $resolvedMedia "$StorageContainer`:$remoteMedia"
    if ($LASTEXITCODE -ne 0) { throw "failed to copy media backup" }
    docker exec $StorageContainer mc mb --ignore-existing "restore/$RestoreBucket"
    if ($LASTEXITCODE -ne 0) { throw "failed to create restore-check bucket" }
    docker exec $StorageContainer mc mirror --overwrite $remoteMedia "restore/$RestoreBucket"
    if ($LASTEXITCODE -ne 0) { throw "failed to restore media backup" }
    $sourceCount = (Get-ChildItem -LiteralPath $resolvedMedia -Recurse -File).Count
    $restoredCount = [int](docker exec $StorageContainer mc ls --recursive --json "restore/$RestoreBucket" | Measure-Object -Line).Lines
    if ($sourceCount -ne $restoredCount) {
        throw "media restore count mismatch: source=$sourceCount restored=$restoredCount"
    }
    Write-Host "Media restore check passed: $restoredCount objects"
}
    docker exec $StorageContainer mc alias set restore http://localhost:9000 $StorageAccessKey $StorageSecretKey
    if ($LASTEXITCODE -ne 0) { throw "object-store authentication failed" }
