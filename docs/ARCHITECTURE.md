# Architecture

```text
Xiaomi Sound / XiaoAI
  -> xiaomusic custom command plugins
  -> Music Agent HTTP server on 127.0.0.1:8765
  -> NeteaseMusic macOS desktop app over CDP :9222
  -> macOS selected audio output
```

Music Agent first uses local deterministic matching:

1. `runtime/playlist_aliases.json` weighted alias table.
2. Exact/fuzzy playlist name matching.
3. Netease public playlist search.
4. Optional LLM fallback.

Native playlist playback is preferred: CDP clicks the page-level `播放全部`
button and sets random play mode. Agent-managed queues are fallback only.
