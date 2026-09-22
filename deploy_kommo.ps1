# Deploy Kommo + CDR pipeline to PBX gateway (run from this folder).
param(
    [string]$Host = "root@vultr",
    [string]$RemoteDir = "/opt/callspire-pbx-gateway"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$files = @(
    "kommo_crm.py",
    "kommo_recording.py",
    "kommo_call_worker.py",
    "kommo_jobs_db.py",
    "kommo_store.py",
    "kommo_oauth.py",
    "kommo_service.py",
    "app_kommo.py",
    "permissions_db.py",
    "cdr_client.py",
    "app.py",
    "templates/admin.html",
    "templates/admin_kommo.html",
    "gateway-web-softphone/gateway_web_softphone/mount.py"
)

Write-Host "Uploading Kommo/CDR files to ${Host}:${RemoteDir}/ ..."
foreach ($f in $files) {
    $local = Join-Path $here $f
    if (-not (Test-Path $local)) { throw "Missing $local" }
    $remote = "${Host}:${RemoteDir}/$f"
    scp $local $remote
}

Write-Host "Restarting gateway service ..."
ssh $Host "systemctl restart callspire-pbx-gateway 2>/dev/null || systemctl restart mikopbx-cdr.service; systemctl is-active callspire-pbx-gateway 2>/dev/null || systemctl is-active mikopbx-cdr.service"

Write-Host ""
Write-Host "Done. Check: journalctl -u callspire-pbx-gateway -n 50 | grep kommo"
