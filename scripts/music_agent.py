#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import json
import subprocess
import base64
import os
import re
import time
import sys
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
CACHE_TRACKS_FILE = ROOT / 'runtime' / 'playlist_tracks_cache.json'
CACHE_URL_FILE = ROOT / 'runtime' / 'song_url_cache.json'
ARTIST_INDEX_FILE = ROOT / 'runtime' / 'playlist_artist_index.json'
EMBEDDINGS_FILE = ROOT / 'runtime' / 'playlist_embeddings.json'
ENV_FILE = ROOT / '.env.local'
YESPLAY_AGENT_DEFAULT_URL = 'http://127.0.0.1:27232/agent'
MUSIC_AGENT_URL = os.environ.get('MUSIC_AGENT_URL') or f"http://127.0.0.1:{os.environ.get('MUSIC_AGENT_PORT', '8765')}"
CACHE_TRACKS_TTL = 86400
# Netease direct audio URLs are signed and can expire quickly. Keep only a
# short cache; stale URLs cause ffplay HTTP 403 and silent playback failure.
URL_CACHE_TTL = 300
AUDIO_SWITCH_COOLDOWN = 1800

_last_audio_switch_time = 0
_last_audio_switch_device = ''
_embedding_disabled_until = 0

# Hot-path caches.  Voice commands are latency sensitive; repeatedly parsing
# the same runtime JSON files and respawning Python just to hit pyncm adds
# avoidable overhead.  These caches are mtime/size guarded, so external edits to
# the cache files are still picked up without changing any matching semantics.
_json_file_cache = {}
_json_file_cache_lock = threading.Lock()
_env_cache = {'mtime': None, 'size': None, 'data': None}
_netease_worker = None
_netease_worker_lock = threading.Lock()
_netease_worker_io_lock = threading.Lock()
_semantic_mapper_warm = False
_semantic_mapper_warm_lock = threading.Lock()

try:
    _scripts_dir = str(ROOT / 'scripts')
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    from intent_filter import classify_music_intent
except Exception as _intent_import_error:
    classify_music_intent = None


def local_intent_classify(query):
    if classify_music_intent is None:
        return {'intent': 'unknown', 'binary': None, 'confidence': 0.0, 'scores': {}, 'error': 'intent_filter unavailable'}
    try:
        return classify_music_intent(query)
    except Exception as e:
        return {'intent': 'unknown', 'binary': None, 'confidence': 0.0, 'scores': {}, 'error': str(e)}


def _load_json_cached(path, default=None):
    path = Path(path)
    try:
        st = path.stat()
    except FileNotFoundError:
        return default
    key = str(path)
    with _json_file_cache_lock:
        hit = _json_file_cache.get(key)
        if hit and hit.get('mtime') == st.st_mtime and hit.get('size') == st.st_size:
            return hit.get('data')
    try:
        data = json.loads(path.read_text())
    except Exception:
        return default
    with _json_file_cache_lock:
        _json_file_cache[key] = {'mtime': st.st_mtime, 'size': st.st_size, 'data': data}
    return data


def _save_json_cached(path, data, **json_kwargs):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, **json_kwargs))
    try:
        st = path.stat()
        with _json_file_cache_lock:
            _json_file_cache[str(path)] = {'mtime': st.st_mtime, 'size': st.st_size, 'data': data}
    except Exception:
        pass


def _cloud_music_python():
    venv = CLOUD_MCP_DIR / '.venv' / 'bin' / 'python3'
    return str(venv if venv.exists() else Path(sys.executable))


