#!/usr/bin/env bash
# Deploy callspire-pbx-gateway + web softphone to production WITHOUT touching
# permissions.db or config.yaml (caller IDs, Kommo mappings, OAuth, AMI, app users).
#
# Usage (from repo root callspire-pbx-gateway/):
#   ./scripts/deploy-to-prod.sh user@vultr /opt/mikopbx-cdr-proxy
#
# Optional: path to built SPA dist on your machine:
#   SOFTPHONE_DIST=/path/to/softphone-web/dist ./scripts/deploy-to-prod.sh ...

set -euo pipefail

TARGET="${1:?usage: $0 user@host /opt/mikopbx-cdr-proxy}"
GW="$(cd "$(dirname "$0")/.." && pwd)"
SOFTPHONE_DIST="${SOFTPHONE_DIST:-}"

echo "=== Gateway source: $GW"
echo "=== Target:       $TARGET"
echo ""
echo "NEVER overwritten on server: permissions.db, config.yaml, venv/"
echo ""

# --- Python gateway (exclude secrets & DB) ---
RSYNC_EXCLUDES=(
  --exclude 'permissions.db'
  --exclude 'permissions.db-*'
  --exclude 'config.yaml'
  --exclude 'venv/'
  --exclude '.venv/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude 'kommo_jobs.sqlite'
  --exclude 'kommo_store.sqlite'
  --exclude '.git/'
)

if command -v rsync >/dev/null 2>&1; then
  rsync -avz "${RSYNC_EXCLUDES[@]}" \
    "$GW/" "$TARGET:${TARGET%/}/"
else
  echo "rsync not found — using scp for key files only"
  scp "$GW"/app.py "$GW"/app_kommo.py "$GW"/permissions_db.py "$GW"/auth.py \
      "$GW"/cdr_client.py "$GW"/miko_rest_client.py "$GW"/requirements.txt \
      "$GW"/requirements-web-softphone.txt \
      "$TARGET/"
  for f in kommo_crm.py kommo_recording.py kommo_call_worker.py kommo_jobs_db.py kommo_store.py; do
    scp "$GW/$f" "$TARGET/"
  done
  scp -r "$GW/gateway-web-softphone" "$TARGET/"
  scp -r "$GW/templates/"* "$TARGET/templates/"
fi

# --- Web softphone static (optional) ---
if [[ -n "$SOFTPHONE_DIST" && -d "$SOFTPHONE_DIST" ]]; then
  echo "Uploading SPA dist -> $TARGET/softphone-web/dist/"
  ssh "${TARGET%%:*}"@${TARGET#*@} "mkdir -p ${TARGET%/}/softphone-web/dist"
  if command -v rsync >/dev/null 2>&1; then
    rsync -avz --delete "$SOFTPHONE_DIST/" "$TARGET/softphone-web/dist/"
  else
    scp -r "$SOFTPHONE_DIST/"* "$TARGET/softphone-web/dist/"
  fi
else
  echo "Skip SPA: set SOFTPHONE_DIST=/path/to/softphone-web/dist to upload UI"
fi

echo ""
echo "Done. On the server run post-deploy steps from scripts/PROD_DEPLOY.ru.md"
