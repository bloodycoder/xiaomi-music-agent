# netease-music-mcp

MCP server to control [NeteaseMusic (网易云音乐)](https://music.163.com/) on macOS via Chrome DevTools Protocol.

No API keys. No login flow. Just controls the desktop app you already have open.

## What it does

| Tool | Description |
|------|-------------|
| `music_play` | Search and play a song by name |
| `music_pause` | Toggle play / pause |
| `music_next` | Next track |
| `music_prev` | Previous track |
| `music_status` | Now playing info (title, artist, cover, play mode) |

## How it works

NeteaseMusic's macOS desktop app is built on Electron. When launched with `--remote-debugging-port=9222`, it exposes a Chrome DevTools Protocol endpoint. This server connects over CDP and manipulates the app's DOM — typing into the search box, clicking play/pause/next/prev buttons, reading the player bar.

This means:
- **Your account, your audio quality** — no re-authentication needed
- **VIP benefits work** — whatever your subscription includes
- **Real app control** — not a separate player or web stream

## Setup

### 1. Launch NeteaseMusic with CDP enabled

```bash
open -a NeteaseMusic --args --remote-debugging-port=9222
```

> **Tip:** To always launch with CDP, create an alias:
> ```bash
> alias netease='open -a NeteaseMusic --args --remote-debugging-port=9222'
> ```

### 2. Install

```bash
git clone https://github.com/1nwooozip/netease-music-mcp.git
cd netease-music-mcp
npm install
```

### 3. Configure in Claude Code

Add to your `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "netease-music": {
      "command": "node",
      "args": ["/path/to/netease-music-mcp/server.js"]
    }
  }
}
```

Or for Claude Desktop, add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "netease-music": {
      "command": "node",
      "args": ["/path/to/netease-music-mcp/server.js"]
    }
  }
}
```

## CLI

Also works as a standalone CLI:

```bash
node music.js play "富士山下 陈奕迅"
node music.js status
node music.js pause
node music.js next
node music.js prev
```

## Requirements

- macOS
- NeteaseMusic desktop app
- Node.js >= 18

## License

MIT
