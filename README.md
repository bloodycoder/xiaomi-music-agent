# Xiaomi Music Agent

把小爱音箱变成 Mac 上的网易云音乐智能语音入口。

> 目标：说一句“小爱同学，智能音乐 kkecho”，Mac 自动控制网易云音乐客户端播放你的歌单，并优先走本地匹配，不烧 LLM token。

## Features

- 小爱音箱语音入口，基于 xiaomusic 自定义口令。
- 网易云音乐 macOS 客户端播放，基于 Chrome DevTools Protocol 控制。
- 本地歌单 alias 表：优先匹配自己的网易云歌单，支持别名/发音相似/场景词。
- 原生网易云播放队列：点击歌单页“播放全部”，并自动切到“随机播放”。
- LLM 只是兜底：本地 alias 和搜索命中时不调用大模型。
- 支持 `智能音乐`、`智能播放`、`智能下一首`、`智能上一首`、`智能暂停`、`智能问`。
- launchd 开机自启动。
- Key、cookie、session、日志全部本地保存，默认 `.gitignore` 防泄漏。

## Architecture

```text
小爱音箱
  ↓
xiaomusic 捕获“智能音乐 ...”
  ↓
Mac Music Agent 本地匹配/兜底语义
  ↓
NeteaseMusic CDP 控制网易云客户端
  ↓
Mac 音频输出到蓝牙音箱 / Xiaomi Sound / 其他输出设备
```

## Requirements

- macOS
- `/Applications/NeteaseMusic.app`
- Python 3.10+
- Node.js + npm
- Git
- 小米音箱 + 可用的 xiaomusic 账号配置

## Quick Start

```bash
git clone https://github.com/YOUR_NAME/xiaomi-music-agent.git
cd xiaomi-music-agent
bash scripts/install.sh
```

默认安装到：

```text
~/xiaomi-music
```

也可以指定：

```bash
XIAOMI_MUSIC_ROOT=/path/to/xiaomi-music bash scripts/install.sh
```

## Configure

复制并编辑：

```bash
cp .env.example ~/xiaomi-music/.env.local
```

LLM 是可选的。歌单 alias 命中、普通搜歌、网易云原生歌单播放都不需要 LLM。

参考 xiaomusic 配置：

```text
config/xiaomusic-setting.example.json
```

关键口令：

```json
{
  "智能播放": "exec#smartplay(\"{arg}\")",
  "智能音乐": "exec#smartplay(\"{arg}\")",
  "智能下一首": "exec#smartnext()",
  "智能上一首": "exec#smartprev()",
  "智能暂停": "exec#smartpause()",
  "智能问": "exec#smartask(\"{arg}\")"
}
```

## Start Services

```bash
~/xiaomi-music/scripts/deploy.sh
~/xiaomi-music/scripts/status.sh
```

会安装三个用户级 LaunchAgents：

```text
com.$USER.xiaomusic
com.$USER.music-agent
com.$USER.netease-cdp
```

## Netease Login and Playlist Alias Table

如果使用 `cloud-music-mcp`/pyncm 拉歌单，需要在本地扫码登录网易云账号。登录文件只保存在本机，默认不会提交。

同步当前网易云客户端侧边栏可见歌单：

```bash
node ~/xiaomi-music/scripts/sync_netease_client_playlists.js
python3 ~/xiaomi-music/scripts/build_playlist_aliases.py
```

alias 表位置：

```text
~/xiaomi-music/runtime/playlist_aliases.json
```

可以手动编辑，例如：

```json
{
  "text": "开开echo",
  "weight": 860,
  "reason": "manual asr alias"
}
```

## Voice Commands

```text
小爱同学，智能音乐 kkecho
小爱同学，智能音乐 下雨天睡觉听的歌单
小爱同学，智能播放 陈奕迅 十年
小爱同学，智能下一首
小爱同学，智能上一首
小爱同学，智能暂停
小爱同学，智能问 今天晚上吃什么
```

## Security Before Publishing

```bash
bash scripts/security_check.sh
```

不要提交：

- `.env.local`
- `cookies.json`
- `session.ncm`
- `runtime/*`
- `audit/*`
- `xiaomusic/conf/setting.json`
- logs / QR code / local account data

更多见：[`docs/SECURITY.md`](docs/SECURITY.md)

## Troubleshooting

### Netease CDP 不可用

```bash
open -a /Applications/NeteaseMusic.app --args --remote-debugging-port=9222
curl --noproxy '*' http://127.0.0.1:9222/json/version
```

### xiaomusic 捕获但不执行

检查 `active_cmd` 是否包含：

```text
智能播放,智能音乐,智能下一首,智能上一首,智能暂停,智能问
```

### 官方小爱回答抢话

插件会尽量 pause/stop 官方回答，但 xiaomusic 是轮询对话记录，无法从云端源头禁止官方回答。建议使用 `智能音乐` / `智能播放` 前缀。

## License

MIT. xiaomusic、NeteaseMusic、cloud-music-mcp 等第三方项目遵循其各自许可证。
