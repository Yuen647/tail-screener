"""Standalone reports for sourced strategies; no legacy scoring or price plans."""
from html import escape
import json
from math import isfinite
from pathlib import Path
from urllib.parse import urlparse


def _text(value):
    if value is None:
        return '未记录'
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _escaped(value):
    return escape(_text(value), quote=True)


def _number(value, digits=2):
    if isinstance(value, bool):
        return '—'
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return '—'
    return f'{value:,.{digits}f}' if isfinite(value) else '—'


def _source_link(url, label='查看策略出处'):
    try:
        parsed = urlparse(str(url))
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return '来源链接未核验'
    except ValueError:
        return '来源链接未核验'
    return f'<a href="{_escaped(url)}" rel="noreferrer">{escape(label)}</a>'


def _list(items, empty='未记录'):
    if not isinstance(items, list):
        items = [items] if items else []
    return '<ul>' + ''.join('<li>' + _escaped(item) + '</li>' for item in items) + '</ul>' if items else '<p>' + escape(empty) + '</p>'


def _holding_text(value):
    if not isinstance(value, dict):
        return _text(value)
    return '；'.join(_text(value[key]) for key in ('label', 'entry_timing', 'exit_timing') if value.get(key)) or '未记录'


def _policy_name(value):
    if isinstance(value, dict):
        return _text(value.get('name') or value.get('label') or value.get('id'))
    return _text(value)


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _formula_coverage(report):
    """Count complete formula inputs, never treat quote or history counts as it."""
    stats = report.get('stats') or {}
    complete = _count(stats.get('formula_evaluated'))
    evaluated = _count(stats.get('evaluated'))
    missing = _count(stats.get('data_incomplete'))
    if complete is None and evaluated is not None and missing is not None and missing <= evaluated:
        complete = evaluated - missing
    if complete is None and isinstance(report.get('all_evaluated'), list):
        complete = sum(isinstance(row, dict) and not row.get('data_gaps')
                       and isinstance(row.get('raw_entry_match'), bool) for row in report['all_evaluated'])
    eligible = _count(stats.get('eligible'))
    remaining = eligible - complete if eligible is not None and complete is not None and complete <= eligible else None
    fraction = (f'{complete}/{eligible}（{complete / eligible:.2%}）'
                if eligible and complete is not None and complete <= eligible else _text(complete))
    return complete, remaining, fraction


