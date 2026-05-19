#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json
import subprocess
import base64
import os
import re
import time
import threading
import difflib
import random
import shutil
import hashlib
import concurrent.futures
from pathlib import Path

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
NETEASE_DIR = ROOT / 'agents' / 'netease-music-mcp'
CLOUD_MCP_DIR = ROOT / 'agents' / 'cloud-music-mcp'
PLAYLISTS_FILE = ROOT / 'runtime' / 'playlists.json'
ALIASES_FILE = ROOT / 'runtime' / 'playlist_aliases.json'
QUEUE_FILE = ROOT / 'runtime' / 'active_playlist_queue.json'
CACHE_TRACKS_FILE = ROOT / 'runtime' / 'playlist_tracks_cache.json'
CACHE_URL_FILE = ROOT / 'runtime' / 'song_url_cache.json'
ARTIST_INDEX_FILE = ROOT / 'runtime' / 'playlist_artist_index.json'
EMBEDDINGS_FILE = ROOT / 'runtime' / 'playlist_embeddings.json'
ENV_FILE = ROOT / '.env.local'
YESPLAY_AGENT_DEFAULT_URL = 'http://127.0.0.1:27232/agent'
CACHE_TRACKS_TTL = 86400
# Netease direct audio URLs are signed and can expire quickly. Keep only a
# short cache; stale URLs cause ffplay HTTP 403 and silent playback failure.
URL_CACHE_TTL = 300
AUDIO_SWITCH_COOLDOWN = 1800

_last_audio_switch_time = 0
_last_audio_switch_device = ''
_embedding_disabled_until = 0



AUDIO_SWITCHER_SRC = ROOT / 'scripts' / 'set_audio_output.c'
AUDIO_SWITCHER_BIN = ROOT / 'runtime' / 'set_audio_output'
BLUETOOTH_CONNECT_SRC = ROOT / 'scripts' / 'connect_bluetooth_audio.m'
BLUETOOTH_CONNECT_BIN = ROOT / 'runtime' / 'connect_bluetooth_audio'
DEFAULT_AUDIO_OUTPUT_DEVICE = 'Xiaomi Sound-4567'


def preferred_audio_output_device():
    env = load_env()
    # MUSIC_OUTPUT_DEVICE can be set in .env.local if the Bluetooth name changes.
    return (env.get('MUSIC_OUTPUT_DEVICE') or os.environ.get('MUSIC_OUTPUT_DEVICE') or DEFAULT_AUDIO_OUTPUT_DEVICE).strip()


def preferred_bluetooth_device():
    env = load_env()
    # Defaults to the audio output device name. Set MUSIC_BLUETOOTH_DEVICE to a
    # Bluetooth address/name if the CoreAudio output name differs.
    return (env.get('MUSIC_BLUETOOTH_DEVICE') or os.environ.get('MUSIC_BLUETOOTH_DEVICE') or preferred_audio_output_device()).strip()


