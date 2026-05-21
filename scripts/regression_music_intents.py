#!/usr/bin/env python3
"""Regression checks for Music Agent intent routing.

These tests protect high-value voice intents that are easy to break when tuning
playlist/entity heuristics. They intentionally import scripts/music_agent.py and
exercise the same helper functions used by /play.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUSIC_AGENT = ROOT / "scripts" / "music_agent.py"


def load_agent():
    spec = importlib.util.spec_from_file_location("music_agent", MUSIC_AGENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def assert_true(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run_helper_regressions():
    ma = load_agent()
    from intent_filter import classify_music_intent
    cases = []

    classifier_expect = {
        "陈楚生的天外的天": "explicit_track",
        "陈楚生 天外的天": "explicit_track",
        "播放陈楚生天外的天": "explicit_track",
        "适合周三的歌单": "mood_playlist",
        "适合星期三听的歌单": "mood_playlist",
        "找higher brothers的歌": "artist_collection",
        "周三的歌": "artist_collection",
        "苏醒的歌": "artist_collection",
        "播放苏醒的歌": "artist_collection",
    }
    for q, expected in classifier_expect.items():
        pred = classify_music_intent(q)
        assert_true(pred.get("intent") == expected, f"{q}: classifier expected {expected}, got {pred}")
        assert_true(float(pred.get("confidence") or 0) >= 0.45, f"{q}: classifier low confidence {pred}")

    # Explicit artist + song title must route to one-song playback, not playlist.
    for q in ["陈楚生的天外的天", "陈楚生 天外的天", "播放陈楚生天外的天"]:
        tracks = ma.try_explicit_song_tracks(q)
        assert_true(tracks, f"{q}: expected explicit song match")
        top = tracks[0]
        assert_true(top.get("name") == "天外的天", f"{q}: expected song 天外的天, got {top}")
        assert_true("陈楚生" in (top.get("artist") or ""), f"{q}: expected artist 陈楚生, got {top}")
        assert_true(not ma.should_use_playlist(q), f"{q}: should not be playlist intent")
        assert_true(not ma.is_artist_collection_query(q), f"{q}: should not be artist_collection intent")
        cases.append({"query": q, "expect": "explicit_song", "top": top})

    # Day-of-week scene playlist must not be misread as artist “周三”.
    for q in ["适合周三的歌单", "适合星期三听的歌单"]:
        assert_true(not ma.is_artist_collection_query(q), f"{q}: should not be artist_collection")
        assert_true(ma.try_explicit_song_tracks(q) is None, f"{q}: should not be explicit song")
        cases.append({"query": q, "expect": "playlist_scene_not_artist"})

    # Real artist collection intents should still be preserved.
    for q in ["找higher brothers的歌", "周三的歌", "苏醒的歌", "播放苏醒的歌"]:
        assert_true(ma.is_artist_collection_query(q), f"{q}: expected artist_collection")
        assert_true(ma.try_explicit_song_tracks(q) is None, f"{q}: should not be explicit song")
        cases.append({"query": q, "expect": "artist_collection"})

    return cases


def live_play_check(query: str):
    url = "http://127.0.0.1:8765/play?q=" + urllib.parse.quote(query)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = json.load(r)
    assert_true(data.get("ok"), f"live /play failed: {data}")
    assert_true(data.get("source") == "explicit_song", f"expected source explicit_song, got {data}")
    track = data.get("track") or {}
    assert_true(track.get("name") == "天外的天", f"expected 天外的天, got {track}")
    assert_true("陈楚生" in (track.get("artist") or ""), f"expected 陈楚生, got {track}")
    return data


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--live-play", action="store_true", help="also call local /play and verify response")
    args = ap.parse_args(argv)

    results = {"helper_cases": run_helper_regressions()}
    if args.live_play:
        results["live_play"] = live_play_check("陈楚生的天外的天")
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("OK: music intent regressions passed", file=sys.stderr)


if __name__ == "__main__":
    main()
