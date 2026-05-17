# Security

This project is designed to run locally on your Mac. It should not require you
to upload Xiaomi, Netease, or LLM credentials to any third-party service beyond
the services you explicitly configure.

## Never commit

- `.env.local` or any real API key
- Xiaomi account/password/cookie
- Netease `cookies.json` / `session.ncm`
- QR code files
- launchd logs and runtime logs
- `runtime/*` generated state
- `audit/*` snapshots
- `xiaomusic/conf/setting.json`

## Before pushing

Run:

```bash
bash scripts/security_check.sh
```

Also manually inspect:

```bash
git status --short
git ls-files | grep -Ei 'env|cookie|session|qrcode|runtime|audit|log'
```

Expected safe matches are only examples such as `.env.example` and docs.
