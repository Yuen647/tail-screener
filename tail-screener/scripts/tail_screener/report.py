"""Self-contained, escaped HTML and inspectable JSON artifacts."""
from datetime import datetime
from html import escape
from pathlib import Path
import json
from urllib.parse import urlparse


def safe_link(url, label):
    try:
        if urlparse(str(url)).scheme not in ('http', 'https'):
            return escape(label)
    except ValueError:
        return escape(label)
    return f'<a href="{escape(str(url),quote=True)}" rel="noreferrer">{escape(label)}</a>'


def fmt(value, digits=2):
    return '—' if value is None else f'{value:,.{digits}f}'


def coverage_details(report):
    """Keep catalog completeness separate from ratios with different denominators."""
    context = report['context']
    counts = context.get('coverage_counts', {})
    meta = report.get('universe_metadata', {})
    valid = counts.get('valid_quotes', report['stats'].get('universe'))
    denominator = counts.get('quote_denominator', meta.get('eligible_symbols'))
    market = counts.get('market_quotes', report['stats'].get('market_quotes_used'))
    catalog_complete = meta.get('catalog_complete')
    catalog_label = ('通过' if catalog_complete is True else '未通过' if catalog_complete is False
                     else '未记录，需查看输入快照')

    def ratio(numerator, denominator, fallback=None):
        if numerator is not None and denominator is not None:
            percent = f'（{numerator/denominator:.1%}）' if denominator else '（分母为0，不计算比例）'
            return f'{numerator:,} / {denominator:,} {percent}'
        return f'{fallback:.1%}（原始记录未提供计数口径）' if fallback is not None else '未记录'

    catalog_ratio = ratio(meta.get('received_unique'), meta.get('reported_total'))
    quote_ratio = ratio(valid, denominator, context.get('valid_quote_coverage'))
    rows = [
        ('股票目录完整性', catalog_label),
        ('目录记录取得数 / 源接口记录总数', catalog_ratio + '；源记录可能包含历史代码，不等于沪深在市股票数'),
        ('有效报价 / 已取得目录中的沪深标的', quote_ratio + '；目录未通过核验时，不能视为全市场覆盖率'),
        ('行业已映射 / 本次市场统计样本', ratio(counts.get('industry_mapped'), market, context.get('industry_coverage'))),
        ('上涨标的 / 本次市场统计样本', ratio(counts.get('advancing_quotes'), market, context.get('market_breadth'))),
    ]
    if report.get('analysis_mode') == 'post_close':
        research = context.get('research', {})
        rows.append(('收盘日期与时点有效 / 已收到有效报价',
                     ratio(research.get('valid_close_count'), valid, research.get('same_session_quote_coverage'))
                     + '；日期核验比例，不代表全市场覆盖率'))
    else:
        rows.append(('新鲜且带日期报价 / 已取得目录中的沪深标的',
                     ratio(counts.get('dated_quotes'), denominator, context.get('dated_quote_coverage'))))
    return ''.join(f'<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>' for label, value in rows)


