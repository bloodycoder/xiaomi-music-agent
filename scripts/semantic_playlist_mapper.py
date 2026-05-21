#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, math, os, re, time
from pathlib import Path

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
PLAYLISTS_FILE = ROOT / 'runtime' / 'playlists.json'
TRACK_CACHE = ROOT / 'runtime' / 'playlist_tracks_cache.json'
ALIASES_FILE = ROOT / 'runtime' / 'playlist_aliases.json'
INDEX_FILE = ROOT / 'runtime' / 'semantic_playlist_index.json'
_MODEL_CACHE = {}



def compact(s: str) -> str:
    s = (s or '').lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def load_playlists():
    d = json.loads(PLAYLISTS_FILE.read_text(encoding='utf-8'))
    pls = d.get('playlists', d if isinstance(d, list) else [])
    return pls


def load_aliases():
    if not ALIASES_FILE.exists():
        return {}
    try:
        data = json.loads(ALIASES_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}

    # Support multiple historical shapes:
    # 1) list[{'id':..., 'aliases':[{'text':...}]}]
    # 2) {'playlists': [ ...same as #1... ], ...meta}
    # 3) {'<pid>': ['alias1','alias2']} or {'<pid>': {'aliases':[...]}}
    if isinstance(data, dict) and isinstance(data.get('playlists'), list):
        rows = data.get('playlists') or []
    elif isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = []
        for k, v in data.items():
            if k in ('version', 'generated_at', 'note'):
                continue
            rows.append({'id': k, 'aliases': v})
    else:
        rows = []

    out = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        pid = str(r.get('id') or r.get('playlist_id') or '')
        if not pid:
            continue
        aliases_raw = r.get('aliases') or []
        aliases = []
        if isinstance(aliases_raw, list):
            for a in aliases_raw:
                if isinstance(a, dict):
                    t = a.get('text') or a.get('alias') or ''
                    if t:
                        aliases.append(str(t))
                elif isinstance(a, str) and a.strip():
                    aliases.append(a.strip())
        elif isinstance(aliases_raw, dict):
            for v in aliases_raw.values():
                if isinstance(v, str) and v.strip():
                    aliases.append(v.strip())
        elif isinstance(aliases_raw, str) and aliases_raw.strip():
            aliases.append(aliases_raw.strip())
        out[pid] = aliases
    return out


def load_tracks():
    if not TRACK_CACHE.exists():
        return {}
    try:
        d = json.loads(TRACK_CACHE.read_text(encoding='utf-8'))
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    # Handle nested structure: {"playlists": {"<pid>": {"tracks": [...]}}}
    if 'playlists' in d:
        return d['playlists']
    return d


def profile_text(pl, aliases, tracks):
    pid = str(pl.get('id') or '')
    name = pl.get('name') or ''
    creator = pl.get('creator') or ''

    # Collect artists from tracks
    trows = tracks.get(pid, {}).get('tracks', []) if isinstance(tracks.get(pid, {}), dict) else []
    artists = []
    track_samples = []
    for t in (trows or [])[:20]:
        tn = t.get('name') or ''
        ar = t.get('artist') or ''
        if tn or ar:
            track_samples.append(f"{tn} {ar}".strip())
        for x in ar.split('/'):
            x = x.strip()
            if x and x not in artists:
                artists.append(x)
        if len(artists) >= 15:
            break

    # Filter out noise aliases (auto-generated templates like "播放xxx", "放xxx", "xxx歌单")
    als = aliases.get(pid, [])
    distinct_aliases = []
    name_compact = name.lower().replace(' ', '').replace('丨', '').replace('|', '').replace('·', '')
    for a in als:
        a_compact = a.lower().replace(' ', '').replace('丨', '').replace('|', '').replace('·', '')
        # Skip if it's just "播放/放 + name" or "name + 歌单"
        if a_compact == '播放' + name_compact:
            continue
        if a_compact == '放' + name_compact:
            continue
        if a_compact == name_compact + '歌单':
            continue
        # Skip pinyin/spelling aliases (short, all ASCII)
        if a.isascii() and len(a) <= 6 and a.lower() != name.lower():
            continue
        distinct_aliases.append(a)

    # Build profile: name first (most important), then artists, then tracks, then creator + aliases
    parts = [f"歌单:{name}"]
    if artists:
        parts.append('歌手:' + '，'.join(artists[:15]))
    if track_samples:
        parts.append('歌曲:' + '，'.join(track_samples[:10]))
    if creator:
        parts.append(f"创建者:{creator}")
    if distinct_aliases:
        parts.append('别名:' + '，'.join(distinct_aliases[:10]))

    return '\n'.join(parts)



