#!/usr/bin/env python3
from __future__ import annotations
import csv
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path('/Users/picard/xiaomi-music')
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
QF = ROOT / 'runtime' / 'benchmark_queries.txt'
OUT = ROOT / 'runtime' / 'benchmark_results.csv'
REVIEW = ROOT / 'runtime' / 'benchmark_review.md'

import local_playlist_mapper as base  # noqa: E402
import semantic_playlist_mapper as sem  # noqa: E402


def ms_since(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


def score_value(row: dict | None):
    if not row:
        return ''
    v = row.get('score', '')
    if isinstance(v, float):
        return f'{v:.6f}'
    return v


def playlist_name(row: dict | None) -> str:
    return (row or {}).get('playlist_name', '')


def baseline_predict_payload(query: str, top_k: int = 3) -> dict:
    playlists = base.ma.load_playlists()
    rule = base.apply_rules(query, playlists)
    rows = base.predict(query, top_k)
    if rule:
        top = dict(rows[0]) if rows else {
            'playlist_id': '', 'playlist_name': '', 'score': 0.0,
            'doc_sim': 0.0, 'label_sim': 0.0, 'is_mine': False, 'count': 0, 'reason': ''
        }
        top.update(rule)
        top['score'] = max(float(top.get('score', 0.0)), 9.9)
        top['reason'] = rule.get('reason', 'rule')
        rows = [top] + [r for r in rows if r.get('playlist_id') != rule['playlist_id']]
    fallback, fb_reason = base.should_fallback_online(query, rows)
    return {
        'query': query,
        'decision': 'online_fallback' if fallback else 'local',
        'decision_reason': fb_reason,
        'top1': rows[0] if rows else None,
        'candidates': rows[:top_k],
    }


def load_previous_user_fields() -> dict[str, dict]:
    if not OUT.exists():
        return {}
    fields = {}
    try:
        with OUT.open('r', encoding='utf-8') as f:
            for r in csv.DictReader(f):
                q = r.get('query') or ''
                if not q:
                    continue
                fields[q] = {
                    'user_score_baseline': r.get('user_score_baseline', ''),
                    'user_score_semantic': r.get('user_score_semantic', ''),
                    'user_pick': r.get('user_pick', ''),
                    'notes': r.get('notes', ''),
                }
    except Exception:
        return {}
    return fields


def pct(values, p):
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(values) - 1)
    if f == c:
        return values[f]
    return values[f] * (c - k) + values[c] * (k - f)


def latency_summary(rows: list[dict]) -> dict:
    sem_ms = [float(r['semantic_ms']) for r in rows if r.get('semantic_ms')]
    base_ms = [float(r['baseline_ms']) for r in rows if r.get('baseline_ms')]
    emb_ms = [float(r['semantic_embed_ms']) for r in rows if r.get('semantic_embed_ms')]
    score_ms = [float(r['semantic_score_ms']) for r in rows if r.get('semantic_score_ms')]
    return {
        'baseline_avg_ms': statistics.mean(base_ms) if base_ms else 0,
        'baseline_p50_ms': statistics.median(base_ms) if base_ms else 0,
        'baseline_p95_ms': pct(base_ms, 95),
        'semantic_avg_ms': statistics.mean(sem_ms) if sem_ms else 0,
        'semantic_p50_ms': statistics.median(sem_ms) if sem_ms else 0,
        'semantic_p95_ms': pct(sem_ms, 95),
        'semantic_max_ms': max(sem_ms) if sem_ms else 0,
        'semantic_embed_avg_ms': statistics.mean(emb_ms) if emb_ms else 0,
        'semantic_score_avg_ms': statistics.mean(score_ms) if score_ms else 0,
    }