def collection_diagnostics_html(report):
    diagnostics = report.get('diagnostics') or {}
    if diagnostics.get('mode') != 'actual_collection':
        return ''
    categories = diagnostics.get('request_categories', {})
    stage_names = {'universe':'股票目录与全市场报价', 'industries':'行业映射', 'history':'历史量价',
                   'deep':'深入采集调度', 'enrichment':'财报与公告核查', 'refresh':'分时与报价刷新', 'indices':'指数'}
    category_names = {'ok':'成功响应', 'timeout':'超时', 'circuit_open':'熔断跳过',
                      'budget_expired':'预算截止跳过', 'connection_error':'连接失败', 'proxy_error':'代理连接失败',
                      'http_error':'HTTP错误', 'tls_error':'TLS错误', 'invalid_response':'响应解析失败'}
    remaining = [(stage,count) for stage,count in diagnostics.get('remaining_failures', {}).items() if count]
    remaining_html = ('；'.join(f'{escape(stage_names.get(stage,stage))} {count} 项' for stage,count in remaining)
                      if remaining else '已记录的采集阶段没有剩余错误；是否通过筛选仍以数据核验和规则结果为准。')
    stages = diagnostics.get('stage_seconds') or report.get('stage_seconds', {})
    stage_rows = ''.join(f'<tr><td>{escape(stage_names.get(stage,stage))}</td><td>{fmt(seconds)} 秒</td></tr>'
                         for stage,seconds in stages.items())
    source_rows = ''.join(
        f'<tr><td>{escape(str(row.get("host") or "未记录"))}</td>'
        f'<td>{escape(category_names.get(row.get("category"),str(row.get("category") or "未记录")))}</td>'
        f'<td>{row.get("attempts",0)}</td><td>{escape(str(row.get("status_code") or "—"))}</td>'
        f'<td>{fmt(row["elapsed_ms"]/1000) if row.get("elapsed_ms") is not None else "—"} 秒</td></tr>'
        for row in diagnostics.get('sources', []))
    source_html = ('<details><summary>逐数据源请求记录</summary><p>计数包含重试；熔断或预算截止表示请求被跳过。'
                   '下表耗时为各请求耗时相加，并发请求存在重叠，不能当作本次采集总耗时。</p>'
                   '<table><tr><th>数据源</th><th>结果</th><th>次数</th><th>HTTP状态</th><th>请求耗时合计</th></tr>'
                   +source_rows+'</table></details>' if source_rows else '<p>未记录逐数据源请求明细。</p>')
    total = diagnostics.get('elapsed_seconds', report.get('stats',{}).get('elapsed_seconds'))
    budget = diagnostics.get('budget_seconds', report.get('config',{}).get('budget_seconds'))
    return (f'<div class="section"><h2>本次采集诊断</h2>'
            f'<p>采集总耗时 <b>{fmt(total)} 秒</b>（实际经过时间） · 配置采集预算 <b>{fmt(budget)} 秒</b></p>'
            f'<p>成功响应 <b>{categories.get("ok",0)}</b> 次 · 超时 <b>{categories.get("timeout",0)}</b> 次 · '
            f'熔断跳过 <b>{categories.get("circuit_open",0)}</b> 次 · 预算截止跳过 <b>{categories.get("budget_expired",0)}</b> 次。'
            '统计本次实际采集记录，包含重试；出现超时不等于接口永久不可用。</p>'
            f'<p><b>剩余采集缺项：</b>{remaining_html}</p>'
            + ('<details><summary>各阶段实际耗时</summary><table><tr><th>阶段</th><th>实际经过时间</th></tr>'
               +stage_rows+'</table></details>' if stage_rows else '')
            +source_html+'</div>')


def watch_conditions_html(item):
    """Render the frozen observations and next-scan tests without price plans."""
    def value_text(value, unit):
        if value is None:
            return '未记录'
        if unit == 'checklist':
            return '；'.join(str(v) for v in value) if isinstance(value, list) else str(value)
        if isinstance(value, (int, float)):
            if unit == 'fraction':
                return f'{value:.1%}'
            if unit == 'percent':
                return f'{value:+.2f}%'
            return fmt(value)
        return str(value)

    def condition_row(condition):
        unit = condition.get('unit')
        observed = condition.get('observed')
        required = condition.get('required') or {}
        operator = required.get('operator')
        if operator == 'price_over_recomputed_ma20_between':
            source = observed if isinstance(observed, dict) else {}
            observed_text = f"参考价 {fmt(source.get('price'))} / MA20 {fmt(source.get('ma20'))}"
            threshold = (f"实时价 / 重新计算的MA20 须在 {fmt(required.get('minimum'))}～"
                         f"{fmt(required.get('maximum'))} 倍；每次重算")
        else:
            observed_text = value_text(observed, unit)
            prefix = '' if operator == 'all_pass' else str(operator or '') + ' '
            threshold = prefix + value_text(required.get('value'), unit)
        return (f'<tr><td>{escape(str(condition.get("label") or "复核条件"))}</td>'
                f'<td>{escape(observed_text)}</td><td>{escape(threshold)}</td></tr>')

    conditions = ''.join(condition_row(row) for row in item.get('watch_conditions', []))
    invalidations = ''.join(condition_row(row) for row in item.get('watch_invalidation', []))
    basis = (item.get('watch_basis') or {}).get('rule')
    result = ('<h4>下次扫描的量化复核条件</h4><p>以下为本次观测值；环境改善后，个股条件也须重新通过全部检查。</p>'
              '<table><tr><th>复核项</th><th>本次观测</th><th>重新筛选要求</th></tr>'
              + conditions + '</table>')
    if invalidations:
        result += ('<h4>移出观察或暂停资格的条件</h4><table><tr><th>失效情形</th><th>本次观测</th>'
                   '<th>维持观察所需条件</th></tr>' + invalidations + '</table>')
    if basis:
        result += '<p><small>' + escape(str(basis)) + '</small></p>'
    return result