# Lightweight pragmatic/mood layer.
#
# The mapper remains embedding-first.  This NLU layer only translates a user's
# situation/activity into mood descriptors before retrieval, then applies small
# semantic rerank nudges.  It is a compact mood taxonomy, not a playlist-id rule
# table: no scene below points to a specific playlist id/name as the answer.
_SCENE_REQUEST_WORDS = (
    '适合', '推荐', '来点', '来个', '放点', '放首', '给我', '想听', '心情',
    '氛围', '场景', '歌单', '音乐', 'bgm', 'BGM', '整点', '安排', '播放', '听'
)
_LITERAL_SOUND_WORDS = ('雨声', '雷雨声', '白噪音', '自然声', '环境音', 'asmr', 'amsr')
_SLEEP_WORDS = ('睡觉', '睡前', '睡眠', '入睡', '助眠', '失眠', '晚安')
_NOISE_PLAYLIST_WORDS = ('雨声', '雨 声', '雷雨', '白噪音', '自然声', '环境音', 'asmr', 'amsr', '助眠专用背景音乐')
_GENERIC_PLAYLIST_WORDS = ('随机推荐', '周一', '周二', '周三', '周四', '周五', '年度歌曲', '五星歌单')

# Mood dimensions are retrieval hints.  Add new scenes by giving them trigger
# words + mood terms; avoid adding playlist ids or one-off exact query rules.
_MOOD_PROFILES = {
    'rain_cozy': {
        'triggers': ('下雨', '雨天', '雨夜', '阴雨', '下着雨'),
        'query_terms': '雨天心情 舒缓 舒服 慵懒 卧室 氛围感 安静 放松 chill 温柔 人声 歌单',
        'positive_name_terms': ('慵懒', '卧室', '氛围', '舒适', '舒服', 'chill', '另类独立', '安静', '放松'),
        'negative_name_terms': _NOISE_PLAYLIST_WORDS,
    },
    'night_cozy': {
        'triggers': ('晚上', '夜晚', '深夜', '夜里', '半夜', '睡不着'),
        'query_terms': '夜晚心情 安静 舒服 卧室 氛围感 放松 温柔 慵懒 chill R&B 人声 歌单',
        'positive_name_terms': ('卧室', '氛围', '安静', '舒适', '舒服', 'chill', 'r&b', 'R&B', '慵懒'),
        'negative_name_terms': ('健身', '精神氮泵', '硬核', '车机'),
    },
    'morning_fresh': {
        'triggers': ('早起', '早上', '清晨', '起床', '洗漱', '洗脸', '化妆'),
        'query_terms': '早起 清晨 起床 洗漱 清爽 舒服 轻松 元气 阳光 提神 不吵 歌单',
        'positive_name_terms': ('起床', '洗漱', '化妆', '元气', '日系少女', '咖啡店', '轻快'),
        'negative_name_terms': _NOISE_PLAYLIST_WORDS + ('睡眠', '助眠', '冥想'),
    },
    'commute_drive': {
        'triggers': ('通勤', '上班路上', '下班路上', '开车', '车上', '路上', '堵车', '地铁'),
        'query_terms': '通勤 开车 路上 轻快 有节奏 提神 旋律 华语 说唱 流行 赶走焦虑 歌单',
        'positive_name_terms': ('下班路上', '车机', '旋律rap', '有节奏', '轻快', '说唱时刻'),
        'negative_name_terms': _NOISE_PLAYLIST_WORDS + ('睡眠', '助眠', '冥想', '安静60分钟'),
    },
    'cooking_light': {
        'triggers': ('做饭', '做菜', '下厨', '厨房', '煮饭', '炒菜', '吃饭'),
        'query_terms': '做饭 做菜 厨房 轻松 不吵 舒服 咖啡店 爵士 散步 chill 温柔 歌单',
        'positive_name_terms': ('做菜', '咖啡店', '爵士', '散步', 'chill', '轻松', '舒服'),
        'negative_name_terms': ('硬核', '精神氮泵', '健身', '助眠专用'),
    },
    'toilet_casual': {
        'triggers': ('拉屎', '上厕所', '厕所', '蹲坑', '大便', '便便', '马桶'),
        'query_terms': '厕所 摸鱼 短时间 轻松 随意 休闲 搞笑 不严肃 有节奏 流行 歌单',
        'positive_name_terms': ('随机推荐', '轻快', '流行', '咖啡店', 'chill', '下班路上'),
        'negative_name_terms': ('冥想', '助眠', '睡眠', '安静60分钟', '古典音乐'),
    },
    'intimate_sexy': {
        'triggers': ('做爱', '亲热', '啪啪啪', 'doi', 'do爱', '爱爱', '情侣', '约会', '暧昧'),
        'query_terms': '亲密 情侣 做爱 约会 暧昧 性感 氛围 R&B 慢节奏 浪漫 温柔 歌单',
        'positive_name_terms': ('情侣', 'do', 'Do', '成人氛围', 'Sex', 'sex', 'R&B', 'r&b', '氛围'),
        'negative_name_terms': ('健身', '硬核', '精神氮泵', '雨声', '冥想'),
    },
    'leisure_chill': {
        'triggers': ('休闲', '摸鱼', '放空', '闲着', '发呆', '周末', '散步', '晒太阳'),
        'query_terms': '休闲 放空 摸鱼 惬意 chill 轻松 咖啡店 散步 舒服 温柔 歌单',
        'positive_name_terms': ('chill', '咖啡店', '散步', '惬意', '卧室', '氛围', '另类独立'),
        'negative_name_terms': ('健身', '硬核', '精神氮泵'),
    },
    'relax_calm': {
        'triggers': ('放松', '舒缓', '舒服', '解压', '不焦虑', '焦虑', '累了', '疲惫', '轻松'),
        'query_terms': '放松 舒缓 舒服 解压 安静 轻柔 不焦虑 温柔 慵懒 chill 歌单',
        'positive_name_terms': ('安静', '轻柔', '放松', '不再焦虑', '舒适', '舒服', '卧室', '氛围', 'chill'),
        'negative_name_terms': ('硬核', '精神氮泵', '健身'),
    },
    'meditation_empty': {
        'triggers': ('冥想', '打坐', '静坐', '瑜伽', '禅', '正念', '空灵'),
        'query_terms': '冥想 静坐 打坐 瑜伽 禅 空灵 安静 平静 呼吸 放松 轻音乐 歌单',
        'positive_name_terms': ('冥想', '静坐', '打坐', '瑜伽', '禅', '空灵', '安静', '轻音乐'),
        'negative_name_terms': ('说唱', 'rap', 'RAPPER', '精神氮泵', '健身'),
    },
    'focus_study': {
        'triggers': ('学习', '工作', '写代码', '读代码', '看书', '读书', '论文', '专注', '干活'),
        'query_terms': '学习 工作 写代码 看书 专注 沉浸 lofi 轻音乐 安静 不吵 BGM 歌单',
        'positive_name_terms': ('学习', '工作', '专注', '写代码', '读代码', '看书', '论文', 'lofi', 'Lofi', '自习室'),
        'negative_name_terms': ('健身', '精神氮泵', '硬核'),
    },
    'sleep_rest': {
        'triggers': _SLEEP_WORDS,
        'query_terms': '睡前 睡觉 睡眠 助眠 安静 轻柔 放松 不焦虑 晚安 轻音乐 歌单',
        'positive_name_terms': ('睡眠', '助眠', '安静', '轻柔', '放松', '晚安', '雨声'),
        'negative_name_terms': ('健身', '精神氮泵', '硬核', '车机'),
    },
    'bath_comfort': {
        'triggers': ('洗澡', '洗漱', '泡澡', '沐浴', '冲澡'),
        'query_terms': '洗澡 洗漱 泡澡 舒服 轻松 清爽 起床 化妆 不吵 女孩 bgm 歌单',
        'positive_name_terms': ('洗澡', '洗漱', '起床', '化妆', '舒服', '轻松', '清爽'),
        'negative_name_terms': ('硬核', '精神氮泵', '健身', '冥想'),
    },
    'fitness_energy': {
        'triggers': ('健身', '运动', '跑步', '撸铁', '燃脂', '锻炼'),
        'query_terms': '健身 运动 跑步 节奏 能量 燃 提神 说唱 hiphop 精神氮泵 歌单',
        'positive_name_terms': ('健身', '精神氮泵', '说唱', 'rap', '节奏', '硬核'),
        'negative_name_terms': ('睡眠', '助眠', '冥想', '安静60分钟'),
    },
}


