#!/usr/bin/env bash
set -euo pipefail
uid="$(id -u)"
for svc in xiaomusic music-agent netease-cdp; do
  launchctl kickstart -k "gui/${uid}/com.${USER}.${svc}" || true
done