def opportunity_summary_html(report):
    summary = report.get('opportunity_summary')
    if not summary:
        return ''
    combined = summary.get('data_classification') == 'combined_with_other'
    other_label = '其他未通过（含待核数据）' if combined else '存在其他规则阻断'
    rows = [('深入核查', 'evaluated_count'), ('全部规则通过', 'rule_passed_count'),
            ('仅环境未通过', 'environment_only_count'), (other_label, 'other_blocked_count'),
            ('数据核验不完整', 'data_incomplete_count')]
    headings = ''.join('<th>' + label + '</th>' for label, _ in rows)
    values = ''.join('<td>' + ('未单独分类' if key == 'data_incomplete_count' and summary.get(key) is None
                              else escape(str(summary.get(key, '未记录')))) + '</td>' for _, key in rows)
    classification_note = ('盘中个股的数据缺项合并在“其他未通过（含待核数据）”，未单独计数，不能据此宣称没有缺项。'
                           if combined else '')
    return ('<div class="section"><h2>本次深入核查分布</h2><table><tr>' + headings + '</tr><tr>'
            + values + '</tr></table><p>' + classification_note + '统计本次全部深入核查标的；展示名单还受数量、行业和相关性限制。'
            '同一交易日重复运行不能用于判断长期候选率，策略效果仍需不同交易日的前向记录。</p></div>')


