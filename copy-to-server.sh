#!/usr/bin/env bash
# Копирует kommo bundle на сервер одной командой.
# Использование:
#   ./copy-to-server.sh user@pbx.example.com /opt/mikopbx-cdr-proxy

set -euo pipefail
TARGET="${1:?usage: $0 user@host /opt/mikopbx-cdr-proxy}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Copying Kommo bundle from $DIR to $TARGET ..."
scp "$DIR"/*.py "$TARGET/"
scp "$DIR/templates/admin.html" "$TARGET/templates/"
scp "$DIR/templates/admin_kommo.html" "$TARGET/templates/"
echo "Done. Restart gateway after deploy (mobile releases: /admin/mobile-releases)."
