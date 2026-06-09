#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"

LABEL_PREFIX="com.${USER}"
UID_NUM="$(id -u)"
STATE_DIR="$XIAOMI_MUSIC_ROOT/runtime/health"
SECURE_DIR="$XIAOMI_MUSIC_ROOT/runtime/secure"
LOG="$STATE_DIR/watchdog.log"
TOKEN="$XIAOMI_MUSIC_ROOT/xiaomusic/conf/.mi.token"
TOKEN_BACKUP="$SECURE_DIR/.mi.token.backup"
XIAOMUSIC_ERR="$XIAOMI_MUSIC_ROOT/runtime/launchd/xiaomusic.err.log"
mkdir -p "$STATE_DIR" "$SECURE_DIR"
chmod 700 "$SECURE_DIR" 2>/dev/null || true

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*" >> "$LOG"; }
notify() {
  # 静默模式：只记录状态，不做小爱音箱语音播报，也不发 macOS 通知。
  # 需要修复的地方由后续 kick/restore 逻辑自动执行。
  local msg="$1"
  log "SILENT_ALERT: $msg"
}

kick() {
  local svc="$1"
  local label="${LABEL_PREFIX}.${svc}"
  log "kickstart $label"
  /bin/launchctl kickstart -k "gui/${UID_NUM}/${label}" >> "$LOG" 2>&1 || true
}

check_http() {
  local url="$1"
  /usr/bin/curl --noproxy '*' -fsS --max-time 5 "$url" >/dev/null 2>&1
}

backup_token_if_present() {
  if [ -s "$TOKEN" ]; then
    cp -p "$TOKEN" "$TOKEN_BACKUP.tmp"
    chmod 600 "$TOKEN_BACKUP.tmp" 2>/dev/null || true
    mv "$TOKEN_BACKUP.tmp" "$TOKEN_BACKUP"
  fi
}

restore_token_if_missing() {
  if [ ! -s "$TOKEN" ]; then
    if [ -s "$TOKEN_BACKUP" ]; then
      mkdir -p "$(dirname "$TOKEN")"
      cp -p "$TOKEN_BACKUP" "$TOKEN"
      chmod 600 "$TOKEN" 2>/dev/null || true
      log "restored missing xiaomusic token from backup"
      kick xiaomusic
      return 0
    fi
    notify "xiaomusic 登录 token 缺失，且没有备份；需要重新登录小米账号。"
    return 1
  fi
}


main() {
  log "watchdog tick"

  restore_token_if_missing || true

  # If token is present, keep a private backup so accidental deletion can be repaired.
  backup_token_if_present

  if ! check_http "http://127.0.0.1:${XIAOMUSIC_PORT}/"; then
    notify "xiaomusic 8090 不可用，已尝试自动重启。"
    kick xiaomusic
  fi

  if ! check_http "http://127.0.0.1:${MUSIC_AGENT_PORT}/health"; then
    notify "Music Agent 8765 不可用，已尝试自动重启。"
    kick music-agent
  fi

  if ! check_http "http://127.0.0.1:${NETEASE_CDP_PORT}/json/version"; then
    notify "网易云 CDP 9222 不可用，已尝试自动重启。"
    kick netease-cdp
  fi

}

main "$@"