def write_report(report, output: Path):
    output.mkdir(parents=True,exist_ok=True)
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    post_close=report.get('analysis_mode')=='post_close'
    run_status=report.get('run_status',{})
    incomplete=run_status.get('data_status') in ('failed','incomplete')
    market_incomplete=run_status.get('market_scope_complete') is False or report.get('universe_metadata',{}).get('catalog_complete') is False
    replay=report.get('mode')=='离线回放' or run_status.get('timing_status')=='replay'
    title='收盘数据筛选 · 下一交易日观察' if post_close else '14:30 尾盘短线观察'
    if market_incomplete:
        title='全市场扫描未完成 · 采集核查报告'
    subtitle=('采集或核验未完成，当前无法形成可信的筛选名单。下列记录用于检查采集范围与缺项。' if incomplete else
              ('按已核验的完整交易日收盘数据筛选，结合原采集时点已知财报与公告，仅供冻结输入的研究复核。' if replay else
               '按已核验的完整交易日收盘数据筛选，结合生成时点已知财报与公告，供下一交易日复核。')
              if post_close else '低位整理 → 温和放量 → 趋势转强 → 公告与成交复核。隔夜持有，下一交易日按计划退出。')
    title_date=(report.get('data_session') or '交易日待核验') if post_close else report['as_of'][:10]
    data_note=f" · 行情所属交易日：{escape(str(report.get('data_session') or '待核验'))}" if post_close else ''
    cards=[]
    watch_cards=[]
    diagnostic_cards=[]

    def reason_text(value):
        if market_incomplete and value == '全市场收盘上涨家数占比过低':
            return '已采集样本上涨家数占比低于门槛（全市场尚未核验）'
        return value

    for i,r in enumerate(report['picks'],1):
        m=r['metrics']
        evidence=r.get('catalysts',[])+r.get('notice_risks',[])
        links=' · '.join(safe_link(e['url'],e['title']) for e in evidence[:5])
        f=r.get('financials') or {}
        if f.get('source_url'):
            links += '<p>'+safe_link(f['source_url'],f"财报 {f.get('report_date','—')} · 营收增速 {fmt(f.get('revenue_growth_pct'))}%")+'</p>'
        signals='；'.join(r.get('signals') or []) or '未发现足够明确的低位转强证据'
        enhanced=f"ATR14：{fmt(m.get('atr14_pct')*100) if m.get('atr14_pct') is not None else '—'}% · 10日整理宽度：{fmt(m.get('consolidation_width_10d')*100) if m.get('consolidation_width_10d') is not None else '—'}% · 尾盘高点回撤：{fmt(m.get('tail_peak_pullback_pct')*100) if m.get('tail_peak_pullback_pct') is not None else '—'}%"
        watching=bool(r.get('watch_eligible')) and not incomplete and not market_incomplete
        blockers=r.get('environment_blockers',r['blockers']) if watching else r['blockers']
        risks=''.join(f'<li>{escape(reason_text(x))}</li>' for x in blockers)
        plan=None if r.get('watch_eligible') or incomplete or market_incomplete else r.get('plan')
        if plan:
            plan_html=f'''<div class="plan">参考买价 {fmt(plan['reference_entry'])} · 追价上限 {fmt(plan['entry_ceiling'])}<br>
            下一交易日参考止盈 {fmt(plan['target_reference'])} / 止损 {fmt(plan['stop_reference'])} · 最晚退出 {escape(plan['time_exit'])}<br>
            <b>报价条件有效至 {escape(plan['valid_until'])}</b><br>{escape(plan['rule'])}</div>'''
        elif watching:
            plan_html='<div class="blocked"><b>个股条件已满足，市场/行业待确认。</b>观察名单不具备入场资格；重新扫描确认全部条件后再判断。以下报价仅作数据参考，不是买入价。</div>'
        elif post_close:
            plan_html='<div class="blocked">盘后研究不提供当前买卖价格。下一交易日重新扫描、核验公告与实时盘口后再判断。</div>'
        else:
            plan_html='<div class="blocked">暂不生成买价和退出价：先解决下列条件。</div>'
        badge=({'ready':'通过研究规则 · 待次日复核','filtered':'未通过研究筛选','incomplete':'研究数据待补齐'}.get(r.get('research_status'),'盘后观察')
               if post_close else '条件候选 · 待复核' if plan else '观察 / 暂不入场')
        if watching:
            badge='个股条件已满足 · 市场/行业待确认'
        score_label='盘后研究分，非盘中评分/胜率' if post_close else '规则分，非胜率'
        score_html=(f"{r['score']}/100 <small>{score_label}</small>" if r.get('research_status') != 'incomplete'
                    else '证据不完整<small>评分不用于推荐或排序</small>')
        volume_label='完整交易日量比' if post_close else '时间校正量比'
        tail_note='<small>尾段采用截至14:57的连续竞价分钟，不包含收盘集合竞价；仅研究已发生的量价。</small>' if post_close else ''
        qualified=(r.get('research_status')=='ready' if post_close else bool(plan))
        target_cards=(watch_cards if watching else cards if qualified and not incomplete and not market_incomplete
                      else diagnostic_cards)
        conditions_html=watch_conditions_html(r) if watching else ''
        price_label='收盘参考价' if post_close else '源报价'
        reason_label='<h4>尚未通过的条件</h4>' if watching else ''
        target_cards.append(f'''<article><div class="row"><h3>{escape(r['name'] or r['code'])} <small>{escape(r['code'])}</small></h3>
        <span class="badge">{escape(badge)}</span></div>
        <div class="row"><div><small>{price_label}</small><strong>¥{fmt(r['price'])}</strong> <span>{fmt(r.get('change_pct'))}%</span><small>源报价时间 {escape(r.get('quote_at') or '未核验日期')}</small></div><strong>{score_html}</strong></div>
        <div class="stats"><span>行业：{escape(r['industry'] or '未核验')}</span><span>60日区间位置：{fmt(m.get('range_position_60d',0)*100) if m.get('range_position_60d') is not None else '—'}%</span>
        <span>{volume_label}：{fmt(m.get('volume_ratio_time_adjusted'))}</span><span>MA20：{fmt(m.get('ma20'))}</span></div>
        <p>{escape(enhanced)}</p>{tail_note}<p>{escape(signals)}</p>{plan_html}{conditions_html}{reason_label}<ul>{risks}</ul><div class="links">{links or '缺少可核验事件链接'}</div></article>''')
    sectors=sorted(report['context'].get('sectors',{}).items(),key=lambda x:(x[1]['breadth'],x[1]['median_change_pct']),reverse=True)[:8]
    sector_rows=''.join(f'<tr><td>{escape(name)}</td><td>{x["members"]}</td><td>{x["breadth"]:.0%}</td><td>{x["median_change_pct"]:+.2f}%</td><td>{x["amount_share"]:.1%}</td></tr>' for name,x in sectors)
    stats=report['stats']
    final_count=stats.get('research_ready_count',0) if post_close else stats['conditional_count']
    final_count_label=('当前确认通过（核查未完成）' if incomplete else
                       '全部研究规则通过' if post_close else '全部入场规则通过')
    watch_count=len(watch_cards)
    list_title=f'全部规则通过（{final_count}只）'
    research_coverage=('<p>盘后报价不使用实时120秒时效判断；收盘价仅为历史成交参考，不代表现在仍可成交。</p>' if post_close else '')
    warnings=''.join(f'<li>{escape(w)}</li>' for w in report.get('warnings',[]))
    status_html=f"<div class=\"section\"><h2>{escape(run_status.get('label','运行状态未记录'))}</h2><p>{escape(run_status.get('summary','请检查原始数据与警告，不能将零候选直接解释为市场没有合格股票。'))}</p><ul>{''.join('<li>'+escape(r)+'</li>' for r in run_status.get('reasons',[]))}</ul></div>"
    empty_message=('本次数据核验尚未完成，无法形成可信名单。下方缺项记录不是股票推荐。' if incomplete else
                   f'本次全部规则通过 0 只；另有 {watch_count} 只个股条件已满足，仍需市场或行业环境确认。' if watch_count else
                   '本次分层筛选没有通过全部规则的标的；下方展示未通过记录，不是股票推荐。')
    empty_html='<div class="section">'+empty_message+'</div>'
    watch_html=(f'<h2>待环境确认观察名单（{watch_count}只）</h2>'
                '<p>这些标的通过个股与数据条件，尚未通过市场或行业环境条件。观察资格不等于全部规则通过；下次扫描须重新核查价格、量能、消息和环境。</p>'
                +''.join(watch_cards) if watch_cards else '')
    diagnostics_html=(f'<details class="section"><summary>未通过与缺项记录（{len(diagnostic_cards)}只，非推荐名单）</summary><p>这些记录用于核查数据缺项和未通过条件；排列顺序不代表投资优先级。</p>'+''.join(diagnostic_cards)+'</details>' if diagnostic_cards else '')
    rejection_items=((reason,count) for reason,count in report.get('rejection_counts',{}).items()
                     if reason != '盘后研究仅供观察，须下一交易日按实时规则重新筛选')
    rejection_rows=''.join(f'<tr><td>{escape(reason_text(reason))}</td><td>{count}</td></tr>' for reason,count in sorted(rejection_items,key=lambda item:-item[1])[:10])
    coverage_html=coverage_details(report)
    collection_html=collection_diagnostics_html(report)
    diagnostics_link=(' · <a href="diagnostics.json">本次诊断 JSON</a>' if report.get('diagnostics') else '')
    presentation_note=('<div class="notice">'+escape(report['presentation_note'])+'</div>' if report.get('presentation_note') else '')
    replay_note=('<div class="notice"><b>冻结快照离线回放 · 未重新采集市场数据</b><br>'
                 '名单和指标来自保存的输入；用于检查历史数据与规则，不代表当前行情或当前入场资格。</div>' if replay else '')
    generated_at=report.get('generated_at') or report['as_of']
    timestamp_note=(f"生成时点：{escape(generated_at)} · 输入参考时点：{escape(report['as_of'])}" if replay
                    else f"生成时点：{escape(generated_at)}")
    opportunity_html=opportunity_summary_html(report)
    html=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{title} · {escape(title_date)}</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f3f6fa;color:#16273c;font:15px/1.65 system-ui,-apple-system,"Microsoft YaHei",sans-serif}}main{{max-width:1200px;margin:auto;padding:30px 22px 60px}}header{{padding:30px;background:#152d47;color:white;border-radius:16px}}h1{{font-size:30px;margin:8px 0}}header p{{color:#d5e6ef}}h2{{font-size:21px}}.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}}.kpis div,article,.section{{background:white;border:1px solid #d9e4ee;border-radius:12px;padding:20px}}.kpis b{{display:block;font-size:27px}}small{{display:block;font-size:12px;color:#60748a}}header small{{color:#b9d6eb}}article{{margin:16px 0}}h3{{margin:0;font-size:20px}}h3 small{{font-weight:400}}.row{{display:flex;justify-content:space-between;align-items:center;gap:14px;margin-bottom:12px}}.badge{{font-size:12px;white-space:nowrap;background:#eef1f4;color:#44586e;padding:5px 9px;border-radius:7px}}.stats{{display:flex;gap:18px;flex-wrap:wrap;border-block:1px solid #e7edf4;padding:12px 0;font-size:13px}}.plan{{background:#e9f6f1;padding:14px;border-left:4px solid #239671}}.blocked,.notice{{background:#fff4e9;padding:14px;border-left:4px solid #d98736}}li{{margin:4px 0}}.links{{font-size:12px;border-top:1px solid #e5edf5;padding-top:12px}}a{{color:#176d9b}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{padding:9px;text-align:left;border-bottom:1px solid #e4ebf3}}.section{{margin:20px 0}}footer{{font-size:12px;color:#5c6f86}}@media(max-width:700px){{.kpis{{grid-template-columns:repeat(2,1fr)}}main{{padding:15px}}header{{padding:20px}}h1{{font-size:24px}}.row{{align-items:flex-start}}}}
    </style></head><body><main><header><small>UZI · TAIL SCREENER · 沪深 A 股</small><h1>{title}</h1><p>{subtitle}</p><small>{timestamp_note} · 模式：{escape(report['mode'])}{data_note}</small></header>
    {replay_note}{presentation_note}<div class="kpis"><div><b>{stats['universe']}</b><small>本次沪深有效报价</small></div><div><b>{stats['liquid_candidates']}</b><small>初筛通过</small></div><div><b>{stats['history_checked']}</b><small>历史量价已取得</small></div><div><b>{final_count}</b><small>{final_count_label}</small><b>{watch_count}</b><small>待环境确认观察（非入场信号）</small></div></div>
    {status_html}{collection_html}<div class="notice">评分未做样本外收益验证，不代表胜率。资金潜伏、抢筹仅为量价代理信号，不能确认交易主体。A股买入当日不能卖；隔夜跳空、跌停、手续费和滑点会改变实际收益。<ul>{warnings}</ul></div>
    {opportunity_html}<h2>{list_title}</h2>{''.join(cards) or empty_html}{watch_html}{diagnostics_html}
    <div class="section"><h2>主要未通过条件</h2><p>统计深入核查标的，不代表全市场失败率。同一股票可能有多个未通过条件。</p><table><tr><th>条件</th><th>数量</th></tr>{rejection_rows}</table></div>
    <div class="section"><h2>已采集样本的行业量价统计</h2><p>只统计本次成功采集且满足时点要求的报价。目录或报价覆盖不足时，样本可能偏向特定行业，不能代表全市场强弱；成交额不等同资金净流入。</p><table><tr><th>行业</th><th>样本</th><th>样本上涨比例</th><th>样本涨幅中位数</th><th>样本成交额占比</th></tr>{sector_rows}</table></div>
    <div class="section"><h2>采集与筛选范围</h2><p>目标是沪深A股全市场横截面，实际取得范围如下；历史量价最多 {report['config']['history_candidates']} 只，财报、公告、分时和盘口最多 {report['config']['deep_candidates']} 只。没有对每只股票做全文财报审计。</p>
    <table>{coverage_html}</table>
    {research_coverage}
    <p>版本 {escape(report.get('strategy_version','1'))} · 配置 {escape(report.get('config_hash','未冻结')[:12])} · 代码 {escape(report.get('code_version','未冻结')[:12])} · 相关性/行业/数量过滤 {len(report.get('selection_excluded',[]))} 只</p>
    <p><a href="report.json">完整规则结果 JSON</a> · <a href="snapshot.json">本次输入快照与来源</a> · <a href="outcomes.csv">本次信号结果记录表</a>{diagnostics_link}</p></div>
    <footer>默认仅研究和模拟记录，不连接券商、不下单。v2 增强门槛是待前向验证的规则；规则分不是盈利概率。报告超过报价有效期后须重新核验。次日卖出时间是计划，不能保证成交。</footer></main></body></html>'''
    target=output/'index.html'
    target.write_text(html,encoding='utf-8')
    return target
