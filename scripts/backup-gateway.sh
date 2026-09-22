#!/usr/bin/env bash
# Snapshot PBX Gateway on the production server (code + config + SQLite).
#
# Run on the server:
#   cd /opt/mikopbx-cdr-proxy
#   chmod +x scripts/backup-gateway.sh
#   sudo ./scripts/backup-gateway.sh
#
# Options:
#   --tar              also create backups/bkp-YYYYMMDD-HHMMSS.tar.gz
#   --with-uploads     include kommo_upload_recordings/ (can be large)
#   --keep N           keep last N backups (default: 10)
#   --dest DIR         backup root (default: $GATEWAY_DIR/backups)
#   --dry-run          print actions only
#
# Env:
#   GATEWAY_DIR=/opt/mikopbx-cdr-proxy

set -euo pipefail

GATEWAY_DIR="${GATEWAY_DIR:-/opt/mikopbx-cdr-proxy}"
BACKUP_PARENT=""
KEEP=10
CREATE_TAR=0
WITH_UPLOADS=0
DRY_RUN=0

usage() {
  sed -n '2,18p' "$0" | sed 's/^# \?//'
  exit "${1:-0}"
}

log() {
  printf '[backup-gateway] %s\n' "$*"
}

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run] '
    printf '%q ' "$@"
    printf '\n'
  else
    "$@"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tar) CREATE_TAR=1 ;;
    --with-uploads) WITH_UPLOADS=1 ;;
    --keep) KEEP="${2:?--keep requires a number}"; shift ;;
    --dest) BACKUP_PARENT="${2:?--dest requires a path}"; shift ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage 1
      ;;
  esac
  shift
done

if [[ ! -d "$GATEWAY_DIR" ]]; then
  echo "Gateway directory not found: $GATEWAY_DIR" >&2
  exit 1
fi

GATEWAY_DIR="$(cd "$GATEWAY_DIR" && pwd)"
BACKUP_PARENT="${BACKUP_PARENT:-$GATEWAY_DIR/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_PARENT/bkp-$STAMP"
MANIFEST="$DEST/MANIFEST.txt"

_backup_sqlite() {
  local src="$1"
  local dest="$2"
  [[ -f "$src" ]] || return 0
  run mkdir -p "$(dirname "$dest")"
  if command -v sqlite3 >/dev/null 2>&1; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "sqlite3 backup $src -> $dest"
    else
      sqlite3 "$src" ".backup '$dest'"
    fi
  else
    log "sqlite3 not found — copying $src (stop gateway for a consistent DB if needed)"
    run cp -a "$src" "$dest"
  fi
}

_copy_tree() {
  local src="$1"
  local dest="$2"
  shift 2
  [[ -e "$src" ]] || return 0
  run mkdir -p "$dest"
  if command -v rsync >/dev/null 2>&1; then
    run rsync -a "$@" "$src/" "$dest/"
  else
    run cp -a "$src/." "$dest/"
  fi
}

_write_manifest() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "would write $MANIFEST"
    return 0
  fi
  {
    echo "PBX Gateway backup"
    echo "created_at_local=$(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "created_at_utc=$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "hostname=$(hostname -f 2>/dev/null || hostname)"
    echo "user=$(whoami)"
    echo "gateway_dir=$GATEWAY_DIR"
    echo "backup_dir=$DEST"
    echo "with_uploads=$WITH_UPLOADS"
    echo ""
    if [[ -d "$DEST" ]]; then
      echo "backup_size=$(du -sh "$DEST" 2>/dev/null | awk '{print $1}')"
      echo "file_count=$(find "$DEST" -type f 2>/dev/null | wc -l | tr -d ' ')"
    fi
    echo ""
    if command -v sqlite3 >/dev/null 2>&1; then
      if [[ -f "$DEST/permissions.db" ]]; then
        echo "permissions.callerid_permissions=$(sqlite3 "$DEST/permissions.db" "SELECT COUNT(*) FROM callerid_permissions;" 2>/dev/null || echo '?')"
        echo "permissions.kommo_extension_users=$(sqlite3 "$DEST/permissions.db" "SELECT COUNT(*) FROM kommo_extension_users;" 2>/dev/null || echo '?')"
        echo "permissions.app_users=$(sqlite3 "$DEST/permissions.db" "SELECT COUNT(*) FROM app_users;" 2>/dev/null || echo '?')"
      fi
      if [[ -f "$DEST/kommo_jobs.sqlite" ]]; then
        echo "kommo_jobs.total=$(sqlite3 "$DEST/kommo_jobs.sqlite" "SELECT COUNT(*) FROM kommo_call_jobs;" 2>/dev/null || echo '?')"
      fi
    fi
    echo ""
    echo "Contents:"
    find "$DEST" -mindepth 1 -maxdepth 2 -printf '%P\n' 2>/dev/null | sort || true
  } >"$MANIFEST"
}

