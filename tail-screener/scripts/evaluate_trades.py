#!/usr/bin/env python3
"""Evaluate recorded fills; do not infer entries/exits from daily highs."""
import argparse
import csv
import json
from pathlib import Path
import sys
from datetime import time
from math import isfinite

if __name__ == '__main__':
    # Reuse the skill's prepared environment; an offline audit never downloads.
    from tail_screener.runtime import prepare_runtime
    runtime_exit = prepare_runtime(Path(__file__).resolve().parents[1], sys.argv[1:],
                                   offline=True, entrypoint=Path(__file__))
    if runtime_exit is not None:
        raise SystemExit(runtime_exit)

from tail_screener.engine import is_a_share, stamp
from tail_screener.calendar import calendar_context


def evaluate_trade(row, commission=.0003, minimum=5, stamp_tax=.0005, transfer=.00001, paper_slippage_bps=10):
    code=row['code']
    if not is_a_share(code):
        raise ValueError('只支持沪深A股')
    entry,exit=stamp(row['entry_at']),stamp(row['exit_at'])
    if entry is None or exit is None or len(row['entry_at'])<=10 or len(row['exit_at'])<=10:
        raise ValueError('需完整成交日期时间')
    cal=calendar_context(entry)
    exit_cal=calendar_context(exit)
    if not cal.get('verified') or not cal.get('is_session') or not exit_cal.get('is_session'):
        raise ValueError('成交日期不是可核验交易日')
    if exit.date().isoformat()<cal['next_session']:
        raise ValueError('违反A股T+1')
    for at in (entry, exit):
        clock=at.time().replace(tzinfo=None)
        if not (time(9,30)<=clock<=time(11,30) or time(13)<=clock<=time(15)):
            raise ValueError('成交时间不在连续交易或收盘竞价时段')
    ep,xp=float(row['entry_price']),float(row['exit_price'])
    qty=int(row['shares'])
    if not isfinite(ep) or not isfinite(xp) or ep<=0 or xp<=0 or qty<=0:
        raise ValueError('成交价和数量必须有效')
    kind=row.get('kind')
    if kind not in ('actual','paper'):
        raise ValueError('kind 必须为 actual 或 paper')
    slip=paper_slippage_bps/10000 if kind=='paper' else 0
    bought=ep*(1+slip)*qty
    sold=xp*(1-slip)*qty
    fees=max(minimum,bought*commission)+max(minimum,sold*commission)+(bought+sold)*transfer+sold*stamp_tax
    net=sold-bought-fees
    return {'code':code,'kind':kind,'gross_pnl':round((xp-ep)*qty,2),'fees':round(fees,2),
            'net_pnl':round(net,2),'net_return_pct':round(net/bought*100,4),
            'overdue_exit':exit.date().isoformat()>cal['next_session'],
            'entry_notional':round(bought,2),'slippage_bps_per_side':paper_slippage_bps if kind=='paper' else 0}


def main():
    p=argparse.ArgumentParser(description='根据已记录成交计算净收益，不以事后最高价推算收益')
    p.add_argument('csv_file')
    p.add_argument('--commission',type=float,default=.0003)
    p.add_argument('--minimum',type=float,default=5)
    p.add_argument('--stamp-tax',type=float,default=.0005)
    p.add_argument('--transfer',type=float,default=.00001)
    p.add_argument('--paper-slippage-bps',type=float,default=10)
    args=p.parse_args()
    costs={k:getattr(args,k) for k in ('commission','minimum','stamp_tax','transfer','paper_slippage_bps')}
    if any(not isfinite(x) or x<0 for x in costs.values()) or costs['paper_slippage_bps']>=10000:
        p.error('费用和滑点必须为非负有限数字，滑点需低于10000bp')
    trades,errors=[],[]
    with open(args.csv_file,encoding='utf-8-sig',newline='') as f:
        for line,row in enumerate(csv.DictReader(f),2):
            try:
                trades.append(evaluate_trade(row,**costs))
            except (ValueError,KeyError,TypeError) as exc:
                errors.append({'line':line,'error':str(exc)})
    print(json.dumps({'trades':trades,'errors':errors,'closed_trades':len(trades),
                      'net_pnl':round(sum(t['net_pnl'] for t in trades),2),
                      'win_rate_of_recorded_trades':sum(t['net_pnl']>0 for t in trades)/len(trades) if trades else None,
                      'note':'仅评价提交的已闭合成交，不能证明策略胜率；未成交和未退出记录需另行保留。'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
