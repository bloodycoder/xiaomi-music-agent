#!/usr/bin/env python3
"""User-editable disable rules for local music matching.

Rules live in runtime/music_disabled.json.  They are intentionally simple and
are evaluated locally; no network/API/LLM calls are made here.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

ROOT = Path(os.environ.get('XIAOMI_MUSIC_ROOT', Path.home() / 'xiaomi-music')).expanduser()
DISABLED_FILE = ROOT / 'runtime' / 'music_disabled.json'
PLAYLIST_BLACKLIST_FILE = ROOT / 'runtime' / 'playlist_blacklist.json'


def norm_text(text: Any) -> str:
    text = str(text or '').lower()
    text = text.replace('＆', '&').replace('（', '(').replace('）', ')')
    return re.sub(r'[^0-9a-z\u4e00-\u9fff]+', '', text)


def _merge_playlist_blacklist_alias(rules: Dict[str, Any]) -> Dict[str, Any]:
    """Merge runtime/playlist_blacklist.json into disable rules.

    Supported blacklist formats:
      ["歌单名", 123456]
      {"ids": [123456], "names": ["精确歌单名"], "contains": ["年度歌单"], "regex": ["广告|营销"]}
    """
    if not PLAYLIST_BLACKLIST_FILE.exists():
        return rules
    try:
        data = json.loads(PLAYLIST_BLACKLIST_FILE.read_text(encoding='utf-8'))
    except Exception:
        return rules

    merged = dict(rules or {})
    disabled = list(_rows(merged, 'disabled_playlists', 'playlists'))
    contains = list(_rows(merged, 'disabled_playlist_contains', 'playlist_contains', 'contains'))
    regex = list(_rows(merged, 'disabled_playlist_regex', 'playlist_regex', 'regex'))

    if isinstance(data, list):
        disabled.extend(data)
    elif isinstance(data, dict):
        for item in data.get('ids') or data.get('id') or []:
            disabled.append({'id': item})
        for item in data.get('names') or data.get('name') or []:
            disabled.append({'name': item})
        contains.extend(data.get('contains') or data.get('keywords') or [])
        regex.extend(data.get('regex') or data.get('patterns') or [])

    if disabled:
        merged['disabled_playlists'] = disabled
    if contains:
        merged['disabled_playlist_contains'] = contains
    if regex:
        merged['disabled_playlist_regex'] = regex
    return merged


def load_disable_rules(path: Path | None = None) -> Dict[str, Any]:
    p = Path(path or DISABLED_FILE)
    data: Dict[str, Any] = {}
    if p.exists():
        try:
            raw = json.loads(p.read_text(encoding='utf-8'))
            data = raw if isinstance(raw, dict) else {}
        except Exception:
            data = {}
    if path is None:
        data = _merge_playlist_blacklist_alias(data)
    return data


def _rows(data: Dict[str, Any], *keys: str) -> List[Any]:
    out: List[Any] = []
    for k in keys:
        v = data.get(k)
        if isinstance(v, list):
            out.extend(v)
    return out


def _string_rule_matches_playlist(rule: str, pl: Dict[str, Any]) -> bool:
    rid = str(pl.get('id') or '')
    r = str(rule or '').strip()
    if not r:
        return False
    if r.isdigit() and r == rid:
        return True
    return norm_text(r) == norm_text(pl.get('name') or '')


def playlist_disabled(pl: Dict[str, Any], rules: Dict[str, Any] | None = None) -> bool:
    rules = rules if rules is not None else load_disable_rules()
    pid = str(pl.get('id') or '')
    pname = norm_text(pl.get('name') or '')
    for row in _rows(rules, 'disabled_playlists', 'playlists'):
        if isinstance(row, str):
            if _string_rule_matches_playlist(row, pl):
                return True
            continue
        if not isinstance(row, dict):
            continue
        rid = str(row.get('id') or row.get('playlist_id') or '').strip()
        if rid and rid == pid:
            return True
        rname = norm_text(row.get('name') or row.get('playlist_name') or '')
        if rname and rname == pname:
            return True

    raw_name = str(pl.get('name') or '')
    for row in _rows(rules, 'disabled_playlist_contains', 'playlist_contains'):
        kw = str(row or '').strip()
        if kw and kw in raw_name:
            return True

    for row in _rows(rules, 'disabled_playlist_regex', 'playlist_regex'):
        pattern = str(row or '').strip()
        if not pattern:
            continue
        try:
            if re.search(pattern, raw_name, re.I):
                return True
        except re.error:
            continue
    return False


def filter_playlists(playlists: Iterable[Dict[str, Any]] | None, rules: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    rules = rules if rules is not None else load_disable_rules()
    return [p for p in (playlists or []) if isinstance(p, dict) and not playlist_disabled(p, rules)]


def _track_artist_text(track: Dict[str, Any]) -> str:
    artist = track.get('artist') or track.get('artists') or ''
    if isinstance(artist, list):
        return '/'.join(str(x.get('name') if isinstance(x, dict) else x) for x in artist)
    return str(artist or '')


def _string_rule_matches_track(rule: str, track: Dict[str, Any]) -> bool:
    r = str(rule or '').strip()
    if not r:
        return False
    tid = str(track.get('id') or '')
    if r.isdigit() and r == tid:
        return True
    rn = norm_text(r)
    name = norm_text(track.get('name') or '')
    artist = norm_text(_track_artist_text(track))
    # Accept either "歌曲名" or "歌曲名 - 歌手" style strings.
    return rn == name or rn == norm_text(f'{track.get("name") or ""} {_track_artist_text(track)}') or (name and artist and rn == name + artist)


def track_disabled(track: Dict[str, Any], rules: Dict[str, Any] | None = None) -> bool:
    rules = rules if rules is not None else load_disable_rules()
    tid = str(track.get('id') or '')
    name = norm_text(track.get('name') or '')
    artist = norm_text(_track_artist_text(track))

    for row in _rows(rules, 'disabled_artists', 'artists'):
        rartist = norm_text(row.get('name') if isinstance(row, dict) else row)
        if rartist and rartist in artist:
            return True

    for row in _rows(rules, 'disabled_tracks', 'tracks', 'songs', 'disabled_songs'):
        if isinstance(row, str):
            if _string_rule_matches_track(row, track):
                return True
            continue
        if not isinstance(row, dict):
            continue
        rid = str(row.get('id') or row.get('track_id') or row.get('song_id') or '').strip()
        if rid and rid == tid:
            return True
        rname = norm_text(row.get('name') or row.get('track_name') or row.get('song_name') or '')
        rartist = norm_text(row.get('artist') or row.get('artists') or '')
        if rname and rname == name and (not rartist or rartist in artist):
            return True
    return False


def filter_tracks(tracks: Iterable[Dict[str, Any]] | None, rules: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
    rules = rules if rules is not None else load_disable_rules()
    return [t for t in (tracks or []) if isinstance(t, dict) and not track_disabled(t, rules)]