def _contains_any(text, words):
    t = (text or '').lower()
    return any(str(w).lower() in t for w in words)


def _matched_moods(query):
    q = compact(query)
    matched = []
    for mood, profile in _MOOD_PROFILES.items():
        if _contains_any(q, profile.get('triggers') or ()): 
            matched.append(mood)
    return matched


def analyze_query_mood(query):
    """Return a small NLU record used to make embedding retrieval pragmatic.

    The idea is general scene/activity -> mood expansion:
      下雨/夜晚/早起/通勤/做饭/拉屎/做爱/休闲/放松/冥想/学习/睡前/洗澡/健身
    all become mood descriptors before embedding retrieval.  Literal sound
    requests such as "放点雨声" still stay literal and keep matching rain-sound
    playlists.
    """
    q = compact(query)
    moods = _matched_moods(q)
    has_scene_request = _contains_any(q, _SCENE_REQUEST_WORDS)
    has_literal_sound = _contains_any(q, _LITERAL_SOUND_WORDS)

    # Literal content wins when the user explicitly asks for sound/ASMR.  Sleep
    # without explicit rain/white-noise remains a general sleep mood request.
    if has_literal_sound:
        return {
            'intent': 'literal_content',
            'moods': [],
            'expanded_query': query,
            'notes': ['literal_sound'],
        }

    if moods:
        terms = []
        for mood in moods:
            terms.extend((_MOOD_PROFILES[mood].get('query_terms') or '').split())
        # Preserve order while de-duplicating terms.
        seen = set()
        expanded_terms = []
        for t in terms:
            if t and t not in seen:
                seen.add(t)
                expanded_terms.append(t)
        return {
            'intent': 'scene_mood' if has_scene_request or moods else 'default',
            'moods': moods,
            'expanded_query': f"{query} {' '.join(expanded_terms)}".strip(),
            'notes': [f'{mood}_mood' for mood in moods],
        }

    return {'intent': 'default', 'moods': [], 'expanded_query': query, 'notes': []}


