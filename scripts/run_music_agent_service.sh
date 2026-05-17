#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"
exec /usr/bin/python3 "$XIAOMI_MUSIC_ROOT/scripts/music_agent.py"
