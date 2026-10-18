#!/usr/bin/env python3
"""Audit a frozen daily signal cohort and an outcome CSV without network calls."""
import argparse
import csv
import json
from pathlib import Path
import sys

if __name__ == '__main__':
    # Reuse the skill's prepared environment; an offline audit never downloads.
    from tail_screener.runtime import prepare_runtime
    runtime_exit = prepare_runtime(Path(__file__).resolve().parents[1], sys.argv[1:],
                                   offline=True, entrypoint=Path(__file__))
    if runtime_exit is not None:
        raise SystemExit(runtime_exit)

from tail_screener.validation import load_reports, validate_cohort


def main(argv=None):
    parser = argparse.ArgumentParser(description='冻结信号台账的离线前向分段验证；不调参，不保证收益')
    parser.add_argument('reports', nargs='+', type=Path, help='主扫描 report.json 或包含它们的目录；每交易日只输入事前指定的一次主扫描')
    parser.add_argument('--outcomes', type=Path, required=True, help='完整结果 CSV，含未成交/未退出/跳过')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--warmup-days', type=int, default=20)
    parser.add_argument('--test-days', type=int, default=5)
    parser.add_argument('--gap-days', type=int, default=1, help='训练和测试间隔的已记录报告日期数')
    parser.add_argument('--commission', type=float, default=.0003)
    parser.add_argument('--minimum', type=float, default=5)
    parser.add_argument('--stamp-tax', type=float, default=.0005)
    parser.add_argument('--transfer', type=float, default=.00001)
    parser.add_argument('--paper-slippage-bps', type=float, default=10)
    args = parser.parse_args(argv)
    costs = {key: getattr(args, key) for key in ('commission', 'minimum', 'stamp_tax', 'transfer', 'paper_slippage_bps')}
    try:
        reports, signals = load_reports(args.reports)
        with args.outcomes.open(encoding='utf-8-sig', newline='') as handle:
            rows = list(csv.DictReader(handle))
        result = validate_cohort(reports, signals, rows, costs=costs, warmup_days=args.warmup_days,
                                 test_days=args.test_days, gap_days=args.gap_days)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.error(str(exc))
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding='utf-8')
    print(text)
    return result


if __name__ == '__main__':
    main()
