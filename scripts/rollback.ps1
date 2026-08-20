param(
    [Parameter(Mandatory = $true)] [string]$PreviousImageSha,
    [Parameter(Mandatory = $true)] [string]$Namespace,
    [string]$ImageRepository = "ghcr.io/urbanmend/urbenmend",
    [string[]]$Deployments = @("api", "worker", "beat")
)

$ErrorActionPreference = "Stop"
$image = "$ImageRepository`:$PreviousImageSha"

foreach ($deployment in $Deployments) {
    kubectl -n $Namespace set image "deployment/$deployment" "app=$image"
    if ($LASTEXITCODE -ne 0) { throw "failed to update deployment $deployment" }
    kubectl -n $Namespace rollout status "deployment/$deployment" --timeout=10m
    if ($LASTEXITCODE -ne 0) { throw "rollback did not become ready for $deployment" }
}

Write-Host "Rollback complete: $image"