def _history_collection_html(report):
    """Show aggregated collection outcomes, keeping per-symbol logs in JSON."""
    diagnostics = report.get('history_collection') or {}
    stats = report.get('stats') or {}
    attempted = diagnostics.get('attempted_count', stats.get('history_attempted'))
    succeeded = diagnostics.get('succeeded_count', stats.get('history_checked'))
    failed = diagnostics.get('failed_count', stats.get('history_failed'))
    deferred = diagnostics.get('deferred_count')
    not_attempted = diagnostics.get('not_attempted_count', stats.get('not_attempted'))
    fields = [('已尝试读取历史（含缓存）', attempted), ('已取得历史', succeeded),
              ('尝试后仍未取得历史', failed), ('延期或跳过', deferred), ('尚未调度读取历史', not_attempted)]
    counts = ''.join('<tr><th>' + label + '</th><td>' + _escaped(value) + '</td></tr>' for label, value in fields)
    categories = diagnostics.get('unfinished_counts') or {}
    labels = {'request_failed': '实际请求失败', 'circuit_deferred': '本地熔断后暂缓后续读取',
              'budget_exhausted': '运行时间预算已用完', 'not_attempted': '尚未尝试',
              'access_blocked': '来源访问受限', 'recovery_exhausted': '低并发恢复仍未成功，已暂缓剩余读取',
              'recovery_limit': '已到恢复探测次数上限，已暂缓剩余读取'}
    category_rows = ''.join('<li>' + escape(labels.get(str(key), str(key))) + '：' + _escaped(value) + ' 只</li>'
                            for key, value in categories.items()) if isinstance(categories, dict) else ''
    recovery = diagnostics.get('recovery') or {}
    reason = recovery.get('terminal_reason') if isinstance(recovery, dict) else None
    reason_note = '<p><b>采集停止原因：</b>' + _escaped(labels.get(str(reason), reason)) + '</p>' if reason else ''
    source_labels = {'timeout': '请求超时', 'connection_error': '连接失败',
                     'proxy_error': '代理连接异常', 'http_denied': '来源拒绝访问',
                     'http_error': 'HTTP 服务错误', 'tls_error': '证书或 TLS 连接失败',
                     'invalid_response': '返回内容不符合接口格式', 'request_error': '网络请求失败',
                     'circuit_open': '本地熔断拦截（未发出网络请求）', 'budget_expired': '预算用完，未继续请求'}
    event_counts = diagnostics.get('category_counts') or {}
    event_rows = ''.join('<li>' + escape(source_labels.get(str(key), str(key))) + '：' + _escaped(value) + ' 次</li>'
                         for key, value in event_counts.items()) if isinstance(event_counts, dict) else ''
    event_html = ('<h3>历史采集异常记录</h3><ul>' + event_rows
                  + '</ul><p class="muted">同一股票重试可重复计数，本地熔断拦截也有记录，不等同于股票数或网络请求数。</p>') if event_rows else ''
    grouped = {}
    for row in report.get('source_diagnostics') or []:
        if not isinstance(row, dict) or row.get('category') in (None, 'ok'):
            continue
        key = (str(row.get('host') or '来源未记录'), str(row['category']), str(row.get('status_code') or ''))
        attempts = _count(row.get('attempts'))
        if key not in grouped:
            grouped[key] = attempts
        elif attempts is not None:
            grouped[key] = (grouped[key] or 0) + attempts
    source_rows = []
    for (host, category, code), count in sorted(grouped.items(), key=lambda pair: -(pair[1] or 0))[:6]:
        label = source_labels.get(category, category) + (f'（HTTP {code}）' if code else '')
        source_rows.append('<tr><td>' + escape(host) + '</td><td>' + escape(label)
                           + '</td><td>' + _escaped(count) + '</td></tr>')
    source_html = ('<h3>来源失败原因摘要</h3><table><tr><th>来源主机</th><th>原因</th><th>记录次数</th></tr>'
                   + ''.join(source_rows) + '</table><p class="muted">最多展示 6 类原因，记录次数含重试与未发出请求的拦截，'
                   '不能当作失败股票数。这里是整次采集的来源记录，可能包含目录或报价请求；完整明细保留在 report.json。</p>') if source_rows else ''
    health = diagnostics.get('host_health') or {}
    state_labels = {'blocked': '访问受限，已停止尝试', 'cooldown': '故障冷却中',
                    'probe_in_flight': '正在探测恢复', 'ready': '允许调度，未必已恢复'}
    health_rows = []
    for row in (health.get('routes') or [])[:4]:
        if not isinstance(row, dict):
            continue
        cause = row.get('cause') or {}
        category = cause.get('category')
        reason_label = source_labels.get(category, category) if category else '未记录异常原因'
        if cause.get('status_code') is not None:
            reason_label += '（HTTP ' + str(cause['status_code']) + '）'
        health_rows.append('<tr><td>' + _escaped(row.get('host')) + '</td><td>'
                           + _escaped(state_labels.get(row.get('state'), row.get('state'))) + '</td><td>'
                           + _escaped(reason_label) + '</td></tr>')
    health_html = ('<h3>采集结束时的历史来源状态</h3><table><tr><th>主机</th><th>调度状态</th><th>最近记录的原因</th></tr>'
                   + ''.join(health_rows) + '</table><p class="muted">调度允许不等于接口已经恢复成功；'
                   '同一提供方的主备地址也不算独立数据来源。</p>') if health_rows else ''
    return ('<section class="section"><h2>历史数据采集情况</h2>'
            '<p>行情报价与历史日线是不同输入：有报价不等于已取得足够历史，也不等于已能判断策略公式。</p>'
            '<table>' + counts + '</table><p class="muted">延期或跳过与从未请求可能重叠，不能相加。'
            '未记录的计数不按零处理；失败与未尝试均不记作策略未触发。</p>'
            + ('<h3>未完成原因汇总</h3><ul>' + category_rows + '</ul>' if category_rows else '')
            + reason_note + health_html + event_html + source_html + '</section>')