def ensure_audio_switcher_built():
    """Build the tiny CoreAudio helper on demand.

    We avoid Homebrew dependencies such as SwitchAudioSource so launchd can run
    this on a fresh Mac. The binary is kept in runtime/ and rebuilt if the C
    source changes.
    """
    if not AUDIO_SWITCHER_SRC.exists():
        return False, f'missing {AUDIO_SWITCHER_SRC}'
    try:
        if AUDIO_SWITCHER_BIN.exists() and AUDIO_SWITCHER_BIN.stat().st_mtime >= AUDIO_SWITCHER_SRC.stat().st_mtime:
            return True, str(AUDIO_SWITCHER_BIN)
        AUDIO_SWITCHER_BIN.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(
            [
                'cc', str(AUDIO_SWITCHER_SRC),
                '-framework', 'CoreAudio', '-framework', 'CoreFoundation',
                '-o', str(AUDIO_SWITCHER_BIN),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
        )
        if p.returncode != 0:
            return False, p.stdout.strip() or 'cc failed'
        return True, str(AUDIO_SWITCHER_BIN)
    except Exception as e:
        return False, str(e)



def ensure_bluetooth_connector_built():
    """Build Objective-C IOBluetooth helper on demand."""
    if not BLUETOOTH_CONNECT_SRC.exists():
        return False, f'missing {BLUETOOTH_CONNECT_SRC}'
    try:
        if BLUETOOTH_CONNECT_BIN.exists() and BLUETOOTH_CONNECT_BIN.stat().st_mtime >= BLUETOOTH_CONNECT_SRC.stat().st_mtime:
            return True, str(BLUETOOTH_CONNECT_BIN)
        BLUETOOTH_CONNECT_BIN.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(
            [
                'clang', '-fobjc-arc', str(BLUETOOTH_CONNECT_SRC),
                '-framework', 'Foundation', '-framework', 'IOBluetooth',
                '-o', str(BLUETOOTH_CONNECT_BIN),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=25,
        )
        if p.returncode != 0:
            return False, p.stdout.strip() or 'clang failed'
        return True, str(BLUETOOTH_CONNECT_BIN)
    except Exception as e:
        return False, str(e)


def ensure_bluetooth_connected():
    """Best-effort connect of the paired Bluetooth speaker before playback."""
    target = preferred_bluetooth_device()
    if not target:
        return {'ok': True, 'skipped': True, 'reason': 'empty MUSIC_BLUETOOTH_DEVICE'}
    ok, info = ensure_bluetooth_connector_built()
    if not ok:
        print(f'[music-agent] bluetooth connector unavailable: {info}', flush=True)
        return {'ok': False, 'device': target, 'error': info}
    try:
        p = subprocess.run(
            [str(BLUETOOTH_CONNECT_BIN), target],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=18,
        )
        out = (p.stdout or '').strip()
        if p.returncode != 0:
            print(f'[music-agent] bluetooth connect failed for {target!r}: {out}', flush=True)
        return {'ok': p.returncode == 0, 'device': target, 'output': out}
    except Exception as e:
        print(f'[music-agent] bluetooth connect error for {target!r}: {e}', flush=True)
        return {'ok': False, 'device': target, 'error': str(e)}

def ensure_preferred_audio_output():
    """Best-effort connect Bluetooth and switch macOS output before playback."""
    global _last_audio_switch_time, _last_audio_switch_device
    target = preferred_audio_output_device()
    now = time.time()
    if _last_audio_switch_device == target and (now - _last_audio_switch_time) < AUDIO_SWITCH_COOLDOWN:
        return {'ok': True, 'skipped': True, 'reason': f'already on {target} (switched {now - _last_audio_switch_time:.0f}s ago)', 'bluetooth': {'ok': True, 'skipped': True}}
    bluetooth = ensure_bluetooth_connected()
    if not target:
        return {'ok': True, 'skipped': True, 'reason': 'empty MUSIC_OUTPUT_DEVICE', 'bluetooth': bluetooth}
    ok, info = ensure_audio_switcher_built()
    if not ok:
        print(f'[music-agent] audio output switcher unavailable: {info}', flush=True)
        return {'ok': False, 'device': target, 'error': info, 'bluetooth': bluetooth}
    last_out = ''
    # After a Bluetooth connection succeeds, the A2DP/CoreAudio output device can
    # appear a few seconds later. Retry so playback does not race the audio
    # profile becoming available.
    for attempt in range(1, 7):
        try:
            p = subprocess.run(
                [str(AUDIO_SWITCHER_BIN), target],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=8,
            )
            out = (p.stdout or '').strip()
            last_out = out
            if p.returncode == 0:
                _last_audio_switch_time = time.time()
                _last_audio_switch_device = target
                return {'ok': True, 'device': target, 'output': out, 'bluetooth': bluetooth, 'attempts': attempt}
            if attempt < 6 and ('not found' in out.lower() or 'Available:' in out):
                time.sleep(1.0)
                continue
            print(f'[music-agent] audio output switch failed for {target!r}: {out}', flush=True)
            return {'ok': False, 'device': target, 'output': out, 'bluetooth': bluetooth, 'attempts': attempt}
        except Exception as e:
            last_out = str(e)
            if attempt < 6:
                time.sleep(1.0)
                continue
            print(f'[music-agent] audio output switch error for {target!r}: {e}', flush=True)
            return {'ok': False, 'device': target, 'error': str(e), 'bluetooth': bluetooth, 'attempts': attempt}
    return {'ok': False, 'device': target, 'output': last_out, 'bluetooth': bluetooth, 'attempts': 6}


def clear_native_queue():
    return {'ok': True, 'skipped': True, 'reason': 'mpv backend does not use native netease queue'}


def run_play_query(query, timeout=45):
    cleared = clear_native_queue()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        audio_future = executor.submit(ensure_preferred_audio_output)
        play_future = executor.submit(play_query_via_mpv, query, timeout=timeout)
        audio = audio_future.result()
        rc, out = play_future.result()
    if isinstance(audio, dict):
        audio = {**audio, 'cleared_queue': cleared}
    return rc, out, audio

MPV_SOCKET = ROOT / 'runtime' / 'mpv.sock'
MPV_STATE_FILE = ROOT / 'runtime' / 'mpv_state.json'
RUNTIME_DIR = ROOT / 'runtime'
FFMPEG_BIN = RUNTIME_DIR / 'ffmpeg'
FFPLAY_BIN = RUNTIME_DIR / 'ffplay'



def save_mpv_state(state):
    MPV_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    MPV_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def load_mpv_state():
    if not MPV_STATE_FILE.exists():
        return None
    try:
        return json.loads(MPV_STATE_FILE.read_text())
    except Exception:
        return None


def search_song_tracks(keyword, limit=10):
    keyword = (keyword or '').strip()
    if not keyword:
        return []
    script = f"""
import sys, json
sys.path.insert(0, '{CLOUD_MCP_DIR}/src')
from cloud_music_mcp.auth import load_session
load_session()
from pyncm import apis
kw = {keyword!r}
result = apis.cloudsearch.GetSearchResult(kw, stype=1, limit={int(limit)})
items=[]
if result.get('code') == 200 and result.get('result', {{}}).get('songs'):
    for t in result['result']['songs']:
        artists = '/'.join([a.get('name','') for a in t.get('ar', []) if a.get('name')])
        items.append({{'id': str(t.get('id')), 'name': t.get('name',''), 'artist': artists, 'duration_ms': int(t.get('dt') or 0)}})
print(json.dumps({{'success': True, 'tracks': items}}, ensure_ascii=False))
"""
    p = subprocess.run([PYTHON_VENV, '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    data = json.loads(p.stdout.strip())
    return data.get('tracks') or []


def get_song_url(song_id, force_refresh=False):
    cached = None if force_refresh else get_cached_url(song_id)
    if cached:
        return cached
    script = f"""
import sys, json
sys.path.insert(0, '{CLOUD_MCP_DIR}/src')
from cloud_music_mcp.auth import load_session
load_session()
from pyncm import apis
r = apis.track.GetTrackAudioV1([int('{song_id}')], level='standard')
url=''
if r.get('code') == 200 and r.get('data'):
    item = r['data'][0]
    url = item.get('url') or ''
print(json.dumps({{'success': bool(url), 'url': url, 'raw': r}}, ensure_ascii=False))
"""
    p = subprocess.run([PYTHON_VENV, '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    data = json.loads(p.stdout.strip())
    if not data.get('success'):
        raise RuntimeError(f'get song url failed for {song_id}: {p.stdout[:500]}')
    url = data['url']
    if url:
        cache_song_url(song_id, url)
    return url




def ensure_ffplay_available():
    if FFPLAY_BIN.exists():
        FFPLAY_BIN.chmod(0o755)
        return str(FFPLAY_BIN)
    url = 'https://evermeet.cx/ffmpeg/getrelease/ffplay/zip'
    zip_path = RUNTIME_DIR / 'ffplay.zip'
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request, zipfile
    urllib.request.urlretrieve(url, zip_path)
    with zipfile.ZipFile(zip_path, 'r') as zf:
        zf.extractall(RUNTIME_DIR)
    if FFPLAY_BIN.exists():
        FFPLAY_BIN.chmod(0o755)
        return str(FFPLAY_BIN)
    return None

def ensure_mpv_available():
    exe = shutil.which('mpv') if 'shutil' in globals() else __import__('shutil').which('mpv')
    return exe or ensure_ffplay_available()



def local_player_process_alive(pid):
    if not pid:
        return False
    try:
        # macOS: kill(pid, 0) still succeeds for zombies, so inspect ps STAT.
        p = subprocess.run(
            ['ps', '-p', str(int(pid)), '-o', 'stat='],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        if p.returncode != 0:
            return False
        stat = (p.stdout or '').strip()
        if not stat or stat.startswith('Z') or 'Z' in stat:
            return False
        return True
    except Exception:
        return False

def stop_mpv_process():
    state = load_mpv_state() or {}
    pid = state.get('pid')
    if pid:
        try:
            os.kill(int(pid), 15)
        except Exception:
            pass
    try:
        MPV_SOCKET.unlink()
    except FileNotFoundError:
        pass


def play_mpv_url(url, track, queue_meta):
    mpv = ensure_mpv_available()
    if not mpv:
        return 127, 'mpv/ffplay not installed'
    stop_mpv_process()
    MPV_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    is_ffplay = str(mpv).endswith('ffplay')
    log_path = RUNTIME_DIR / ('ffplay.log' if is_ffplay else 'mpv.log')
    log_file = open(log_path, 'ab')
    if is_ffplay:
        args = [mpv, '-nodisp', '-autoexit', '-loglevel', 'warning', url]
    else:
        args = [mpv, '--no-video', '--force-window=no', f'--input-ipc-server={MPV_SOCKET}', '--audio-display=no', url]
    try:
        p = subprocess.Popen(args, stdout=log_file, stderr=log_file)
    finally:
        try:
            log_file.close()
        except Exception:
            pass

    # Catch URL/decoder failures instead of reporting success with no audio.
    time.sleep(0.35)
    rc = p.poll()
    if rc is not None:
        tail = ''
        try:
            tail = log_path.read_text(errors='ignore')[-1200:]
        except Exception:
            pass
        return rc or 1, f"local player exited immediately rc={rc}; log_tail={tail}"

    state = {
        'pid': p.pid,
        'url': url,
        'track': track,
        'queue_meta': queue_meta,
        'started_at': time.time(),
        'paused': False,
        'backend': 'ffplay' if is_ffplay else 'mpv',
    }
    save_mpv_state(state)
    return 0, f"{'ffplay' if is_ffplay else 'mpv'} playing: {track.get('name')} - {track.get('artist')}"

def mpv_command(command):
    state = load_mpv_state() or {}
    if state.get('backend') == 'ffplay':
        return False, {'ok': False, 'error': 'ffplay backend has no ipc control'}
    pid = state.get('pid')
    if not pid:
        return False, {'ok': False, 'error': 'no mpv state'}
    if not Path(f'/proc/{pid}').exists() and not Path(f'/dev/fd/{pid}').exists():
        # best-effort on macOS, just continue to socket probe
        pass
    if not MPV_SOCKET.exists():
        return False, {'ok': False, 'error': 'mpv socket missing'}
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(2)
    s.connect(str(MPV_SOCKET))
    s.sendall((json.dumps(command) + '\n').encode('utf-8'))
    data = s.recv(65536).decode('utf-8', 'ignore').strip()
    s.close()
    lines = [x for x in data.splitlines() if x.strip()]
    parsed = json.loads(lines[-1]) if lines else {'ok': True}
    return True, parsed


def mpv_pause_toggle(pause=True):
    ok, data = mpv_command({'command': ['set_property', 'pause', bool(pause)]})
    if ok:
        state = load_mpv_state() or {}
        state['paused'] = bool(pause)
        save_mpv_state(state)
    return ok, data


def mpv_status():
    state = load_mpv_state() or {}
    if not state:
        return {'playing': False}
    paused = bool(state.get('paused'))
    alive = local_player_process_alive(state.get('pid'))
    t = state.get('track') or {}
    return {
        'playing': bool(alive and not paused),
        'alive': bool(alive),
        'paused': paused,
        'pid': state.get('pid'),
        'title': t.get('name',''),
        'artist': t.get('artist',''),
        'mode': f"Agent Queue ({state.get('backend','mpv')})",
        'source': state.get('backend','mpv'),
    }


def play_query_via_mpv(query, timeout=45):
    tracks = search_song_tracks(query, limit=10)
    if not tracks:
        return 1, f'no song found for query: {query}'
    track = tracks[0]
    tid = track.get('id')
    url = get_song_url(tid)
    queue_meta = {'type': 'single_search', 'query': query, 'tracks': [track], 'index': 0, 'managed_by': 'mpv'}
    save_queue(queue_meta)
    rc, out = play_mpv_url(url, track, queue_meta)
    if rc != 0 and tid and ('403' in str(out) or 'Forbidden' in str(out) or 'exited immediately' in str(out)):
        invalidate_cached_url(tid)
        try:
            fresh_url = get_song_url(tid, force_refresh=True)
            rc2, out2 = play_mpv_url(fresh_url, track, queue_meta)
            return rc2, f'{out}; retried with fresh url -> {out2}'
        except Exception as e:
            return rc, f'{out}; fresh url retry failed: {e}'
    return rc, out


PLAYLIST_KEYWORDS = [
    '我的歌单', '歌单', '推荐', '下雨', '睡觉', '睡前', '睡眠', '入睡', '工作', '运动',
    '放松', '舒缓', '轻松', '安静', '洗澡', '沐浴', '泡澡', '助眠', '怀旧', '写作', '游戏', '动漫', '动画',
    '钢琴', '钢琴曲', '口琴', '英语', '日漫',
    # ASR/users often say "Higher Brothers 的歌" meaning "play a collection
    # of this artist's songs", not a single song search result.
    '的歌', '一些歌', '几首歌', '多放几首', '合集', '精选', '热门',
]


def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


# ═══════════════════════════════════════════════════════════════
# Playlist embedding semantic index
# ═══════════════════════════════════════════════════════════════

def embedding_api_config():
    env = load_env()
    # Allow embeddings to use a different provider from chat. This is useful when
    # OPENAI_BASE_URL points to a chat-only service that returns 404 for /embeddings.
    base_url = (env.get('EMBEDDING_BASE_URL') or os.environ.get('EMBEDDING_BASE_URL') or env.get('OPENAI_BASE_URL') or '').strip()
    api_key = (env.get('EMBEDDING_API_KEY') or os.environ.get('EMBEDDING_API_KEY') or env.get('OPENAI_API_KEY') or '').strip()
    model = (env.get('EMBEDDING_MODEL_NAME') or os.environ.get('EMBEDDING_MODEL_NAME') or 'text-embedding-3-small').strip()
    return base_url, api_key, model


def embedding_model_name():
    return embedding_api_config()[2]


def playlist_embedding_threshold():
    env = load_env()
    raw = env.get('PLAYLIST_EMBEDDING_THRESHOLD') or os.environ.get('PLAYLIST_EMBEDDING_THRESHOLD') or '0.45'
    try:
        return float(raw)
    except Exception:
        return 0.45


def load_embedding_cache():
    if not EMBEDDINGS_FILE.exists():
        return {}
    try:
        return json.loads(EMBEDDINGS_FILE.read_text())
    except Exception:
        return {}


def save_embedding_cache(cache):
    EMBEDDINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMBEDDINGS_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def playlist_embeddings_signature(playlists):
    parts = []
    for pl in playlists or []:
        parts.append(f"{pl.get('id')}:{pl.get('name')}:{pl.get('count')}:{pl.get('is_mine')}")
    for path in (PLAYLISTS_FILE, ALIASES_FILE, CACHE_TRACKS_FILE):
        try:
            st = path.stat()
            parts.append(f'{path.name}:{int(st.st_mtime)}:{st.st_size}')
        except FileNotFoundError:
            parts.append(f'{path.name}:missing')
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()


def playlist_semantic_text(playlist):
    """Text embedded for one playlist: name + aliases + scene hints + cached tracks."""
    pid = str(playlist.get('id') or '')
    name = playlist.get('name') or ''
    chunks = [
        f"歌单名称：{name}",
        f"创建者：{playlist.get('creator') or ''}",
        '来源：我的歌单' if playlist.get('is_mine') else '来源：收藏歌单',
    ]

    # Existing curated aliases are valuable semantic descriptions.
    aliases = []
    for pl in load_playlist_aliases() or []:
        if str(pl.get('id')) == pid:
            aliases = [a.get('text', '') for a in (pl.get('aliases') or []) if a.get('text')]
            break
    if aliases:
        chunks.append('别名/可能叫法：' + '，'.join(aliases[:12]))

    # Reuse local hint table as semantic labels, but only for playlists whose
    # names are explicitly configured as preferred targets.
    name_norm = norm_text(name)
    hint_words = []
    for keywords, preferred_names in LOCAL_PLAYLIST_HINTS:
        if any(norm_text(pn) == name_norm for pn in preferred_names):
            hint_words.extend(keywords)
    if hint_words:
        chunks.append('适合场景/情绪：' + '，'.join(sorted(set(hint_words), key=hint_words.index)))

    tracks = get_cached_tracks(pid) or []
    if tracks:
        track_lines = []
        artist_counts = {}
        for t in tracks[:40]:
            artist = t.get('artist') or ''
            title = t.get('name') or ''
            if title or artist:
                track_lines.append(f"{title} - {artist}".strip(' -'))
            for a in artist.split('/'):
                a = a.strip()
                if a:
                    artist_counts[a] = artist_counts.get(a, 0) + 1
        if artist_counts:
            top_artists = [a for a, _ in sorted(artist_counts.items(), key=lambda x: x[1], reverse=True)[:10]]
            chunks.append('常见歌手：' + '，'.join(top_artists))
        if track_lines:
            chunks.append('代表歌曲：' + '；'.join(track_lines[:30]))

    return '\n'.join(x for x in chunks if x)


def fetch_embeddings(inputs, timeout=30):
    base_url, api_key, model = embedding_api_config()
    if not base_url or not api_key or not model:
        raise RuntimeError('missing embedding API config')
    import urllib.request
    body = json.dumps({'model': model, 'input': inputs}).encode('utf-8')
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    items = data.get('data') or []
    items.sort(key=lambda x: int(x.get('index', 0)))
    vectors = [x.get('embedding') for x in items]
    if len(vectors) != len(inputs) or any(not isinstance(v, list) for v in vectors):
        raise RuntimeError('embedding API returned invalid vector count')
    return vectors


def ensure_playlist_embedding_index(playlists):
    """Build/load local playlist embedding index. Fails soft to keep playback working."""
    if not playlists:
        return None
    model = embedding_model_name()
    sig = playlist_embeddings_signature(playlists)
    cache = load_embedding_cache()
    if cache.get('signature') == sig and cache.get('model') == model and cache.get('items'):
        return cache

    items = []
    for pl in playlists:
        pid = str(pl.get('id') or '')
        if not pid:
            continue
        items.append({
            'playlist_id': pid,
            'playlist_name': pl.get('name', ''),
            'is_mine': bool(pl.get('is_mine')),
            'count': int(pl.get('count') or 0),
            'text': playlist_semantic_text(pl),
        })
    if not items:
        return None

    # Batch all playlists; current playlist count is small enough for one call.
    vectors = fetch_embeddings([x['text'] for x in items], timeout=45)
    for item, vec in zip(items, vectors):
        item['embedding'] = vec

    cache = {
        'version': 1,
        'generated_at': time.time(),
        'model': model,
        'signature': sig,
        'threshold': playlist_embedding_threshold(),
        'items': items,
    }
    save_embedding_cache(cache)
    print(f'[music-agent] playlist embedding index built: {len(items)} playlists model={model}', flush=True)
    return cache


def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += float(x) * float(y)
        na += float(x) * float(x)
        nb += float(y) * float(y)
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


def embedding_match_playlist(query, playlists):
    """Semantic local playlist match. Return None when below threshold.

    Intended position in the pipeline:
      exact/alias fast match → embedding local match → online search/LLM.
    """
    global _embedding_disabled_until
    if not playlists:
        return None
    if time.time() < _embedding_disabled_until:
        return None
    try:
        index = ensure_playlist_embedding_index(playlists)
        if not index or not index.get('items'):
            return None
        q_vec = fetch_embeddings([f'用户想听的歌单/音乐场景：{query}'], timeout=20)[0]
        threshold = playlist_embedding_threshold()
        candidates = []
        for item in index.get('items') or []:
            score = cosine_similarity(q_vec, item.get('embedding'))
            # Tiny preference for own playlists and non-empty playlists.
            adjusted = score + (0.015 if item.get('is_mine') else 0.0) + (0.005 if int(item.get('count') or 0) > 0 else 0.0)
            candidates.append((adjusted, score, item))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (x[0], bool(x[2].get('is_mine')), int(x[2].get('count') or 0)), reverse=True)
        adjusted, raw_score, item = candidates[0]
        second = candidates[1][1] if len(candidates) > 1 else 0.0
        if raw_score < threshold:
            print(f'[music-agent] embedding playlist miss: best={item.get("playlist_name")} score={raw_score:.3f} threshold={threshold:.3f}', flush=True)
            return None
        return {
            'playlist_id': str(item['playlist_id']),
            'playlist_name': item.get('playlist_name', ''),
            'reason': f'embedding semantic match score={raw_score:.3f}, second={second:.3f}, threshold={threshold:.3f}',
            'score': int(raw_score * 1000),
            'embedding_score': raw_score,
            'is_mine': bool(item.get('is_mine')),
        }
    except Exception as e:
        # Avoid paying a timeout/404 on every voice command when the configured
        # provider is chat-only or temporarily down. The next query after the
        # cooldown will retry automatically.
        _embedding_disabled_until = time.time() + 600
        print(f'[music-agent] embedding playlist match unavailable; disabled for 600s: {e}', flush=True)
        return None


def run_node(args, timeout=45):
    p = subprocess.run(
        ['node', 'music.js', *args],
        cwd=str(NETEASE_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return p.returncode, p.stdout.strip()



def yesplay_agent_url():
    env = load_env()
    return (env.get('YESPLAY_AGENT_URL') or os.environ.get('YESPLAY_AGENT_URL') or YESPLAY_AGENT_DEFAULT_URL).rstrip('/')


def yesplay_request(method, path, payload=None, timeout=8):
    import urllib.request
    import urllib.error
    base = yesplay_agent_url()
    url = f'{base}/{path.lstrip("/")}'
    data = None
    headers = {'Content-Type': 'application/json'}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode('utf-8'))


def yesplay_health():
    try:
        data = yesplay_request('GET', 'health', timeout=2)
        return bool(data.get('ok')), data
    except Exception as e:
        return False, {'ok': False, 'error': str(e)}


def yesplay_play_track_ids(track_ids, source_id='agent', source_type='agent', shuffle=False):
    ok, health = yesplay_health()
    if not ok:
        return False, {'ok': False, 'health': health}
    data = yesplay_request('POST', 'playIds', {
        'ids': [int(x) for x in track_ids if str(x).isdigit()],
        'sourceId': source_id,
        'sourceType': source_type,
        'shuffle': bool(shuffle),
    }, timeout=12)
    return bool(data.get('ok')), data


def yesplay_control(action):
    ok, health = yesplay_health()
    if not ok:
        return False, {'ok': False, 'health': health}
    data = yesplay_request('POST', action, {}, timeout=6)
    return bool(data.get('ok')), data


def load_playlists():
    if not PLAYLISTS_FILE.exists():
        return None
    data = json.loads(PLAYLISTS_FILE.read_text())
    if data.get('success') and data.get('playlists'):
        return data['playlists']
    return None


# ═══════════════════════════════════════════════════════════════
# Cache system — avoids repeated Netease API calls
# ═══════════════════════════════════════════════════════════════

def load_tracks_cache():
    if not CACHE_TRACKS_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_TRACKS_FILE.read_text())
        if time.time() - data.get('updated_at', 0) < CACHE_TRACKS_TTL:
            return data.get('playlists', {})
    except Exception:
        pass
    return {}


def save_tracks_cache(cache):
    CACHE_TRACKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_TRACKS_FILE.write_text(json.dumps({
        'updated_at': time.time(),
        'playlists': cache,
    }, ensure_ascii=False, indent=2))


def get_cached_tracks(playlist_id):
    cache = load_tracks_cache()
    entry = cache.get(str(playlist_id))
    if entry:
        return entry.get('tracks')
    return None


def cache_playlist_tracks(playlist_id, name, tracks):
    cache = load_tracks_cache()
    cache[str(playlist_id)] = {'name': name, 'tracks': tracks, 'cached_at': time.time()}
    save_tracks_cache(cache)


def load_url_cache():
    if not CACHE_URL_FILE.exists():
        return {}
    try:
        data = json.loads(CACHE_URL_FILE.read_text())
        urls = data.get('urls', {})
        now = time.time()
        return {k: v for k, v in urls.items() if now - v.get('cached_at', 0) < URL_CACHE_TTL}
    except Exception:
        return {}


def save_url_cache(cache):
    CACHE_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_URL_FILE.write_text(json.dumps({
        'updated_at': time.time(),
        'urls': cache,
    }, ensure_ascii=False, indent=2))


def get_cached_url(song_id):
    cache = load_url_cache()
    entry = cache.get(str(song_id))
    if entry:
        return entry.get('url')
    return None


def cache_song_url(song_id, url):
    cache = load_url_cache()
    cache[str(song_id)] = {'url': url, 'cached_at': time.time()}
    save_url_cache(cache)


def invalidate_cached_url(song_id):
    cache = load_url_cache()
    if str(song_id) in cache:
        cache.pop(str(song_id), None)
        save_url_cache(cache)


# ═══════════════════════════════════════════════════════════════
# Artist → playlist reverse index
# ═══════════════════════════════════════════════════════════════

def build_artist_index():
    """Build reverse index: artist name → playlists dominated by that artist.

    Reads from the tracks cache, so it's fast (no API calls).
    Regenerated whenever the tracks cache is refreshed.
    """
    tracks_cache = load_tracks_cache()
    if not tracks_cache:
        return {}

    artist_map = {}
    for pid, entry in tracks_cache.items():
        tracks = entry.get('tracks') or []
        if len(tracks) < 3:
            continue
        name = entry.get('name', '')
        # Count tracks per artist
        artist_counts = {}
        for t in tracks:
            artist = (t.get('artist') or '').strip()
            if not artist:
                continue
            # Handle multi-artist tracks (split on /)
            for a in artist.split('/'):
                a = a.strip()
                if a:
                    artist_counts[a] = artist_counts.get(a, 0) + 1

        # Find dominant artists (>50% of tracks or >= 3 tracks)
        total = len(tracks)
        for artist, count in artist_counts.items():
            if count >= 3 and count / total >= 0.4:
                artist_key = norm_text(artist)
                if artist_key not in artist_map:
                    artist_map[artist_key] = []
                artist_map[artist_key].append({
                    'playlist_id': str(pid),
                    'playlist_name': name,
                    'artist': artist,
                    'track_count': total,
                    'artist_track_count': count,
                    'ratio': count / total,
                })

    # Save to disk
    ARTIST_INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARTIST_INDEX_FILE.write_text(json.dumps({
        'updated_at': time.time(),
        'artists': artist_map,
    }, ensure_ascii=False, indent=2))

    return artist_map


def load_artist_index():
    if not ARTIST_INDEX_FILE.exists():
        return {}
    try:
        data = json.loads(ARTIST_INDEX_FILE.read_text())
        return data.get('artists', {})
    except Exception:
        return {}


def match_artist_playlist(query):
    """Check if query contains an artist name that maps to a known playlist.

    Returns a match dict like fast_match_playlist, or None.
    """
    index = load_artist_index()
    if not index:
        return None

    # Extract potential artist name: strip fillers and playlist keywords
    artist_query = strip_query_fillers(query)
    if len(artist_query) < 2:
        return None

    # Direct lookup
    matches = index.get(artist_query)
    if not matches:
        # Try partial matching: query contained in artist key or vice versa
        for artist_key, pl_list in index.items():
            if artist_query in artist_key or (len(artist_query) >= 3 and artist_key in artist_query):
                matches = pl_list
                break
        if not matches:
            # Bigram fuzzy match against artist names
            best_sim = 0.35
            for artist_key, pl_list in index.items():
                sim = text_similarity(query, artist_key)
                if sim > best_sim:
                    best_sim = sim
                    matches = pl_list

    if not matches:
        return None

    # Prefer playlists with higher ratio and more tracks
    matches_sorted = sorted(matches, key=lambda x: (x['ratio'], x['track_count']), reverse=True)
    best = matches_sorted[0]
    weight = get_playlist_weight(best['playlist_id'])
    weight_bonus = int((weight - 1.0) * 200)

    return {
        'playlist_id': best['playlist_id'],
        'playlist_name': best['playlist_name'],
        'reason': f"artist index: {best['artist']} dominates {best['playlist_name']} ({best['artist_track_count']}/{best['track_count']} tracks, ratio={best['ratio']:.0%})",
        'score': 750 + weight_bonus,
        'is_mine': True,
    }


# ═══════════════════════════════════════════════════════════════
# Text similarity — character bigram + Jaccard for fuzzy matching
# ═══════════════════════════════════════════════════════════════

def char_bigrams(text):
    """Extract character bigrams from text for fuzzy similarity."""
    text = text.lower().strip()
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def text_similarity(query, target):
    """Combined character + bigram Jaccard similarity.

    Handles ASR errors: char-level catches substitutions,
    bigram-level catches insertions/deletions/reordering.
    """
    q = norm_text(query)
    t = norm_text(target)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0

    q_chars = set(q)
    t_chars = set(t)
    char_inter = q_chars & t_chars
    char_union = q_chars | t_chars
    char_sim = len(char_inter) / len(char_union) if char_union else 0

    q_bg = char_bigrams(q)
    t_bg = char_bigrams(t)
    bg_inter = q_bg & t_bg
    bg_union = q_bg | t_bg
    bg_sim = len(bg_inter) / len(bg_union) if bg_union else 0

    # Bigrams weighted higher for longer text
    if len(q) >= 3 and len(t) >= 3:
        return 0.3 * char_sim + 0.7 * bg_sim
    return 0.5 * char_sim + 0.5 * bg_sim


def get_playlist_weight(playlist_id):
    """Read preference weight from alias table. Default 1.0."""
    table = load_playlist_aliases()
    if table:
        for pl in table:
            if str(pl.get('id')) == str(playlist_id):
                return float(pl.get('preference_weight', 1.0))
    return 1.0


def prefetch_track_urls(queue, start_index, count=5):
    """Background prefetch song URLs for upcoming tracks in a queue."""
    tracks = queue.get('tracks') or []
    for i in range(start_index, min(start_index + count, len(tracks))):
        tid = str(tracks[i].get('id', ''))
        if tid and not get_cached_url(tid):
            try:
                url = get_song_url(tid)
                cache_song_url(tid, url)
            except Exception:
                pass


PYTHON_VENV = str(CLOUD_MCP_DIR / '.venv' / 'bin' / 'python3')




def norm_text(text):
    """Normalize Chinese/English text for fast local matching."""
    text = (text or '').lower()
    # Normalize a few common variants/typos seen in playlist names and speech ASR.
    text = text.replace('chuckberry', 'chunkberry').replace('chuck berry', 'chunkberry')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)


QUERY_FILLERS = [
    '智能播放', '智能音乐', '找一下', '找点', '找', '搜索', '播放', '放一下', '放点', '放首', '放', '听一下', '听点', '听',
    '音乐', '歌曲', '歌单', '的歌', '列表', '推荐', '推荐一个', '来点', '来一首', '给我', '帮我',
    '我的', '我想', '想要', '适合', '现在', '正在', '在', '一个', '一些', '一点', '的', '歌',
]


def strip_query_fillers(query):
    q = norm_text(query)
    # Remove longer fillers first. Keep content words such as 国语/助眠/运动.
    for word in sorted(QUERY_FILLERS, key=len, reverse=True):
        q = q.replace(norm_text(word), '')
    return q


LOCAL_PLAYLIST_HINTS = [
    # (query keywords, preferred playlist names in order)
    # Prefer real created/favorited playlists that exist in runtime/playlists.json.
    # Keep public online search as a last resort because NetEase public results can
    # be very noisy for scene words like "洗澡/舒缓/睡前".
    (['洗澡', '沐浴', '泡澡', '洗浴', '洗漱'], ['精致女孩的起床洗漱洗澡化妆bgm', '慵懒卧室——氛围感', '【古典音乐】安静 轻柔 放松 不再焦虑', '安静60分钟']),
    (['舒缓', '放松', '轻松', '安静', '解压', '不焦虑'], ['【古典音乐】安静 轻柔 放松 不再焦虑', '慵懒卧室——氛围感', '安静60分钟', '安静40分钟', '咖啡店音乐', '另类独立&CHILL歌单']),
    (['睡前', '睡觉前', '入睡', '失眠', '晚安'], ['【古典音乐】安静 轻柔 放松 不再焦虑', '雨 声（amsr睡眠用）', '「3D音效」雷雨助眠专用背景音乐', '慵懒卧室——氛围感', '安静60分钟', '雨声睡觉']),
    (['睡觉', '睡眠', '助眠'], ['雨 声（amsr睡眠用）', '「3D音效」雷雨助眠专用背景音乐', '【古典音乐】安静 轻柔 放松 不再焦虑', '雨声睡觉']),
    (['下雨', '雨天', '雨夜', '雨声', '雷雨'], ['雨 声（amsr睡眠用）', '「3D音效」雷雨助眠专用背景音乐', '雨声睡觉']),
    (['钢琴', '纯音乐', '古典'], ['【古典音乐】安静 轻柔 放松 不再焦虑', '世界经典古典音乐100首', '安静60分钟']),
    (['工作', '写作', '码字', '学习', '专注', '论文', '看书'], ['读论文/写代码/看书专用BGM', 'Lofi hiphop • 沉浸在惬意学习时光里', '氛围自习室｜学习工作专注歌单', '学习歌单‖极静轻音乐01', '学习时听']),
    (['冥想', '瑜伽', '打坐', '禅'], ['禅.静坐.打坐.冥想.瑜伽.空灵音乐', '冥想']),
    (['运动', '跑步', '健身', '锻炼'], ['健身歌单【精神氮泵】', '健身房听的说唱']),
    (['做饭', '烧饭', '煮饭', '烹饪', '厨房', 'cooking'], ['咖啡店音乐', '【情侣】适合做菜Do 的时候听的歌']),
    (['口琴'], ['口琴']),
    (['英语', '英文'], ['怀旧英语', '英文说唱']),
    (['怀旧', '老歌'], ['怀旧', '怀旧英语', '国语']),
    (['日漫', '动漫', '动画', '二次元', '番剧'], ['日漫']),
    (['游戏', '游戏音乐', '原声', 'ost'], ['VG', 'Persona5 -女神异闻录5']),
    (['高达', 'gundam'], ['Gundam Rock']),
    (['jojo'], ['JOJO']),
    (['特摄'], ['特摄']),
    (['罗大佑'], ['罗大佑']),
    (['小虎队'], ['小虎队']),
    (['国语', '中文', '华语'], ['国语']),
]


def _playlist_score_for_name(query_norm, query_core, playlist):
    name = playlist.get('name', '')
    name_norm = norm_text(name)
    if not name_norm:
        return 0, ''

    mine_bonus = 80 if playlist.get('is_mine') else 0

    # Strong path: user literally said a playlist name or a clear part of it.
    if query_core and query_core == name_norm:
        return 1200 + mine_bonus, 'exact playlist name'
    if name_norm in query_norm:
        return 1050 + mine_bonus, 'playlist name contained in query'
    if query_core and len(query_core) >= 2 and query_core in name_norm:
        return 850 + mine_bonus + min(len(query_core), 10), 'query contained in playlist name'
    if len(name_norm) >= 2 and name_norm in query_core:
        return 830 + mine_bonus + min(len(name_norm), 10), 'playlist name contained in stripped query'

    # English/fuzzy fallback for short ASR variations. Keep threshold high to avoid
    # accidentally turning normal song searches into playlist playback.
    if query_core and len(query_core) >= 4:
        ratio = difflib.SequenceMatcher(None, query_core, name_norm).ratio()
        if ratio >= 0.88:
            return int(760 * ratio) + mine_bonus, f'fuzzy playlist name {ratio:.2f}'

    return 0, ''


def fast_match_playlist(query, playlists):
    """Return a high-confidence local playlist match without calling LLM.

    Priority:
    1. Alias table (most reliable, manually curated).
    2. Exact/name-contained matches.
    3. Character-bigram Jaccard similarity (handles ASR errors).
    4. Curated local keyword hints for common intents.
    5. Preference weight bonus applied to all candidates.

    If confidence is low, return None so the slower online-search/LLM path runs.
    """
    alias_match = alias_table_match_playlist(query)
    if alias_match:
        return alias_match

    if not playlists:
        return None

    query_norm = norm_text(query)
    query_core = strip_query_fillers(query)

    # Artist → playlist reverse index: e.g. "jennie的歌单" → Ruby playlist.
    # Do not run the fuzzy artist matcher on broad scene/mood requests like
    # "适合洗澡舒缓的歌单"; that previously could win before semantic matching.
    scene_words = {norm_text(kw) for kws, _ in LOCAL_PLAYLIST_HINTS for kw in kws}
    has_scene_word = any(w and w in query_norm for w in scene_words)
    if not has_scene_word and (is_artist_collection_query(query) or '歌手' in query_norm or (query_core and len(query_core) <= 24)):
        artist_match = match_artist_playlist(query)
        if artist_match and artist_match['score'] >= 650:
            return artist_match

    candidates = []

    # Name matching + bigram similarity across all playlists.
    for pl in playlists:
        reasons = []
        best_score = 0

        # Literal name matching (strongest signal).
        name_score, name_reason = _playlist_score_for_name(query_norm, query_core, pl)
        if name_score:
            best_score = name_score
            reasons.append(name_reason)

        # Bigram/char Jaccard similarity — catches ASR errors like
        # "cake echo"→"kkecho" or "睡觉前"→"雨声睡觉".
        sim = text_similarity(query, pl.get('name', ''))
        if sim >= 0.35:
            bigram_score = int(sim * 800)
            if bigram_score > best_score:
                best_score = bigram_score
                reasons.append(f'bigram {sim:.2f}')
            elif bigram_score > 0:
                reasons.append(f'bigram {sim:.2f}')

        if best_score:
            candidates.append((best_score, '; '.join(reasons), pl))

    # Intent hints for common scenarios. Use both created and favorited
    # playlists, because the user's useful scene playlists are often favorites.
    by_norm_name = {norm_text(p.get('name', '')): p for p in playlists}
    for keywords, preferred_names in LOCAL_PLAYLIST_HINTS:
        hit_kw = next((kw for kw in keywords if norm_text(kw) in query_norm), None)
        if not hit_kw:
            continue
        for rank, pname in enumerate(preferred_names):
            pl = by_norm_name.get(norm_text(pname))
            if pl and int(pl.get('count') or 0) > 0:
                # Scene hints should beat weak bigram matches but not exact aliases.
                # Slightly prefer user's own playlists, but allow favorites.
                scene_score = 820 - rank * 22 + (25 if pl.get('is_mine') else 0)
                candidates.append((scene_score, f'scene keyword {hit_kw}->{pname}', pl))
                break

    if not candidates:
        return None

    # Apply playlist preference weight as score bonus.
    weighted = []
    for score, reason, pl in candidates:
        weight = get_playlist_weight(str(pl['id']))
        weight_bonus = int((weight - 1.0) * 200)
        weighted.append((score + weight_bonus, reason, pl, weight))

    weighted.sort(key=lambda x: (x[0], bool(x[3] > 1.0), bool(x[2].get('is_mine')), int(x[2].get('count') or 0)), reverse=True)
    score, reason, pl, weight = weighted[0]

    if score < 650:
        return None

    return {
        'playlist_id': str(pl['id']),
        'playlist_name': pl.get('name', ''),
        'reason': reason + (f' weight={weight:.1f}' if weight != 1.0 else ''),
        'score': score,
        'is_mine': bool(pl.get('is_mine')),
        # Scene/mood playlists are usually curated by order. Shuffling them often
        # starts from a random off-vibe track, which feels "驴头不对马嘴".
        'shuffle': not str(reason).startswith('scene keyword'),
    }


def load_playlist_aliases():
    if not ALIASES_FILE.exists():
        return None
    try:
        data = json.loads(ALIASES_FILE.read_text())
        return data.get('playlists') or []
    except Exception as e:
        print(f'[music-agent] failed to load playlist aliases: {e}', flush=True)
        return None


def _score_alias_against_query(query_norm, query_core, alias_norm, alias_weight):
    if not alias_norm:
        return 0, ''
    # Exact cleaned intent == alias is strongest.
    if query_core and query_core == alias_norm:
        return alias_weight + 360, 'alias exact'
    if query_norm == alias_norm:
        return alias_weight + 340, 'alias exact full query'
    # User said "播放 XXX 歌单": after normalization alias appears in full query.
    if len(alias_norm) >= 2 and alias_norm in query_norm:
        return alias_weight + 260 + min(len(alias_norm), 20), 'alias contained in query'
    # User said a shorter part, e.g. "夏亚" for "逆袭的夏亚".
    if query_core and len(query_core) >= 2 and query_core in alias_norm:
        return alias_weight + 160 + min(len(query_core), 20), 'query contained in alias'
    # ASR/fuzzy for roman tokens, e.g. k k echo vs kkecho, chuck/chunk.
    if query_core and len(query_core) >= 4 and re.search(r'[a-z0-9]', query_core + alias_norm):
        ratio = difflib.SequenceMatcher(None, query_core, alias_norm).ratio()
        if ratio >= 0.78:
            return int(alias_weight * ratio) + 80, f'alias fuzzy {ratio:.2f}'
    return 0, ''


def alias_table_match_playlist(query):
    """Weighted alias-table match. Returns None if confidence is low."""
    table = load_playlist_aliases()
    if not table:
        return None
    query_norm = norm_text(query)
    query_core = strip_query_fillers(query)
    candidates = []
    for pl in table:
        mine_bonus = 120 if pl.get('is_mine') else 0
        count_bonus = min(int(pl.get('count') or 0), 100) // 20
        for alias in pl.get('aliases') or []:
            alias_norm = norm_text(alias.get('text', ''))
            score, relation = _score_alias_against_query(
                query_norm, query_core, alias_norm, int(alias.get('weight') or 0)
            )
            if score:
                candidates.append((
                    score + mine_bonus + count_bonus,
                    relation,
                    alias,
                    pl,
                ))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, relation, alias, pl = candidates[0]

    # Confidence gate. Alias table is meant to be broad, so weak substring/fuzzy
    # hits should not hijack normal song searches.
    if score < 760:
        return None

    return {
        'playlist_id': str(pl['id']),
        'playlist_name': pl.get('name', ''),
        'reason': f"alias table: {alias.get('text')} ({alias.get('reason')}; {relation})",
        'score': score,
        'is_mine': bool(pl.get('is_mine')),
    }

def save_queue(queue):
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2))


def load_queue():
    if not QUEUE_FILE.exists():
        return None
    try:
        return json.loads(QUEUE_FILE.read_text())
    except Exception:
        return None


def fetch_playlist_tracks(playlist_id, force_refresh=False):
    if not force_refresh:
        cached = get_cached_tracks(playlist_id)
        if cached:
            return cached
    script = f"""
import sys, json
sys.path.insert(0, '{CLOUD_MCP_DIR}/src')
from cloud_music_mcp.auth import load_session
load_session()
from pyncm import apis
result = apis.playlist.GetPlaylistInfo(int('{playlist_id}'))
if result.get('code') == 200 and result.get('playlist', {{}}).get('tracks'):
    tracks=[]
    for t in result['playlist']['tracks']:
        artists = '/'.join([a.get('name','') for a in t.get('ar', []) if a.get('name')])
        tracks.append({{'id': str(t.get('id')), 'name': t.get('name',''), 'artist': artists, 'duration_ms': int(t.get('dt') or 0)}})
    name = result['playlist'].get('name', '')
    print(json.dumps({{'success': True, 'tracks': tracks, 'name': name}}, ensure_ascii=False))
else:
    print(json.dumps({{'success': False, 'raw': result}}, ensure_ascii=False))
"""
    p = subprocess.run([PYTHON_VENV, '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    data = json.loads(p.stdout.strip())
    if not data.get('success'):
        raise RuntimeError(f'fetch playlist tracks failed: {p.stdout[:500]}')
    tracks = data['tracks']
    name = data.get('name', '')
    cache_playlist_tracks(playlist_id, name, tracks)
    return tracks


def play_track_from_queue(queue, index):
    tracks = queue.get('tracks') or []
    if not tracks:
        raise RuntimeError('empty queue')
    index = index % len(tracks)
    t = tracks[index]
    tid = t.get('id')
    url = get_song_url(tid)
    rc, out = play_mpv_url(url, t, queue)

    # Signed Netease URLs can expire before our cache TTL. If ffplay/mpv exits
    # immediately or reports 403, invalidate the URL and retry once with a fresh
    # URL before giving up. This prevents the common “Agent says playing but no
    # sound” failure.
    if rc != 0 and tid and ('403' in str(out) or 'Forbidden' in str(out) or 'exited immediately' in str(out)):
        invalidate_cached_url(tid)
        try:
            fresh_url = get_song_url(tid, force_refresh=True)
            rc2, out2 = play_mpv_url(fresh_url, t, queue)
            out = f'{out}; retried with fresh url -> {out2}'
            rc = rc2
        except Exception as e:
            out = f'{out}; fresh url retry failed: {e}'

    queue['index'] = index
    queue['current'] = t
    queue['last_started_at'] = time.time()
    save_queue(queue)

    # Record queue_index in mpv_state so the player thread can always tell
    # which track this process is playing — needed to detect external /next /prev.
    state = load_mpv_state() or {}
    state['queue_index'] = index
    save_mpv_state(state)

    return rc, out, t


def try_play_tracks_with_yesplay(tracks, source_id='agent', source_type='agent', playlist_name='', shuffle=False):
    ids = [str(t.get('id')) for t in (tracks or []) if str(t.get('id') or '').isdigit()]
    if not ids:
        return False, 'no numeric track ids for YesPlayMusic'
    ok, data = yesplay_play_track_ids(ids, source_id=source_id, source_type=source_type, shuffle=shuffle)
    if not ok:
        return False, json.dumps(data, ensure_ascii=False)
    queue = {
        'type': source_type,
        'managed_by': 'yesplaymusic',
        'playlist_id': str(source_id),
        'playlist_name': playlist_name,
        'index': 0,
        'shuffle': bool(shuffle),
        'paused': False,
        'created_at': time.time(),
        'tracks': tracks,
        'yesplay_status': data.get('status'),
    }
    save_queue(queue)
    return True, json.dumps(data, ensure_ascii=False)


def try_play_playlist(playlist_id, playlist_name='', shuffle=True, prefer_native=False):
    """Play playlist through Agent-managed queue.

    Audio setup and track fetching run in parallel. First track plays as soon
    as its URL is available; remaining URLs are prefetched in the background.
    """
    cleared = clear_native_queue()
    if prefer_native:
        audio = ensure_preferred_audio_output()
        if isinstance(audio, dict):
            audio = {**audio, 'cleared_queue': cleared}
        rc, out = run_node(['playlist', str(playlist_id)], timeout=30)
        if rc == 0:
            try:
                QUEUE_FILE.unlink()
            except FileNotFoundError:
                pass
            return f'native-netease:playlist:{playlist_id}', f'native playlist queue; audio={audio}; shuffle requested; {out}'
        print(f'[music-agent] native playlist play failed, fallback to agent queue: {out}', flush=True)

    # Parallelize: audio setup and track fetch run concurrently.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        audio_future = executor.submit(ensure_preferred_audio_output)
        tracks_future = executor.submit(fetch_playlist_tracks, playlist_id)
        audio = audio_future.result()
        tracks = tracks_future.result()

    if isinstance(audio, dict):
        audio = {**audio, 'cleared_queue': cleared}

    if shuffle and len(tracks) > 1:
        random.shuffle(tracks)
    queue = {
        'type': 'playlist',
        'managed_by': 'ffplay',
        'playlist_id': str(playlist_id),
        'playlist_name': playlist_name,
        'index': 0,
        'shuffle': bool(shuffle),
        'paused': False,
        'created_at': time.time(),
        'tracks': tracks,
    }
    rc2, out2, t = play_track_from_queue(queue, 0)
    if rc2 != 0:
        raise RuntimeError(f'playback failed: {out2}')

    # Prefetch URLs for upcoming tracks in background.
    threading.Thread(target=prefetch_track_urls, args=(queue, 1, 5), daemon=True).start()

    # Start the sequential player thread (idempotent).  It will wait for the
    # currently-running ffplay process to finish, then advance to the next track.
    ensure_queue_player_running()

    mode = 'shuffled' if shuffle else 'ordered'
    return f'agent-queue:playlist:{playlist_id}', f"queued {len(tracks)} {mode} tracks; audio={audio}; now playing: {t.get('name')} - {t.get('artist')} | {out2}"


def search_online_playlists(keyword, limit=5):
    """Search public Netease playlists without using LLM."""
    keyword = (keyword or '').strip()
    if not keyword:
        return []
    script = f"""
import sys, json
sys.path.insert(0, '{CLOUD_MCP_DIR}/src')
from cloud_music_mcp.auth import load_session
load_session()
from pyncm import apis
kw = {keyword!r}
result = apis.cloudsearch.GetSearchResult(kw, stype=1000, limit={int(limit)})
items = []
if result.get('code') == 200 and result.get('result', {{}}).get('playlists'):
    for pl in result['result']['playlists']:
        items.append({{
            'id': str(pl.get('id')),
            'name': pl.get('name',''),
            'count': pl.get('trackCount') or pl.get('bookCount') or 0,
            'creator': (pl.get('creator') or {{}}).get('nickname',''),
            'is_mine': False,
        }})
print(json.dumps({{'success': True, 'playlists': items}}, ensure_ascii=False))
"""
    p = subprocess.run([PYTHON_VENV, '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=12)
    data = json.loads(p.stdout.strip())
    return data.get('playlists') or []


def fast_online_playlist_match(query):
    """Search online playlists as a no-LLM fallback when local playlists miss."""
    keyword = strip_query_fillers(query) or norm_text(query)
    if len(keyword) < 2:
        return None
    # Avoid searching meaningless generic words.
    if keyword in {'推荐', '歌单', '音乐', '歌曲'}:
        return None
    results = search_online_playlists(keyword, limit=5)
    if not results:
        return None
    # Prefer the first result from Netease search, but skip empty playlists.
    pl = next((x for x in results if int(x.get('count') or 0) > 0), results[0])
    return {
        'playlist_id': str(pl['id']),
        'playlist_name': pl.get('name', ''),
        'reason': f'online playlist search: {keyword}',
        'score': 500,
        'is_mine': False,
    }



def is_artist_collection_query(query):
    q = norm_text(query)
    return ('的歌' in q or '一些歌' in q or '几首歌' in q or '热门歌曲' in q or '精选歌曲' in q) and not any(x in q for x in ('一首歌', '这首歌', '那首歌'))


def extract_artist_collection_keyword(query):
    q = strip_query_fillers(query)
    # strip_query_fillers normalizes/removes spaces; if that becomes too short,
    # fall back to a lightly cleaned original string for English artist search.
    if len(q) >= 2:
        return q
    raw = re.sub(r'(?i)智能播放|智能音乐|播放|放|找|搜索|的歌|一些歌|几首歌|歌单|歌曲|音乐', ' ', query or '')
    raw = re.sub(r'\s+', ' ', raw).strip()
    return raw


def search_artist_tracks(keyword, limit=30):
    """Search songs and keep tracks whose artist matches the requested artist.

    This is more reliable than playing public playlists for requests like
    "Higher Brothers 的歌": public playlist pages often contain unrelated podcast
    or recommendation items, while song search gives structured artist fields.
    """
    keyword = (keyword or '').strip()
    if not keyword or len(norm_text(keyword)) < 2:
        return []
    script = f"""
import sys, json
sys.path.insert(0, '{CLOUD_MCP_DIR}/src')
from cloud_music_mcp.auth import load_session
load_session()
from pyncm import apis
kw = {keyword!r}
result = apis.cloudsearch.GetSearchResult(kw, stype=1, limit={int(limit)})
items = []
if result.get('code') == 200 and result.get('result', {{}}).get('songs'):
    for t in result['result']['songs']:
        artists = '/'.join([a.get('name','') for a in t.get('ar', []) if a.get('name')])
        artist_aliases = '/'.join(['/'.join(a.get('alias') or a.get('alia') or []) for a in t.get('ar', [])])
        items.append({{
            'id': str(t.get('id')),
            'name': t.get('name',''),
            'artist': artists,
            'artist_aliases': artist_aliases,
            'duration_ms': int(t.get('dt') or 0),
            'pop': float(t.get('pop') or 0),
        }})
print(json.dumps({{'success': True, 'tracks': items}}, ensure_ascii=False))
"""
    p = subprocess.run([PYTHON_VENV, '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    data = json.loads(p.stdout.strip())
    tracks = data.get('tracks') or []
    kw_norm = norm_text(keyword)
    matched = []
    seen = set()
    for t in tracks:
        artist_norm = norm_text((t.get('artist') or '') + ' ' + (t.get('artist_aliases') or ''))
        if not artist_norm:
            continue
        if kw_norm in artist_norm or artist_norm in kw_norm:
            tid = t.get('id') or (t.get('name'), t.get('artist'))
            if tid in seen:
                continue
            seen.add(tid)
            t.pop('artist_aliases', None)
            matched.append(t)
    # If strict artist matching finds too few tracks, use the top search results
    # but still return a managed queue rather than a single song.
    if len(matched) < 3:
        fallback = []
        for t in tracks:
            tid = t.get('id') or (t.get('name'), t.get('artist'))
            if tid in seen:
                continue
            seen.add(tid)
            t.pop('artist_aliases', None)
            fallback.append(t)
        matched.extend(fallback[: max(0, 10 - len(matched))])
    return matched


def try_play_artist_collection(query):
    keyword = extract_artist_collection_keyword(query)
    tracks = search_artist_tracks(keyword, limit=50)
    if not tracks:
        return None
    queue = {
        'type': 'artist_collection',
        'managed_by': 'ffplay',
        'artist_query': keyword,
        'playlist_name': f'{keyword} 的歌',
        'index': 0,
        'shuffle': False,
        'paused': False,
        'created_at': time.time(),
        'tracks': tracks,
    }
    rc, out, t = play_track_from_queue(queue, 0)
    if rc != 0:
        raise RuntimeError(f'artist collection play failed: {out}')

    # Prefetch upcoming URLs in background.
    threading.Thread(target=prefetch_track_urls, args=(queue, 1, 5), daemon=True).start()

    # Start the sequential player thread.
    ensure_queue_player_running()
    return {
        'ok': True,
        'action': 'playlist',
        'source': 'artist_collection',
        'playlist_id': f'artist:{keyword}',
        'playlist_name': f'{keyword} 的歌',
        'artist_query': keyword,
        'track_count': len(tracks),
        'track': t,
        'cdp_result': f'artist managed queue {len(tracks)} tracks; now playing: {t.get("name")} - {t.get("artist")} | {out}',
    }

def ask_llm(query, playlists):
    env = load_env()
    base_url = env.get('OPENAI_BASE_URL', '')
    api_key = env.get('OPENAI_API_KEY', '')
    model = env.get('MODEL_NAME', 'deepseek-v4-flash')

    if not base_url or not api_key:
        return None, 'missing API config'

    playlist_text = json.dumps(playlists, ensure_ascii=False, indent=2)

    prompt = f"""用户想听音乐：{query}

这是用户的网易云歌单列表：
{playlist_text}

请从中选择最合适的一个歌单。
只返回 JSON：
{{
  "type": "playlist",
  "playlist_id": "...",
  "playlist_name": "...",
  "reason": "..."
}}"""

    import urllib.request
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 1024,
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )
    resp = urllib.request.urlopen(req, timeout=30)
    data = json.loads(resp.read())
    content = data['choices'][0]['message']['content'].strip()
    return content, None



def ask_general_llm(query):
    """Ask the configured chat model for a short spoken answer."""
    env = load_env()
    base_url = env.get('OPENAI_BASE_URL', '')
    api_key = env.get('OPENAI_API_KEY', '')
    model = env.get('MODEL_NAME', 'deepseek-v4-flash')

    if not base_url or not api_key:
        return None, 'missing API config'

    now = time.strftime('%Y-%m-%d %H:%M:%S')
    system_prompt = (
        '你是运行在用户 Mac 上、通过 Xiaomi Sound 播报的中文语音助手。'
        '回答要适合直接朗读：自然、简短、不要 Markdown、不要列表太长。'
        '除非用户要求详细解释，否则控制在 120 个中文字以内。'
        '如果涉及不确定的实时信息，说明你可能需要联网或进一步确认。'
    )
    user_prompt = f'当前本地时间：{now}\n用户问题：{query}'

    import urllib.request
    body = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt},
        ],
        'temperature': 0.5,
        'max_tokens': 512,
    }).encode('utf-8')

    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=body,
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )
    resp = urllib.request.urlopen(req, timeout=45)
    data = json.loads(resp.read())
    content = data['choices'][0]['message']['content'].strip()
    # Keep output TTS-friendly if the model ignores the prompt.
    content = re.sub(r'```[\s\S]*?```', '', content).strip()
    content = re.sub(r'\s+', ' ', content).strip()
    if len(content) > 300:
        content = content[:300].rstrip() + '。'
    return content, None

def extract_json(text):
    # Try to extract JSON from markdown code blocks
    m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', text)
    if m:
        text = m.group(1).strip()
    # Find first { ... }
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        return m.group(0)
    # Try to repair truncated JSON
    text = text.strip()
    if text.startswith('{'):
        # Count unclosed braces
        missing = text.count('{') - text.count('}')
        if missing > 0:
            text = text.rstrip()
            # If last char is inside an unterminated string, close it
            if not text.endswith('"'):
                text += '"'
            text += '}' * missing
    return text


def validate_llm_result(parsed, playlists):
    if not isinstance(parsed, dict):
        return False, 'not a dict'
    pid = str(parsed.get('playlist_id', ''))
    if not pid:
        return False, 'missing playlist_id'
    valid_ids = {str(p['id']) for p in playlists}
    if pid not in valid_ids:
        return False, f'playlist_id {pid} not found in user playlists'
    return True, None


def should_use_playlist(query):
    q = norm_text(query)
    if any(norm_text(kw) in q for kw in PLAYLIST_KEYWORDS):
        return True
    # Artist-style requests such as "陈奕迅的歌" / "Higher Brothers 的歌"
    # should play a playlist/collection. Single-track requests like "一首歌"
    # are intentionally excluded.
    if q.endswith('歌') and not any(x in q for x in ('一首歌', '这首歌', '那首歌')) and len(q) >= 3:
        return True
    return False



# ═══════════════════════════════════════════════════════════════
# Sequential queue player (ffplay/mpv backend)
# ═══════════════════════════════════════════════════════════════
# ffplay has no IPC and no native playlist support — it plays one URL and exits.
# Instead of polling, we run a single daemon thread that starts ffplay, blocks on
# the child process via os.waitpid, and when the process exits naturally (track
# finished) advances to the next track.  External /next, /prev, /pause work by
# killing the current ffplay process and updating the queue state; the player
# thread wakes from waitpid, reads the new state, and resumes from the new index.
#
# Architecture:
#   _queue_player_loop()  — daemon thread, sole owner of sequential playback
#   ensure_queue_player_running() — idempotent launcher
#   /next, /prev handlers — kill ffplay, update index, restart via play_track_from_queue
#   /pause handler        — kill ffplay, set paused flag

_queue_player_started = False
_queue_player_lock = threading.Lock()


def ensure_queue_player_running():
    global _queue_player_started
    with _queue_player_lock:
        if _queue_player_started:
            return
        _queue_player_started = True
    threading.Thread(target=_queue_player_loop, daemon=True).start()
    print('[music-agent] queue player thread started', flush=True)


def _queue_player_loop():
    """Play tracks sequentially.  Block on each ffplay process; advance on exit."""
    while True:
        try:
            queue = load_queue()
            if not queue or not queue.get('tracks') or queue.get('managed_by') not in ('ffplay', 'mpv'):
                time.sleep(1)
                continue

            if queue.get('paused'):
                time.sleep(0.5)
                continue

            tracks = queue.get('tracks') or []

            # ── If a player process is already alive, wait for it ──
            state = load_mpv_state() or {}
            pid = state.get('pid')
            if pid and local_player_process_alive(pid):
                try:
                    os.waitpid(int(pid), 0)
                except ChildProcessError:
                    pass
                except Exception as e:
                    print(f'[music-agent] qplayer waitpid error: {e}', flush=True)

                # Reload state after process exit.  If /next or /prev changed the
                # index while we were waiting, keep the external value.  Otherwise
                # advance by one.  Also re-read mpv_state to catch a /pause that
                # killed the process — the paused flag may have been set between
                # the kill and queue save.
                state2 = load_mpv_state() or {}
                queue = load_queue()
                if not queue or queue.get('paused') or state2.get('paused'):
                    continue
                prev_index = state.get('queue_index')
                cur_index = queue.get('index', 0)
                if prev_index is not None and cur_index == prev_index:
                    queue['index'] = (cur_index + 1) % len(tracks)
                    save_queue(queue)
                continue

            # ── No process running — play the track at current index ──
            index = queue.get('index', 0) % len(tracks)
            track = tracks[index]
            tid = str(track.get('id', ''))

            try:
                url = get_song_url(tid)
            except Exception as e:
                print(f'[music-agent] qplayer skip track [{index}] {track.get("name")}: {e}', flush=True)
                queue['index'] = (index + 1) % len(tracks)
                save_queue(queue)
                continue

            rc, out = play_mpv_url(url, track, queue)
            if rc != 0 and tid and ('403' in str(out) or 'Forbidden' in str(out) or 'exited immediately' in str(out)):
                invalidate_cached_url(tid)
                try:
                    fresh_url = get_song_url(tid, force_refresh=True)
                    rc, out = play_mpv_url(fresh_url, track, queue)
                except Exception:
                    pass

            if rc != 0:
                print(f'[music-agent] qplayer play failed [{index}]: {out}', flush=True)
                queue['index'] = (index + 1) % len(tracks)
                save_queue(queue)
                continue

            # Record which queue index this process is playing so we can detect
            # external changes when waitpid returns.
            state = load_mpv_state() or {}
            state['queue_index'] = index
            save_mpv_state(state)

            queue['index'] = index
            queue['current'] = track
            queue['last_started_at'] = time.time()
            save_queue(queue)

            print(f'[music-agent] qplayer [{index + 1}/{len(tracks)}] {track.get("name")} - {track.get("artist")}', flush=True)

            # Prefetch upcoming URLs in background.
            threading.Thread(target=prefetch_track_urls, args=(queue, index + 1, 3), daemon=True).start()

            # Loop back to top — it will wait for this process and advance.

        except Exception as e:
            print(f'[music-agent] qplayer error: {e}', flush=True)
            time.sleep(2)


def queue_monitor_loop():
    """Keep NeteaseMusic aligned with the managed playlist queue.

    The native app may stop after a searched single track, or auto-advance into
    an old native queue. In either case, if we have an active Agent playlist and
    the user has not explicitly paused it, advance to the next Agent-queue track.
    """
    while True:
        try:
            queue = load_queue()
            if queue and queue.get('tracks') and queue.get('managed_by') == 'agent' and not queue.get('paused'):
                age = time.time() - float(queue.get('last_started_at', 0))
                if age >= 15:
                    rc, out = run_node(['status'], timeout=15)
                    if rc == 0:
                        st = json.loads(out)
                        current = queue.get('current') or {}
                        expected = (current.get('name') or '').strip().lower()
                        actual = (st.get('title') or '').strip().lower()
                        duration_sec = max(0, int(current.get('duration_ms') or 0) / 1000)
                        should_advance = False
                        why = ''
                        if st.get('playing') and expected and actual and expected not in actual and actual not in expected:
                            should_advance = True
                            why = f'unexpected native track actual={actual!r} expected={expected!r}'
                        elif not st.get('playing') and duration_sec > 0 and age >= duration_sec + 8:
                            should_advance = True
                            why = f'native stopped after expected duration age={age:.1f}s duration={duration_sec:.1f}s'
                        elif not st.get('playing') and duration_sec <= 0 and age >= 360:
                            should_advance = True
                            why = f'native stopped with unknown duration age={age:.1f}s'

                        if should_advance:
                            idx = int(queue.get('index', 0)) + 1
                            rc2, out2, track = play_track_from_queue(queue, idx)
                            print(f'[music-agent] queue monitor advanced: {why} -> {track}', flush=True)
        except Exception as e:
            print(f'[music-agent] queue monitor error: {e}', flush=True)
        time.sleep(5)

def refresh_all_caches():
    """Daily background refresh of all playlist track caches and artist index."""
    while True:
        try:
            playlists = load_playlists()
            refreshed = 0
            if playlists:
                for pl in playlists:
                    pid = str(pl.get('id', ''))
                    if not pid:
                        continue
                    existing = get_cached_tracks(pid)
                    if existing is None:
                        try:
                            tracks = fetch_playlist_tracks(pid, force_refresh=True)
                            refreshed += 1
                            print(f'[music-agent] cache seeded: {pl.get("name")} ({pid}) {len(tracks)} tracks', flush=True)
                        except Exception as e:
                            print(f'[music-agent] cache seed failed for {pl.get("name")} ({pid}): {e}', flush=True)
            # Rebuild artist index after any new cache data
            if refreshed:
                index = build_artist_index()
                artist_count = len(index)
                print(f'[music-agent] artist index rebuilt: {artist_count} artists mapped', flush=True)
        except Exception as e:
            print(f'[music-agent] cache refresh error: {e}', flush=True)
        time.sleep(CACHE_TRACKS_TTL)


class Handler(BaseHTTPRequestHandler):
    def json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        try:
            if u.path == '/health':
                self.json(200, {'ok': True})
                return
            if u.path == '/ask':
                q = (qs.get('q') or [''])[0].strip()
                if not q:
                    self.json(400, {'ok': False, 'error': 'missing q'})
                    return
                answer, err = ask_general_llm(q)
                if err:
                    self.json(500, {'ok': False, 'error': f'LLM error: {err}'})
                    return
                self.json(200, {'ok': True, 'q': q, 'answer': answer})
                return
            if u.path == '/play':
                q = (qs.get('q') or [''])[0].strip()
                if not q:
                    self.json(400, {'ok': False, 'error': 'missing q'})
                    return

                playlists = load_playlists()

                # Fast path: first try high-confidence local playlist matching.
                # This avoids the LLM latency for clear requests like "国语",
                # "助眠", "下雨睡觉的音乐", "写作音乐", etc.
                if playlists:
                    local_match = fast_match_playlist(q, playlists)
                    if local_match:
                        playlist_id = local_match['playlist_id']
                        playlist_name = local_match['playlist_name']
                        orpheus_url, cdp_result = try_play_playlist(playlist_id, playlist_name, shuffle=bool(local_match.get('shuffle', True)))
                        print(f'[music-agent] fast matched playlist: {playlist_name} ({playlist_id}) reason: {local_match["reason"]} score:{local_match["score"]} cdp: {cdp_result}', flush=True)
                        self.json(200, {
                            'ok': True,
                            'action': 'playlist',
                            'source': 'local_match',
                            'playlist_id': playlist_id,
                            'playlist_name': playlist_name,
                            'reason': local_match['reason'],
                            'score': local_match['score'],
                            'is_mine': local_match['is_mine'],
                            'shuffle': bool(local_match.get('shuffle', True)),
                            'orpheus_url': orpheus_url,
                            'cdp_result': cdp_result,
                        })
                        return

                # Semantic local playlist path: embed the user's created/favorited
                # playlists, then choose the nearest one. If it is still below
                # threshold, fall through to online playlist search / LLM.
                if playlists and should_use_playlist(q):
                    embed_match = embedding_match_playlist(q, playlists)
                    if embed_match:
                        playlist_id = embed_match['playlist_id']
                        playlist_name = embed_match['playlist_name']
                        orpheus_url, cdp_result = try_play_playlist(playlist_id, playlist_name)
                        print(f'[music-agent] embedding matched playlist: {playlist_name} ({playlist_id}) reason: {embed_match["reason"]} cdp: {cdp_result}', flush=True)
                        self.json(200, {
                            'ok': True,
                            'action': 'playlist',
                            'source': 'embedding_local',
                            'playlist_id': playlist_id,
                            'playlist_name': playlist_name,
                            'reason': embed_match['reason'],
                            'score': embed_match['score'],
                            'embedding_score': embed_match['embedding_score'],
                            'is_mine': embed_match['is_mine'],
                            'orpheus_url': orpheus_url,
                            'cdp_result': cdp_result,
                        })
                        return

                # Artist collection path for requests like "Higher Brothers 的歌".
                # Build our own queue from structured song search artist fields;
                # public playlists can contain unrelated podcast/recommendation items.
                if should_use_playlist(q) and is_artist_collection_query(q):
                    try:
                        artist_result = try_play_artist_collection(q)
                    except Exception as e:
                        artist_result = None
                        print(f'[music-agent] artist collection search failed for {q!r}: {e}', flush=True)
                    if artist_result:
                        self.json(200, artist_result)
                        return

                # No-LLM online playlist fallback. If user's local playlists do not
                # match, search public Netease playlists before spending time on LLM.
                if should_use_playlist(q):
                    try:
                        online_match = fast_online_playlist_match(q)
                    except Exception as e:
                        online_match = None
                        print(f'[music-agent] online playlist search failed for {q!r}: {e}', flush=True)
                    if online_match:
                        playlist_id = online_match['playlist_id']
                        playlist_name = online_match['playlist_name']
                        orpheus_url, cdp_result = try_play_playlist(playlist_id, playlist_name, prefer_native=False)
                        print(f'[music-agent] online matched playlist: {playlist_name} ({playlist_id}) reason: {online_match["reason"]} cdp: {cdp_result}', flush=True)
                        self.json(200, {
                            'ok': True,
                            'action': 'playlist',
                            'source': 'online_search',
                            'playlist_id': playlist_id,
                            'playlist_name': playlist_name,
                            'reason': online_match['reason'],
                            'orpheus_url': orpheus_url,
                            'cdp_result': cdp_result,
                        })
                        return

                    # Slowest semantic path: only use LLM if local + online search both
                    # could not confidently resolve a playlist-like request.
                    if not playlists:
                        self.json(404, {
                            'ok': False,
                            'action': 'playlist',
                            'q': q,
                            'playlist_only': True,
                            'error': 'query contains playlist intent, but no local playlists cached and online search found no playlist',
                        })
                        return

                    llm_raw, err = ask_llm(q, playlists)
                    if err:
                        self.json(502, {
                            'ok': False,
                            'action': 'playlist',
                            'q': q,
                            'playlist_only': True,
                            'error': f'query contains playlist intent; refusing single-track fallback after LLM error: {err}',
                        })
                        return

                    try:
                        parsed = json.loads(extract_json(llm_raw))
                    except json.JSONDecodeError as e:
                        self.json(502, {
                            'ok': False,
                            'action': 'playlist',
                            'q': q,
                            'playlist_only': True,
                            'error': f'query contains playlist intent; refusing single-track fallback because LLM returned invalid JSON: {e}',
                            'raw': llm_raw,
                        })
                        return

                    valid, reason = validate_llm_result(parsed, playlists)
                    if not valid:
                        self.json(502, {
                            'ok': False,
                            'action': 'playlist',
                            'q': q,
                            'playlist_only': True,
                            'error': f'query contains playlist intent; refusing single-track fallback because LLM result invalid: {reason}',
                            'parsed': parsed,
                        })
                        return

                    playlist_id = str(parsed['playlist_id'])
                    playlist_name = parsed.get('playlist_name', '')
                    llm_reason = parsed.get('reason', '')

                    orpheus_url, cdp_result = try_play_playlist(playlist_id, playlist_name)
                    print(f'[music-agent] LLM chose playlist: {playlist_name} ({playlist_id}) reason: {llm_reason} cdp: {cdp_result}', flush=True)
                    self.json(200, {
                        'ok': True,
                        'action': 'playlist',
                        'source': 'llm',
                        'playlist_id': playlist_id,
                        'playlist_name': playlist_name,
                        'reason': llm_reason,
                        'orpheus_url': orpheus_url,
                        'cdp_result': cdp_result,
                    })
                    return

                # Regular search-based play: clear any managed playlist queue.
                try:
                    QUEUE_FILE.unlink()
                except FileNotFoundError:
                    pass
                rc, out, audio = run_play_query(q)
                self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'action': 'play', 'q': q, 'output': out, 'audio_output': audio})
                return
            if u.path == '/status':
                data = mpv_status()
                self.json(200, {'ok': True, 'status': data})
                return
            if u.path in ['/pause', '/next', '/prev']:
                cmd = u.path.strip('/')
                if cmd in ('next', 'prev'):
                    queue = load_queue()
                    if queue and queue.get('tracks') and queue.get('managed_by') in ('agent', 'mpv', 'ffplay'):
                        queue['paused'] = False
                        step = 1 if cmd == 'next' else -1
                        idx = int(queue.get('index', 0)) + step
                        rc, out, track = play_track_from_queue(queue, idx)
                        self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'action': cmd, 'queue': True, 'track': track, 'output': out})
                        return
                audio = None
                if cmd == 'pause':
                    state = load_mpv_state() or {}
                    if state.get('backend') == 'ffplay':
                        pid = state.get('pid')
                        ok = False
                        if pid:
                            try:
                                os.kill(int(pid), 15)
                                ok = True
                            except Exception:
                                ok = False
                        state['paused'] = True
                        save_mpv_state(state)
                        queue = load_queue()
                        if queue and queue.get('tracks'):
                            queue['paused'] = True
                            queue['paused_at'] = time.time()
                            save_queue(queue)
                        self.json(200 if ok else 500, {'ok': ok, 'action': cmd, 'queue': True, 'player': 'ffplay', 'output': {'killed_pid': pid}})
                        return
                    ok, data = mpv_pause_toggle(True)
                    queue = load_queue()
                    if queue and queue.get('tracks'):
                        queue['paused'] = True
                        queue['paused_at'] = time.time()
                        save_queue(queue)
                    self.json(200 if ok else 500, {'ok': ok, 'action': cmd, 'queue': True, 'player': 'mpv', 'output': data})
                    return
                self.json(500, {'ok': False, 'action': cmd, 'error': 'unhandled control path'})
                return
            self.json(404, {'ok': False, 'error': 'not found'})
        except Exception as e:
            self.json(500, {'ok': False, 'error': str(e)})

    def log_message(self, fmt, *args):
        print('[music-agent]', self.address_string(), fmt % args, flush=True)


if __name__ == '__main__':
    threading.Thread(target=queue_monitor_loop, daemon=True).start()
    threading.Thread(target=refresh_all_caches, daemon=True).start()
    # Build artist index on startup if tracks cache already populated
    if CACHE_TRACKS_FILE.exists() and not ARTIST_INDEX_FILE.exists():
        def _build_index_startup():
            time.sleep(3)
            idx = build_artist_index()
            print(f'[music-agent] startup artist index built: {len(idx)} artists', flush=True)
        threading.Thread(target=_build_index_startup, daemon=True).start()
    port = int(os.environ.get('MUSIC_AGENT_PORT', '8765'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'music-agent listening on http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