def write_review(rows: list[dict], warmup_ms: float):
    s = latency_summary(rows)
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    lines = []
    lines.append('# 歌单映射 Benchmark 评分表')
    lines.append('')
    lines.append(f'更新时间：{now}（第五轮：加入匹配延迟指标）')
    lines.append('')
    lines.append('本轮只增加 benchmark 计时，不改匹配逻辑。请继续对 baseline 和 semantic 结果分别打分（1~5）。')
    lines.append('')
    lines.append('## 延迟摘要')
    lines.append('')
    lines.append('| 指标 | 数值 | 说明 |')
    lines.append('|---|---:|---|')
    lines.append(f'| semantic warmup | {warmup_ms:.1f} ms | 进程内首次加载 index/model 的预热耗时，不计入逐条热路径 |')
    lines.append(f"| semantic avg | {s['semantic_avg_ms']:.1f} ms | 逐条 query 的 in-process 热路径平均耗时 |")
    lines.append(f"| semantic p50 | {s['semantic_p50_ms']:.1f} ms | 逐条 query 中位数 |")
    lines.append(f"| semantic p95 | {s['semantic_p95_ms']:.1f} ms | 逐条 query 95 分位 |")
    lines.append(f"| semantic max | {s['semantic_max_ms']:.1f} ms | 最慢 query |")
    lines.append(f"| semantic embed avg | {s['semantic_embed_avg_ms']:.1f} ms | 主要耗时：query embedding |")
    lines.append(f"| semantic rerank avg | {s['semantic_score_avg_ms']:.1f} ms | 向量打分 + mood rerank |")
    lines.append(f"| baseline avg | {s['baseline_avg_ms']:.1f} ms | local_playlist_mapper 进程内算法耗时 |")
    lines.append(f"| baseline p95 | {s['baseline_p95_ms']:.1f} ms | local_playlist_mapper 95 分位 |")
    lines.append('')
    lines.append('## 明细')
    lines.append('')
    lines.append('| # | Query | B-Dec | Baseline Top1 | B ms | S-Score | S-Dec | Semantic Top1 | S ms | Emb ms | Score ms | B(1-5) | S(1-5) | Pick | 备注 |')
    lines.append('|---|-------|-------|---------------|-----:|---------|-------|---------------|-----:|-------:|---------:|--------|--------|------|------|')
    for i, r in enumerate(rows, 1):
        b_dec = r['baseline_decision']
        s_dec = r['semantic_decision']
        b_dec_fmt = '**local**' if b_dec == 'local' else b_dec.replace('online_fallback', 'online')
        s_dec_fmt = '**local**' if s_dec == 'local' else s_dec.replace('online_fallback', 'online')
        score = r['semantic_score']
        try:
            score_fmt = f"{float(score):.3f}"
        except Exception:
            score_fmt = str(score)
        sem_name = r['semantic_top1']
        if s_dec != 'local' and sem_name:
            sem_name = f'{sem_name} (fallback)'
        lines.append(
            f"| {i} | {r['query']} | {b_dec_fmt} | {r['baseline_top1']} | {float(r['baseline_ms']):.1f} | "
            f"{score_fmt} | {s_dec_fmt} | {sem_name} | {float(r['semantic_ms']):.1f} | "
            f"{float(r['semantic_embed_ms']):.1f} | {float(r['semantic_score_ms']):.1f} | "
            f"{r.get('user_score_baseline','')} | {r.get('user_score_semantic','')} | {r.get('user_pick','')} | {r.get('notes','')} |"
        )
    lines.append('')
    lines.append('## 本轮说明')
    lines.append('')
    lines.append('- `semantic_ms` 是已经在同一 Python 进程内预热后的映射耗时，适合评估常驻服务里的热路径。')
    lines.append('- `semantic warmup` 是模型/index 首次加载成本；服务启动后应后台预热，避免第一条语音命令承担这部分耗时。')
    lines.append('- 本轮未改 mood taxonomy、阈值、rerank 或 entity fallback，只是把 latency 加入 benchmark。')
    REVIEW.write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    queries = [x.strip() for x in QF.read_text(encoding='utf-8').splitlines() if x.strip()]
    previous = load_previous_user_fields()

    # Warm semantic mapper once: this pays model/index loading outside per-query
    # hot-path latency, mirroring a warmed long-running agent process.
    t0 = time.perf_counter()
    sem.predict('benchmark warmup', top_k=1, return_timing=True)
    warmup_ms = ms_since(t0)

    rows = []
    for q in queries:
        t0 = time.perf_counter()
        bj = baseline_predict_payload(q, top_k=3)
        baseline_ms = ms_since(t0)

        sj = sem.predict(q, top_k=3, return_timing=True)
        timing = sj.get('timing') or {}
        prev = previous.get(q, {})
        rows.append({
            'query': q,
            'baseline_decision': bj['decision'],
            'baseline_top1': playlist_name(bj.get('top1')),
            'baseline_score': score_value(bj.get('top1')),
            'baseline_ms': f'{baseline_ms:.3f}',
            'semantic_decision': sj['decision'],
            'semantic_top1': playlist_name(sj.get('top1')),
            'semantic_score': score_value(sj.get('top1')),
            'semantic_ms': f"{float(timing.get('total_ms', 0)):.3f}",
            'semantic_load_index_ms': f"{float(timing.get('load_index_ms', 0)):.3f}",
            'semantic_nlu_ms': f"{float(timing.get('nlu_ms', 0)):.3f}",
            'semantic_embed_ms': f"{float(timing.get('embed_ms', 0)):.3f}",
            'semantic_score_ms': f"{float(timing.get('score_ms', 0)):.3f}",
            'user_score_baseline': prev.get('user_score_baseline', ''),
            'user_score_semantic': prev.get('user_score_semantic', ''),
            'user_pick': prev.get('user_pick', ''),
            'notes': prev.get('notes', ''),
        })

    with OUT.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    write_review(rows, warmup_ms)

    s = latency_summary(rows)
    print(f'written {OUT} rows={len(rows)}')
    print(f'written {REVIEW}')
    print(f"semantic latency: avg={s['semantic_avg_ms']:.1f}ms p50={s['semantic_p50_ms']:.1f}ms p95={s['semantic_p95_ms']:.1f}ms max={s['semantic_max_ms']:.1f}ms warmup={warmup_ms:.1f}ms")
    print(f"baseline latency: avg={s['baseline_avg_ms']:.1f}ms p95={s['baseline_p95_ms']:.1f}ms")


if __name__ == '__main__':
    main()
