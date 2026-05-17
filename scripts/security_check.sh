#!/usr/bin/env bash
set -euo pipefail

fail=0
say_err() { echo "ERROR: $*" >&2; fail=1; }

sensitive_regex='(^|/)(\.env\.local|cookies\.json|session\.ncm|login_qrcode\.png|qrcode\.json|setting\.json|xiaomusic\.log\.txt)$|(^|/)(runtime|audit|data|node_modules|\.venv)(/|$)'
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ "$f" = "runtime/.gitkeep" ]; then
      continue
    fi
    if [[ "$f" =~ $sensitive_regex ]]; then
      say_err "sensitive/generated file is tracked: $f"
    fi
  done < <(git ls-files)
fi

scan_paths=(README.md LICENSE .env.example .gitignore scripts plugins agents/netease-music-mcp config patches docs)
patterns=(
  "sk-[A-Za-z0-9_-]{20,}"
  "gho_[A-Za-z0-9_]{20,}"
  "Bearer[[:space:]]+[A-Za-z0-9._-]{20,}"
    "password[[:space:]]*=[[:space:]]*[^[:space:]\"']+"
  "cookie[[:space:]]*[:=][[:space:]]*[^[:space:]\"']{12,}"
)
for pat in "${patterns[@]}"; do
  if grep -RInE --exclude-dir=node_modules --exclude-dir=.git --exclude='package-lock.json' --exclude='security_check.sh' "$pat" "${scan_paths[@]}" >/tmp/xiaomi_music_secret_hits 2>/dev/null; then
    cat /tmp/xiaomi_music_secret_hits >&2
    say_err "possible secret pattern found: $pat"
  fi
done
rm -f /tmp/xiaomi_music_secret_hits

if grep -RInE --exclude-dir=node_modules --exclude-dir=.git --exclude='security_check.sh' '/Users/[^[:space:]]+' "${scan_paths[@]}" >/tmp/xiaomi_music_path_hits 2>/dev/null; then
  cat /tmp/xiaomi_music_path_hits >&2
  say_err "hardcoded local path found"
fi
rm -f /tmp/xiaomi_music_path_hits

if [ "$fail" -ne 0 ]; then
  echo "Security check failed." >&2
  exit 1
fi

echo "Security check OK."
