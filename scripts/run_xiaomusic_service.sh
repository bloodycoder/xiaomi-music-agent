#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"
cd "$XIAOMI_MUSIC_ROOT/xiaomusic"
exec ./.venv/bin/python xiaomusic.py
