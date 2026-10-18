"""Rank a verified saved report with the frozen experimental HGB33 model."""
import argparse,json
from pathlib import Path
from html import escape
from tail_screener.experimental_ranking import load_and_rank

def render(r):
    esc=lambda x:escape(str(x))
    rows=[]
    for x in r['candidates']:
        k=x['kline_review'];patterns='；'.join(k.get('patterns',[])) or '无已定义特殊形态'
        rows.append('<tr>'+''.join('<td>'+esc(v)+'</td>' for v in [x['rank'],x['name'],x['code'],f'{x["experimental_score"]:.6f}',x['liquidity_rank'],f'{x["price"]:.2f}',f'{x["r1_pct"]:+.2f}%',f'{x["r5_pct"]:+.2f}%',f'{x["ma5_change_pct"]:+.2f}%',k.get('position','未知')+'；'+patterns])+'</tr>')
    h=r['historical_comparison']
    note=('已完成实验排序。' if r['status']=='computed' else '排序状态：'+r['status']+'。')
    if r['status']=='unsupported_horizon':note+='当前输入的持有期与模型 H2 标签不同，未修改用户输入或生成其他期限的模型名次。'
    context=r.get('feature_context',{})
    return '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>HGB33实验排序</title><style>body{font-family:system-ui,"Microsoft YaHei",sans-serif;max-width:1200px;margin:32px auto;padding:0 22px;color:#20334b;line-height:1.7}table{border-collapse:collapse;font-size:14px;width:100%}th,td{border:1px solid #d7dfe9;padding:9px;text-align:left;vertical-align:top}th{background:#edf2f8}.table{overflow-x:auto}.note{background:#fff4de;padding:15px}small{color:#5d6d80}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><h1>HGB33 实验候选排序</h1>'''+f'<p>信号日 {esc(r["data_session"])} · 展示 {len(rows)} / 请求 {r["requested_top"]} 只 · 最多 {r["configuration"]["max_positions"]} 仓 · 用户持有期 {r["configuration"]["max_holding_sessions"]} · 名义单仓预算 {r["nominal_budget_yuan"]:.2f} 元</p><p class="note">{esc(note)} 模型训练截止 {esc(r["model"]["training"]["last_signal"])}；排名分数不是胜率或当前预期收益。新名次未自动继承原名单的个股公告审查，也未改动账户。</p>'+('<p>这是已有快照的离线重排，没有更新行情。</p>' if r['source_offline_preview'] else '<p>仅使用来源报告对应的完整日线快照，未额外获取盘中数据。</p>')+f'<p>严格合格 {r.get("strict_eligible_count",0)} 只，完成评分 {r.get("ranked_count",0)} 只；与原展示名单重合 {r.get("overlap_with_baseline",0)} 只。市场背景特征完整 {context.get("complete_symbols",0)} / {context.get("active_symbols",0)} 只。</p><div class="table"><table><thead><tr>'+''.join('<th>'+x+'</th>' for x in ['研究名次','名称','代码','模型分数','原流动性名次','信号收盘','1日涨跌','5日涨跌','MA5变化','K线证据'])+'</tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'+f'<h2>采用此实验方案的依据</h2><p>既有 2019—2021 逐年前推研究：原流动性净均值 {h["original_filled_net_pct"]:+.4f}%，33特征HGB {h["hgb33_filled_net_pct"]:+.4f}%；HGB 去最高五笔后 {h["hgb33_trim5_pct"]:+.4f}%。这是历史条件化成交样本，不是本次快照回报。当前包只恢复最后一折模型，不是将三折平均表现归给单个模型。</p><p>按分数选取前 N 名，不要求分数为正，不强制买满仓位。表中量价指标是观察证据，不是模型贡献拆解。完整顺序及来源哈希见同目录 report.json。</p><details><summary>模型适用范围与缺项</summary><pre>'+esc(json.dumps({k:r.get(k) for k in ['status','tested_budget_and_entry_scope_matches','context_complete','unscored','source','model']},ensure_ascii=False,indent=2))+'</pre></details></body></html>'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--report',required=True,type=Path);p.add_argument('--snapshot',type=Path)
    p.add_argument('--output',required=True,type=Path);p.add_argument('--top',type=int)
    args=p.parse_args()
    try:
        if args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir())):raise ValueError('output_must_be_new_or_empty')
        result=load_and_rank(args.report,args.snapshot,top=args.top)
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
        (args.output/'index.html').write_text(render(result),encoding='utf-8')
    except (OSError,ValueError,KeyError,TypeError) as exc:p.error(str(exc))
    print(json.dumps({'status':result['status'],'output':str(args.output.resolve()),'displayed':len(result['candidates']),'ranked':result.get('ranked_count',0),'account_changed':False},ensure_ascii=False))
    return 0 if result['status']=='computed' else 1

if __name__=='__main__':raise SystemExit(main())
