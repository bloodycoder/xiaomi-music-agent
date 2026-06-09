#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"

ROOT="$XIAOMI_MUSIC_ROOT"
ENV_FILE="$ROOT/.env.local"
AUDIO_SRC="$ROOT/scripts/set_audio_output.c"
AUDIO_BIN="$ROOT/runtime/set_audio_output"
BT_SRC="$ROOT/scripts/connect_bluetooth_audio.m"
BT_BIN="$ROOT/runtime/connect_bluetooth_audio"
DEFAULT_DEVICE="Xiaomi Sound-4567"
VOICE="${SPEAKER_ALERT_VOICE:-Tingting}"

load_env_value() {
  local key="$1" val=""
  if [ -f "$ENV_FILE" ]; then
    val="$(awk -F= -v k="$key" '$1==k {sub(/^[^=]*=/, ""); print; exit}' "$ENV_FILE" | sed 's/^ *//;s/ *$//' || true)"
  fi
  printf '%s' "$val"
}

OUTPUT_DEVICE="${MUSIC_OUTPUT_DEVICE:-$(load_env_value MUSIC_OUTPUT_DEVICE)}"
OUTPUT_DEVICE="${OUTPUT_DEVICE:-$DEFAULT_DEVICE}"
BT_DEVICE="${MUSIC_BLUETOOTH_DEVICE:-$(load_env_value MUSIC_BLUETOOTH_DEVICE)}"
BT_DEVICE="${BT_DEVICE:-$OUTPUT_DEVICE}"

build_helpers() {
  mkdir -p "$ROOT/runtime"
  if [ ! -x "$AUDIO_BIN" ] || [ "$AUDIO_SRC" -nt "$AUDIO_BIN" ]; then
    cc "$AUDIO_SRC" -framework CoreAudio -framework CoreFoundation -o "$AUDIO_BIN"
  fi
  if [ ! -x "$BT_BIN" ] || [ "$BT_SRC" -nt "$BT_BIN" ]; then
    clang -fobjc-arc "$BT_SRC" -framework Foundation -framework IOBluetooth -o "$BT_BIN"
  fi
}

current_output() {
  "$AUDIO_BIN" --list | awk -F '\t' '/default/ {print $2; exit}'
}

ensure_speaker_output() {
  build_helpers
  "$BT_BIN" "$BT_DEVICE" >/dev/null 2>&1 || true
  for _ in 1 2 3 4 5 6 7 8; do
    if [ "$(current_output || true)" = "$OUTPUT_DEVICE" ]; then
      return 0
    fi
    "$AUDIO_BIN" "$OUTPUT_DEVICE" >/dev/null 2>&1 || true
    sleep 1
  done
  [ "$(current_output || true)" = "$OUTPUT_DEVICE" ]
}

if [ "${1:-}" = "--check" ]; then
  ensure_speaker_output
  echo "speaker alert output ready: $OUTPUT_DEVICE"
  exit 0
fi

MSG="${*:-小米音乐出现故障，请检查。}"
if ensure_speaker_output; then
  # say 使用当前系统默认输出；上面已验证默认输出是小爱蓝牙音箱。
  /usr/bin/say -v "$VOICE" "$MSG"
else
  echo "speaker alert failed: output device unavailable: $OUTPUT_DEVICE" >&2
  exit 1
fi