def _playlist_mood_adjustment(row, nlu):
    """Small rerank adjustment after semantic retrieval.

    No profile maps to a playlist id.  We only nudge playlists whose names/text
    advertise compatible moods and demote obvious opposite/literal-noise content.
    Scores stay mostly embedding-driven.
    """
    if not nlu or nlu.get('intent') != 'scene_mood':
        return 0.0, []
    name = row.get('playlist_name') or ''
    text = row.get('text') or ''
    hay = f"{name}\n{text}"
    delta = 0.0
    reasons = []
    for mood in nlu.get('moods') or []:
        prof = _MOOD_PROFILES.get(mood) or {}
        if _contains_any(hay, prof.get('negative_name_terms') or ()): 
            delta -= 0.08
            reasons.append(f'{mood}:demote_negative_mood')
        if _contains_any(name, prof.get('positive_name_terms') or ()): 
            delta += 0.055
            reasons.append(f'{mood}:boost_mood_name')
    if _contains_any(name, _GENERIC_PLAYLIST_WORDS):
        delta -= 0.02
        reasons.append('demote_generic_playlist')
    # Multiple matched moods should help query rewrite more than manual nudges.
    delta = max(min(delta, 0.14), -0.18)
    return delta, reasons


def get_model(model_name):
    if model_name not in _MODEL_CACHE:
        from sentence_transformers import SentenceTransformer
        _MODEL_CACHE[model_name] = SentenceTransformer(model_name)
    return _MODEL_CACHE[model_name]


