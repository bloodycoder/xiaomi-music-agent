#!/usr/bin/env bash
set -euo pipefail
if [ "$(uname -s)" != "Darwin" ]; then echo "This installer currently targets macOS." >&2; exit 1; fi
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export XIAOMI_MUSIC_ROOT="${XIAOMI_MUSIC_ROOT:-$HOME/xiaomi-music}"
mkdir -p "$XIAOMI_MUSIC_ROOT" "$XIAOMI_MUSIC_ROOT/agents" "$XIAOMI_MUSIC_ROOT/scripts" "$XIAOMI_MUSIC_ROOT/runtime"
for cmd in git python3 node npm curl; do command -v "$cmd" >/dev/null || { echo "Missing dependency: $cmd" >&2; exit 1; }; done

# Copy this project's portable assets into the runtime root.
rsync -a --delete "$REPO_DIR/scripts/" "$XIAOMI_MUSIC_ROOT/scripts/"
rsync -a --delete "$REPO_DIR/plugins/" "$XIAOMI_MUSIC_ROOT/plugins/"
rsync -a --delete "$REPO_DIR/agents/netease-music-mcp/" "$XIAOMI_MUSIC_ROOT/agents/netease-music-mcp/" --exclude node_modules
mkdir -p "$XIAOMI_MUSIC_ROOT/runtime"
[ -f "$XIAOMI_MUSIC_ROOT/.env.local" ] || cp "$REPO_DIR/.env.example" "$XIAOMI_MUSIC_ROOT/.env.local"

# xiaomusic upstream clone.
if [ ! -d "$XIAOMI_MUSIC_ROOT/xiaomusic" ]; then
  git clone https://github.com/hanxi/xiaomusic.git "$XIAOMI_MUSIC_ROOT/xiaomusic"
fi
cd "$XIAOMI_MUSIC_ROOT/xiaomusic"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U pip
python -m pip install -e .

# Install smart plugins and apply {arg} patch best-effort.
mkdir -p "$XIAOMI_MUSIC_ROOT/xiaomusic/plugins"
cp "$XIAOMI_MUSIC_ROOT/plugins/"*.py "$XIAOMI_MUSIC_ROOT/xiaomusic/plugins/"
patch -p1 < "$REPO_DIR/patches/xiaomusic-command-handler-arg.patch" || true

# Node deps for Netease desktop CDP control.
cd "$XIAOMI_MUSIC_ROOT/agents/netease-music-mcp"
npm install

cat <<MSG
Install done.
Next steps:
1) Edit $XIAOMI_MUSIC_ROOT/.env.local if you want LLM fallback.
2) Configure xiaomusic account/device using its UI or copy config/xiaomusic-setting.example.json as a guide.
3) Start services: $XIAOMI_MUSIC_ROOT/scripts/deploy.sh
4) Check: $XIAOMI_MUSIC_ROOT/scripts/status.sh
MSG
