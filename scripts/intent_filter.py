#!/usr/bin/env python3
"""Fast local music-intent classifier.

No LLM. This is a tiny local ML router that classifies query text before the
heavier playlist/song heuristics:

- mood_playlist: scene/activity/mood/list recommendation
- explicit_track: user gave a concrete song title, often with artist
- artist_collection: user wants an artist's songs/collection
- unknown: low confidence fallback

Implementation: character n-gram TF-IDF + LogisticRegression. It is not a
remote neural model; it is local, deterministic, and typically sub-millisecond
after load. The API is intentionally stable so it can later be swapped for an
ONNX/tiny neural classifier without touching music_agent routing.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "runtime" / "intent_filter.joblib"
MODEL_VERSION = "2026-05-21-v2"

# Seed training data. Keep this small and human-readable; add every production
# regression here, then run scripts/regression_music_intents.py.
TRAINING: List[Tuple[str, str]] = [
    # explicit_track: artist + song / direct song title requests
    ("陈楚生 天外的天", "explicit_track"),
    ("陈楚生的天外的天", "explicit_track"),
    ("播放陈楚生天外的天", "explicit_track"),
    ("智能播放陈楚生天外的天", "explicit_track"),
    ("周杰伦 稻香", "explicit_track"),
    ("播放周杰伦稻香", "explicit_track"),
    ("陈奕迅 十年", "explicit_track"),
    ("播放陈奕迅十年", "explicit_track"),
    ("孙燕姿 遇见", "explicit_track"),
    ("王菲 红豆", "explicit_track"),
    ("五月天 突然好想你", "explicit_track"),
    ("林俊杰 江南", "explicit_track"),
    ("播放牧神记主题曲", "explicit_track"),
    ("我要听天外的天", "explicit_track"),
    ("放一首稻香", "explicit_track"),
    ("听十年", "explicit_track"),

    # artist_collection: artist/entity collection, not one exact song
    ("找higher brothers的歌", "artist_collection"),
    ("higher brothers的歌", "artist_collection"),
    ("来点higher brothers的歌", "artist_collection"),
    ("周三的歌", "artist_collection"),
    ("找周三的歌", "artist_collection"),
    ("陈楚生的歌", "artist_collection"),
    ("苏醒的歌", "artist_collection"),
    ("播放苏醒的歌", "artist_collection"),
    ("智能播放苏醒的歌", "artist_collection"),
    ("放苏醒的歌", "artist_collection"),
    ("播放陈楚生的歌", "artist_collection"),
    ("播放周三的歌", "artist_collection"),
    ("来点陈楚生的歌", "artist_collection"),
    ("陈奕迅的歌", "artist_collection"),
    ("周杰伦的热门歌曲", "artist_collection"),
    ("播放罗大佑经典", "artist_collection"),
    ("来点小虎队经典", "artist_collection"),
    ("找一些王菲的歌", "artist_collection"),
    ("来几首孙燕姿", "artist_collection"),

    # mood_playlist: scene/activity/time/mood/list recommendations
    ("适合周三的歌单", "mood_playlist"),
    ("适合星期三听的歌单", "mood_playlist"),
    ("周三适合听的音乐", "mood_playlist"),
    ("今天周三适合听什么", "mood_playlist"),
    ("下雨天推荐个歌单", "mood_playlist"),
    ("现在下雨给我下雨适合的歌单", "mood_playlist"),
    ("上班后舒缓的音乐", "mood_playlist"),
    ("下班的歌单", "mood_playlist"),
    ("睡前的歌单", "mood_playlist"),
    ("舒缓的歌单", "mood_playlist"),
    ("晚上想听安静一点的", "mood_playlist"),
    ("早起给我来点清爽的", "mood_playlist"),
    ("通勤路上来点有节奏的", "mood_playlist"),
    ("做饭来点不吵的", "mood_playlist"),
    ("我要拉屎了来点歌", "mood_playlist"),
    ("做爱的时候来点氛围音乐", "mood_playlist"),
    ("休闲放空来点舒服的", "mood_playlist"),
    ("来个放松歌单", "mood_playlist"),
    ("冥想的时候放点音乐", "mood_playlist"),
    ("写代码来个专注歌单", "mood_playlist"),
    ("看书学习来个安静歌单", "mood_playlist"),
    ("洗澡的时候来点轻松的", "mood_playlist"),
    ("健身的时候来点燃的", "mood_playlist"),
    ("适合开车听的歌单", "mood_playlist"),
    ("来点雨声睡觉", "mood_playlist"),
]


def _build_model() -> Pipeline:
    xs = [x for x, _ in TRAINING]
    ys = [y for _, y in TRAINING]
    model = Pipeline([
        ("tfidf", TfidfVectorizer(analyzer="char", ngram_range=(1, 4), lowercase=True, min_df=1)),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced", random_state=42)),
    ])
    model.fit(xs, ys)
    return model


@lru_cache(maxsize=1)
def load_model() -> Pipeline:
    try:
        payload = joblib.load(MODEL_PATH)
        if isinstance(payload, dict) and payload.get("version") == MODEL_VERSION and payload.get("model") is not None:
            return payload["model"]
    except Exception:
        pass
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    model = _build_model()
    joblib.dump({"version": MODEL_VERSION, "model": model}, MODEL_PATH)
    return model


def classify_music_intent(query: str, unknown_threshold: float = 0.45) -> Dict[str, object]:
    q = (query or "").strip()
    if not q:
        return {"intent": "unknown", "binary": None, "confidence": 0.0, "scores": {}, "reason": "empty query"}
    model = load_model()
    labels = [str(x) for x in model.classes_]
    probs = model.predict_proba([q])[0]
    scores = {str(label): float(prob) for label, prob in zip(labels, probs)}
    intent = max(scores, key=scores.get)
    confidence = float(scores[intent])
    if confidence < unknown_threshold:
        intent = "unknown"
    binary = 0 if intent == "mood_playlist" else (1 if intent in ("explicit_track", "artist_collection") else None)
    return {
        "intent": intent,
        "binary": binary,
        "confidence": confidence,
        "scores": scores,
        "reason": "local char-ngram logistic-regression classifier",
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = [classify_music_intent(q) | {"query": q} for q in args.queries]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"{row['query']} -> {row['intent']} binary={row['binary']} conf={row['confidence']:.3f} scores={row['scores']}")