def _selection_html(report):
    """Explain the supplied ranking without recomputing it or changing ties."""
    selection = report.get('selection') or {}
    policy = selection.get('policy') or {}
    summary = selection.get('summary') or {}
    if not policy:
        return '', ''
    if str(policy.get('id') or '').startswith('legacy'):
        return ('<section class="section"><h2>旧版名单展示方式</h2>'
                '<p><b>旧版代码顺序，仅历史名单复核，无策略优先级。</b>'
                '本次保留原运行的展示顺序和数量，不将股票代码顺序解释为投资排序，也不补造 Alvarez 排名。</p></section>', '')
    source = policy.get('source') or policy
    requested = summary.get('request_count', summary.get('requested_count'))
    source_html = _source_link(source.get('url'), '查看排序原文')
    rule = policy.get('rule') or policy.get('name') or 'RSI(2) 从低到高排序'
    header = (f'<section class="section"><h2>公式触发排序依据</h2>'
              '<p><b>入场与退出公式来自 Larry Connors 的 2009 年文章；同时触发时的排序依据来自 Cesar Alvarez 的 2017 年文章。</b>'
              '这是一项明确列出的组合选择适配，不能说成 Connors 原文已经规定了这套排序。</p>'
              f'<p>{_escaped(source.get("author"))} · {_escaped(source.get("published_date"))} · '
              f'{_escaped(source.get("title"))} · {source_html}</p>'
              f'<p><b>排序规则：</b>{_escaped(rule)}</p>'
              f'<p>{_escaped(policy.get("adaptation") or "将公开的 RSI(2) 升序方法应用于本次 Connors 公式触发集合；本市场表现尚未验证。")}</p>'
              f'<p><b>本次范围：</b>已核验公式触发 {_escaped(summary.get("qualified_count"))} 只；'
              f'请求前 {_escaped(requested)} 个名额，实际展示 {_escaped(summary.get("selected_count"))} 只。'
              f'边界 RSI(2)：{_escaped(summary.get("cutoff_value"))}。</p>'
              f'<p><b>并列处理：</b>{_escaped(policy.get("tie_policy") or "边界 RSI(2) 完全相同的标的全部保留，实际数量可超过请求值；代码只用于相同数值的稳定展示。")}</p>'
              '<p>排序使用未四舍五入的 RSI(2)。更低代表该指标衡量的短期超卖程度更深，不代表预期胜率或收益更高。'
              '排名仅针对本次取得完整证据的触发标的；未评估股票不参与，不能称全市场最优。</p></section>')
    ranked = report.get('ranked_matches', selection.get('ranked_matches', []))
    rows = []
    for item in ranked:
        if not isinstance(item, dict) or item.get('raw_entry_match') is not True or item.get('data_gaps'):
            continue
        selected = '已选入本次展示' if item.get('selected') is True else '未进入本次展示名额'
        rows.append('<tr><td>' + _escaped(item.get('selection_rank')) + '</td><td>'
                    + _escaped(item.get('name') or item.get('code')) + '<small>' + _escaped(item.get('code'))
                    + '</small></td><td>' + _escaped(item.get('ranking_value')) + '</td><td>'
                    + selected + '</td><td>' + _escaped(item.get('selection_reason')) + '</td></tr>')
    table = ('<details class="section"><summary>全部已核验触发标的的排序（' + str(len(rows)) + '只）</summary>'
             '<p>依照同一排序规则保留全部触发记录，包括展示上限之外的标的。相同数值享有相同优先级；代码先后不代表策略优劣。</p>'
             '<table><tr><th>公式排序位次</th><th>标的</th><th>RSI(2) 完整排序值</th><th>展示状态</th><th>依据</th></tr>'
             + ''.join(rows) + '</table></details>') if rows else ''
    return header, table