_rotate_old_backups() {
  [[ "$KEEP" -gt 0 ]] || return 0
  [[ -d "$BACKUP_PARENT" ]] || return 0

  mapfile -t OLD_DIRS < <(find "$BACKUP_PARENT" -mindepth 1 -maxdepth 1 -type d -name 'bkp-*' -printf '%T@ %p\n' 2>/dev/null | sort -n | awk '{print $2}')
  local excess=$(( ${#OLD_DIRS[@]} - KEEP ))
  if [[ "$excess" -le 0 ]]; then
    return 0
  fi
  local i=0
  while [[ "$i" -lt "$excess" ]]; do
    log "Removing old backup: ${OLD_DIRS[$i]}"
    run rm -rf "${OLD_DIRS[$i]}"
    local base
    base="$(basename "${OLD_DIRS[$i]}")"
    [[ -f "$BACKUP_PARENT/${base}.tar.gz" ]] && run rm -f "$BACKUP_PARENT/${base}.tar.gz"
    i=$((i + 1))
  done
}

log "Gateway: $GATEWAY_DIR"
log "Target:  $DEST"

run mkdir -p "$DEST"

# --- Python sources & docs ---
shopt -s nullglob
for f in "$GATEWAY_DIR"/*.py "$GATEWAY_DIR"/requirements*.txt "$GATEWAY_DIR"/README*.md "$GATEWAY_DIR"/*.md; do
  [[ -f "$f" ]] || continue
  run cp -a "$f" "$DEST/"
done
shopt -u nullglob

# --- Secrets & config (critical for restore) ---
for f in config.yaml config.example.yaml; do
  [[ -f "$GATEWAY_DIR/$f" ]] && run cp -a "$GATEWAY_DIR/$f" "$DEST/"
done

# --- SQLite (consistent copy when sqlite3 is available) ---
for db in permissions.db kommo_jobs.sqlite kommo_store.sqlite; do
  _backup_sqlite "$GATEWAY_DIR/$db" "$DEST/$db"
done

# --- Static assets & UI ---
_copy_tree "$GATEWAY_DIR/templates" "$DEST/templates"
_copy_tree "$GATEWAY_DIR/static" "$DEST/static"
_copy_tree "$GATEWAY_DIR/gateway-web-softphone" "$DEST/gateway-web-softphone" \
  --exclude '__pycache__' --exclude '*.pyc' --exclude '.venv' --exclude 'venv'
_copy_tree "$GATEWAY_DIR/softphone-web" "$DEST/softphone-web"

# --- Kommo client uploads (small metadata) ---
_copy_tree "$GATEWAY_DIR/kommo_client_uploads" "$DEST/kommo_client_uploads"

if [[ "$WITH_UPLOADS" -eq 1 ]]; then
  _copy_tree "$GATEWAY_DIR/kommo_upload_recordings" "$DEST/kommo_upload_recordings"
else
  log "Skip kommo_upload_recordings/ (use --with-uploads to include)"
fi

_write_manifest

if [[ "$CREATE_TAR" -eq 1 ]]; then
  TAR_PATH="$BACKUP_PARENT/bkp-$STAMP.tar.gz"
  log "Creating archive: $TAR_PATH"
  if [[ "$DRY_RUN" -eq 0 ]]; then
    tar -C "$BACKUP_PARENT" -czf "$TAR_PATH" "bkp-$STAMP"
    log "Archive size: $(du -sh "$TAR_PATH" | awk '{print $1}')"
  fi
fi

_rotate_old_backups

log "Done: $DEST"
if [[ -f "$MANIFEST" ]]; then
  log "Manifest: $MANIFEST"
fi
