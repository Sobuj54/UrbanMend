param(
    [Parameter(Mandatory = $true)] [string]$ImageSha,
    [Parameter(Mandatory = $true)] [string]$Namespace,
    [string]$ImageRepository = "ghcr.io/urbanmend/urbenmend",
    [string]$ApiDeployment = "api",
    [string]$WorkerDeployment = "worker",
    [string]$BeatDeployment = "beat",
    [string]$MigrationJob = "migrate-$ImageSha"
)

$ErrorActionPreference = "Stop"
$image = "$ImageRepository`:$ImageSha"
$templatePath = Join-Path $PSScriptRoot "..\deploy\migration-job.yaml"

function Invoke-Kubectl {
    param([Parameter(ValueFromRemainingArguments = $true)] [string[]]$Arguments)
    & kubectl @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl failed with exit code $LASTEXITCODE"
    }
}

# Migrations run before new pods so the schema is ready for both API and worker code.
$manifest = (Get-Content -Raw -Encoding utf8 $templatePath).
    Replace("__MIGRATION_JOB__", $MigrationJob).
    Replace("__NAMESPACE__", $Namespace).
    Replace("__IMAGE__", $image)
$manifest | kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw "failed to create migration Job" }
Invoke-Kubectl -n $Namespace wait --for=condition=complete --timeout=10m "job/$MigrationJob"

foreach ($deployment in @($ApiDeployment, $WorkerDeployment, $BeatDeployment)) {
    Invoke-Kubectl -n $Namespace set image "deployment/$deployment" "app=$image"
    Invoke-Kubectl -n $Namespace rollout status "deployment/$deployment" --timeout=10m
}

Write-Host "Deployment complete: $image"
