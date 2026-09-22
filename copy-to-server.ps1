#Requires -Version 5.1
<#
.SYNOPSIS
  Копирует gateway Kommo/CDR файлы на сервер через scp.
.EXAMPLE
  .\copy-to-server.ps1 -Server root@vultr -GatewayPath /opt/callspire-pbx-gateway
#>
param(
    [Parameter(Mandatory = $true)]
    [string] $Server,
    [string] $GatewayPath = "/opt/callspire-pbx-gateway"
)

$BundleDir = $PSScriptRoot
Write-Host "Copying from $BundleDir to ${Server}:${GatewayPath} ..."

$kommoFiles = @(
    "kommo_crm.py",
    "kommo_recording.py",
    "kommo_call_worker.py",
    "kommo_jobs_db.py",
    "kommo_store.py",
    "app_kommo.py"
)

foreach ($f in $kommoFiles) {
    scp "$BundleDir\$f" "${Server}:${GatewayPath}/"
}

$mountPy = "$BundleDir\gateway-web-softphone\gateway_web_softphone\mount.py"
if (Test-Path $mountPy) {
    scp $mountPy "${Server}:${GatewayPath}/gateway-web-softphone/gateway_web_softphone/"
    Write-Host "Copied mount.py (softphone /api/kommo/* proxy)"
}

scp "$BundleDir\templates\admin_kommo.html" "${Server}:${GatewayPath}/templates/"

Write-Host "Done. Restart: ssh $Server 'systemctl restart callspire-pbx-gateway || systemctl restart mikopbx-cdr.service'"
Write-Host "If app.py is custom on server, apply APP_PY_PATCH.md first."
