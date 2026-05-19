#!/usr/bin/env python3
from __future__ import annotations
import csv, json, subprocess, sys
from pathlib import Path

ROOT = Path('/Users/picard/xiaomi-music')
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
QF = ROOT / 'runtime' / 'benchmark_queries.txt'
OUT = ROOT / 'runtime' / 'benchmark_results.csv'

import semantic_playlist_mapper as sem

queries = [x.strip() for x in QF.read_text(encoding='utf-8').splitlines() if x.strip()]
rows = []
# Load semantic model/index once inside current process. Baseline remains subprocess
# because it is cheap and uses its existing CLI/json contract.
for q in queries:
    b = subprocess.check_output(['python3','scripts/local_playlist_mapper.py','predict',q,'--top-k','3','--json'], cwd=ROOT, text=True)
    bj = json.loads(b)
    sj = sem.predict(q, top_k=3)
    rows.append({
        'query': q,
        'baseline_decision': bj['decision'],
        'baseline_top1': (bj.get('top1') or {}).get('playlist_name',''),
        'baseline_score': (bj.get('top1') or {}).get('score',''),
        'semantic_decision': sj['decision'],
        'semantic_top1': (sj.get('top1') or {}).get('playlist_name',''),
        'semantic_score': (sj.get('top1') or {}).get('score',''),
        'user_score_baseline': '',
        'user_score_semantic': '',
        'user_pick': '',
        'notes': '',
    })

with OUT.open('w', newline='', encoding='utf-8') as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
    w.writeheader(); w.writerows(rows)
print(f'written {OUT} rows={len(rows)}')
