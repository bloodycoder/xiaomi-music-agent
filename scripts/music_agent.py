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
from pathlib import Path

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
NETEASE_DIR = ROOT / 'agents' / 'netease-music-mcp'
CLOUD_MCP_DIR = ROOT / 'agents' / 'cloud-music-mcp'
PLAYLISTS_FILE = ROOT / 'runtime' / 'playlists.json'
ALIASES_FILE = ROOT / 'runtime' / 'playlist_aliases.json'
QUEUE_FILE = ROOT / 'runtime' / 'active_playlist_queue.json'
ENV_FILE = ROOT / '.env.local'


PLAYLIST_KEYWORDS = [
    '我的歌单', '歌单', '推荐', '下雨', '睡觉', '工作', '运动',
    '放松', '助眠', '怀旧', '写作', '游戏', '动漫', '动画',
    '钢琴', '钢琴曲', '口琴', '英语', '日漫',
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


def load_playlists():
    if not PLAYLISTS_FILE.exists():
        return None
    data = json.loads(PLAYLISTS_FILE.read_text())
    if data.get('success') and data.get('playlists'):
        return data['playlists']
    return None


PYTHON_VENV = str(CLOUD_MCP_DIR / '.venv' / 'bin' / 'python3')




def norm_text(text):
    """Normalize Chinese/English text for fast local matching."""
    text = (text or '').lower()
    # Normalize a few common variants/typos seen in playlist names and speech ASR.
    text = text.replace('chuckberry', 'chunkberry').replace('chuck berry', 'chunkberry')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)


QUERY_FILLERS = [
    '智能播放', '智能音乐', '播放', '放一下', '放点', '放首', '放', '听一下', '听点', '听',
    '音乐', '歌曲', '歌单', '列表', '推荐', '推荐一个', '来点', '来一首', '给我', '帮我',
    '我的', '我想', '想要', '适合', '现在', '正在', '在', '一个', '一些', '一点', '的',
]


def strip_query_fillers(query):
    q = norm_text(query)
    # Remove longer fillers first. Keep content words such as 国语/助眠/运动.
    for word in sorted(QUERY_FILLERS, key=len, reverse=True):
        q = q.replace(norm_text(word), '')
    return q


