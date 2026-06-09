#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export XIAOMI_MUSIC_ROOT="${XIAOMI_MUSIC_ROOT:-$ROOT}"
PYTHON_BIN="${MUSIC_AGENT_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "$XIAOMI_MUSIC_ROOT/agents/cloud-music-mcp/.venv/bin/python3" ]; then
    PYTHON_BIN="$XIAOMI_MUSIC_ROOT/agents/cloud-music-mcp/.venv/bin/python3"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi
cd "$XIAOMI_MUSIC_ROOT"
echo "[sync] root=$XIAOMI_MUSIC_ROOT"
echo "[sync] python=$PYTHON_BIN"
echo "[sync] disabled rules: $XIAOMI_MUSIC_ROOT/runtime/music_disabled.json"
exec "$PYTHON_BIN" "$XIAOMI_MUSIC_ROOT/scripts/sync_netease_playlists.py" "$@"
