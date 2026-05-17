# Troubleshooting

## `/health` fails

Run `~/xiaomi-music/scripts/status.sh` and inspect `~/xiaomi-music/runtime/launchd/` logs.

## Netease CDP fails

Start manually:

```bash
open -a /Applications/NeteaseMusic.app --args --remote-debugging-port=9222
```

## Voice command not matched

Ensure xiaomusic `active_cmd` contains the smart commands and the `{arg}` patch
has been applied.
