#!/usr/bin/env bash
set -euo pipefail
uid="$(id -u)"
for svc in xiaomusic music-agent netease-cdp; do
  launchctl bootout "gui/${uid}/com.${USER}.${svc}" 2>/dev/null || true
done
