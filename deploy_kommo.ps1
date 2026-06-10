# Deploy Kommo auth fixes to PBX gateway (run from repo root or this folder).
param(
    [string]$Host = "root@vultr",
    [string]$RemoteDir = "/opt/mikopbx-cdr-proxy"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$files = @(
    "kommo_oauth.py",
    "kommo_service.py",
    "app_kommo.py",
    "permissions_db.py",
    "templates/admin.html"
)

Write-Host "Uploading Kommo files to ${Host}:${RemoteDir}/ ..."
foreach ($f in $files) {
    $local = Join-Path $here $f
    if (-not (Test-Path $local)) { throw "Missing $local" }
    scp $local "${Host}:${RemoteDir}/$f"
}

Write-Host "Restarting mikopbx-cdr.service ..."
ssh $Host "systemctl restart mikopbx-cdr.service && systemctl is-active mikopbx-cdr.service"

Write-Host ""
Write-Host "Done. In admin: Disconnect -> Save settings -> Authorize -> Test token."
Write-Host "Test token must show impl_version: 20260526-amocrm-oauth-docs-v13"