def _start_netease_worker_locked():
    """Start a persistent pyncm worker used by search/url/playlist calls.

    The old implementation spawned a fresh Python interpreter for every API
    call.  Keeping pyncm/auth loaded in one worker preserves behavior while
    removing process-start and session-load cost from the playback hot path.
    """
    global _netease_worker
    if _netease_worker and _netease_worker.poll() is None:
        return _netease_worker

    worker_code = r'''
import json, sys, traceback
sys.path.insert(0, __CLOUD_SRC__)
from cloud_music_mcp.auth import load_session
from pyncm import apis
load_session()
print(json.dumps({"ok": True, "ready": True}), flush=True)

def _track_obj(t):
    artists = '/'.join([a.get('name','') for a in t.get('ar', []) if a.get('name')])
    return {'id': str(t.get('id')), 'name': t.get('name',''), 'artist': artists, 'duration_ms': int(t.get('dt') or 0)}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        cmd = req.get('cmd')
        if cmd == 'search_song':
            result = apis.cloudsearch.GetSearchResult(req.get('keyword') or '', stype=1, limit=int(req.get('limit') or 10))
            tracks = []
            if result.get('code') == 200 and result.get('result', {}).get('songs'):
                tracks = [_track_obj(t) for t in result['result']['songs']]
            resp = {'ok': True, 'tracks': tracks}
        elif cmd == 'song_url':
            song_id = str(req.get('song_id') or '')
            r = apis.track.GetTrackAudioV1([int(song_id)], level='standard')
            url = ''
            if r.get('code') == 200 and r.get('data'):
                url = r['data'][0].get('url') or ''
            resp = {'ok': bool(url), 'url': url, 'raw': r if not url else {'code': r.get('code')}}
        elif cmd == 'playlist_tracks':
            result = apis.playlist.GetPlaylistInfo(int(req.get('playlist_id')))
            if result.get('code') == 200 and result.get('playlist', {}).get('tracks'):
                tracks = [_track_obj(t) for t in result['playlist']['tracks']]
                resp = {'ok': True, 'tracks': tracks, 'name': result['playlist'].get('name', '')}
            else:
                resp = {'ok': False, 'raw': result}
        else:
            resp = {'ok': False, 'error': 'unknown cmd'}
    except Exception as e:
        resp = {'ok': False, 'error': str(e), 'traceback': traceback.format_exc(limit=3)}
    print(json.dumps(resp, ensure_ascii=False), flush=True)
'''.replace('__CLOUD_SRC__', repr(str(CLOUD_MCP_DIR / 'src')))

    p = subprocess.Popen(
        [_cloud_music_python(), '-u', '-c', worker_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    ready = p.stdout.readline().strip()
    try:
        data = json.loads(ready)
        if not data.get('ready'):
            raise RuntimeError(ready)
    except Exception as e:
        try:
            p.kill()
        except Exception:
            pass
        try:
            err = (p.stderr.read(1000) if p.stderr else '')
        except Exception:
            err = ''
        raise RuntimeError(f'netease worker failed to start: {ready} {err} {e}')
    _netease_worker = p
    print('[music-agent] pyncm worker started', flush=True)
    return p


def netease_worker_request(req, timeout=25):
    # Serialized line protocol: one worker, one in-flight request.  On failure we
    # drop the worker and callers fall back to the legacy subprocess path.
    global _netease_worker
    deadline = time.time() + timeout
    with _netease_worker_io_lock:
        try:
            with _netease_worker_lock:
                p = _start_netease_worker_locked()
            if p.stdin is None or p.stdout is None:
                raise RuntimeError('worker pipes unavailable')
            p.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
            p.stdin.flush()
            # readline() cannot be interrupted portably, so keep the worker calls
            # short and fall back if the child exits unexpectedly.
            line = p.stdout.readline()
            if not line:
                raise RuntimeError('worker closed stdout')
            data = json.loads(line)
            if time.time() > deadline:
                raise TimeoutError('worker request timeout')
            return data
        except Exception:
            with _netease_worker_lock:
                if _netease_worker:
                    try:
                        _netease_worker.kill()
                    except Exception:
                        pass
                    _netease_worker = None
            raise


def warm_netease_worker():
    try:
        netease_worker_request({'cmd': 'search_song', 'keyword': '陈奕迅 十年', 'limit': 1}, timeout=20)
    except Exception as e:
        print(f'[music-agent] pyncm worker warmup failed: {e}', flush=True)


def warm_semantic_playlist_mapper():
    """Preload semantic playlist mapper model/index at service startup.

    This does not change matching logic. It only shifts the SentenceTransformer
    load and semantic index init off the first user-visible request.
    """
    global _semantic_mapper_warm
    with _semantic_mapper_warm_lock:
        if _semantic_mapper_warm:
            return
        _semantic_mapper_warm = True
    try:
        import semantic_playlist_mapper as sem
        t0 = time.perf_counter()
        sem.predict('benchmark warmup', top_k=1, return_timing=True)
        print(f'[music-agent] semantic playlist mapper warmed in {(time.perf_counter() - t0) * 1000:.1f} ms', flush=True)
    except Exception as e:
        print(f'[music-agent] semantic playlist mapper warmup failed: {e}', flush=True)



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
        play_future = executor.submit(play_query_via_ter_playlist, query, timeout=timeout)
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
TER_PLAYER_DIR = ROOT / 'players' / 'ter-agent-player'
TER_PLAYER_BIN = TER_PLAYER_DIR / 'target' / 'release' / 'ter-agent-player'
TER_PLAYER_SOCKET = RUNTIME_DIR / 'ter_player.sock'
TER_PLAYER_LOG = RUNTIME_DIR / 'ter_player.log'



def save_mpv_state(state):
    _save_json_cached(MPV_STATE_FILE, state, ensure_ascii=False, indent=2)


def load_mpv_state():
    if not MPV_STATE_FILE.exists():
        return None
    try:
        return _load_json_cached(MPV_STATE_FILE, default=None) or json.loads(MPV_STATE_FILE.read_text())
    except Exception:
        return None


def search_song_tracks(keyword, limit=10):
    keyword = (keyword or '').strip()
    if not keyword:
        return []
    try:
        data = netease_worker_request({'cmd': 'search_song', 'keyword': keyword, 'limit': int(limit)}, timeout=20)
        if data.get('ok'):
            return data.get('tracks') or []
    except Exception as e:
        print(f'[music-agent] search worker fallback: {e}', flush=True)
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
    p = subprocess.run([_cloud_music_python(), '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    data = json.loads(p.stdout.strip())
    return data.get('tracks') or []


def get_song_url(song_id, force_refresh=False):
    cached = None if force_refresh else get_cached_url(song_id)
    if cached:
        return cached
    try:
        data = netease_worker_request({'cmd': 'song_url', 'song_id': str(song_id)}, timeout=20)
        if data.get('ok') and data.get('url'):
            url = data['url']
            cache_song_url(song_id, url)
            return url
    except Exception as e:
        print(f'[music-agent] url worker fallback for {song_id}: {e}', flush=True)
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
    p = subprocess.run([_cloud_music_python(), '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
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

def _preferred_player_backend():
    env = load_env()
    return (os.environ.get('MUSIC_PLAYER_BACKEND') or env.get('MUSIC_PLAYER_BACKEND') or 'ter').strip().lower()


def _ter_player_build_env():
    env = os.environ.copy()
    cargo_bin = str(Path.home() / '.cargo' / 'bin')
    env['PATH'] = f"{cargo_bin}:/opt/homebrew/bin:/usr/local/bin:" + env.get('PATH', '')
    return env


def ensure_ter_player_available(build_if_missing=True):
    if TER_PLAYER_BIN.exists():
        TER_PLAYER_BIN.chmod(0o755)
        return str(TER_PLAYER_BIN)
    if not build_if_missing:
        return None
    cargo = shutil.which('cargo') or str(Path.home() / '.cargo' / 'bin' / 'cargo')
    if not Path(cargo).exists() and shutil.which('cargo') is None:
        return None
    if not TER_PLAYER_DIR.exists():
        return None
    try:
        p = subprocess.run(
            [cargo, 'build', '--release', '--bin', 'agent_player'],
            cwd=str(TER_PLAYER_DIR),
            env=_ter_player_build_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        if p.returncode != 0:
            print(f'[music-agent] ter player build failed: {p.stdout[-2000:]}', flush=True)
            return None
    except Exception as e:
        print(f'[music-agent] ter player build error: {e}', flush=True)
        return None
    if TER_PLAYER_BIN.exists():
        TER_PLAYER_BIN.chmod(0o755)
        return str(TER_PLAYER_BIN)
    return None


def ensure_mpv_available():
    if _preferred_player_backend() in ('ter', 'ter-music-rust', 'rust'):
        ter = ensure_ter_player_available(build_if_missing=True)
        if ter:
            return ter
    exe = shutil.which('mpv') if 'shutil' in globals() else __import__('shutil').which('mpv')
    return exe or ensure_ffplay_available()



def ter_player_command(req, timeout=5):
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(str(TER_PLAYER_SOCKET))
    s.sendall((json.dumps(req, ensure_ascii=False) + '\n').encode('utf-8'))
    chunks = []
    while True:
        try:
            data = s.recv(65536)
        except socket.timeout:
            break
        if not data:
            break
        chunks.append(data)
        if b'\n' in data:
            break
    s.close()
    raw = b''.join(chunks).decode('utf-8', 'ignore').strip()
    lines = [x for x in raw.splitlines() if x.strip()]
    return json.loads(lines[-1]) if lines else {'ok': False, 'error': 'empty ter player response'}


def ensure_ter_player_running():
    bin_path = ensure_ter_player_available(build_if_missing=True)
    if not bin_path:
        return False, {'ok': False, 'error': 'ter-music-rust agent_player binary not available'}
    try:
        if TER_PLAYER_SOCKET.exists():
            resp = ter_player_command({'cmd': 'status'}, timeout=1)
            data = resp.get('data') or {}
            if resp.get('ok') and data.get('playlist_engine') is True:
                return True, resp
            # Socket exists but daemon did not answer with a healthy playlist
            # engine. Kill every matching daemon instead of only unlinking the
            # socket; otherwise an unlinked but still-running stale process can
            # keep holding audio/IPC state and make later /play requests hang.
            killed = kill_ter_player_processes(grace=0.35)
            if killed:
                print(f'[music-agent] killed stale ter player processes: {killed}', flush=True)
            time.sleep(0.2)
    except Exception as e:
        killed = kill_ter_player_processes(grace=0.35)
        if killed:
            print(f'[music-agent] killed unresponsive ter player processes after status error: {killed}; error={e}', flush=True)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(TER_PLAYER_LOG, 'ab')
    try:
        p = subprocess.Popen([bin_path, '--socket', str(TER_PLAYER_SOCKET), '--resolver', MUSIC_AGENT_URL], stdout=log_file, stderr=log_file, cwd=str(TER_PLAYER_DIR))
    finally:
        try:
            log_file.close()
        except Exception:
            pass
    for _ in range(30):
        time.sleep(0.1)
        if p.poll() is not None:
            tail = ''
            try:
                tail = TER_PLAYER_LOG.read_text(errors='ignore')[-1200:]
            except Exception:
                pass
            return False, {'ok': False, 'error': f'ter player exited rc={p.returncode}', 'log_tail': tail}
        try:
            resp = ter_player_command({'cmd': 'status'}, timeout=1)
            if resp.get('ok'):
                return True, {'ok': True, 'pid': p.pid, 'status': resp.get('data')}
        except Exception:
            pass
    return False, {'ok': False, 'error': 'ter player socket not ready', 'pid': p.pid}



def play_ter_playlist(playlist_id, playlist_name, tracks, start_index=0, shuffle=False, play_mode='loop_all'):
    # The Rust daemon now spawns ffplay children. If the daemon was killed or
    # wedged during earlier experiments, old ffplay children can be orphaned and
    # keep producing duplicate audio. Reap them before loading a fresh playlist.
    kill_local_player_processes(grace=0.15)
    ok, info = ensure_ter_player_running()
    if not ok:
        return 127, f"ter-music-rust unavailable: {info}"
    payload_tracks = []
    for t in tracks or []:
        payload_tracks.append({
            'id': str(t.get('id', '')),
            'name': t.get('name', ''),
            'artist': t.get('artist', ''),
            'url': t.get('url', ''),
            'duration_ms': t.get('duration_ms'),
        })
    req = {
        'cmd': 'load_playlist',
        'playlist_id': str(playlist_id),
        'playlist_name': playlist_name,
        'tracks': payload_tracks,
        'start_index': int(start_index or 0),
        'shuffle': bool(shuffle),
        'play_mode': play_mode,
        'volume': 0.85,
    }
    try:
        resp = ter_player_command(req, timeout=120)
    except Exception as e:
        return 1, f'ter-music-rust load_playlist error: {e}'
    if not resp.get('ok'):
        return 1, f"ter-music-rust load_playlist failed: {resp.get('error') or resp}"
    data = resp.get('data') or {}
    current = data.get('track') or (payload_tracks[start_index] if payload_tracks else {})
    state = {
        'pid': info.get('pid') or (load_mpv_state() or {}).get('pid'),
        'playlist_id': str(playlist_id),
        'playlist_name': playlist_name,
        'index': int(start_index or 0),
        'track': current,
        'started_at': time.time(),
        'paused': False,
        'backend': 'ter-music-rust',
    }
    save_mpv_state(state)
    return 0, f"ter-music-rust playlist loaded: {playlist_name or playlist_id} -> {current.get('name', '')} - {current.get('artist', '')}"


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

def list_local_player_pids():
    """Return PIDs of local lightweight players owned by this agent.

    ffplay has no IPC. If a previous play request races with queue-player
    supervision, orphan ffplay processes can keep playing. Treat the local
    player backend as single-instance and reap every ffplay/mpv process whose
    command points at our runtime ffplay binary or mpv IPC socket.
    """
    pids = []
    try:
        p = subprocess.run(
            ['ps', '-axo', 'pid=,command='],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        ffplay_bin = str(FFPLAY_BIN)
        socket_arg = f'--input-ipc-server={MPV_SOCKET}'
        for line in (p.stdout or '').splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_s, cmd = parts
            try:
                pid = int(pid_s)
            except Exception:
                continue
            if pid == os.getpid():
                continue
            if ffplay_bin in cmd or socket_arg in cmd:
                pids.append(pid)
    except Exception:
        pass
    # Include recorded PID even if ps filtering missed it. Do not include the
    # ter-music-rust daemon: it is a long-lived IPC player, not a one-song
    # child process like ffplay/mpv.
    try:
        state = load_mpv_state() or {}
        pid = state.get('pid')
        if pid and state.get('backend') != 'ter-music-rust':
            pid = int(pid)
            if pid not in pids:
                pids.append(pid)
    except Exception:
        pass
    return pids


def kill_local_player_processes(grace=0.35):
    pids = list_local_player_pids()
    killed = []
    for pid in pids:
        try:
            os.kill(int(pid), 15)
            killed.append(int(pid))
        except ProcessLookupError:
            pass
        except Exception:
            pass
    if killed and grace:
        time.sleep(grace)
        for pid in list(killed):
            if local_player_process_alive(pid):
                try:
                    os.kill(int(pid), 9)
                except Exception:
                    pass
    return killed


def list_ter_player_pids():
    pids = []
    try:
        p = subprocess.run(
            ['ps', '-axo', 'pid=,command='],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        bin_path = str(TER_PLAYER_BIN)
        sock_path = str(TER_PLAYER_SOCKET)
        for line in (p.stdout or '').splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_s, cmd = parts
            try:
                pid = int(pid_s)
            except Exception:
                continue
            if pid == os.getpid():
                continue
            if bin_path in cmd or ('ter-agent-player' in cmd and sock_path in cmd):
                pids.append(pid)
    except Exception:
        pass
    return pids


def kill_ter_player_processes(grace=0.35):
    pids = list_ter_player_pids()
    killed = []
    for pid in pids:
        try:
            os.kill(int(pid), 15)
            killed.append(int(pid))
        except ProcessLookupError:
            pass
        except Exception:
            pass
    if killed and grace:
        time.sleep(grace)
        for pid in list(killed):
            if local_player_process_alive(pid):
                try:
                    os.kill(int(pid), 9)
                except Exception:
                    pass
    try:
        TER_PLAYER_SOCKET.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return killed


def stop_mpv_process():
    state = load_mpv_state() or {}
    if state.get('backend') == 'ter-music-rust':
        try:
            ter_player_command({'cmd': 'stop'}, timeout=2)
        except Exception:
            pass
    kill_local_player_processes()
    try:
        MPV_SOCKET.unlink()
    except FileNotFoundError:
        pass


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
    state = load_mpv_state() or {}
    if state.get('backend') == 'ter-music-rust':
        try:
            data = ter_player_command({'cmd': 'pause' if pause else 'resume'}, timeout=3)
            ok = bool(data.get('ok'))
        except Exception as e:
            ok, data = False, {'ok': False, 'error': str(e)}
    else:
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
    if state.get('backend') == 'ter-music-rust':
        t = state.get('track') or {}
        try:
            resp = ter_player_command({'cmd': 'status'}, timeout=2)
            data = resp.get('data') or {}
            rt = data.get('track') or t
            return {
                'playing': bool(data.get('playing')),
                'alive': bool(data.get('alive')),
                'paused': bool(data.get('paused')),
                'finished': bool(data.get('finished')),
                'pid': state.get('pid'),
                'title': rt.get('name','') if isinstance(rt, dict) else t.get('name',''),
                'artist': rt.get('artist','') if isinstance(rt, dict) else t.get('artist',''),
                'elapsed': data.get('elapsed'),
                'duration': data.get('duration'),
                'mode': 'Agent Queue (ter-music-rust)',
                'source': 'ter-music-rust',
                'playlist_engine': bool(data.get('playlist_engine')),
                'playlist_id': data.get('playlist_id'),
                'playlist_name': data.get('playlist_name'),
                'index': data.get('index'),
                'track_count': data.get('track_count'),
                'play_mode': data.get('play_mode'),
                'order': data.get('order'),
            }
        except Exception as e:
            # The Rust playlist daemon is lazy-started by /play. Treat a missing
            # socket as idle rather than a service error.
            return {
                'playing': False,
                'alive': False,
                'paused': False,
                'source': 'ter-music-rust',
                'playlist_engine': True,
                'title': t.get('name',''),
                'artist': t.get('artist',''),
                'idle_reason': str(e),
            }
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


def play_query_via_ter_playlist(query, timeout=45):
    tracks = search_song_tracks(query, limit=10)
    if not tracks:
        return 1, f'no song found for query: {query}'
    track = tracks[0]
    # Even single-song playback is normalized into a one-item playlist so the
    # Rust player owns the same lifecycle for song and playlist playback.
    rc, out = play_ter_playlist(
        playlist_id=f'single:{track.get("id") or norm_text(query)}',
        playlist_name=track.get('name') or query,
        tracks=[track],
        start_index=0,
        shuffle=False,
        play_mode='single',
    )
    return rc, out


def is_confident_song_match(query, track):
    """Whether top search result clearly satisfies an explicit song request.

    Protects queries like “陈楚生的天外的天” / “周杰伦 稻香” from
    high-confidence playlist shortcuts. This intentionally requires title and
    artist evidence so generic scene/entity requests still go to playlist logic.
    """
    q = norm_text(query)
    title = norm_text((track or {}).get('name', ''))
    artist = norm_text((track or {}).get('artist', ''))
    if not q or not title:
        return False
    if any(x in q for x in ('歌单', '推荐', '适合', '来点', '一些歌', '几首歌', '的歌单')):
        return False
    # ASR often inserts 的 between artist and title: 陈楚生的天外的天.
    q_compact = q.replace('的', '')
    title_compact = title.replace('的', '')
    artist_names = [a for a in re.split(r'[/、,&，和]+', artist) if a]
    artist_hit = any(a and (a in q or a in q_compact) for a in artist_names)
    title_hit = title in q or title_compact in q_compact
    if artist_hit and title_hit:
        return True
    # If the full query is basically just the title, allow title-only exact-ish
    # song playback. Avoid very short names to prevent accidental matches.
    if len(title) >= 4 and (q == title or q_compact == title_compact):
        return True
    return False


def try_explicit_song_tracks(query):
    if should_use_playlist(query):
        return None
    try:
        tracks = search_song_tracks(query, limit=5)
    except Exception as e:
        print(f'[music-agent] explicit song search failed for {query!r}: {e}', flush=True)
        return None
    if tracks and is_confident_song_match(query, tracks[0]):
        return tracks
    return None


def play_explicit_song_tracks(query, tracks):
    track = tracks[0]
    rc, out = play_ter_playlist(
        playlist_id=f'single:{track.get("id") or norm_text(query)}',
        playlist_name=track.get('name') or query,
        tracks=[track],
        start_index=0,
        shuffle=False,
        play_mode='single',
    )
    return rc, out, track


PLAYLIST_KEYWORDS = [
    '我的歌单', '歌单', '推荐', '下雨', '睡觉', '睡前', '睡眠', '入睡', '工作', '运动',
    '放松', '舒缓', '轻松', '安静', '洗澡', '沐浴', '泡澡', '助眠', '怀旧', '写作', '游戏', '动漫', '动画',
    '钢琴', '钢琴曲', '口琴', '英语', '日漫',
    # ASR/users often say "Higher Brothers 的歌" meaning "play a collection
    # of this artist's songs", not a single song search result.
    '的歌', '一些歌', '几首歌', '多放几首', '合集', '精选', '热门',
]


def load_env():
    global _env_cache
    try:
        st = ENV_FILE.stat()
    except FileNotFoundError:
        return {}
    if _env_cache.get('mtime') == st.st_mtime and _env_cache.get('size') == st.st_size and _env_cache.get('data') is not None:
        return dict(_env_cache['data'])
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    _env_cache = {'mtime': st.st_mtime, 'size': st.st_size, 'data': dict(env)}
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
        return _load_json_cached(EMBEDDINGS_FILE, default=None) or json.loads(EMBEDDINGS_FILE.read_text())
    except Exception:
        return {}


def save_embedding_cache(cache):
    _save_json_cached(EMBEDDINGS_FILE, cache, ensure_ascii=False, indent=2)


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
    data = _load_json_cached(PLAYLISTS_FILE, default=None)
    if data is None:
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
        data = _load_json_cached(CACHE_TRACKS_FILE, default=None)
        if data is None:
            data = json.loads(CACHE_TRACKS_FILE.read_text())
        if time.time() - data.get('updated_at', 0) < CACHE_TRACKS_TTL:
            return data.get('playlists', {})
    except Exception:
        pass
    return {}


def save_tracks_cache(cache):
    _save_json_cached(CACHE_TRACKS_FILE, {
        'updated_at': time.time(),
        'playlists': cache,
    }, ensure_ascii=False, indent=2)


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
        data = _load_json_cached(CACHE_URL_FILE, default=None)
        if data is None:
            data = json.loads(CACHE_URL_FILE.read_text())
        urls = data.get('urls', {})
        now = time.time()
        return {k: v for k, v in urls.items() if now - v.get('cached_at', 0) < URL_CACHE_TTL}
    except Exception:
        return {}


def save_url_cache(cache):
    _save_json_cached(CACHE_URL_FILE, {
        'updated_at': time.time(),
        'urls': cache,
    }, ensure_ascii=False, indent=2)


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
    _save_json_cached(ARTIST_INDEX_FILE, {
        'updated_at': time.time(),
        'artists': artist_map,
    }, ensure_ascii=False, indent=2)

    return artist_map


def load_artist_index():
    if not ARTIST_INDEX_FILE.exists():
        return {}
    try:
        data = _load_json_cached(ARTIST_INDEX_FILE, default=None) or json.loads(ARTIST_INDEX_FILE.read_text())
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
        data = _load_json_cached(ALIASES_FILE, default=None) or json.loads(ALIASES_FILE.read_text())
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

def fetch_playlist_tracks(playlist_id, force_refresh=False):
    if not force_refresh:
        cached = get_cached_tracks(playlist_id)
        if cached:
            return cached
    try:
        data = netease_worker_request({'cmd': 'playlist_tracks', 'playlist_id': str(playlist_id)}, timeout=30)
        if data.get('ok'):
            tracks = data.get('tracks') or []
            name = data.get('name', '')
            cache_playlist_tracks(playlist_id, name, tracks)
            return tracks
    except Exception as e:
        print(f'[music-agent] playlist worker fallback for {playlist_id}: {e}', flush=True)
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
    p = subprocess.run([_cloud_music_python(), '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    data = json.loads(p.stdout.strip())
    if not data.get('success'):
        raise RuntimeError(f'fetch playlist tracks failed: {p.stdout[:500]}')
    tracks = data['tracks']
    name = data.get('name', '')
    cache_playlist_tracks(playlist_id, name, tracks)
    return tracks


def try_play_tracks_with_yesplay(tracks, source_id='agent', source_type='agent', playlist_name='', shuffle=False):
    ids = [str(t.get('id')) for t in (tracks or []) if str(t.get('id') or '').isdigit()]
    if not ids:
        return False, 'no numeric track ids for YesPlayMusic'
    ok, data = yesplay_play_track_ids(ids, source_id=source_id, source_type=source_type, shuffle=shuffle)
    if not ok:
        return False, json.dumps(data, ensure_ascii=False)
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

    rc2, out2 = play_ter_playlist(playlist_id, playlist_name, tracks, start_index=0, shuffle=shuffle, play_mode=('shuffle' if shuffle else 'loop_all'))
    if rc2 != 0:
        raise RuntimeError(f'playback failed: {out2}')

    mode = 'shuffled' if shuffle else 'ordered'
    first = tracks[0] if tracks else {}
    return f'agent-queue:playlist:{playlist_id}', f"queued {len(tracks)} {mode} tracks; audio={audio}; now playing: {first.get('name')} - {first.get('artist')} | {out2}"


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
    if any(x in q for x in ('一首歌', '这首歌', '那首歌')):
        return False
    # “适合周三的歌单” / “星期三听的歌单” are scene/day-of-week
    # playlist requests, not “artist 周三 的歌”. Only treat “的歌单” as
    # artist intent when no scene/listening modifier is present.
    if '歌单' in q and any(x in q for x in (
        '适合', '推荐', '时候', '的时候', '时听', '听的歌单', '想听',
        '今天', '今晚', '早上', '晚上', '上午', '下午', '下班', '上班',
        '周一', '周二', '周三', '周四', '周五', '周六', '周日',
        '星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日',
        '礼拜一', '礼拜二', '礼拜三', '礼拜四', '礼拜五', '礼拜六', '礼拜日',
    )):
        return False
    return ('的歌' in q or '一些歌' in q or '几首歌' in q or '热门歌曲' in q or '精选歌曲' in q)


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
    shuffle = False
    rc, out = play_ter_playlist(f'artist:{keyword}', f'{keyword} 的歌', tracks, start_index=0, shuffle=shuffle, play_mode='loop_all')
    if rc != 0:
        raise RuntimeError(f'artist collection play failed: {out}')
    first = tracks[0] if tracks else {}
    return {
        'ok': True,
        'action': 'playlist',
        'source': 'artist_collection',
        'playlist_id': f'artist:{keyword}',
        'playlist_name': f'{keyword} 的歌',
        'artist_query': keyword,
        'track_count': len(tracks),
        'track': first,
        'cdp_result': f'ter-music-rust artist playlist loaded: {len(tracks)} tracks; now playing: {first.get("name")} - {first.get("artist")} | {out}',
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

                intent = local_intent_classify(q)
                print(f'[music-agent] intent_filter q:{q!r} intent:{intent}', flush=True)

                playlists = load_playlists()

                # Explicit song requests must win over playlist shortcuts. Queries
                # like “陈楚生的天外的天” are not playlist intents even though
                # local artist-index playlist matching may find unrelated dominant
                # artist playlists.
                explicit_tracks = try_explicit_song_tracks(q)
                explicit_source = 'explicit_song'
                if (
                    not explicit_tracks
                    and intent.get('intent') == 'explicit_track'
                    and float(intent.get('confidence') or 0) >= 0.52
                    and not should_use_playlist(q)
                ):
                    try:
                        explicit_tracks = search_song_tracks(q, limit=5)
                        explicit_source = 'explicit_song_classifier'
                    except Exception as e:
                        explicit_tracks = None
                        print(f'[music-agent] intent explicit search failed q:{q!r}: {e}', flush=True)
                if explicit_tracks:
                    cleared = clear_native_queue()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                        audio_future = executor.submit(ensure_preferred_audio_output)
                        play_future = executor.submit(play_explicit_song_tracks, q, explicit_tracks)
                        audio = audio_future.result()
                        rc, out, track = play_future.result()
                    if isinstance(audio, dict):
                        audio = {**audio, 'cleared_queue': cleared}
                    print(f'[music-agent] explicit song matched: {track.get("name")} - {track.get("artist")} q:{q} result:{out}', flush=True)
                    self.json(200 if rc == 0 else 500, {
                        'ok': rc == 0,
                        'action': 'play',
                        'source': explicit_source,
                        'intent': intent,
                        'q': q,
                        'track': track,
                        'output': out,
                        'audio_output': audio,
                    })
                    return

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

                # Regular search-based play: Rust player receives a one-shot track.
                rc, out, audio = run_play_query(q)
                self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'action': 'play', 'q': q, 'output': out, 'audio_output': audio})
                return
            if u.path == '/song_url':
                sid = (qs.get('id') or qs.get('song_id') or [''])[0]
                if not sid:
                    self.json(400, {'ok': False, 'error': 'missing id'})
                    return
                try:
                    url = get_song_url(sid)
                    self.json(200, {'ok': True, 'id': sid, 'url': url})
                except Exception as e:
                    self.json(500, {'ok': False, 'id': sid, 'error': str(e)})
                return
            if u.path == '/status':
                data = mpv_status()
                self.json(200, {'ok': True, 'status': data})
                return
            if u.path in ['/pause', '/next', '/prev']:
                cmd = u.path.strip('/')
                state = load_mpv_state() or {}
                backend = state.get('backend') or 'ter-music-rust'
                if backend == 'ter-music-rust':
                    try:
                        req = {'cmd': 'pause' if cmd == 'pause' else ('next' if cmd == 'next' else 'prev')}
                        resp = ter_player_command(req, timeout=30)
                        ok = bool(resp.get('ok'))
                        self.json(200 if ok else 500, {'ok': ok, 'action': cmd, 'player': 'ter-music-rust', 'queue': True, 'output': resp.get('data') or resp})
                    except Exception as e:
                        self.json(500, {'ok': False, 'action': cmd, 'player': 'ter-music-rust', 'error': str(e)})
                    return
                if cmd == 'pause':
                    ok, data = mpv_pause_toggle(True)
                    self.json(200 if ok else 500, {'ok': ok, 'action': cmd, 'player': backend, 'output': data})
                    return
                self.json(409, {'ok': False, 'action': cmd, 'player': backend, 'error': 'next/prev require ter-music-rust playlist engine'})
                return
            if u.path == '/resume':
                try:
                    resp = ter_player_command({'cmd': 'resume'}, timeout=10)
                    ok = bool(resp.get('ok'))
                    self.json(200 if ok else 500, {'ok': ok, 'action': 'resume', 'player': 'ter-music-rust', 'output': resp.get('data') or resp})
                except Exception as e:
                    self.json(500, {'ok': False, 'action': 'resume', 'player': 'ter-music-rust', 'error': str(e)})
                return
            if u.path == '/seek':
                try:
                    seconds = (qs.get('seconds') or qs.get('s') or [None])[0]
                    ratio = (qs.get('ratio') or qs.get('r') or [None])[0]
                    req = {'cmd': 'seek'}
                    if seconds is not None:
                        req['seconds'] = float(seconds)
                    if ratio is not None:
                        req['ratio'] = float(ratio)
                    resp = ter_player_command(req, timeout=10)
                    ok = bool(resp.get('ok'))
                    self.json(200 if ok else 500, {'ok': ok, 'action': 'seek', 'player': 'ter-music-rust', 'output': resp.get('data') or resp})
                except Exception as e:
                    self.json(500, {'ok': False, 'action': 'seek', 'player': 'ter-music-rust', 'error': str(e)})
                return
            if u.path == '/mode':
                try:
                    mode = (qs.get('play_mode') or qs.get('mode') or ['loop_all'])[0]
                    resp = ter_player_command({'cmd': 'set_mode', 'play_mode': mode}, timeout=10)
                    ok = bool(resp.get('ok'))
                    self.json(200 if ok else 500, {'ok': ok, 'action': 'mode', 'player': 'ter-music-rust', 'output': resp.get('data') or resp})
                except Exception as e:
                    self.json(500, {'ok': False, 'action': 'mode', 'player': 'ter-music-rust', 'error': str(e)})
                return
            self.json(404, {'ok': False, 'error': 'not found'})
        except Exception as e:
            self.json(500, {'ok': False, 'error': str(e)})

    def log_message(self, fmt, *args):
        print('[music-agent]', self.address_string(), fmt % args, flush=True)


if __name__ == '__main__':
    threading.Thread(target=refresh_all_caches, daemon=True).start()
    threading.Thread(target=warm_netease_worker, daemon=True).start()
    threading.Thread(target=warm_semantic_playlist_mapper, daemon=True).start()
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
