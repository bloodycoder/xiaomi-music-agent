# ter-music-rust 播放器后端

当前已把播放器主线从 `ffplay`/Python 队列推进，切到 `ter-music-rust` 的 headless playlist daemon。

## 当前架构

```text
Music Agent Python
  负责：语音意图、歌单匹配、拉取歌单 tracks、网易云单曲 URL resolver
  不再负责：歌单 index、自动下一首、next/prev 状态机、播放模式

ter-music-rust ter-agent-player
  负责：playlist queue、当前 index、自动下一首、next/prev、pause/resume、seek、play mode、status、音频播放
```

## 文件

```text
players/ter-agent-player/src/main.rs
players/ter-agent-player/target/release/ter-agent-player
scripts/music_agent.py
```

## IPC

Unix socket：

```text
runtime/ter_player.sock
```

Rust daemon 主播放命令：

```text
load_playlist
next
prev
pause
resume
seek
set_mode
stop
status
shutdown
```

`play_url` 在 Rust IPC 中仅保留为调试/测试命令；Music Agent 正式播放路径不再调用它。单曲播放也会包装成一首歌的 `load_playlist`。

## 播放模式

Rust 侧现在支持：

```text
single      单曲结束后停止
sequence    顺序播放到最后一首停止
loop_all    列表循环
repeat_one  单曲循环
shuffle     随机顺序播放并循环
```

HTTP 入口：

```bash
curl --noproxy '*' -sS 'http://127.0.0.1:8765/mode?mode=loop_all'
curl --noproxy '*' -sS 'http://127.0.0.1:8765/mode?mode=repeat_one'
curl --noproxy '*' -sS 'http://127.0.0.1:8765/mode?mode=shuffle'
```

## Seek

```bash
curl --noproxy '*' -sS 'http://127.0.0.1:8765/seek?seconds=30'
curl --noproxy '*' -sS 'http://127.0.0.1:8765/seek?ratio=0.5'
```

## URL resolver

Rust 播放器管理 playlist，但网易云直链仍由 Python 提供 resolver：

```text
GET /song_url?id=<netease_song_id>
```

Rust 在播放每首歌前按需调用 resolver，避免一次性把整张歌单所有临时 URL 传过去导致过期。

## Python 旧队列清理

`scripts/music_agent.py` 已移除旧的：

```text
active_playlist_queue.json 主链路
save_queue/load_queue/play_track_from_queue
prefetch_track_urls
ensure_queue_player_running/_queue_player_loop
queue_monitor_loop
```

现在 `/play` 的所有播放路径都统一成 playlist：

```text
普通单曲搜索
→ search_song_tracks
→ play_ter_playlist(load_playlist, tracks=[one_track], play_mode=single)

歌单/艺人合集
→ fetch/search tracks
→ play_ter_playlist(load_playlist, tracks=[...], play_mode=loop_all/shuffle)

Rust daemon 自己维护 index、播放模式和自动下一首。
```

## 验证结果

已验证：

```bash
curl --noproxy '*' -sS 'http://127.0.0.1:8765/play?q=下雨听的歌单'
curl --noproxy '*' -sS http://127.0.0.1:8765/status
curl --noproxy '*' -sS http://127.0.0.1:8765/next
curl --noproxy '*' -sS http://127.0.0.1:8765/prev
curl --noproxy '*' -sS http://127.0.0.1:8765/pause
curl --noproxy '*' -sS 'http://127.0.0.1:8765/seek?seconds=5'
```

`/status` 应看到：

```text
source = ter-music-rust
playlist_engine = true
track_count > 1
play_mode = loop_all/shuffle/repeat_one/...
```

另外用两个 0.45 秒本地 WAV 通过 Rust socket 直接加载测试歌单，已验证 Rust daemon 会在第一首结束后自动推进到第二首。

## 单曲也是 playlist

已验证直接调用单曲搜索路径：

```text
play_query_via_ter_playlist('周杰伦 稻香')
```

`/status` 返回：

```text
playlist_id = single:<song_id>
track_count = 1
play_mode = single
source = ter-music-rust
playlist_engine = true
```

## 2026-05-20 修复：Rust daemon 改为 ffplay 流式子进程

故障现象：最初 Rust daemon 用 `rodio` 先把整首歌下载到内存再播放，`load_playlist` 会阻塞几十秒到 120 秒；小爱侧 `smartplay` 90 秒超时后表现为“智能播放了但没声音”。同时 daemon 单线程同步处理 IPC，下载期间 `/status` / `/next` 也会卡住。

当前修复：保留 `ter-music-rust` 作为歌单/状态/next/prev daemon，但每首歌实际播放改为 spawn `runtime/ffplay` 流式播放 URL，不再整首下载后才开始。这样 `/play` 通常 1-2 秒返回，`/status` 继续显示 `source=ter-music-rust` 和 `playlist_engine=true`。

注意：`seek` 暂时返回 unsupported；`pause/resume` 通过 `SIGSTOP/SIGCONT` 控制 ffplay 子进程。

## 2026-05-20 修复：智能控制时避免 xiaomusic test-tone 抢声

故障现象：用户说“智能下一首”后听到持续“bi”声。日志显示不是 Rust/ffplay 在循环，而是 xiaomusic 自己的 `test-tone.wav` 1 秒本地测试音被下一首定时器反复播放。

修复：`smartplay/smartnext/smartprev/smartpause` 插件在调用 Music Agent 前只取消 xiaomusic 本地 next timer 和本地播放状态，不再让 xiaomusic 的本地 `test-tone` 队列继续跑；同时 `command_handler.py` 对智能播放/控制 exec 不再后台发送 Xiaomi cloud `player_pause/player_stop`，避免误伤 Mac→Xiaomi Sound 的蓝牙音频。`智能问/智能对话` 仍保留官方回答打断逻辑。