def write_author_report(report, output: Path):
    """Write the supplied evidence unchanged and render only formula matches.

    A confirmed formula is a completed-session observation, not proof of an
    available fill. Intraday rows remain provisional even if mislabeled by a
    caller. Missing-data rows are summarized outside the candidate section.
    """
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    strategy = report.get('strategy') or {}
    validation = strategy.get('validation') or {}
    validation_label = _text(validation.get('label') or 'ETF公式迁移实验 · A股有效性未验证')
    validation_label = validation_label.replace('；', ' · ')
    validation_states = []
    for key, label in [('a_share_backtest_completed', 'A股回测'),
                       ('a_share_out_of_sample_validated', '样本外验证'),
                       ('a_share_execution_validated', '成交条件验证')]:
        value = validation.get(key)
        state = '已标记完成，需查阅验证证据' if value is True else '未完成' if value is False else '未记录'
        validation_states.append(label + '：' + state)
    validation_note = ('<div class="notice"><b>' + escape(validation_label) + '</b><p>'
                       + _escaped(validation.get('source_scope') or '原 ETF 规则迁移至沪深个股，作者原文不能证明本次A股股票池和成交方案有效。')
                       + '</p><p>' + escape(' · '.join(validation_states)) + '</p><p>'
                       + _escaped(validation.get('decision') or '当前用于公式迁移研究，不能当作已经验证的A股荐股策略。') + '</p>'
                       + ('<p>当前未达到已验证荐股策略的使用条件。</p>'
                          if validation.get('recommendation_ready') is False else '') + '</div>')
    stats = report.get('stats') or {}
    status = report.get('run_status') or {}
    formula_count, formula_remaining, formula_fraction = _formula_coverage(report)
    post_close = report.get('analysis_mode') == 'post_close'
    replay = report.get('mode') == '离线回放' or status.get('timing_status') == 'replay'
    rerank = report.get('mode') == '离线排序对照'
    legacy_selection = str(((report.get('selection') or {}).get('policy') or {}).get('id') or '').startswith('legacy')
    incomplete = status.get('data_status') != 'complete'
    mode_label = '冻结输入排序对照' if rerank else '收盘公式筛选' if post_close else '盘中预备观察'
    title = _text(strategy.get('name') or '来源策略') + ' · ' + mode_label
    session = report.get('data_session') or '交易日待核验'
    as_of = report.get('as_of')
    generated_at = report.get('generated_at') or as_of
    analysis = (report.get('context') or {}).get('analysis') or {}
    phase_label = {'pre_open': '开盘前或集合竞价', 'pre_market_closed': '开盘前休市',
                   'opening_auction': '开盘集合竞价', 'midday_break': '午间休市',
                   'post_close': '收盘后', 'continuous_or_closing_auction': '交易进行中',
                   'non_session_or_unverified': '非交易时段或日历待核验'}.get(analysis.get('session_phase'))
    phase_note = ('<p class="muted">采集结束阶段：' + _escaped(phase_label)
                  + '。' + _escaped(analysis.get('freshness_note') or '') + '</p>') if phase_label else ''
    candidates = [item for item in report.get('candidates', []) if isinstance(item, dict)]
    matches = [item for item in candidates if item.get('raw_entry_match') is True and not item.get('data_gaps')]
    evaluated = report.get('all_evaluated') or candidates
    gaps = [item for item in evaluated if isinstance(item, dict) and item.get('data_gaps')]
    rsi_method = next(((item.get('metrics') or {}).get('rsi_method') for item in evaluated
                       if isinstance(item, dict) and (item.get('metrics') or {}).get('rsi_method')), None)
    cards = []
    for item in matches:
        metrics = item.get('metrics') or {}
        observed = lambda key: item.get(key) if item.get(key) is not None else metrics.get(key)
        confirmed = post_close and item.get('confirmation_status') == 'confirmed_close'
        badge = '收盘公式已确认 · 执行尚未核验' if confirmed else '盘中预备观察 · 收盘待确认' if not post_close else '公式触发待核验'
        rows = []
        for condition in item.get('conditions', []):
            if not isinstance(condition, dict):
                continue
            passed = condition.get('passed')
            passed_text = '满足' if passed is True else '未满足' if passed is False else '待核验'
            threshold = (str(condition.get('operator') or '') + ' ' + _text(condition.get('threshold'))
                         if 'threshold' in condition else '见原规则说明')
            rows.append('<tr><td>' + _escaped(condition.get('label')) + '</td><td>'
                        + _escaped(condition.get('observed', condition.get('value'))) + '</td><td>'
                        + escape(threshold) + '</td><td>' + passed_text + '</td></tr>')
        exit_condition = item.get('exit_trigger')
        exit_trigger = exit_condition.get('triggered') if isinstance(exit_condition, dict) else exit_condition
        exit_text = ('本次观测已满足 RSI(2) > 70 的退出公式' if exit_trigger is True and post_close else
                     '盘中退出公式暂时触发，须等收盘确认' if exit_trigger is True else
                     '本次观测未触发退出公式' if exit_trigger is False else '本次退出公式状态未记录')
        priority = ('<small>公式触发排序：第 ' + _escaped(item['selection_rank']) + ' 位</small>'
                    if item.get('selection_rank') is not None and not legacy_selection else '')
        selection_reason = ('<div class="notice"><b>为何选入本次展示：</b>' + _escaped(item['selection_reason'])
                            + '<br><small>排序值 RSI(2)：' + _escaped(item.get('ranking_value'))
                            + '；位次表示公式排序，不表示未来收益排名。</small></div>'
                            if item.get('selection_reason') and not legacy_selection else '')
        cards.append(f'''<article>
<div class="row"><h3>{priority}{_escaped(item.get('name') or item.get('code'))}<small>{_escaped(item.get('code'))}</small></h3><span class="badge">{badge}</span></div>
<div class="metrics"><div><small>{'收盘参考价' if post_close else '盘中参考报价'}</small><b>¥{_number(observed('price'))}</b></div><div><small>RSI(2)</small><b>{_number(observed('rsi2'))}</b></div><div><small>SMA200</small><b>{_number(observed('sma200'))}</b></div></div>
<small>源报价时间：{_escaped(item.get('quote_at'))} · 价格只作公式输入参考。</small>
{selection_reason}
<table><thead><tr><th>原规则条件</th><th>本次观测</th><th>要求</th><th>状态</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<p><b>原规则假设持有：</b>{escape(_holding_text(item.get('holding_period') or strategy.get('holding_period') or report.get('holding_period')))}</p>
<p>{exit_text}。退出按已确认收盘指标判断，不能用盘中暂时越过阈值代替。</p>
<p class="muted">收盘确认仅指观测满足公式，不表示A股策略有效或盈利能力已经验证。公式触发不等于执行已经核验。当前未对该股完成交易计划、公告全文、可成交价格及实际持仓核查。</p>
</article>''')

    count_rows = [
        ('本次有效报价', stats.get('universe')),
        ('策略股票范围', stats.get('eligible')),
        ('历史数据已取得', stats.get('history_checked')),
        ('历史采集失败', stats.get('history_failed')),
        ('完整公式核验', formula_count),
        ('完整公式覆盖率', formula_fraction),
        ('未评估', stats.get('unassessed')),
        ('公式触发', stats.get('raw_matches')),
        ('收盘确认', stats.get('confirmed_matches') if post_close else 0),
        ('盘中预备', stats.get('provisional_matches')),
        ('本报告展示', len(matches)),
    ]
    coverage_rows = ''.join('<tr><th>' + label + '</th><td>' + _escaped(value) + '</td></tr>'
                            for label, value in count_rows)
    gap_rows = ''.join('<tr><td>' + _escaped(item.get('code')) + '</td><td>'
                       + _escaped(item.get('name')) + '</td><td>' + _escaped(item.get('data_gaps')) + '</td></tr>'
                       for item in gaps[:50])
    gap_html = ('<details class="section"><summary>已评估输入中的数据缺项记录（'
                + str(len(gaps)) + '只）</summary><p>下列记录不进入公式触发名单。'
                + ('页面仅展示前 50 条，完整记录保留在 report.json。' if len(gaps) > 50 else '') + '</p>'
                '<table><tr><th>代码</th><th>名称</th><th>缺项</th></tr>' + gap_rows + '</table></details>' if gaps else '')
    empty = ('尚无股票具备完整公式判断输入；当前零触发来自数据不足，不能据此认定全市场没有机会。'
             if formula_count == 0 and stats.get('eligible') else
             '本次采集或评估范围未完成，当前可核验范围内没有可展示的公式触发标的；不能据此认定全市场没有机会。'
             if incomplete or stats.get('unassessed') or stats.get('history_failed') else
             '本次已评估范围内没有满足该策略公式的标的；不代表其他独立策略也没有机会。')
    coverage_incomplete = incomplete or bool(formula_remaining) or bool(stats.get('history_failed'))
    if coverage_incomplete:
        scope_note = (f'原公式完整核验 {_escaped(formula_fraction)}，其余输入尚未完成核验。'
                      if formula_count is not None else '完整公式核验数量未记录，不能以报价数量或已取得历史的数量替代。')
        result_note = ('尚无股票具备完整公式判断输入，本次零触发不能当作策略筛选结论。'
                       if formula_count == 0 else
                       f'本次 {_escaped(stats.get("raw_matches"))} 只公式触发仅代表已完成核验的 {_escaped(formula_count)} 只股票。'
                       if formula_count is not None else '本次触发数量不能代表全市场筛选结论。')
        collection_notice = '<div class="notice"><b>数据核验未完成。</b>' + scope_note + '<br>' + result_note + '</div>'
    else:
        collection_notice = ''
    quote_coverage = (report.get('universe_metadata') or {}).get('valid_quote_coverage')
    if quote_coverage is None:
        quote_coverage = (report.get('context') or {}).get('valid_quote_coverage')
    quote_coverage_note = (f' · 覆盖率 {quote_coverage:.2%}'
                           if isinstance(quote_coverage, (int, float)) and not isinstance(quote_coverage, bool)
                           and isfinite(quote_coverage) and 0 <= quote_coverage <= 1 else '')
    last_kpi = (_escaped(formula_remaining), '尚未完成公式核验') if coverage_incomplete else (
        _escaped(stats.get('raw_matches')), '公式触发 · 不等于执行已核验')
    candidate_heading = ('当前尚无可完整判断的公式输入' if formula_count == 0 and coverage_incomplete else
                         f'已核验 {formula_count} 只中的公式触发（{len(matches)}只展示）'
                         if coverage_incomplete and formula_count is not None and not matches else
                         ('收盘公式触发观察' if post_close else '盘中预备观察') + f'（{len(matches)}只）')
    history_html = _history_collection_html(report)
    input_note = ('<div class="notice"><b>冻结输入回放，未重新采集市场数据。</b>'
                  '下列公式结果用于复核原输入，不表示当前市场状态。</div>' if replay else '')
    if rerank:
        input_note += ('<div class="notice"><b>本次为新排序规则与原冻结输入的对照。</b>'
                       '展示名单可能与原报告不同，不能称为原名单重现；行情与公式输入仍来自原快照。</div>')
    provenance = report.get('replay_provenance') or {}
    source_policy = provenance.get('source_selection_policy')
    execution_policy = provenance.get('execution_selection_policy', provenance.get('execution_policy'))
    if replay and (source_policy is not None or execution_policy is not None):
        input_note += ('<div class="section"><h2>本次回放的展示规则</h2><p>原运行：'
                       + escape(_policy_name(source_policy)) + '</p><p>本次处理：'
                       + escape(_policy_name(execution_policy)) + '</p></div>')
    timing_note = ('按最近完整交易日核查收盘公式，实际完成范围与缺项见下方。信号在收盘之后才可确认，不能据此假设已按同一收盘价成交。'
                   if post_close else '可在任意时间运行，收盘后才确认原规则。本次按盘中预备模式计算，尚未走收盘确认流程；即使采集跨过收盘，也须另行核验最终结果。')
    adaptations = _list(strategy.get('adaptations'), '尚未记录适配差异，不能将本次沪深个股结果视为原市场验证。')
    warnings = _list(report.get('warnings'), '无额外运行警告。')
    selection_html, ranked_html = _selection_html(report)
    snapshot_link = ' · <a href="snapshot.json">冻结输入与来源</a>' if (output/'snapshot.json').exists() else ''
    html = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · {_escaped(session)}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f3f6fa;color:#16273c;font:15px/1.7 system-ui,-apple-system,"Microsoft YaHei",sans-serif}}main{{max-width:1100px;margin:auto;padding:28px 22px 60px}}header{{padding:28px;background:#152d47;color:#fff;border-radius:14px}}header small{{color:#c0d8e8}}h1{{font-size:29px;margin:9px 0}}h2{{font-size:21px}}h3{{margin:0;font-size:20px}}small{{display:block;color:#60748a;font-size:12px}}article,.section,.kpis>div{{background:white;border:1px solid #d9e4ee;border-radius:12px;padding:20px;margin:18px 0}}.notice{{padding:16px;border-left:4px solid #d98736;background:#fff4e9;margin:18px 0}}.kpis,.metrics{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}}.kpis b,.metrics b{{display:block;font-size:25px}}.row{{display:flex;justify-content:space-between;align-items:center;gap:16px}}.badge{{background:#edf2f8;color:#365b78;padding:6px 10px;border-radius:7px;font-size:12px}}.metrics{{margin:18px 0}}table{{border-collapse:collapse;width:100%;font-size:13px;margin:15px 0}}td,th{{text-align:left;vertical-align:top;padding:9px;border-bottom:1px solid #e3eaf1;overflow-wrap:anywhere}}a{{color:#176d9b}}li{{margin:6px 0}}.muted,footer{{color:#60748a;font-size:13px}}summary{{cursor:pointer}}@media(max-width:640px){{main{{padding:14px}}header{{padding:20px}}h1{{font-size:24px}}.row{{display:block}}.badge{{display:inline-block;margin-top:10px}}.kpis{{grid-template-columns:1fr}}.kpis>div{{margin:4px 0}}}}
