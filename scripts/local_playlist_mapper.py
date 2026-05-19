#!/usr/bin/env python3
"""Local, no-cloud query -> playlist mapper.

This is deliberately dependency-free: no embedding API, no sklearn, no jieba.
It ranks playlists using character/word n-gram TF-IDF over:
  - playlist name/creator/source
  - aliases and local semantic hints from music_agent.py
  - cached representative tracks/artists if available
  - user-labeled query examples in runtime/playlist_mapping_labels.jsonl

It is meant for offline debugging and human annotation before wiring anything
back into playback.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import music_agent as ma  # noqa: E402

LABELS_FILE = ROOT / 'runtime' / 'playlist_mapping_labels.jsonl'
MODEL_FILE = ROOT / 'runtime' / 'local_playlist_mapper_model.json'
RULES_FILE = ROOT / 'runtime' / 'playlist_mapping_rules.json'


def now_ts() -> int:
    return int(time.time())


COMMON_FILLERS = [
    '小爱同学', '智能播放', '智能', '播放', '帮我', '给我', '我想听', '想听',
    '推荐一个', '推荐一些', '推荐点', '推荐', '来一点', '来点', '放一点', '放点',
    '一个歌单', '一些歌', '一点歌', '歌单', '音乐', '歌曲', '现在', '适合', '时候',
]


def normalize(s: str) -> str:
    s = (s or '').lower().strip()
    for w in COMMON_FILLERS:
        s = s.replace(w, ' ')
    # Keep CJK, letters, numbers. Turn punctuation into spaces for word tokens.
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def compact(s: str) -> str:
    return normalize(s).replace(' ', '')


def ngrams(text: str) -> Counter:
    """Mixed features: ascii words + CJK/compact char ngrams."""
    text_n = normalize(text)
    c = Counter()
    if not text_n:
        return c

    # Word-ish tokens, useful for English names like higher brothers / jojo.
    for tok in text_n.split():
        if tok:
            c[f'w:{tok}'] += 1.5
            if len(tok) >= 4 and re.fullmatch(r'[a-z0-9]+', tok):
                # light prefix helps ASR/truncated English.
                c[f'wp:{tok[:4]}'] += 0.4

    cc = text_n.replace(' ', '')
    # CJK/overall char n-grams. Bigrams/trigrams carry most signal; unigrams help short queries.
    for n, weight in ((1, 0.25), (2, 1.0), (3, 1.15), (4, 0.85)):
        if len(cc) >= n:
            for i in range(len(cc) - n + 1):
                c[f'c{n}:{cc[i:i+n]}'] += weight
    return c


def load_labels() -> list[dict]:
    rows = []
    if not LABELS_FILE.exists():
        return rows
    for line in LABELS_FILE.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get('query') and row.get('playlist_id'):
            rows.append(row)
    return rows


def save_label(query: str, playlist: dict, note: str = '', weight: float = 1.0):
    LABELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    row = {
        'query': query,
        'playlist_id': str(playlist.get('id') or playlist.get('playlist_id')),
        'playlist_name': playlist.get('name') or playlist.get('playlist_name') or '',
        'weight': weight,
        'note': note,
        'created_at': now_ts(),
    }
    with LABELS_FILE.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return row


def playlist_key(pl: dict) -> str:
    return str(pl.get('id') or pl.get('playlist_id') or '')


def find_playlist(playlists: list[dict], needle: str) -> dict | None:
    n = compact(needle)
    if not n:
        return None
    exact_id = [p for p in playlists if playlist_key(p) == needle]
    if exact_id:
        return exact_id[0]
    exact_name = [p for p in playlists if compact(p.get('name', '')) == n]
    if exact_name:
        return exact_name[0]
    contains = [p for p in playlists if n in compact(p.get('name', '')) or compact(p.get('name', '')) in n]
    if len(contains) == 1:
        return contains[0]
    if contains:
        # Prefer own playlist then larger count.
        contains.sort(key=lambda p: (bool(p.get('is_mine')), int(p.get('count') or 0)), reverse=True)
        return contains[0]
    return None


def label_groups(labels: list[dict], exclude_index: int | None = None) -> dict[str, list[dict]]:
    g = defaultdict(list)
    for i, row in enumerate(labels):
        if exclude_index is not None and i == exclude_index:
            continue
        g[str(row.get('playlist_id'))].append(row)
    return dict(g)


def playlist_profile_text(pl: dict) -> str:
    pid = playlist_key(pl)
    name = pl.get('name', '')
    parts = [
        ('歌单名称：' + name + '。') * 4,
        '创建者：' + str(pl.get('creator') or ''),
        '我的歌单' if pl.get('is_mine') else '收藏歌单',
    ]

    # Alias table.
    try:
        for row in ma.load_playlist_aliases() or []:
            if str(row.get('id')) == pid:
                aliases = [a.get('text', '') for a in (row.get('aliases') or []) if a.get('text')]
                if aliases:
                    parts.append(('别名叫法：' + '，'.join(aliases[:20]) + '。') * 4)
                break
    except Exception:
        pass

    # Local scene hints are high-value supervision; repeat them to avoid being
    # diluted by long track lists.
    name_norm = ma.norm_text(name)
    hints = []
    for keywords, preferred_names in getattr(ma, 'LOCAL_PLAYLIST_HINTS', []):
        if any(ma.norm_text(pn) == name_norm for pn in preferred_names):
            hints.extend(keywords)
    if hints:
        parts.append(('适合场景情绪：' + '，'.join(dict.fromkeys(hints)) + '。') * 8)

    # Cached tracks are useful, but lower priority than name/scene/labels.
    try:
        tracks = ma.get_cached_tracks(pid) or []
    except Exception:
        tracks = []
    if tracks:
        artists = {}
        lines = []
        for t in tracks[:18]:
            title = t.get('name') or ''
            artist = t.get('artist') or ''
            if title or artist:
                lines.append(f'{title} {artist}'.strip())
            for a in artist.split('/'):
                a = a.strip()
                if a:
                    artists[a] = artists.get(a, 0) + 1
        top_artists = [a for a, _ in sorted(artists.items(), key=lambda x: x[1], reverse=True)[:8]]
        if top_artists:
            parts.append('常见歌手：' + '，'.join(top_artists))
        if lines:
            parts.append('代表歌曲：' + '，'.join(lines[:12]))
    return '\n'.join(x for x in parts if x)


def build_docs(playlists: list[dict], labels: list[dict], exclude_label_index: int | None = None) -> list[dict]:
    grouped = label_groups(labels, exclude_label_index)
    docs = []
    for pl in playlists:
        pid = playlist_key(pl)
        if not pid:
            continue
        parts = [playlist_profile_text(pl)]
        examples = grouped.get(pid, [])
        if examples:
            # Labeled examples are the strongest local supervision. Repeat by weight.
            q_parts = []
            for ex in examples:
                rep = max(1, min(6, int(round(float(ex.get('weight') or 1) * 3))))
                q_parts.extend([ex.get('query', '')] * rep)
            parts.append('用户标注叫法/场景：' + '，'.join(q_parts))
        docs.append({
            'playlist_id': pid,
            'playlist_name': pl.get('name', ''),
            'is_mine': bool(pl.get('is_mine')),
            'count': int(pl.get('count') or 0),
            'text': '\n'.join(x for x in parts if x),
            'label_examples': [x.get('query', '') for x in examples],
        })
    return docs


def compute_idf(doc_vecs: list[Counter]) -> dict[str, float]:
    df = Counter()
    for v in doc_vecs:
        for k in v:
            df[k] += 1
    n = max(1, len(doc_vecs))
    return {k: math.log((1 + n) / (1 + d)) + 1.0 for k, d in df.items()}


def tfidf(vec: Counter, idf: dict[str, float]) -> dict[str, float]:
    out = {}
    for k, v in vec.items():
        out[k] = float(v) * idf.get(k, 1.0)
    return out


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    na = sum(v * v for v in a.values())
    nb = sum(v * v for v in b.values())
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


def exact_bonus(query: str, name: str) -> tuple[float, str]:
    q = compact(query)
    n = compact(name)
    if not q or not n:
        return 0.0, ''
    if q == n:
        return 1.0, 'exact_name'
    if n in q:
        return 0.55, 'name_in_query'
    # Strip common fillers then retry.
    try:
        core = ma.strip_query_fillers(query)
    except Exception:
        core = q
    core = compact(core)
    if core == n:
        return 0.8, 'exact_name_after_strip'
    if core and core in n and len(core) >= 2:
        return 0.35, 'query_core_in_name'
    return 0.0, ''


def predict(query: str, top_k: int = 10, exclude_label_index: int | None = None) -> list[dict]:
    playlists = ma.load_playlists()
    labels = load_labels()
    docs = build_docs(playlists, labels, exclude_label_index)
    doc_raw = [ngrams(d['text']) for d in docs]
    idf = compute_idf(doc_raw + [ngrams(query)])
    doc_vecs = [tfidf(v, idf) for v in doc_raw]
    q_vec = tfidf(ngrams(query), idf)

    rows = []
    for d, dv in zip(docs, doc_vecs):
        doc_sim = cosine(q_vec, dv)
        # Direct similarity to annotated examples. This makes few-shot labels very effective.
        example_sims = []
        for ex in d.get('label_examples') or []:
            example_sims.append(cosine(q_vec, tfidf(ngrams(ex), idf)))
        ex_sim = max(example_sims) if example_sims else 0.0
        bonus, bonus_reason = exact_bonus(query, d['playlist_name'])
        mine_bonus = 0.006 if d['is_mine'] else 0.0
        count_bonus = 0.003 if d['count'] > 0 else 0.0
        # Label examples should dominate when close; base doc keeps zero-shot usable.
        score = 0.55 * doc_sim + 0.70 * ex_sim + bonus + mine_bonus + count_bonus
        reason_bits = []
        if bonus_reason:
            reason_bits.append(bonus_reason)
        if ex_sim:
            reason_bits.append(f'label_sim={ex_sim:.3f}')
        reason_bits.append(f'doc_sim={doc_sim:.3f}')
        rows.append({
            'playlist_id': d['playlist_id'],
            'playlist_name': d['playlist_name'],
            'score': score,
            'doc_sim': doc_sim,
            'label_sim': ex_sim,
            'is_mine': d['is_mine'],
            'count': d['count'],
            'reason': ', '.join(reason_bits),
        })
    rows.sort(key=lambda r: (r['score'], r['is_mine'], r['count']), reverse=True)
    return rows[:top_k]




def load_rules():
    if not RULES_FILE.exists():
        return {'exact': {}, 'contains': []}
    try:
        d=json.loads(RULES_FILE.read_text(encoding='utf-8'))
        if not isinstance(d, dict):
            return {'exact': {}, 'contains': []}
        d.setdefault('exact', {})
        d.setdefault('contains', [])
        return d
    except Exception:
        return {'exact': {}, 'contains': []}


def save_rules(d):
    RULES_FILE.parent.mkdir(parents=True, exist_ok=True)
    RULES_FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')


def apply_rules(query, playlists):
    rules=load_rules()
    qn=compact(query)
    # exact
    pid = rules.get('exact', {}).get(qn)
    if pid:
        for p in playlists:
            if playlist_key(p)==str(pid):
                return {'playlist_id':str(pid), 'playlist_name':p.get('name',''), 'reason':'rule_exact'}
    # contains
    for row in rules.get('contains', []) or []:
        pat=compact(str(row.get('pattern','')))
        if pat and pat in qn:
            pid=str(row.get('playlist_id',''))
            for p in playlists:
                if playlist_key(p)==pid:
                    return {'playlist_id':pid, 'playlist_name':p.get('name',''), 'reason':f"rule_contains:{row.get('pattern','')}"}
    return None


def should_fallback_online(query, rows, local_score_threshold=0.03, min_margin=0.008):
    if not rows:
        return True, 'no_candidates'
    top=rows[0]
    s1=top.get('score',0.0)
    s2=rows[1]['score'] if len(rows)>1 else 0.0
    q=compact(query)
    artist_like=('的歌' in q) or ('歌手' in q)
    if s1 < local_score_threshold:
        return True, f'low_confidence:{s1:.4f}<{local_score_threshold}'
    if (s1 - s2) < min_margin:
        return True, f'low_margin:{(s1-s2):.4f}<{min_margin}'
    if artist_like and top.get('doc_sim',0.0) < 0.06 and top.get('label_sim',0.0) < 0.06:
        return True, 'artist_like_query_without_strong_local_artist_signal'
    return False, 'confident_local'


def cmd_add_rule(args):
    playlists = ma.load_playlists()
    pl = find_playlist(playlists, args.playlist)
    if not pl:
        raise SystemExit(f'找不到歌单: {args.playlist}')
    d=load_rules()
    if args.mode=='exact':
        d.setdefault('exact', {})[compact(args.pattern)] = playlist_key(pl)
    else:
        d.setdefault('contains', []).append({'pattern': args.pattern, 'playlist_id': playlist_key(pl), 'playlist_name': pl.get('name','')})
    save_rules(d)
    print('added rule', args.mode, args.pattern, '->', pl.get('name',''), playlist_key(pl))


def cmd_rules(args):
    print(json.dumps(load_rules(), ensure_ascii=False, indent=2))


def cmd_predict(args):
    playlists = ma.load_playlists()
    rule = apply_rules(args.query, playlists)
    rows = predict(args.query, args.top_k)
    if rule:
        base = dict(rows[0]) if rows else {'playlist_id':'', 'playlist_name':'', 'score':0.0, 'doc_sim':0.0, 'label_sim':0.0, 'is_mine':False, 'count':0, 'reason':''}
        base.update(rule)
        base['score']=max(float(base.get('score',0.0)), 9.9)
        base['reason']=rule.get('reason','rule')
        rows = [base] + [r for r in rows if r.get('playlist_id')!=rule['playlist_id']]
    fallback, fb_reason = should_fallback_online(args.query, rows, args.local_score_threshold, args.min_margin)
    payload={'query': args.query, 'decision': 'online_fallback' if fallback else 'local', 'decision_reason': fb_reason, 'top1': rows[0] if rows else None, 'candidates': rows}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print(f'query: {args.query}')
    print(f"decision: {'online_fallback' if fallback else 'local'}  reason: {fb_reason}")
    for i, r in enumerate(rows, 1):
        mark = '✅' if i == 1 else '  '
        print(f"{i:>2}. {mark} score={r['score']:.4f} doc={r['doc_sim']:.4f} label={r['label_sim']:.4f} "
              f"mine={str(r['is_mine']).lower():<5} count={r['count']:<4} {r['playlist_name']} ({r['playlist_id']})")
        print(f"      {r['reason']}")


def cmd_add_label(args):
    playlists = ma.load_playlists()
    pl = find_playlist(playlists, args.playlist)
    if not pl:
        raise SystemExit(f'找不到歌单: {args.playlist}')
    row = save_label(args.query, pl, args.note, args.weight)
    print('added label:')
    print(json.dumps(row, ensure_ascii=False, indent=2))


def cmd_list(args):
    playlists = ma.load_playlists()
    needle = compact(args.filter or '')
    rows = []
    for p in playlists:
        if not needle or needle in compact(p.get('name', '')) or needle == playlist_key(p):
            rows.append(p)
    for p in rows[:args.limit]:
        print(f"{playlist_key(p)}\t{p.get('count', 0)}\t{'mine' if p.get('is_mine') else 'fav '}\t{p.get('name', '')}")
    if len(rows) > args.limit:
        print(f'... {len(rows) - args.limit} more')


def cmd_labels(args):
    labels = load_labels()
    for i, row in enumerate(labels, 1):
        print(f"{i:>3}. {row.get('query')} -> {row.get('playlist_name')} ({row.get('playlist_id')}) weight={row.get('weight', 1)}")
    print(f'total: {len(labels)}  file: {LABELS_FILE}')


def cmd_eval(args):
    labels = load_labels()
    if not labels:
        print('no labels yet')
        return
    ok1 = 0
    ok3 = 0
    for i, row in enumerate(labels):
        rows = predict(row['query'], top_k=3, exclude_label_index=i if args.leave_one_out else None)
        ids = [r['playlist_id'] for r in rows]
        hit1 = ids[:1] == [str(row['playlist_id'])]
        hit3 = str(row['playlist_id']) in ids
        ok1 += int(hit1)
        ok3 += int(hit3)
        if args.show_all or not hit1:
            print(f"{'✅' if hit1 else '❌'} {row['query']} -> expected {row['playlist_name']} ({row['playlist_id']})")
            for j, r in enumerate(rows, 1):
                print(f"   {j}. {r['score']:.4f} {r['playlist_name']} ({r['playlist_id']})")
    n = len(labels)
    print(f'top1={ok1}/{n} {ok1/n:.1%}   top3={ok3}/{n} {ok3/n:.1%}   leave_one_out={args.leave_one_out}')


def main():
    ap = argparse.ArgumentParser(description='Local offline query -> playlist mapper')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('predict', help='predict playlist candidates for one query')
    p.add_argument('query')
    p.add_argument('--top-k', type=int, default=10)
    p.add_argument('--json', action='store_true')
    p.add_argument('--local-score-threshold', type=float, default=0.03)
    p.add_argument('--min-margin', type=float, default=0.008)
    p.set_defaults(func=cmd_predict)

    p = sub.add_parser('add-label', help='add one human label: query -> playlist')
    p.add_argument('query')
    p.add_argument('playlist', help='playlist id, exact name, or unique name fragment')
    p.add_argument('--weight', type=float, default=1.0)
    p.add_argument('--note', default='')
    p.set_defaults(func=cmd_add_label)

    p = sub.add_parser('list', help='list playlists, optionally filtered')
    p.add_argument('filter', nargs='?', default='')
    p.add_argument('--limit', type=int, default=200)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('labels', help='show current labels')
    p.set_defaults(func=cmd_labels)

    p = sub.add_parser('add-rule', help='add rule pattern -> playlist')
    p.add_argument('mode', choices=['exact','contains'])
    p.add_argument('pattern')
    p.add_argument('playlist')
    p.set_defaults(func=cmd_add_rule)

    p = sub.add_parser('rules', help='show rules')
    p.set_defaults(func=cmd_rules)

    p = sub.add_parser('eval', help='evaluate labels')
    p.add_argument('--leave-one-out', action='store_true', help='exclude each tested label from training profile')
    p.add_argument('--show-all', action='store_true')
    p.set_defaults(func=cmd_eval)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
