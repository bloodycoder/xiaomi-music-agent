#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"
APP="${NETEASE_APP:-/Applications/NeteaseMusic.app}"
PORT="$NETEASE_CDP_PORT"

wait_for_cdp() {
  for _ in {1..20}; do
    if /usr/bin/curl --noproxy '*' -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

start_netease_cdp() {
  if /usr/bin/curl --noproxy '*' -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
    return 0
  fi
  /usr/bin/osascript -e 'tell application "NeteaseMusic" to quit' >/dev/null 2>&1 || true
  sleep 2
  /usr/bin/open -a "$APP" --args --remote-debugging-port=${PORT}
  wait_for_cdp || true
}

start_netease_cdp
while true; do
  if ! /usr/bin/curl --noproxy '*' -fsS "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
    start_netease_cdp
  fi
  sleep 60
done