def embed_texts(model_name, texts):
    m = get_model(model_name)
    vecs = m.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return vecs


def build_index(model_name='BAAI/bge-small-zh-v1.5'):
    pls = load_playlists()
    aliases = load_aliases()
    tracks = load_tracks()
    docs = []
    for p in pls:
        pid = str(p.get('id') or '')
        if not pid:
            continue
        docs.append({
            'playlist_id': pid,
            'playlist_name': p.get('name',''),
            'is_mine': bool(p.get('is_mine')),
            'count': int(p.get('count') or 0),
            'text': profile_text(p, aliases, tracks),
        })
    vecs = embed_texts(model_name, [d['text'] for d in docs])
    for d, v in zip(docs, vecs):
        d['embedding'] = [float(x) for x in v]
    payload = {'model': model_name, 'created_at': time.time(), 'items': docs}
    INDEX_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return payload


def load_index(model_name='BAAI/bge-small-zh-v1.5'):
    if INDEX_FILE.exists():
        d = json.loads(INDEX_FILE.read_text(encoding='utf-8'))
        if d.get('model') == model_name and d.get('items'):
            return d
    return build_index(model_name)


def cosine(a,b):
    return sum(x*y for x,y in zip(a,b))


def _extract_query_core(query):
    """Extract the likely target entity from a query, stripping prefix/suffix noise."""
    core = query
    # "来点/来个" can appear anywhere, extract what follows
    for marker in ['来点', '来个', '来一首', '我想听']:
        idx = core.find(marker)
        if idx >= 0:
            core = core[idx + len(marker):]
            break
    for prefix in ['播放', '放', '听']:
        if core.startswith(prefix):
            core = core[len(prefix):]
            break
    for suffix in ['的歌单', '的歌', '经典', '风格', '那种继续', '那种', '继续',
                   '推荐个歌单', '推荐歌单', '推荐']:
        if core.endswith(suffix) and len(core) > len(suffix):
            core = core[:-len(suffix)]
            break
    return core.strip()

_MOOD_WORDS = {'舒服', '轻松', '安静', '提神', '醒脑', '助眠', '专注', '有节奏',
               '不吵', '好听', '好听的', '放松', '雨声', '失眠', '通勤',
               '舒服的', '安静的', '轻松点', '困', '醒脑的', '提神一点',
               '下雨天', '雨天', '下雨', '洗澡', '做饭', '开车', '看书', '写代码',
               '学习', '睡觉', '睡前', '晚上', '早上', '通勤', '健身', '上班', '下班'}

def _looks_like_entity(text):
    """Heuristic: does this text look like it names a specific artist/band/target
    rather than a mood descriptor?"""
    if not text or len(text) < 2:
        return False
    if len(text) > 10:  # Long phrases are likely descriptions, not entities
        return False
    if text in _MOOD_WORDS:
        return False
    # If any mood word appears inside the text, it's a scene/mood query
    for w in _MOOD_WORDS:
        if w in text:
            return False
    has_cjk = any('一' <= c <= '鿿' for c in text)
    has_alpha = any(c.isalpha() for c in text)
    return has_cjk or has_alpha


