#!/usr/bin/env bash
# Shared environment helpers for Xiaomi Music Agent scripts.
set -euo pipefail
export XIAOMI_MUSIC_ROOT="${XIAOMI_MUSIC_ROOT:-$HOME/xiaomi-music}"
if [ -d "$HOME/.nvm/versions/node" ]; then
  latest_node="$(find "$HOME/.nvm/versions/node" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -1 || true)"
  if [ -n "${latest_node:-}" ]; then export PATH="$latest_node/bin:$PATH"; fi
fi
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
export XIAOMUSIC_PORT="${XIAOMUSIC_PORT:-8090}"
export MUSIC_AGENT_PORT="${MUSIC_AGENT_PORT:-8765}"
export NETEASE_CDP_PORT="${NETEASE_CDP_PORT:-9222}"
export MUSIC_AGENT_URL="${MUSIC_AGENT_URL:-http://127.0.0.1:${MUSIC_AGENT_PORT}}"