LOCAL_PLAYLIST_HINTS = [
    # (query keywords, preferred playlist names in order)
    (['下雨', '雨天', '雨夜', '雨声'], ['助眠钢琴', '助眠', '放松BGM']),
    (['睡觉', '睡眠', '助眠', '入睡', '失眠', '晚安'], ['助眠', '助眠钢琴', '放松BGM']),
    (['钢琴', '纯音乐'], ['助眠钢琴', '放松BGM']),
    (['工作', '写作', '码字', '学习', '专注'], ['写作', '放松BGM', '助眠钢琴']),
    (['运动', '跑步', '健身', '锻炼'], ['运动']),
    (['做饭', '烧饭', '煮饭', '烹饪', '厨房', 'cooking'], ['cooking']),
    (['放松', '舒缓', '轻松', '安静'], ['放松BGM', '助眠', '助眠钢琴']),
    (['口琴'], ['口琴']),
    (['英语', '英文'], ['怀旧英语']),
    (['怀旧', '老歌'], ['怀旧', '怀旧英语', '国语']),
    (['日漫', '动漫', '动画', '二次元', '番剧'], ['日漫']),
    (['游戏', '游戏音乐', '原声', 'ost'], ['VG']),
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
    1. User-owned playlists.
    2. Exact/name-contained matches.
    3. Curated local keyword hints for common intents.

    If confidence is low, return None so the slower LLM/search path can handle it.
    """
    # First use the generated/editable alias table. It contains exact names,
    # ASR variants, semantic aliases, and weighted manual hints.
    alias_match = alias_table_match_playlist(query)
    if alias_match:
        return alias_match

    if not playlists:
        return None

    query_norm = norm_text(query)
    query_core = strip_query_fillers(query)
    candidates = []

    # Name matching across all playlists, with a user-owned bonus.
    for pl in playlists:
        score, reason = _playlist_score_for_name(query_norm, query_core, pl)
        if score:
            candidates.append((score, reason, pl))

    # Intent hints: deliberately prefer user's own playlists. This covers natural
    # requests like “下雨睡觉的音乐” without waiting for LLM.
    own_by_norm_name = {norm_text(p.get('name', '')): p for p in playlists if p.get('is_mine')}
    for keywords, preferred_names in LOCAL_PLAYLIST_HINTS:
        hit_kw = next((kw for kw in keywords if norm_text(kw) in query_norm), None)
        if not hit_kw:
            continue
        for rank, pname in enumerate(preferred_names):
            pl = own_by_norm_name.get(norm_text(pname))
            if pl:
                # Strong enough to bypass LLM, but below literal name matches.
                candidates.append((700 - rank * 25, f'local keyword {hit_kw}->{pname}', pl))
                break

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], bool(x[2].get('is_mine')), int(x[2].get('count') or 0)), reverse=True)
    score, reason, pl = candidates[0]

    # Guard rails: require high confidence. This avoids e.g. “十年” becoming some
    # random playlist just because of a weak fuzzy score.
    if score < 650:
        return None

    return {
        'playlist_id': str(pl['id']),
        'playlist_name': pl.get('name', ''),
        'reason': reason,
        'score': score,
        'is_mine': bool(pl.get('is_mine')),
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
    # User said “播放 XXX 歌单”: after normalization alias appears in full query.
    if len(alias_norm) >= 2 and alias_norm in query_norm:
        return alias_weight + 260 + min(len(alias_norm), 20), 'alias contained in query'
    # User said a shorter part, e.g. “夏亚” for “逆袭的夏亚”.
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


def fetch_playlist_tracks(playlist_id):
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
    print(json.dumps({{'success': True, 'tracks': tracks}}, ensure_ascii=False))
else:
    print(json.dumps({{'success': False, 'raw': result}}, ensure_ascii=False))
"""
    p = subprocess.run([PYTHON_VENV, '-c', script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    data = json.loads(p.stdout.strip())
    if not data.get('success'):
        raise RuntimeError(f'fetch playlist tracks failed: {p.stdout[:500]}')
    return data['tracks']


def play_track_from_queue(queue, index):
    tracks = queue.get('tracks') or []
    if not tracks:
        raise RuntimeError('empty queue')
    index = index % len(tracks)
    t = tracks[index]
    query = (t.get('name','') + ' ' + t.get('artist','')).strip()
    rc, out = run_node(['play', query], timeout=45)
    queue['index'] = index
    queue['current'] = t
    queue['last_started_at'] = time.time()
    save_queue(queue)
    return rc, out, t

def try_play_playlist(playlist_id, playlist_name='', shuffle=True):
    """Play playlist using the native NeteaseMusic queue first.

    Preferred path: CDP opens/clicks the playlist page, clicks the page-level
    “播放全部” button, then switches the native player to random mode. This lets
    NeteaseMusic own the actual queue: no LLM, no token usage, no Agent-managed
    next-track loop.

    Fallback path: if the desktop client cannot open/click the playlist page, use
    the older Agent-managed shuffled queue so voice playback still works.
    """
    rc, out = run_node(['playlist', str(playlist_id)], timeout=30)
    if rc == 0:
        # Native queue is now authoritative; remove stale Agent queue so /next and
        # natural auto-advance are handled by NeteaseMusic itself.
        try:
            QUEUE_FILE.unlink()
        except FileNotFoundError:
            pass
        return f'native-netease:playlist:{playlist_id}', f'native playlist queue; shuffle requested; {out}'

    print(f'[music-agent] native playlist play failed, fallback to agent queue: {out}', flush=True)

    tracks = fetch_playlist_tracks(playlist_id)
    if shuffle and len(tracks) > 1:
        random.shuffle(tracks)
    queue = {
        'type': 'playlist',
        'managed_by': 'agent',
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
        raise RuntimeError(f'native failed: {out}; agent fallback failed: {out2}')
    mode = 'shuffled' if shuffle else 'ordered'
    return f'agent-queue:playlist:{playlist_id}', f"native failed: {out}; fallback queued {len(tracks)} {mode} tracks; now playing: {t.get('name')} - {t.get('artist')} | {out2}"


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
    return any(kw in query for kw in PLAYLIST_KEYWORDS)



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
                # This avoids the LLM latency for clear requests like “国语”,
                # “助眠”, “下雨睡觉的音乐”, “写作音乐”, etc.
                if playlists:
                    local_match = fast_match_playlist(q, playlists)
                    if local_match:
                        playlist_id = local_match['playlist_id']
                        playlist_name = local_match['playlist_name']
                        orpheus_url, cdp_result = try_play_playlist(playlist_id, playlist_name)
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
                            'orpheus_url': orpheus_url,
                            'cdp_result': cdp_result,
                        })
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
                        orpheus_url, cdp_result = try_play_playlist(playlist_id, playlist_name)
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
                        # Fallback to regular search
                        rc, out = run_node(['play', q])
                        self.json(200 if rc == 0 else 500, {
                            'ok': rc == 0, 'action': 'play', 'q': q,
                            'output': out, 'fallback': 'no playlists cached'
                        })
                        return

                    llm_raw, err = ask_llm(q, playlists)
                    if err:
                        # If LLM is unavailable, still keep the voice path useful.
                        rc, out = run_node(['play', q])
                        self.json(200 if rc == 0 else 500, {
                            'ok': rc == 0, 'action': 'play', 'q': q,
                            'output': out, 'fallback': f'LLM error: {err}'
                        })
                        return

                    try:
                        parsed = json.loads(extract_json(llm_raw))
                    except json.JSONDecodeError as e:
                        rc, out = run_node(['play', q])
                        self.json(200 if rc == 0 else 500, {
                            'ok': rc == 0, 'action': 'play', 'q': q,
                            'output': out, 'fallback': f'LLM returned invalid JSON: {e}', 'raw': llm_raw
                        })
                        return

                    valid, reason = validate_llm_result(parsed, playlists)
                    if not valid:
                        rc, out = run_node(['play', q])
                        self.json(200 if rc == 0 else 500, {
                            'ok': rc == 0, 'action': 'play', 'q': q,
                            'output': out, 'fallback': f'LLM result invalid: {reason}', 'parsed': parsed
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
                rc, out = run_node(['play', q])
                self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'action': 'play', 'q': q, 'output': out})
                return
            if u.path == '/status':
                rc, out = run_node(['status'], timeout=15)
                try:
                    data = json.loads(out)
                except Exception:
                    data = {'raw': out}
                self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'status': data})
                return
            if u.path in ['/pause', '/next', '/prev']:
                cmd = u.path.strip('/')
                if cmd in ('next', 'prev'):
                    queue = load_queue()
                    if queue and queue.get('tracks') and queue.get('managed_by') == 'agent':
                        queue['paused'] = False
                        step = 1 if cmd == 'next' else -1
                        idx = int(queue.get('index', 0)) + step
                        rc, out, track = play_track_from_queue(queue, idx)
                        self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'action': cmd, 'queue': True, 'track': track, 'output': out})
                        return
                rc, out = run_node([cmd], timeout=20)
                if cmd == 'pause':
                    queue = load_queue()
                    if queue and queue.get('tracks') and queue.get('managed_by') == 'agent':
                        queue['paused'] = True
                        queue['paused_at'] = time.time()
                        save_queue(queue)
                self.json(200 if rc == 0 else 500, {'ok': rc == 0, 'action': cmd, 'queue': False, 'output': out})
                return
            self.json(404, {'ok': False, 'error': 'not found'})
        except Exception as e:
            self.json(500, {'ok': False, 'error': str(e)})

    def log_message(self, fmt, *args):
        print('[music-agent]', self.address_string(), fmt % args, flush=True)


if __name__ == '__main__':
    threading.Thread(target=queue_monitor_loop, daemon=True).start()
    port = int(os.environ.get('MUSIC_AGENT_PORT', '8765'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    print(f'music-agent listening on http://127.0.0.1:{port}', flush=True)
    server.serve_forever()