</style></head><body><main>
<header><small>UZI · 来源策略 · 沪深 A 股研究</small><h1>{escape(title)}</h1><p>{timing_note}</p><small>生成时点：{_escaped(generated_at)} · 输入参考时点：{_escaped(as_of)}<br>行情所属交易日：{_escaped(session)}</small></header>
{input_note}{validation_note}{collection_notice}
<div class="kpis"><div><b>{_escaped(stats.get('universe'))}</b><small>本次有效报价{escape(quote_coverage_note)}</small></div><div><b>{_escaped(formula_fraction)}</b><small>完整公式核验 / 策略股票范围</small></div><div><b>{last_kpi[0]}</b><small>{last_kpi[1]}</small></div></div>
<section class="section"><h2>{_escaped(status.get('label') or '运行状态待核验')}</h2><p>{_escaped(status.get('summary') or '请查看采集范围和数据缺项。')}</p>{phase_note}{_list(status.get('reasons'), '') if status.get('reasons') else ''}<p>范围未满或采集失败时，零触发不能说明全市场没有机会。未评估股票不记为规则失败。</p></section>
{history_html}
<section class="section"><h2>原策略与本次适配</h2><table><tr><th>策略</th><td>{_escaped(strategy.get('name'))} · {_escaped(strategy.get('id'))}</td></tr><tr><th>作者与出处</th><td>{_escaped(strategy.get('author'))} · {_source_link(strategy.get('source_url'))}</td></tr><tr><th>原始市场</th><td>{_escaped(strategy.get('native_market'))}</td></tr><tr><th>原入场条件</th><td>{_escaped(strategy.get('native_entry'))}</td></tr><tr><th>原退出条件</th><td>{_escaped(strategy.get('native_exit'))}</td></tr></table><h3>A 股适配差异</h3>{adaptations}<p>本报告没有使用旧版 100 分混合评分，也不把其他策略条件拼入原规则。</p><h3>指标实现与原文边界</h3><p>原文未给出 RSI 平滑初值的完整实现细节；本项目单独记录 RSI 计算方法和历史预热范围，不能把实现选择当作原文规定。</p><p>{_escaped(rsi_method or '本报告未记录 RSI 算法细节，请查阅当前策略实现与输入。')}</p></section>
{selection_html}<h2>{escape(candidate_heading)}</h2>{''.join(cards) if cards else '<div class="section">'+empty+'</div>'}
{ranked_html}{gap_html}<section class="section"><h2>原规则假设持有与退出</h2><p><b>原规则假设持有至收盘 RSI(2) &gt; 70，允许跨多个交易日。</b>这是本次实验采用的持有和退出假设，尚未验证其在A股个股上的有效性。没有固定的下一交易日强制退出，也不生成固定止盈、止损或买入价。</p><p>{escape(_holding_text(strategy.get('holding_period') or report.get('holding_period')))}</p><p>A 股买入当日不能卖出。收盘后确认退出条件，并不保证能按该收盘价成交；实际退出需满足交易日、涨跌停、停牌和可成交条件。</p><p>当前是扫描报告，尚未接入实际持仓，不提供自动持仓追踪、订单执行或已持有股票的卖出通知。退出公式展示不能替代持仓管理。</p></section>
<section class="section"><h2>采集与评估范围</h2><table>{coverage_rows}</table><p>数量按本次实际范围记录；公式触发、收盘确认、盘中预备和展示数量含义不同。缺失或未评估不填作未触发。</p><h3>运行提示</h3>{warnings}<p><a href="report.json">完整规则结果 JSON</a>{snapshot_link}</p></section>
<footer>公开来源与明确公式不等于经过本市场验证。报告用于研究与复核，不连接券商、不下单；公式触发不代表胜率或收益承诺。</footer>
</main></body></html>'''
    (output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    target = output/'index.html'
    target.write_text(html, encoding='utf-8')
    return target
