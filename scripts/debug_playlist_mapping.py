#!/usr/bin/env python3
"""Debug natural-language query -> local playlist mapping without playing anything.

This intentionally reuses scripts/music_agent.py's local playlist loaders,
fast/alias rules, and embedding helpers, but never calls any playback code.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import music_agent as ma  # noqa: E402


def _playlist_by_id(playlists):
    return {str(p.get('id') or ''): p for p in playlists or []}


def embedding_candidates(query: str, playlists: list[dict], top_k: int, threshold: float | None = None):
    """Return top embedding candidates. Raises if embedding API/config fails."""
    index = ma.ensure_playlist_embedding_index(playlists)
    if not index or not index.get('items'):
        return []
    q_vec = ma.fetch_embeddings([f'用户想听的歌单/音乐场景：{query}'], timeout=30)[0]
    threshold = ma.playlist_embedding_threshold() if threshold is None else threshold
    rows = []
    for item in index.get('items') or []:
        raw = ma.cosine_similarity(q_vec, item.get('embedding'))
        adjusted = raw + (0.015 if item.get('is_mine') else 0.0) + (0.005 if int(item.get('count') or 0) > 0 else 0.0)
        rows.append({
            'playlist_id': str(item.get('playlist_id') or ''),
            'playlist_name': item.get('playlist_name') or '',
            'raw_score': raw,
            'adjusted_score': adjusted,
            'above_threshold': raw >= threshold,
            'is_mine': bool(item.get('is_mine')),
            'count': int(item.get('count') or 0),
        })
    rows.sort(key=lambda r: (r['adjusted_score'], r['is_mine'], r['count']), reverse=True)
    return rows[:top_k]


def map_one(query: str, top_k: int, threshold: float | None, no_embedding: bool, rebuild: bool):
    playlists = ma.load_playlists()
    if rebuild:
        try:
            ma.EMBEDDINGS_FILE.unlink()
        except FileNotFoundError:
            pass

    fast = ma.fast_match_playlist(query, playlists)
    result = {
        'query': query,
        'playlist_count': len(playlists or []),
        'should_use_playlist': ma.should_use_playlist(query),
        'fast_match': fast,
        'embedding': {
            'enabled': not no_embedding,
            'model': ma.embedding_model_name() if not no_embedding else None,
            'threshold': ma.playlist_embedding_threshold() if threshold is None else threshold,
            'error': None,
            'candidates': [],
        },
        # Current music_agent priority: fast/alias/local rule wins first;
        # embedding is considered next only for playlist-intent queries.
        'recommended_by_debugger': None,
    }

    if not no_embedding:
        try:
            result['embedding']['candidates'] = embedding_candidates(query, playlists, top_k, threshold)
        except Exception as e:
            result['embedding']['error'] = str(e)

    if fast:
        result['recommended_by_debugger'] = {
            'source': 'fast_match',
            'playlist_id': fast.get('playlist_id'),
            'playlist_name': fast.get('playlist_name'),
            'reason': fast.get('reason'),
            'score': fast.get('score'),
        }
    elif result['should_use_playlist'] and result['embedding']['candidates']:
        top = result['embedding']['candidates'][0]
        if top.get('above_threshold'):
            result['recommended_by_debugger'] = {
                'source': 'embedding',
                'playlist_id': top['playlist_id'],
                'playlist_name': top['playlist_name'],
                'raw_score': top['raw_score'],
                'adjusted_score': top['adjusted_score'],
            }

    return result


def print_table(result: dict):
    print('\n' + '═' * 88)
    print(f"query: {result['query']}")
    print(f"playlist_intent: {result['should_use_playlist']}   playlist_count: {result['playlist_count']}")
    rec = result.get('recommended_by_debugger')
    if rec:
        print(f"recommended: [{rec.get('source')}] {rec.get('playlist_name')} ({rec.get('playlist_id')})")
        if rec.get('reason'):
            print(f"reason: {rec.get('reason')}  score={rec.get('score')}")
        if rec.get('raw_score') is not None:
            print(f"score: raw={rec['raw_score']:.4f} adjusted={rec['adjusted_score']:.4f}")
    else:
        print('recommended: <none>')

    fast = result.get('fast_match')
    if fast:
        print('\nfast/alias/local match:')
        print(f"  {fast.get('playlist_name')} ({fast.get('playlist_id')}) score={fast.get('score')} reason={fast.get('reason')} mine={fast.get('is_mine')}")

    emb = result.get('embedding') or {}
    if emb.get('enabled'):
        print(f"\nembedding: model={emb.get('model')} threshold={emb.get('threshold')}")
        if emb.get('error'):
            print(f"  ERROR: {emb['error']}")
        else:
            for i, row in enumerate(emb.get('candidates') or [], 1):
                mark = '✅' if row.get('above_threshold') else '  '
                print(
                    f"  {i:>2}. {mark} raw={row['raw_score']:.4f} adj={row['adjusted_score']:.4f} "
                    f"mine={str(row['is_mine']).lower():<5} count={row['count']:<4} "
                    f"{row['playlist_name']} ({row['playlist_id']})"
                )


def main():
    ap = argparse.ArgumentParser(description='Debug input -> playlist-name mapping without playback.')
    ap.add_argument('queries', nargs='*', help='Queries to map. If empty, read non-empty lines from stdin.')
    ap.add_argument('--top-k', type=int, default=10, help='Number of embedding candidates to show. Default: 10')
    ap.add_argument('--threshold', type=float, default=None, help='Override embedding threshold for this debug run.')
    ap.add_argument('--no-embedding', action='store_true', help='Only show fast/alias/local rule match; do not call embedding API.')
    ap.add_argument('--rebuild', action='store_true', help='Delete and rebuild runtime/playlist_embeddings.json before matching.')
    ap.add_argument('--json', action='store_true', help='Output JSON instead of human-readable table.')
    args = ap.parse_args()

    queries = args.queries or [line.strip() for line in sys.stdin if line.strip()]
    if not queries:
        ap.error('provide at least one query or pipe queries on stdin')

    results = [map_one(q, args.top_k, args.threshold, args.no_embedding, args.rebuild and i == 0) for i, q in enumerate(queries)]
    if args.json:
        print(json.dumps(results if len(results) != 1 else results[0], ensure_ascii=False, indent=2))
    else:
        for r in results:
            print_table(r)


if __name__ == '__main__':
    main()
