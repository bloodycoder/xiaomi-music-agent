#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"
LABEL_PREFIX="com.${USER}"
PLIST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$XIAOMI_MUSIC_ROOT/runtime/launchd"
mkdir -p "$PLIST_DIR" "$LOG_DIR"

write_plist() {
  local label="$1" program="$2" out="$3" err="$4"
  cat > "$PLIST_DIR/${label}.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${label}</string>
  <key>ProgramArguments</key>
  <array><string>${program}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>WorkingDirectory</key><string>${XIAOMI_MUSIC_ROOT}</string>
  <key>StandardOutPath</key><string>${out}</string>
  <key>StandardErrorPath</key><string>${err}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>XIAOMI_MUSIC_ROOT</key><string>${XIAOMI_MUSIC_ROOT}</string>
    <key>XIAOMUSIC_PORT</key><string>${XIAOMUSIC_PORT}</string>
    <key>MUSIC_AGENT_PORT</key><string>${MUSIC_AGENT_PORT}</string>
    <key>NETEASE_CDP_PORT</key><string>${NETEASE_CDP_PORT}</string>
  </dict>
</dict>
</plist>
PLIST
}

write_plist "${LABEL_PREFIX}.xiaomusic" "$XIAOMI_MUSIC_ROOT/scripts/run_xiaomusic_service.sh" "$LOG_DIR/xiaomusic.out.log" "$LOG_DIR/xiaomusic.err.log"
write_plist "${LABEL_PREFIX}.music-agent" "$XIAOMI_MUSIC_ROOT/scripts/run_music_agent_service.sh" "$LOG_DIR/music-agent.out.log" "$LOG_DIR/music-agent.err.log"
write_plist "${LABEL_PREFIX}.netease-cdp" "$XIAOMI_MUSIC_ROOT/scripts/run_netease_cdp_service.sh" "$LOG_DIR/netease-cdp.out.log" "$LOG_DIR/netease-cdp.err.log"

uid="$(id -u)"
for svc in xiaomusic music-agent netease-cdp; do
  label="${LABEL_PREFIX}.${svc}"
  launchctl bootout "gui/${uid}/${label}" 2>/dev/null || true
  launchctl bootstrap "gui/${uid}" "$PLIST_DIR/${label}.plist" 2>/dev/null || true
  launchctl kickstart -k "gui/${uid}/${label}" || true
done

echo "LaunchAgents installed. Run: $XIAOMI_MUSIC_ROOT/scripts/status.sh"
