#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=env.sh
. "$SCRIPT_DIR/env.sh"

# Prefer a Python that already has the local semantic mapper dependencies
# (sentence-transformers) installed, so Music Agent can prewarm the playlist
# mapper at service startup.  Keep /usr/bin/python3 as a safe fallback.
PYTHON_BIN="${MUSIC_AGENT_PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
  if [ -d "$HOME/.pyenv/versions" ]; then
    while IFS= read -r candidate; do
      if "$candidate" - <<'PY' >/dev/null 2>&1
import sentence_transformers  # noqa: F401
PY
      then
        PYTHON_BIN="$candidate"
        break
      fi
    done < <(find "$HOME/.pyenv/versions" -path '*/bin/python3' -print | sort -V)
  fi
fi
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
echo "[run_music_agent_service] using python: $PYTHON_BIN"
exec "$PYTHON_BIN" "$XIAOMI_MUSIC_ROOT/scripts/music_agent.py"
