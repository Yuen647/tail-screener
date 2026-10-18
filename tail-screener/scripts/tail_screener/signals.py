"""Point-in-time tail-session checks and candidate concentration controls.

These are transparent rule filters, not a fitted return predictor.  Turnover on
up-price minutes is a price/amount proxy, never an identified investor's flow.
All percentages below are fractions (0.03 means 3%).  No network or file I/O.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
from math import isfinite, sqrt
from statistics import mean
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
DEFAULTS = {
    'max_atr_pct': .06,
    'max_base_width_pct': .15,
    'min_intraday_close_location': .6,
    'max_tail_pullback_pct': .015,
    'max_tail_jump_pct': .03,
    'tail_window_minutes': 15,
    'min_tail_up_amount_ratio': .45,
    'minute_max_age_seconds': 180,
}


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _stamp(value):
    try:
        if not value or len(str(value)) <= 10:
            return None
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def _day(value):
    try:
        return date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _historical_checks(history, now, c, metrics, gaps, signals):
    parsed = [(_day(bar.get('date')), bar) for bar in history]
    if any(day is None for day, _ in parsed):
        gaps.append('波动率核验所需日线日期无效')
        return
    # Today's incomplete candle and future daily bars never enter a factor.
    completed = sorted(((day, bar) for day, bar in parsed if day < now.date()), key=lambda item: item[0])
    if len({day for day, _ in completed}) != len(completed):
        gaps.append('波动率核验所需日线日期重复')
        return
    if len(completed) < 15:
        gaps.append('ATR14核验至少需要15根已完成日线')
        return
    values = []
    for _, bar in completed[-15:]:
        high, low, close = (_number(bar.get(k)) for k in ('high', 'low', 'close'))
        if any(v is None or v <= 0 for v in (high, low, close)) or not low <= close <= high:
            gaps.append('波动率核验所需日线高低收无效')
            return
        values.append((high, low, close))
    true_ranges = [max(high-low, abs(high-values[i-1][2]), abs(low-values[i-1][2]))
                   for i, (high, low, _) in enumerate(values) if i]
    atr = mean(true_ranges)
    atr_pct = atr / values[-1][2]
    closes = [v[2] for v in values[-10:]]
    width = max(closes)/min(closes)-1
    metrics.update(atr14=round(atr, 6), atr14_pct=round(atr_pct, 6),
                   atr_method='14个已完成交易日真实波幅的算术均值，非Wilder递推',
                   consolidation_width_10d=round(width, 6))
    if atr_pct > c['max_atr_pct']:
        gaps.append('ATR14相对波动过高，隔夜风险超出规则门槛')
    if width > c['max_base_width_pct']:
        gaps.append('近10日收盘整理区间过宽')
    else:
        signals.append('近10日已完成收盘价处于较窄整理区间')


def _minute_vwap(recent, row, metrics, gaps):
    """Use explicit share quantities only; amount-weighted prices are not VWAP."""
    cumulative = all('cumulative_amount' in b and 'cumulative_volume' in b for _, b in recent)
    individual = all('volume' in b for _, b in recent)
    vwap = None
    if cumulative:
        pairs = [(_number(b['cumulative_amount']), _number(b['cumulative_volume'])) for _, b in recent]
        valid = all(a is not None and a > 0 and v is not None and v > 0 for a, v in pairs)
        valid = valid and all(a2 >= a1 and v2 >= v1 for (a1, v1), (a2, v2) in zip(pairs, pairs[1:]))
        if valid:
            vwap = pairs[-1][0]/pairs[-1][1]
            metrics['minute_vwap_scope'] = '当日累计成交额/累计成交股数'
        else:
            gaps.append('分钟累计成交量额无效，无法核验VWAP')
    elif individual:
        amounts = [_number(b['amount']) for _, b in recent]
        volumes = [_number(b['volume']) for _, b in recent]
        if (all(v is not None and v >= 0 for v in volumes) and sum(volumes) > 0
                and all((a == 0) == (v == 0) for a, v in zip(amounts, volumes))):
            vwap = sum(amounts)/sum(volumes)
            metrics['minute_vwap_scope'] = '尾盘窗口成交额/成交股数'
        else:
            gaps.append('分钟成交量额无效，无法核验VWAP')
    if vwap is not None:
        # This broad check catches lot/share unit mistakes without pretending
        # that close extrema bound every trade within the minute.
        price = _number(row.get('price'))
        if price is None or price <= 0 or not .5 <= vwap/price <= 1.5:
            gaps.append('分钟VWAP价格尺度异常，需核查成交量单位')
            return
        metrics['minute_vwap'] = round(vwap, 6)
        metrics['minute_vwap_supported'] = price >= vwap
        if price < vwap:
            gaps.append('最新价未守住可核验的分钟VWAP')


def advanced_checks(row, history, minutes, now, config):
    """Return {metrics, gaps, signals}; nonempty gaps block a live candidate.

    Minute rows require at, close and *incremental* amount. Optional volume is
    in shares, not lots; cumulative_amount / cumulative_volume are also yuan /
    shares. Missing optional volume leaves minute_vwap unknown and adds no
    invented evidence. A flat window has no directional-amount denominator.
    """
    if now.tzinfo is None:
        raise ValueError('now 必须明确时区')
    now = now.astimezone(TZ)
    c = {key: config.get(key, default) for key, default in DEFAULTS.items()}
    if any(_number(value) is None for value in c.values()):
        raise ValueError('增强信号配置须为有限数字')
    n = c['tail_window_minutes']
    if not isinstance(n, int) or isinstance(n, bool) or not 3 <= n <= 60:
        raise ValueError('tail_window_minutes 必须是3—60的整数')
    metrics = {'tail_window_minutes': n, 'minute_vwap': None,
               'minute_vwap_scope': None, 'minute_vwap_supported': None,
               'tail_flow_basis': '上涨分钟成交额占比是量价代理，不等同主动买入或主力净流入'}
    gaps, signals = [], []
    if not isinstance(history,list) or any(not isinstance(b,dict) for b in history):
        return {'metrics':metrics,'gaps':['增强信号所需日线结构无效'],'signals':[]}
    if not isinstance(minutes,list) or any(not isinstance(b,dict) for b in minutes):
        return {'metrics':metrics,'gaps':['增强信号所需分钟结构无效'],'signals':[]}
    _historical_checks(history, now, c, metrics, gaps, signals)

    price, high, low = (_number(row.get(k)) for k in ('price', 'high', 'low'))
    if any(v is None or v <= 0 for v in (price, high, low)) or not low <= price <= high or high == low:
        gaps.append('当日高低价或最新价无效，无法核验收盘位置')
    else:
        location = (price-low)/(high-low)
        metrics['intraday_close_location'] = round(location, 6)
        if location < c['min_intraday_close_location']:
            gaps.append('最新价位于当日区间偏弱位置')

    parsed = [(_stamp(b.get('at')), b) for b in minutes]
    if any(at is None for at, _ in parsed):
        gaps.append('分钟时间字段无效')
    if any(at is not None and at > now for at, _ in parsed):
        gaps.append('分钟数据含未来时间记录')
    eligible = [(at, b) for at, b in parsed if at is not None and at <= now and at.date() == now.date()]
    if any(b <= a for (a, _), (b, _) in zip(eligible, eligible[1:])):
        gaps.append('当日分钟时间重复或顺序错误')
    recent = eligible[-n:]
    if len(recent) < n:
        gaps.append(f'缺少连续{n}分钟尾盘成交记录')
        return {'metrics': metrics, 'gaps': list(dict.fromkeys(gaps)), 'signals': signals}
    if (any(not time(13, 0) <= at.time().replace(tzinfo=None) <= time(14, 57) for at, _ in recent)
            or any((b-a).total_seconds() != 60 for (a, _), (b, _) in zip(recent, recent[1:]))):
        gaps.append('尾盘分钟跨午休、非连续或不在下午连续竞价时段')
    if not 0 <= (now-recent[-1][0]).total_seconds() <= c['minute_max_age_seconds']:
        gaps.append('尾盘分钟序列过期')
    prices = [_number(b.get('close')) for _, b in recent]
    amounts = [_number(b.get('amount')) for _, b in recent]
    if any(v is None or v <= 0 for v in prices) or any(v is None or v < 0 for v in amounts):
        gaps.append('尾盘分钟成交价格或增量成交额无效')
        return {'metrics': metrics, 'gaps': list(dict.fromkeys(gaps)), 'signals': signals}
    positive_coverage = sum(a > 0 for a in amounts)/n
    tail_return = prices[-1]/prices[0]-1
    peak_pullback = 1-prices[-1]/max(prices)
    # The first row's prior price is outside the window: its amount is excluded
    # from direction attribution, and unchanged-price minutes remain neutral.
    up = sum(amounts[i] for i in range(1, n) if prices[i] > prices[i-1])
    down = sum(amounts[i] for i in range(1, n) if prices[i] < prices[i-1])
    directional = up+down
    ratio = up/directional if directional > 0 else None
    metrics.update(tail_first_at=recent[0][0].isoformat(), tail_last_at=recent[-1][0].isoformat(),
                   tail_return=round(tail_return, 6), tail_peak_pullback_pct=round(peak_pullback, 6),
                   tail_amount=round(sum(amounts), 2), tail_positive_amount_coverage=round(positive_coverage, 6),
                   tail_up_amount_ratio=round(ratio, 6) if ratio is not None else None,
                   tail_directional_amount=round(directional, 2),
                   tail_directional_amount_coverage=round(directional/sum(amounts), 6) if sum(amounts) else 0)
    if positive_coverage < .8:
        gaps.append('尾盘窗口有效成交分钟覆盖不足80%')
    if peak_pullback > c['max_tail_pullback_pct']:
        gaps.append('尾盘从窗口高点回撤过大')
    if tail_return > c['max_tail_jump_pct']:
        gaps.append('尾盘短时拉升过快，避免追逐突发脉冲')
    if ratio is not None:
        if ratio < c['min_tail_up_amount_ratio']:
            gaps.append('尾盘下跌分钟成交额占优')
        elif ratio >= .5 and directional >= sum(amounts)*.2:
            signals.append('尾盘上涨分钟成交额占优（价格方向代理，不代表主力买入）')
    _minute_vwap(recent, row, metrics, gaps)
    return {'metrics': metrics, 'gaps': list(dict.fromkeys(gaps)), 'signals': signals}


def _return_map(history, cutoff):
    dated = [(_day(bar.get('date')), bar) for bar in history]
    if any(day is None for day, _ in dated):
        return {}
    rows = sorted((day, _number(bar.get('close'))) for day, bar in dated if day < cutoff)
    if any(close is None or close <= 0 for _, close in rows):
        return {}
    if len({day for day, _ in rows}) != len(rows):
        return {}
    # Match both endpoints: a two-session return must not match a one-day return.
    window = rows[-61:]
    return {(d1, d2): p2/p1-1 for (d1, p1), (d2, p2) in zip(window, window[1:]) if d1 < d2}


def _correlation(first, second):
    first_at = _stamp((first.get('quote') or first.get('snapshot') or {}).get('quote_at'))
    second_at = _stamp((second.get('quote') or second.get('snapshot') or {}).get('quote_at'))
    if first_at is None or second_at is None:
        return None, 0
    cutoff = min(first_at.date(), second_at.date())
    a, b = (_return_map(bundle.get('history', []), cutoff) for bundle in (first, second))
    shared = sorted(a.keys() & b.keys())[-60:]
    if len(shared) < 40:
        return None, len(shared)
    x, y = [a[k] for k in shared], [b[k] for k in shared]
    mx, my = mean(x), mean(y)
    xx, yy = sum((v-mx)**2 for v in x), sum((v-my)**2 for v in y)
    if xx <= 1e-20 or yy <= 1e-20:
        return None, len(shared)
    corr = sum((u-mx)*(v-my) for u, v in zip(x, y))/sqrt(xx*yy)
    return max(-1.0, min(1.0, corr)), len(shared)


def select_diverse(evaluated, bundles, top, max_per_industry, max_return_corr=.85):
    """Preserve ranking; cap industries and correlated qualified observations.

    Histories need >=40 matched daily return intervals from the latest 60.
    Missing/constant series yield a warning, never a fabricated zero correlation.
    Research-ready and environment-wait observations enter the correlation
    constraint; other filtered observations do not. This grants no entry status.
    """
    if not isinstance(top, int) or not 1 <= top or not isinstance(max_per_industry, int) or not 1 <= max_per_industry <= top:
        raise ValueError('候选数和行业上限无效')
    if _number(max_return_corr) is None or not 0 <= max_return_corr <= 1:
        raise ValueError('相关性门槛需介于0和1')
    if isinstance(bundles, dict):
        by_code = bundles
    else:
        by_code = {(b.get('quote') or b.get('snapshot') or {}).get('code'): b for b in bundles}
    selected, excluded, warnings = [], [], []
    industry_count, seen = Counter(), set()
    for candidate in evaluated:
        code = candidate.get('code')
        if not code or code in seen:
            excluded.append({'code': code, 'reason': '重复或缺失股票代码'})
            continue
        seen.add(code)
        industry = candidate.get('industry') or '行业未知'
        if industry_count[industry] >= max_per_industry:
            excluded.append({'code': code, 'reason': '达到单行业候选上限', 'industry': industry})
            continue
        blocked = False
        qualifies = lambda item: (item.get('status') == 'conditional' or item.get('research_status') == 'ready'
                                  or item.get('watch_eligible') is True)
        if qualifies(candidate):
            for previous in selected:
                if not qualifies(previous):
                    continue
                corr, count = _correlation(by_code.get(code, {}), by_code.get(previous['code'], {}))
                if corr is None:
                    warnings.append(f"{code} 与 {previous['code']} 的收益相关性未知（有效对齐区间{count}个），不能确认分散化")
                elif corr > max_return_corr:
                    excluded.append({'code': code, 'reason': '与已选候选的历史日收益高度相关',
                                     'against': previous['code'], 'correlation': round(corr, 6),
                                     'aligned_returns': count})
                    blocked = True
                    break
        if blocked:
            continue
        if len(selected) >= top:
            excluded.append({'code': code, 'reason': '达到候选总数上限'})
            continue
        selected.append(candidate)
        industry_count[industry] += 1
    return {'selected': selected, 'excluded': excluded, 'warnings': list(dict.fromkeys(warnings))}
