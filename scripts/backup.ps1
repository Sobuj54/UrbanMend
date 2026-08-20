param(
    [Parameter(Mandatory = $true)] [string]$OutputDirectory,
    [string]$DatabaseContainer = "urbenmend-db-1",
    [string]$DatabaseName = "urbenmend",
    [string]$DatabaseUser = "urbenmend",
    [string]$StorageContainer = "urbenmend-storage-1",
    [string]$StorageBucket = "urbenmend-media",
    [string]$StorageAccessKey = "urbenmend",
    [string]$StorageSecretKey = "urbenmend-local-dev"
)

$ErrorActionPreference = "Stop"
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$dbDump = Join-Path $resolvedOutput "urbenmend-$stamp.dump"
$mediaManifest = Join-Path $resolvedOutput "media-$stamp.json"
$mediaDirectory = Join-Path $resolvedOutput "media-$stamp"
$remoteMediaDirectory = "/tmp/media-$stamp"

docker exec $DatabaseContainer pg_dump -Fc -U $DatabaseUser -d $DatabaseName -f "/tmp/$([System.IO.Path]::GetFileName($dbDump))"
if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
docker cp "$DatabaseContainer`:/tmp/$([System.IO.Path]::GetFileName($dbDump))" $dbDump
if ($LASTEXITCODE -ne 0) { throw "failed to copy database dump" }

# The manifest is intentionally provider-neutral: production uses the configured S3 tool,
# while local MinIO uses `mc` inside the storage container.
docker exec $StorageContainer mc alias set backup http://localhost:9000 $StorageAccessKey $StorageSecretKey
if ($LASTEXITCODE -ne 0) { throw "object-store authentication failed" }
docker exec $StorageContainer mc ls --recursive --json "backup/$StorageBucket" |
    Set-Content -Encoding utf8 $mediaManifest
if ($LASTEXITCODE -ne 0) { throw "object-store manifest failed" }
docker exec $StorageContainer mkdir -p $remoteMediaDirectory
if ($LASTEXITCODE -ne 0) { throw "failed to create object-store backup directory" }
docker exec $StorageContainer mc mirror --overwrite "backup/$StorageBucket" $remoteMediaDirectory
if ($LASTEXITCODE -ne 0) { throw "object-store backup failed" }
docker cp "$StorageContainer`:$remoteMediaDirectory" $mediaDirectory
if ($LASTEXITCODE -ne 0) { throw "failed to copy object-store backup" }

Write-Host "Database dump: $dbDump"
Write-Host "Media manifest: $mediaManifest"
Write-Host "Media backup: $mediaDirectory"
