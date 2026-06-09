#!/usr/bin/env bash
# Refresh local Netease playlist snapshots after playlist rename/add/delete/edit.
# It keeps runtime/playlist_blacklist.json rules active, refreshes track cache,
# rebuilds aliases and semantic playlist index, then asks launchd to restart the
# music-agent so in-memory caches are dropped.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export XIAOMI_MUSIC_ROOT="${XIAOMI_MUSIC_ROOT:-$SCRIPT_DIR}"
cd "$XIAOMI_MUSIC_ROOT"

echo "[reload] root=$XIAOMI_MUSIC_ROOT"

if [ ! -f runtime/playlist_blacklist.json ]; then
  mkdir -p runtime
  cat > runtime/playlist_blacklist.json <<'JSON'
{
  "ids": [],
  "names": [],
  "contains": [],
  "regex": []
}
JSON
fi

echo "[reload] playlist blacklist: runtime/playlist_blacklist.json"
cat runtime/playlist_blacklist.json

# Full sync: playlist list + every enabled playlist's tracks + aliases.
./scripts/sync_netease_playlists.sh "$@"

# Rebuild local semantic index used by scene/mood playlist matching.
PYTHON_BIN="${MUSIC_AGENT_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -x "$HOME/.pyenv/versions/3.9.6/bin/python3" ]; then
    PYTHON_BIN="$HOME/.pyenv/versions/3.9.6/bin/python3"
  else
    PYTHON_BIN="$(command -v python3)"
  fi
fi

echo "[reload] rebuilding semantic playlist index with $PYTHON_BIN"
"$PYTHON_BIN" scripts/semantic_playlist_mapper.py build-index

# OpenAI embedding cache, if present, is signature-based and will rebuild on next
# request after playlists/tracks changed. Removing it avoids stale renamed/deleted
# playlist entries if the agent process was already warm.
if [ -f runtime/playlist_embeddings.json ]; then
  rm -f runtime/playlist_embeddings.json
  echo "[reload] removed stale runtime/playlist_embeddings.json"
fi

# Restart music-agent if it is managed by launchd, otherwise leave a clear note.
if launchctl print "gui/$(id -u)/com.picard.music-agent" >/dev/null 2>&1; then
  echo "[reload] restarting com.picard.music-agent"
  launchctl kickstart -k "gui/$(id -u)/com.picard.music-agent" || true
else
  echo "[reload] launchd service com.picard.music-agent not found; restart music_agent.py manually if it is running"
fi

# Quick summary.
python3 - <<'PY'
import json
from pathlib import Path
root = Path.cwd()
pls = json.loads((root/'runtime/playlists.json').read_text(encoding='utf-8')).get('playlists', [])
cache = json.loads((root/'runtime/playlist_tracks_cache.json').read_text(encoding='utf-8')).get('playlists', {})
idx_path = root/'runtime/semantic_playlist_index.json'
idx_count = 0
if idx_path.exists():
    idx_count = len(json.loads(idx_path.read_text(encoding='utf-8')).get('items', []))
print(f"[reload] done: raw_playlists={len(pls)} enabled_track_cache={len(cache)} semantic_index={idx_count}")
PY