def predict(query, model_name='BAAI/bge-small-zh-v1.5', top_k=5, threshold=0.50, return_timing=False):
    t0 = time.perf_counter()
    idx_t0 = time.perf_counter()
    idx = load_index(model_name)
    idx_t1 = time.perf_counter()
    nlu_t0 = time.perf_counter()
    nlu = analyze_query_mood(query)
    nlu_t1 = time.perf_counter()
    emb_t0 = time.perf_counter()
    qv = embed_texts(model_name, [nlu.get('expanded_query') or query])[0]
    emb_t1 = time.perf_counter()
    query_core = _extract_query_core(query)
    score_t0 = time.perf_counter()
    rows = []
    for it in idx['items']:
        raw_s = float(cosine(qv, it['embedding']))
        row = {
            'playlist_id': it['playlist_id'],
            'playlist_name': it['playlist_name'],
            'raw_score': raw_s,
            'score': raw_s,
            'is_mine': it['is_mine'],
            'count': it['count'],
            'text': it.get('text', ''),
        }
        adj, reasons = _playlist_mood_adjustment(row, nlu)
        row['score'] = raw_s + adj
        if adj:
            row['score_adjustment'] = adj
            row['adjustment_reasons'] = reasons
        rows.append(row)
    rows.sort(key=lambda r: (r['score'], r['is_mine'], r['count']), reverse=True)
    decision = 'local'
    if not rows or rows[0]['score'] < threshold:
        decision = 'online_fallback'
    elif nlu.get('intent') != 'scene_mood' and _looks_like_entity(query_core):
        # Entity queries (artists, bands): avoid treating a weak mention inside a
        # broad playlist as a dedicated local match.  Above 0.58 we still require
        # the entity to be present; below that, prefer online fallback.
        hay = ((rows[0].get('text') or '') + '\n' + (rows[0].get('playlist_name') or '')).lower()
        if rows[0]['score'] < 0.58 or query_core.lower() not in hay:
            decision = 'online_fallback'
    score_t1 = time.perf_counter()
    out = {'query': query, 'decision': decision, 'threshold': threshold, 'nlu': nlu,
           'top1': rows[0] if rows else None, 'candidates': rows[:top_k]}
    if return_timing:
        out['timing'] = {
            'total_ms': round((time.perf_counter() - t0) * 1000, 3),
            'load_index_ms': round((idx_t1 - idx_t0) * 1000, 3),
            'nlu_ms': round((nlu_t1 - nlu_t0) * 1000, 3),
            'embed_ms': round((emb_t1 - emb_t0) * 1000, 3),
            'score_ms': round((score_t1 - score_t0) * 1000, 3),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('build-index')
    p.add_argument('--model', default='BAAI/bge-small-zh-v1.5')
    p = sub.add_parser('predict')
    p.add_argument('query')
    p.add_argument('--model', default='BAAI/bge-small-zh-v1.5')
    p.add_argument('--top-k', type=int, default=5)
    p.add_argument('--threshold', type=float, default=0.50)
    p.add_argument('--json', action='store_true')
    args = ap.parse_args()
    if args.cmd == 'build-index':
        d = build_index(args.model)
        print(f"indexed {len(d['items'])} playlists model={d['model']}")
    else:
        out = predict(args.query, model_name=args.model, top_k=args.top_k, threshold=args.threshold)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"query: {out['query']}")
            print(f"decision: {out['decision']} threshold={out['threshold']}")
            for i,r in enumerate(out['candidates'],1):
                print(f"{i}. {r['score']:.4f} {r['playlist_name']} ({r['playlist_id']})")

if __name__ == '__main__':
    main()
