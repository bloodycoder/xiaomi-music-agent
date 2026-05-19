#!/usr/bin/env python3
"""Summarize user-scored benchmark results from runtime/benchmark_results.csv."""
from __future__ import annotations
import csv, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / 'runtime' / 'benchmark_results.csv'


def main():
    if not CSV_PATH.exists():
        print(f'CSV not found: {CSV_PATH}')
        sys.exit(1)

    rows = []
    with CSV_PATH.open('r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    scored = [r for r in rows if r.get('user_score_baseline', '').strip() and r.get('user_score_semantic', '').strip()]
    if not scored:
        print('No scored rows found. Fill in user_score_baseline, user_score_semantic, and user_pick columns in runtime/benchmark_results.csv first.')
        sys.exit(0)

    total = len(scored)
    sum_b = sum(int(r['user_score_baseline']) for r in scored)
    sum_s = sum(int(r['user_score_semantic']) for r in scored)
    avg_b = sum_b / total
    avg_s = sum_s / total

    wins_b = sum(1 for r in scored if r.get('user_pick', '').strip().lower() == 'baseline')
    wins_s = sum(1 for r in scored if r.get('user_pick', '').strip().lower() == 'semantic')
    ties = sum(1 for r in scored if r.get('user_pick', '').strip().lower() == 'tie')

    print('=' * 60)
    print('Benchmark Score Summary')
    print('=' * 60)
    print(f'Scored queries:      {total}')
    print(f'')
    print(f'Baseline avg score:  {avg_b:.2f}')
    print(f'Semantic avg score:  {avg_s:.2f}')
    print(f'')
    print(f'Semantic wins:       {wins_s}')
    print(f'Baseline wins:       {wins_b}')
    print(f'Ties:                {ties}')
    print(f'Semantic win rate:   {wins_s / total * 100:.1f}%' if total else '')
    print('')

    # Low-score semantic cases
    low_s = [(r, int(r['user_score_semantic'])) for r in scored if int(r['user_score_semantic']) <= 2]
    if low_s:
        print('-' * 60)
        print(f'Low-score semantic cases (<=2): {len(low_s)}')
        print('-' * 60)
        for r, s in sorted(low_s, key=lambda x: x[1]):
            print(f"  [{s}] {r['query']}")
            print(f"       sem -> {r['semantic_top1']} ({r['semantic_score']})")
            print(f"       base -> {r['baseline_top1']}")
            print()

    # Wrong-decision cases where user_pick contradicts the decision
    print('-' * 60)
    print('Decision mismatches (user pick vs system decision):')
    print('-' * 60)
    for r in scored:
        pick = r.get('user_pick', '').strip().lower()
        b_dec = r.get('baseline_decision', '')
        s_dec = r.get('semantic_decision', '')
        if pick == 'baseline' and s_dec == 'local' and b_dec != 'local':
            print(f"  {r['query']}: user=baseline({b_dec}) but semantic=local -> false local")
        if pick == 'semantic' and s_dec == 'online_fallback' and b_dec == 'local':
            print(f"  {r['query']}: user=semantic({s_dec}) but baseline=local -> semantic correctly fell back")

    # Per-query detail
    print()
    print('=' * 60)
    print('Per-Query Detail')
    print('=' * 60)
    for r in scored:
        pick = r.get('user_pick', '').strip()
        notes = r.get('notes', '').strip()
        extra = f' [{notes}]' if notes else ''
        print(f"  {r['query']}")
        print(f"    B({r['user_score_baseline']}): {r['baseline_top1']} | S({r['user_score_semantic']}): {r['semantic_top1']} | pick={pick}{extra}")


if __name__ == '__main__':
    main()
