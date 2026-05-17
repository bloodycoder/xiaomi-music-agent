#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$SCRIPT_DIR/env.sh"
uid="$(id -u)"
check_http() { local name="$1" url="$2"; if curl --noproxy '*' -fsS "$url" >/dev/null; then echo "✓ $name OK"; else echo "✗ $name FAIL ($url)"; fi; }
echo "LaunchAgents:"
for svc in xiaomusic music-agent netease-cdp; do
  label="com.${USER}.${svc}"
  state="$(launchctl print "gui/${uid}/${label}" 2>/dev/null | awk -F'= ' '/state =/{print $2; exit}' || true)"
  echo "- $label: ${state:-not loaded}"
done
echo "HTTP checks:"
check_http xiaomusic "http://127.0.0.1:${XIAOMUSIC_PORT}/"
check_http music-agent "http://127.0.0.1:${MUSIC_AGENT_PORT}/health"
check_http netease-cdp "http://127.0.0.1:${NETEASE_CDP_PORT}/json/version"
