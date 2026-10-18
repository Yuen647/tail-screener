"""Independently implemented, attributed rules; no legacy screening filters.

Connors' cited example concerns ETFs and closing prices. Using it on mainland
Chinese individual stocks is an unvalidated market adaptation; an intraday value
is only provisional. The article gives no fixed holding period or stop price.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import time
from functools import lru_cache
from statistics import mean

from .engine import number as _engine_number, session_date, stamp


CONNORS_STRATEGY_ID = 'connors_rsi2_a_share_research'
CONNORS_SOURCE = {
    'name': 'Larry Connors RSI(2) 均值回归',
    'author': 'Larry Connors',
    'title': 'How To Enter and Exit a Position on the Close',
    'url': 'https://tradingmarkets.com/recent/how_to_enter_and_exit_a_position_on_the_close-677551',
    'published_date': '2009-08-21',
    'original_market': 'ETF；文中示例为美国上市 IWM、FXI',
    'original_entry': '收盘价 > 200日简单移动平均线，且2周期RSI收盘值 < 5，收盘入场',
    'original_exit': '2周期RSI收盘值 > 70，收盘退出',
    'native_market': 'ETF；文中示例为美国上市 IWM、FXI',
    'native_entry': '收盘价 > SMA200 且 RSI(2) < 5，收盘入场',
    'native_exit': 'RSI(2) > 70，收盘退出',
    'adaptations': ['ETF规则迁移至沪深A股个股，效果未经本地验证',
                    '盘中仅预观察，正式规则仍依据收盘；不要求固定运行时间',
                    'A股新增持仓遵守T+1可卖，价格触发不代表必然成交'],
    'holding_period': '动态持有，直到收盘RSI(2)>70；数日仅为作者案例，无固定或最长持有保证',
    'implementation': '独立实现公式；不复制第三方程序，不继承原市场收益结论',
}
RSI_METHOD = (
    'Wilder RSI(2)：前2个价格变化的上涨/下跌绝对值分别取简单均值作种子；'
    '后续均值=(前均值+当期值)/2；RSI=100-100/(1+平均上涨/平均下跌)。'
    '平均下跌为0且上涨>0时为100；两者均0时取中性50。'
    '原文未规定初始化细节，这是公开声明的算法实现选择。'
)


def number(value):
    try:
        return _engine_number(value)
    except OverflowError:
        return None


def wilder_rsi(closes, period=2):
    """Return Wilder RSI without changing the input; zero-change RSI is 50."""
    if (not isinstance(period, int) or isinstance(period, bool) or period < 1
            or not isinstance(closes, (list, tuple)) or len(closes) < period + 1):
        return None
    values = [number(value) for value in closes]
    if any(value is None or value <= 0 for value in values):
        return None
    changes = [current - previous for previous, current in zip(values, values[1:])]
    gain = mean(max(change, 0) for change in changes[:period])
    loss = mean(max(-change, 0) for change in changes[:period])
    for change in changes[period:]:
        # Divide before addition to avoid unnecessary intermediate overflow.
        gain = gain * ((period - 1) / period) + max(change, 0) / period
        loss = loss * ((period - 1) / period) + max(-change, 0) / period
    if gain == 0 and loss == 0:
        return 50.0
    if loss == 0:
        return 100.0
    # This equivalent form is stable for very large gain/loss ratios.
    if gain >= loss:
        return 100 / (1 + loss / gain)
    ratio = gain / loss
    return 100 * ratio / (1 + ratio)


@lru_cache(maxsize=128)
def _session_dates(first, last):
    """Known exchange sessions only; no network and no weekday fallback."""
    import exchange_calendars
    calendar = exchange_calendars.get_calendar('XSHG')
    return tuple(value.date().isoformat() for value in calendar.sessions_in_range(first, last))


def _base_result(data_session):
    return {
        'strategy_id': CONNORS_STRATEGY_ID, 'source': deepcopy(CONNORS_SOURCE),
        'market_adaptation': '原规则用于ETF；沪深A股个股应用为待验证的市场迁移，盘中仅为收盘信号预观察。',
        'data_session': data_session,
        'metrics': {'price': None, 'sma200': None, 'rsi2': None, 'history_count': 0,
                    'previous_close': None, 'rsi_method': RSI_METHOD,
                    'adjustment_basis': '所有历史日线须由数据源明确标为前复权qfq；目标日使用同尺度报价'},
        'conditions': [], 'raw_entry_match': None, 'confirmation_status': 'incomplete',
        'exit_trigger': {'rule': 'RSI(2)>70，收盘退出', 'threshold': 70,
                         'observed': None, 'triggered': None, 'confirmation_status': 'incomplete',
                         'requires_existing_position': True,
                         'execution_note': 'A股新增持仓须满足T+1可卖；触发条件不保证收盘价成交。'},
        'holding_period': {
            'type': 'dynamic', 'label': '动态持有；数日仅为作者案例，无固定天数或最长持有保证',
            'entry_timing': '原规则收盘确认；盘中为预观察，收盘后确认不代表已按该收盘价成交',
            'exit_timing': '持仓后逐个交易日收盘核查RSI(2)>70；不设自拟止盈止损或固定退出日',
        },
        'data_gaps': [],
    }


def evaluate_connors(history, quote, data_session, confirmed_close, previous_session=None):
    """Evaluate exactly close>SMA200 and RSI(2)<5; exit when RSI(2)>70.

    Input dates must form an unbroken exchange-session sequence. There must be
    at least 200 observations including the target session, exactly once. Any
    target-session daily bar is replaced by the quote, so a live partial bar
    cannot double-count the current observation. For an explicitly confirmed
    close and no quote, a dated, qfq target daily close may instead be used.
    ``confirmed_close`` and ``data_session`` must come from the caller's checked
    clock/calendar context; this pure calculation does not change system time.
    """
    result = _base_result(data_session)
    gaps = result['data_gaps']
    day = session_date(data_session)
    if day is None:
        gaps.append('目标交易日格式无效')
    if not isinstance(confirmed_close, bool):
        gaps.append('收盘确认标识须为布尔值')
    if not isinstance(history, list) or any(not isinstance(row, dict) for row in history):
        gaps.append('历史日线结构无效')
        return result
    if quote is not None and not isinstance(quote, dict):
        gaps.append('目标报价结构无效')
        return result
    if gaps:
        return result
    bars = []
    seen = set()
    for row in history:
        bar_day = session_date(row.get('date'))
        if bar_day is None:
            gaps.append('历史日线含无效日期')
            continue
        if bar_day > day:
            gaps.append('历史日线含目标交易日之后的未来数据')
            continue
        if row['date'] in seen:
            gaps.append('历史日线日期重复')
            continue
        seen.add(row['date'])
        close = number(row.get('close'))
        if close is None or close <= 0:
            gaps.append('历史收盘价必须为有限正数')
            continue
        if row.get('adjustment_actual') != 'qfq':
            gaps.append('历史复权口径缺失、未复权或混合；只接受数据源明确标识的qfq')
            continue
        bars.append((row['date'], close))
    if gaps:
        result['data_gaps'] = list(dict.fromkeys(gaps))
        return result
    bars.sort()
    target_bars = [row for row in bars if row[0] == data_session]
    prior = [row for row in bars if row[0] < data_session]
    quote = quote or {}
    if quote:
        price, prev_close = number(quote.get('price')), number(quote.get('prev_close'))
        at = stamp(quote.get('quote_at'))
        if price is None or price <= 0 or prev_close is None or prev_close <= 0:
            gaps.append('目标报价与昨收必须为有限正数')
        if (at is None or len(str(quote.get('quote_at', ''))) <= 10
                or at.date() != day):
            gaps.append('目标报价未带同一交易日的完整时间')
        elif confirmed_close and at.time().replace(tzinfo=None) < time(15):
            gaps.append('目标报价尚未收盘，不能确认原版收盘信号')
        result['metrics']['price_source'] = 'quote'
    elif confirmed_close and len(target_bars) == 1:
        price = target_bars[0][1]
        prev_close = prior[-1][1] if prior else None
        result['metrics']['price_source'] = 'confirmed_daily_close'
    else:
        price = prev_close = None
        gaps.append('缺少目标报价；仅明确收盘模式可用目标日完整日线代替')
    if not prior:
        gaps.append('缺少目标交易日之前的历史收盘价')
    if prior and prev_close is not None:
        # 1 cent is a price-rounding allowance, not an alpha/strategy threshold.
        difference = abs(prior[-1][1] - prev_close)
        result['metrics']['previous_close_difference'] = difference
        result['metrics']['previous_close_rounding_tolerance'] = .01
        if difference > .01 + 1e-10:
            gaps.append('前复权历史末日收盘与报价昨收不一致，须核验除权或陈旧数据')
    if previous_session is not None:
        previous_day = session_date(previous_session)
        if previous_day is None or previous_day >= day or not prior or prior[-1][0] != previous_session:
            gaps.append('日线末日未覆盖指定的上一交易日')
    dates = [row[0] for row in prior] + [data_session]
    result['metrics'].update(price=price, previous_close=prev_close, history_count=len(dates),
                             historical_completed_count=len(prior),
                             history_last_date=prior[-1][0] if prior else None)
    if len(dates) < 200:
        gaps.append('SMA200至少需要200个连续交易日观测（含目标日一次）')
    if prior:
        try:
            expected = _session_dates(prior[0][0], data_session)
            if not expected or expected[-1] != data_session or tuple(dates) != expected:
                gaps.append('日线日期未覆盖连续已核验交易日，存在缺失、非交易日或停牌间隔')
            if previous_session is not None and (len(expected) < 2 or expected[-2] != previous_session):
                gaps.append('指定上一交易日与离线交易所日历不一致')
            result['metrics']['calendar_source'] = 'exchange_calendars:XSHG'
        except Exception as exc:
            gaps.append(f'连续交易日历无法核验：{type(exc).__name__}')
    if gaps:
        result['data_gaps'] = list(dict.fromkeys(gaps))
        return result
    closes = [row[1] for row in prior] + [price]
    sma200, rsi2 = mean(closes[-200:]), wilder_rsi(closes, 2)
    if rsi2 is None:
        gaps.append('RSI计算所需历史不足或无效')
        return result
    confirmation = 'confirmed_close' if confirmed_close else 'provisional'
    result['metrics'].update(sma200=sma200, rsi2=rsi2,
                             rsi_seed_date=dates[2], rsi_observation_count=len(closes))
    result['conditions'] = [
        {'id': 'above_sma200', 'label': '目标价格严格高于200日简单均线',
         'observed': price, 'operator': '>', 'threshold': sma200, 'passed': price > sma200},
        {'id': 'rsi2_below_5', 'label': '2周期RSI严格低于5',
         'observed': rsi2, 'operator': '<', 'threshold': 5, 'passed': rsi2 < 5},
    ]
    result['raw_entry_match'] = price > sma200 and rsi2 < 5
    result['confirmation_status'] = confirmation
    result['exit_trigger'].update(observed=rsi2, triggered=rsi2 > 70, confirmation_status=confirmation)
    return result
