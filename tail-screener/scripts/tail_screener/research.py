"""Research of the latest completed session, using data collected now.

The report clock is never changed.  A data_session identifies the close being
studied; disclosures are evaluated at the actual collection clock.  This is
not a reconstruction of a historical 14:30 decision and cannot issue a plan.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time, timedelta

from .engine import (TZ, coarse_filter, fraction, notice_review, number,
                     session_date, stamp, technical)
from .providers import calendar_context
from .signals import advanced_checks

TECHNICAL_RULE_GAPS = {
    '不在60日低位区间', '均线尚未转强，防止把下跌误判为低位',
    '价格偏离20日均线过大或仍在其下方', '近期涨幅过大', '量能不足或短时过度放量',
}
ADVANCED_RULE_GAPS = {
    'ATR14相对波动过高，隔夜风险超出规则门槛', '近10日收盘整理区间过宽',
    '最新价位于当日区间偏弱位置', '尾盘窗口有效成交分钟覆盖不足80%',
    '尾盘从窗口高点回撤过大', '尾盘短时拉升过快，避免追逐突发脉冲',
    '尾盘下跌分钟成交额占优', '最新价未守住可核验的分钟VWAP',
}


def resolve_analysis(now, requested='auto', calendar=None):
    """Choose a truthful basis without turning a requested time into now."""
    now = now.astimezone(TZ)
    if requested not in ('auto', 'live', 'post_close'):
        raise ValueError('analysis mode must be auto, live or post_close')
    calendar = calendar if calendar is not None else calendar_context(now)
    outside = (not calendar.get('is_session') or now.time().replace(tzinfo=None) < time(9, 15)
               or now.time().replace(tzinfo=None) >= time(15))
    mode = 'post_close' if requested == 'post_close' or requested == 'auto' and outside else 'live'
    result = {'mode': mode, 'data_session': None, 'calendar_data_session': None,
              'resolved_at': now.isoformat(), 'basis': '盘中当前数据' if mode == 'live' else '最近已完成交易日收盘研究',
              'disclosure_cutoff': now.isoformat()}
    if mode == 'live':
        return result
    if not calendar.get('verified') or calendar.get('session') != now.date().isoformat():
        result['error'] = '当前交易日历未通过核验，无法确定最近已完成交易日'
        return result
    day = (calendar['session'] if calendar.get('is_session') and now.time().replace(tzinfo=None) >= time(15)
           else calendar.get('previous_session'))
    parsed = session_date(day)
    if parsed is None or parsed > now.date():
        result['error'] = '最近已完成交易日无效'
        return result
    data_calendar = calendar_context(datetime.combine(parsed, time(15), TZ))
    if not data_calendar.get('verified') or not data_calendar.get('is_session') or data_calendar.get('session') != day:
        result['error'] = '收盘数据所属交易日无法核验'
        return result
    result.update(data_session=day, calendar_data_session=data_calendar)
    return result


def _completed_quote(quote, day, now):
    at = stamp(quote.get('quote_at'))
    return (at is not None and len(str(quote.get('quote_at'))) > 10 and at <= now
            and at.date().isoformat() == day and at.time().replace(tzinfo=None) >= time(15))


def closed_quote_matches(row, now, data_session):
    """Whether a row is usable in this completed-session cross section."""
    return _completed_quote(row, data_session, now)


def research_context(universe, now, config, analysis):
    """Check the closing session against the calendar, never majority-vote it."""
    result = dict(analysis, disclosure_cutoff=now.isoformat())
    day = analysis.get('data_session')
    counts = Counter()
    valid = 0
    for row in universe:
        at = stamp(row.get('quote_at'))
        if at is not None:
            counts[at.date().isoformat()] += 1
        if day and _completed_quote(row, day, now):
            valid += 1
    coverage = valid / len(universe) if universe else 0
    gaps = []
    # Recheck the calendar at the collection clock: a long collection must not
    # silently keep an obsolete target after a completed-session boundary.
    current = resolve_analysis(now, requested='post_close')
    if not current.get('data_session') or current.get('data_session') != day:
        gaps.append('采集结束时的最近已完成交易日与研究目标不一致或无法核验')
    calendar = analysis.get('calendar_data_session') or {}
    if not day or not calendar.get('verified') or calendar.get('session') != day or not calendar.get('is_session'):
        gaps.append(analysis.get('error', '最近已完成交易日未核验'))
    if not universe or coverage < config['min_universe_coverage']:
        gaps.append('最近已完成交易日的收盘报价覆盖不足')
    window_start = (now.replace(hour=14, minute=57, second=0, microsecond=0)
                    - timedelta(minutes=config.get('tail_window_minutes', 15)-1)).strftime('%H:%M')
    result.update(same_session_quote_coverage=coverage, valid_close_count=valid,
                  quote_date_counts=dict(sorted(counts.items())), verified=not gaps, gaps=gaps,
                  score_basis='盘后研究规则分（0—100），不是盘中入场评分或胜率',
                  minute_scope=f'数据交易日{window_start}—14:57连续竞价尾段；不含收盘集合竞价与盘后填充分钟')
    return result


def research_technical(row, history, now, config, data_session):
    if session_date(data_session) is None:
        return {'score': 0, 'gaps': ['量价研究的数据交易日尚未核验'], 'reasons': [], 'metrics': {}}
    result = technical(row, history, now, config, data_session=data_session, elapsed_minutes=240)
    result['metrics'].update(data_session=data_session, volume_basis='完整交易日成交量/此前20个交易日平均成交量',
                             factor_cutoff='数据交易日之前的日线；该日价格和成交量只由收盘报价提供')
    return result


def _research_advanced(row, history, minutes, now, config, day):
    parsed_day = session_date(day)
    if parsed_day is None:
        return {'metrics': {}, 'gaps': ['收盘研究数据交易日缺失'], 'signals': []}
    if not isinstance(minutes, list) or any(not isinstance(item, dict) for item in minutes):
        return {'metrics': {}, 'gaps': ['收盘研究分钟结构无效'], 'signals': []}
    # This explicit factor boundary is not a report time or reconstructed
    # decision clock.  Original rows and their collection times are retained.
    cutoff = datetime.combine(parsed_day, time(14, 57), TZ)
    invalid = [item for item in minutes if stamp(item.get('at')) is None or stamp(item.get('at')) > now]
    eligible = [item for item in minutes if stamp(item.get('at')) is not None
                and stamp(item.get('at')).date() == parsed_day and stamp(item.get('at')) <= cutoff]
    result = advanced_checks(row, history, eligible, cutoff, config)
    if invalid:
        result['gaps'].append('原始分钟数据含无效或未来时间')
    result['metrics'].update(minute_analysis_cutoff=cutoff.isoformat(),
                             minute_analysis_basis='连续竞价尾段，未评估14:57之后集合竞价；价格区间位置使用全日收盘价')
    return result


def evaluate_research(bundle, context, now, config):
    """Return observations only; readiness is for research, never execution."""
    research = context.get('research') or {}
    day = research.get('data_session')
    data_gaps, rule_gaps = list(research.get('gaps', [])), []
    if not research.get('verified'):
        data_gaps.append('收盘研究的全市场数据时点尚未核验')
    snapshot = bundle.get('snapshot') or {}
    quote = bundle.get('quote') or snapshot
    identity = snapshot.get('code')
    row = dict(snapshot, **quote)
    row['code'] = identity
    if not bundle.get('quote') or not quote.get('source') or quote.get('code') != identity:
        data_gaps.append('缺少身份已核验的独立刷新收盘报价')
    if not _completed_quote(quote, day, now):
        data_gaps.append('收盘报价不属于最近已完成交易日或时间无效')
    cross = bundle.get('cross_quote') or {}
    cross_gaps = []
    if not cross.get('source') or cross.get('source') == quote.get('source') or cross.get('code') != identity:
        cross_gaps.append('独立收盘行情来源缺失或标的身份不一致')
    if not _completed_quote(cross, day, now):
        cross_gaps.append('独立收盘行情交易日或完成时点未核验')
    # Vendors publish final quotes at different post-close times.  Same-session
    # completed values matter here, rather than the live 60-second clock skew.
    for key, tolerance in [('price', config.get('max_quote_disagreement_pct', .005)),
                           ('prev_close', .002), ('amount', .10), ('volume', .10)]:
        primary, secondary = number(quote.get(key)), number(cross.get(key))
        if primary is None or secondary is None or primary <= 0 or secondary <= 0 or abs(secondary / primary - 1) > tolerance:
            cross_gaps.append(f'两源收盘行情{key}缺失或不一致')
    data_gaps.extend(cross_gaps)
    if not coarse_filter(row, config):
        rule_gaps.append('收盘涨幅、成交额或换手率不符合初筛')
    tech = research_technical(row, bundle.get('history', []), now, config, day)
    for gap in tech['gaps']:
        (rule_gaps if gap in TECHNICAL_RULE_GAPS else data_gaps).append(gap)
    data_calendar = research.get('calendar_data_session') or {}
    previous = session_date(data_calendar.get('previous_session'))
    if previous is None or tech['metrics'].get('history_last_date') != previous.isoformat():
        data_gaps.append('日线未覆盖收盘数据交易日之前的已核验交易日')
    advanced = _research_advanced(row, bundle.get('history', []), bundle.get('minutes', []), now, config, day)
    for gap in advanced['gaps']:
        (rule_gaps if gap in ADVANCED_RULE_GAPS else data_gaps).append(gap)
    tech['metrics'].update(advanced['metrics'])
    tech['reasons'].extend(advanced['signals'])
    score = tech['score']
    # 15 points measure agreement and observed close structure, never a usable
    # order book or executable entry capacity.
    if not cross_gaps and not advanced['gaps']:
        score += 15
    finance = bundle.get('financials') or {}
    notice_at, period = stamp(finance.get('published_at')), stamp(finance.get('report_date'))
    if (finance.get('code') != identity or not finance.get('source') or notice_at is None or notice_at > now
            or period is None or notice_at < period or not 0 <= (now - period).days <= 210):
        data_gaps.append('截至本次采集时已披露财报缺失、过期或时间无效')
    else:
        profit, core, growth = (number(finance.get(key)) for key in ('net_profit', 'core_profit', 'revenue_growth_pct'))
        if profit is None or core is None or growth is None:
            data_gaps.append('财报利润、扣非利润或营收增速缺失')
        elif profit <= 0 or core <= 0 or growth < -20:
            rule_gaps.append('亏损、扣非亏损或营收大幅下滑')
        else:
            score += 10 if growth >= 0 else 5
    notices = bundle.get('notices') or {}
    news_gaps, risks, catalysts = notice_review(notices, now)
    if notices.get('code') != identity:
        news_gaps.append('公告标的身份与收盘行情不一致')
    for gap in news_gaps:
        (rule_gaps if gap == '公告标题命中风险或需全文复核事项' else data_gaps).append(gap)
    if not notices.get('verified') or not notices.get('source') or notices.get('code') != identity:
        data_gaps.append('公告数据采集或身份核验未完成')
    if not news_gaps:
        score += 5 + (5 if catalysts else 0)
    industry = context.get('industries', {}).get(identity, '')
    sector = context.get('sectors', {}).get(industry, {})
    if (not sector or fraction(sector.get('breadth')) is None or number(sector.get('median_change_pct')) is None
            or number(sector.get('members')) is None or number(sector['members']) < 5):
        data_gaps.append('收盘行业样本或统计无效')
    elif sector['breadth'] < .5 or sector['median_change_pct'] <= 0:
        rule_gaps.append('所属行业收盘未形成多数上涨')
    else:
        score += 10
    for key, threshold, label in [('universe_coverage', config['min_universe_coverage'], '股票目录覆盖不足'),
                                  ('valid_quote_coverage', config['min_universe_coverage'], '有效收盘报价覆盖不足'),
                                  ('industry_coverage', config['min_industry_coverage'], '行业映射覆盖不足')]:
        value = fraction(context.get(key))
        if value is None or value < threshold:
            data_gaps.append(label)
    indices = context.get('indices', [])
    if (not isinstance(indices, list) or len(indices) != 2 or any(not isinstance(item, dict) for item in indices)
            or {item.get('code') for item in indices} != {'000001.SH', '399001.SZ'}
            or any(not item.get('source') or number(item.get('change_pct')) is None
                   or not -100 < number(item['change_pct']) < 100 or not _completed_quote(item, day, now) for item in indices)):
        data_gaps.append('两大指数收盘身份、数据交易日或涨幅未核验')
    elif any(number(item['change_pct']) <= config['index_drawdown_limit_pct'] for item in indices):
        rule_gaps.append('数据交易日大盘收盘处于急跌环境')
    if fraction(context.get('market_breadth')) is None:
        data_gaps.append('全市场收盘上涨家数占比缺失')
    elif fraction(context['market_breadth']) < config['min_market_breadth']:
        rule_gaps.append('全市场收盘上涨家数占比过低')
    if score < config['min_score']:
        rule_gaps.append('盘后研究规则分未达门槛')
    data_gaps.extend(bundle.get('errors', []))
    gaps = sorted(set(data_gaps + rule_gaps))
    status = 'incomplete' if data_gaps else 'filtered' if rule_gaps else 'ready'
    return {'code': identity, 'name': row.get('name'), 'price': row.get('price'),
            'quote_at': quote.get('quote_at'), 'change_pct': row.get('change_pct'), 'industry': industry,
            'score': min(100, score), 'status': 'watch_only', 'plan': None,
            'blockers': gaps + ['盘后研究仅供观察，须下一交易日按实时规则重新筛选'],
            'research_gaps': gaps, 'research_status': status,
            'research_basis': {'data_session': day, 'collected_at': now.isoformat(),
                               'disclosure_cutoff': now.isoformat(), 'score_basis': research.get('score_basis'),
                               'historical_1430_reconstruction': False},
            'enhanced_filter_blockers': advanced['gaps'], 'metrics': tech['metrics'], 'signals': tech['reasons'],
            'financials': finance, 'notice_risks': risks, 'catalysts': catalysts,
            'source_urls': bundle.get('source_urls', [])}
