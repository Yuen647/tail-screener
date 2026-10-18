"""Pure, point-in-time screening and fail-closed execution conditions."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, time
from decimal import Decimal, ROUND_FLOOR
from math import isfinite
from statistics import mean, median
from zoneinfo import ZoneInfo
import re
from .signals import advanced_checks

TZ = ZoneInfo("Asia/Shanghai")
NEGATIVE_TITLES = ("立案", "调查通知", "退市", "风险警示", "停牌", "预亏", "预减", "下修", "减持", "违约", "冻结", "重大诉讼", "行政处罚", "无法表示意见", "否定意见")
REVIEW_TITLES = ("异常波动", "澄清", "重大资产", "关联交易", "收购", "权益分派", "限售股份", "解除限售", "解禁")
POSITIVE_TITLES = ("预增", "扭亏", "中标", "重大合同", "订单", "回购", "获批", "注册证", "投产")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError):
        return None


def stamp(value):
    """Date-only evidence becomes available at end of day, never at midnight."""
    if not value:
        return None
    try:
        value = str(value)
        if len(value) == 10:
            return datetime.combine(datetime.fromisoformat(value).date(), time.max, TZ)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed.astimezone(TZ)
    except (ValueError, TypeError, OverflowError):
        return None


def session_date(value):
    """Require a real, canonical date rather than a lexicographically sortable string."""
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def fraction(value):
    value = number(value)
    return value if value is not None and 0 <= value <= 1 else None


def floor_price(value):
    return float(Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_FLOOR))


def is_a_share(code):
    # Issuer notice 2025-02-15: 300114 becomes 302132 on 2025-02-17.
    # https://static.cninfo.com.cn/finalpage/2025-02-15/1222544408.PDF
    return bool(re.fullmatch(r"(?:60[0135]\d{3}|688\d{3})\.SH|(?:00[0123]\d{3}|30[01]\d{3}|302132)\.SZ", str(code)))


def validate_config(c):
    numeric = [k for k in c if k not in ("start_time", "last_entry_time", "next_day_exit_time")]
    if any(isinstance(c[k], bool) or number(c[k]) is None for k in numeric):
        raise ValueError("配置中的数值必须是有限数字")
    for k in ("top_n", "history_candidates", "deep_candidates", "workers", "min_history", "max_per_industry"):
        if not isinstance(c[k], int):
            raise ValueError(f"{k} 必须是整数")
    if not 1 <= c['top_n'] <= c['deep_candidates'] <= c['history_candidates'] <= 600:
        raise ValueError("需要 1 ≤ top_n ≤ deep_candidates ≤ history_candidates ≤ 600")
    if not 1 <= c['workers'] <= 16 or not 30 <= c['budget_seconds'] <= 1200:
        raise ValueError("workers 需1—16；budget_seconds 需30—1200")
    if not 65 <= c['min_history'] <= 100 or c['min_amount'] < 100000000:
        raise ValueError("历史日线须65—100个交易日，成交额至少1亿元")
    for k in ('max_range_position', 'max_ma20_premium', 'max_five_day_return', 'max_twenty_day_return', 'min_market_breadth', 'stop_loss_pct', 'take_profit_pct'):
        if not 0 < c[k] < 1:
            raise ValueError(f"{k} 需介于0和1")
    if not 0 < c['min_change_pct'] < c['max_change_pct'] <= 8:
        raise ValueError("涨幅区间需大于0，最高不超过8%")
    if not 0 < c['min_turnover_pct'] < c['max_turnover_pct'] <= 30:
        raise ValueError("换手率区间无效")
    if not 0 < c['min_volume_ratio'] < c['max_volume_ratio'] <= 6:
        raise ValueError("量能区间无效")
    if not 60 <= c['min_score'] <= 100 or not 1 <= c['max_per_industry'] <= c['top_n']:
        raise ValueError("评分门槛至少60；单行业上限需在候选总数以内")
    if not 0.95 <= c['min_universe_coverage'] <= 1 or not 0.90 <= c['min_industry_coverage'] <= 1:
        raise ValueError("覆盖率门槛过低")
    for k, limit in [('quote_max_age_seconds', 120), ('minute_max_age_seconds', 180), ('snapshot_max_age_seconds', 300)]:
        if not 1 <= c[k] <= limit:
            raise ValueError(f"{k} 不可超过{limit}秒")
    start, end = time.fromisoformat(c['start_time']), time.fromisoformat(c['last_entry_time'])
    if not time(14, 30) <= start < end <= time(14, 55):
        raise ValueError("尾盘观察窗口须在14:30—14:55以内")
    if not time(9, 30) < time.fromisoformat(c['next_day_exit_time']) <= time(11, 30):
        raise ValueError("次日时间退出须在9:30—11:30")
    if not -5 <= c['index_drawdown_limit_pct'] < 0:
        raise ValueError("指数跌幅门槛无效")
    if not 0 < c.get('max_quote_disagreement_pct',.005) <= .005 or not 0 < c.get('max_quote_time_skew_seconds',60) <= 60:
        raise ValueError('两源报价偏差不可超过0.5%，时间差不可超过60秒')
    for key in ('max_atr_pct','max_base_width_pct','min_intraday_close_location','max_tail_pullback_pct','max_tail_jump_pct','min_tail_up_amount_ratio','max_return_correlation'):
        if not 0 < c.get(key, .5) <= 1:
            raise ValueError(f'{key} 需大于0且不超过1')
    if not isinstance(c.get('tail_window_minutes',15),int) or not 3 <= c.get('tail_window_minutes',15) <= 60:
        raise ValueError('tail_window_minutes 须为3—60的整数')
    return c


def coarse_filter(row, c):
    if not is_a_share(row.get('code')):
        return False
    name = str(row.get('name') or '').upper().strip()
    if not name or 'ST' in name or '退' in name or name.startswith(('N', 'C')):
        return False
    vals = [number(row.get(k)) for k in ('price', 'amount', 'change_pct', 'turnover_pct')]
    if any(v is None for v in vals):
        return False
    price, amount, change, turnover = vals
    return (price > 0 and amount >= c['min_amount'] and c['min_change_pct'] <= change <= c['max_change_pct']
            and c['min_turnover_pct'] <= turnover <= c['max_turnover_pct'])


def coarse_rank(row):
    # Rank moderate strength/liquidity, not the biggest daily price jump.
    return min(row['amount'] / 1e9, 4) + max(0, 5 - abs(row['change_pct'] - 2.5)) + max(0, 3 - abs(row['turnover_pct'] - 4) / 3)


def trading_minutes(at):
    t = at.astimezone(TZ)
    n = t.hour * 60 + t.minute - 570
    return min(240, max(0, min(120, n)) + max(0, t.hour * 60 + t.minute - 780))


def technical(row, history, now, c, *, data_session=None, elapsed_minutes=None):
    gaps, reasons = [], []
    # The studied session's price/volume comes from the quote.  Exclude its
    # daily candle both intraday (incomplete) and after close (avoid duplication).
    bad = lambda message: {'score': 0, 'gaps': [message], 'reasons': [], 'metrics': {}}
    factor_day = now.date() if data_session is None else session_date(data_session)
    if factor_day is None or factor_day > now.date():
        return bad('量价研究的数据交易日无效或来自未来')
    if elapsed_minutes is not None and (number(elapsed_minutes) is None or not 0 < number(elapsed_minutes) <= 240):
        return bad('量价研究的累计交易分钟数无效')
    if not isinstance(history, list) or any(not isinstance(b, dict) or session_date(b.get('date')) is None for b in history):
        return bad('日线日期格式或记录结构无效')
    if any(session_date(b['date']) > now.date() for b in history):
        return bad('日线含未来交易日')
    bars = [dict(b) for b in history if session_date(b['date']) < factor_day]
    if len({b.get('date') for b in bars}) != len(bars):
        return {'score': 0, 'gaps': ['日线日期重复'], 'reasons': [], 'metrics': {}}
    bars = sorted(bars, key=lambda b: b['date'])
    if len(bars) < c['min_history']:
        return {'score': 0, 'gaps': ['上市历史或日线不足65个交易日'], 'reasons': [], 'metrics': {}}
    if any(any(number(b.get(k)) is None or number(b.get(k)) <= 0 for k in ('open', 'close', 'high', 'low', 'volume')) for b in bars):
        return {'score': 0, 'gaps': ['日线价格或成交量无效'], 'reasons': [], 'metrics': {}}
    for b in bars:
        for k in ('open', 'close', 'high', 'low', 'volume'):
            b[k] = number(b[k])
        if b['low'] > min(b['open'], b['close']) or b['high'] < max(b['open'], b['close']) or b['low'] > b['high']:
            return bad('日线OHLC价格区间不成立')
    if any(number(row.get(k)) is None or number(row.get(k)) <= 0 for k in ('price', 'prev_close', 'volume')):
        return {'score': 0, 'gaps': ['现价、昨收或成交量无效'], 'reasons': [], 'metrics': {}}
    row = dict(row, **{k:number(row[k]) for k in ('price', 'prev_close', 'volume')})
    p = row['price']
    closes = [b['close'] for b in bars]
    if abs(closes[-1] / row['prev_close'] - 1) > .01:
        gaps.append('前复权日线与昨收不一致（除权或过期）')
    last_date = datetime.fromisoformat(bars[-1]['date']).date()
    if (factor_day - last_date).days > 10:
        gaps.append('历史日线过期或长期停牌')
    lo, hi = min(b['low'] for b in bars[-60:]), max(b['high'] for b in bars[-60:])
    pos = (p - lo) / (hi - lo) if hi > lo else 1.0
    ma5, ma10, ma20 = (mean((closes + [p])[-n:]) for n in (5, 10, 20))
    prior_ma20 = mean(closes[-25:-5])
    r5, r20 = p / closes[-5] - 1, p / closes[-20] - 1
    v20 = mean(b['volume'] for b in bars[-20:])
    quiet = mean(b['volume'] for b in bars[-5:]) / mean(b['volume'] for b in bars[-25:-5])
    elapsed = trading_minutes(now) if elapsed_minutes is None else number(elapsed_minutes)
    vr = row.get('volume', 0) / (v20 * elapsed / 240) if elapsed else 0
    obv5 = sum((1 if bars[i]['close'] > bars[i-1]['close'] else -1 if bars[i]['close'] < bars[i-1]['close'] else 0) * bars[i]['volume'] for i in range(len(bars)-5, len(bars)))
    trend = p >= ma5 >= ma10 >= ma20 >= prior_ma20
    if not 0 <= pos <= c['max_range_position']:
        gaps.append('不在60日低位区间')
    if not trend:
        gaps.append('均线尚未转强，防止把下跌误判为低位')
    if not 0 <= p / ma20 - 1 <= c['max_ma20_premium']:
        gaps.append('价格偏离20日均线过大或仍在其下方')
    if r5 > c['max_five_day_return'] or r20 > c['max_twenty_day_return']:
        gaps.append('近期涨幅过大')
    if not c['min_volume_ratio'] <= vr <= c['max_volume_ratio']:
        gaps.append('量能不足或短时过度放量')
    if trend:
        reasons.append('5/10/20日均线向上排列且20日均线未走低')
    if quiet <= .9 and -.03 <= closes[-1] / closes[-6] - 1 <= .06:
        reasons.append('前5日缩量整理（潜伏代理信号，无法确认主力）')
    if obv5 > 0:
        reasons.append('近5日上涨日成交量占优（量价代理信号）')
    score = (20 if trend else 0) + (15 if 0 <= pos <= c['max_range_position'] else 0)
    score += (10 if c['min_volume_ratio'] <= vr <= c['max_volume_ratio'] else 0)
    score += (5 if quiet <= .9 else 0) + (5 if obv5 > 0 else 0)
    return {'score': score, 'gaps': gaps, 'reasons': reasons, 'metrics': {
        'range_position_60d': round(pos, 4), 'ma5': round(ma5, 3), 'ma10': round(ma10, 3),
        'ma20': round(ma20, 3), 'return_5d': round(r5, 4), 'return_20d': round(r20, 4),
        'volume_ratio_time_adjusted': round(vr, 3), 'quiet_volume_ratio': round(quiet, 3),
        'obv5': round(obv5), 'history_last_date': bars[-1]['date'],
        'flow_basis': '量价代理；不等同主力净买入或资金潜伏事实'}}


def sector_context(universe, mapping):
    groups = defaultdict(list)
    usable = [r for r in universe if is_a_share(r.get('code')) and number(r.get('change_pct')) is not None and r.get('amount', 0) > 0
              and 'ST' not in r.get('name', '').upper() and '退' not in r.get('name', '') and not r.get('name', '').startswith(('N', 'C'))]
    for row in usable:
        if mapping.get(row['code']):
            groups[mapping[row['code']]].append(row)
    total_amount = sum(r['amount'] for r in usable) or 1
    sectors = {}
    for name, rows in groups.items():
        if len(rows) < 5:
            continue
        sectors[name] = {'members': len(rows), 'breadth': sum(r['change_pct'] > 0 for r in rows) / len(rows),
                         'median_change_pct': median(r['change_pct'] for r in rows),
                         'amount_share': sum(r['amount'] for r in rows) / total_amount}
    coverage = sum(bool(mapping.get(r['code'])) for r in usable) / max(1, len(usable))
    breadth = sum(r['change_pct'] > 0 for r in usable) / max(1, len(usable))
    return sectors, coverage, breadth


def notice_review(bundle, now):
    gaps, hits, catalysts = [], [], []
    if not bundle.get('verified') or not bundle.get('source'):
        return ['公告接口失败或标的身份未核验'], [], []
    items = bundle.get('items')
    if not isinstance(items, list) or not items:
        return ['公告清单为空或结构未核验'], [], []
    for row in items:
        if not isinstance(row, dict) or not isinstance(row.get('title'), str) or not row['title'].strip():
            gaps.append('公告标题缺失，不能判断公告风险')
            continue
        at = stamp(row.get('published_at'))
        if at is None:
            gaps.append('公告发布时间不明确，需人工核查')
            continue
        if at > now:
            # Provider date-stamps tomorrow's release while display_time is absent.
            gaps.append('发现未来日期公告，发布时间待核')
            continue
        if (now - at).days > 14:
            continue
        title = row.get('title', '')
        if any(k in title for k in NEGATIVE_TITLES + REVIEW_TITLES):
            hits.append(row)
        if (now-at).total_seconds() <= 72*3600 and any(k in title for k in POSITIVE_TITLES):
            catalysts.append(row)
    if hits:
        gaps.append('公告标题命中风险或需全文复核事项')
    return gaps, hits, catalysts


def execution_gaps(q, minutes, now, c):
    gaps = []
    at = stamp(q.get('quote_at'))
    if at is None or not 0 <= (now-at).total_seconds() <= c['quote_max_age_seconds']:
        gaps.append('报价过期、无日期或来自未来')
    required = ('price', 'bid', 'ask', 'bid_size', 'ask_size', 'vwap', 'amount', 'volume', 'limit_up', 'limit_down')
    if any(number(q.get(k)) is None or number(q[k]) <= 0 for k in required):
        gaps.append('双边盘口、涨跌停价或VWAP缺失')
    else:
        q = dict(q, **{k:number(q[k]) for k in required})
        if any(abs(q[k]/q['price']-1) > .01 for k in ('bid', 'ask')) or not 0 <= (q['ask']-q['bid'])/q['price'] <= .003:
            gaps.append('买卖价差或盘口价格异常')
        if not q['vwap'] <= q['price'] <= q['vwap']*1.025 or q['ask'] > q['vwap']*1.025:
            gaps.append('未守住VWAP或追价幅度过大')
        if not q['limit_down'] < q['price'] < q['limit_up'] or not q['limit_down'] < q['bid'] <= q['ask'] < q['limit_up']:
            gaps.append('报价或盘口超出涨跌停区间')
        if q['ask'] >= q['limit_up'] * .985 or q['bid'] <= q['limit_down'] * 1.015:
            gaps.append('距离涨跌停价过近')
        if abs(q['vwap']/(q['amount']/q['volume'])-1) > .001:
            gaps.append('VWAP与成交量额不一致')
        if any(abs(v*100-round(v*100)) > 1e-6 for v in (q['bid'],q['ask'])):
            gaps.append('盘口价格不符合A股分位报价单位')
        if floor_price(min(q['ask']*1.003, q['vwap']*1.025)) < q['ask']:
            gaps.append('卖一价已超过可执行追价上限')
        if q['amount'] < c['min_amount']:
            gaps.append('刷新后成交额低于门槛')
    if not isinstance(minutes, list):
        return gaps + ['分时记录结构无效']
    if any(not isinstance(b, dict) or stamp(b.get('at')) is None for b in minutes):
        return gaps + ['分时记录时间无效']
    if any(stamp(b['at']) > now for b in minutes):
        gaps.append('分时记录包含未来时间')
    valid = [b for b in minutes if stamp(b['at']) <= now]
    all_times = [stamp(b['at']) for b in valid]
    if all_times != sorted(set(all_times)):
        gaps.append('分时记录重复或顺序错误')
    recent = valid[-3:]
    if len(recent) < 3:
        gaps.append('缺少连续三分钟有效成交')
    else:
        times = [stamp(b['at']) for b in recent]
        if (any(t.date() != now.date() for t in times) or any((b-a).total_seconds() != 60 for a,b in zip(times,times[1:]))
                or any((number(b.get('amount')) or 0) <= 0 for b in recent)):
            gaps.append('分钟数据跨日、不连续或无成交')
        if not 0 <= (now-times[-1]).total_seconds() <= c['minute_max_age_seconds']:
            gaps.append('分时成交过期')
        if at is None or not 0 <= (at-times[-1]).total_seconds() <= 180:
            gaps.append('报价与分时不同步')
        close = number(recent[-1].get('close'))
        price = number(q.get('price'))
        if price is None or price <= 0 or close is None or abs(close/price-1) > .01:
            gaps.append('分时价格与报价不一致')
    return gaps


def evaluate(bundle, context, now, c, replay=False):
    q = bundle.get('quote') or bundle['snapshot']
    row = dict(bundle['snapshot'], **q)
    # Snapshot turnover may be stale: live quote must carry the current number.
    gaps = []
    identity = bundle['snapshot'].get('code')
    if not q.get('source') or q.get('code') != identity:
        gaps.append('实时行情来源缺失或标的身份不一致')
    if not bundle.get('quote'):
        gaps.append('缺少独立刷新后的实时报价')
    row['code'] = identity
    cross = bundle.get('cross_quote') or {}
    primary_at, cross_at = stamp(q.get('quote_at')), stamp(cross.get('quote_at'))
    if not cross.get('source') or cross.get('source') == q.get('source') or cross.get('code') != identity:
        gaps.append('独立行情来源缺失或标的身份不一致')
    if (cross_at is None or primary_at is None or not 0 <= (now-cross_at).total_seconds() <= c['quote_max_age_seconds']
            or abs((cross_at-primary_at).total_seconds()) > c.get('max_quote_time_skew_seconds',60)):
        gaps.append('独立行情过期或两源时间不同步')
    for key, tolerance in [('price',c.get('max_quote_disagreement_pct',.005)), ('prev_close',.002), ('amount',.10), ('volume',.10)]:
        primary, secondary = number(q.get(key)), number(cross.get(key))
        if primary is None or secondary is None or primary <= 0 or secondary <= 0 or abs(secondary/primary-1) > tolerance:
            gaps.append(f'两源行情{key}缺失或不一致')
    if not coarse_filter(row, c):
        gaps.append('刷新后的价格、成交额或换手率不符合初筛')
    tech = technical(row, bundle.get('history', []), now, c)
    gaps.extend(tech['gaps'])
    advanced = advanced_checks(row,bundle.get('history',[]),bundle.get('minutes',[]),now,c)
    gaps.extend(advanced['gaps'])
    tech['metrics'].update(advanced['metrics'])
    tech['reasons'].extend(advanced['signals'])
    previous = session_date(context.get('calendar',{}).get('previous_session'))
    if previous is None or previous >= now.date() or tech['metrics'].get('history_last_date') != previous.isoformat():
        gaps.append('日线未更新至已核验的上一交易日')
    score = tech['score']
    finance = bundle.get('financials') or {}
    notice_at = stamp(finance.get('published_at'))
    period = stamp(finance.get('report_date'))
    if (finance.get('code') != row['code'] or not finance.get('source') or notice_at is None or notice_at > now
            or period is None or notice_at < period or not 0 <= (now-period).days <= 210):
        gaps.append('最新已披露财报缺失、过期或披露时间不成立')
    else:
        profit, core, growth = (number(finance.get(k)) for k in ('net_profit', 'core_profit', 'revenue_growth_pct'))
        if profit is None or core is None or growth is None:
            gaps.append('财报利润、扣非利润或营收增速缺失')
        elif profit <= 0 or core <= 0 or growth < -20:
            gaps.append('亏损、扣非亏损或营收大幅下滑')
        else:
            score += 10 if growth >= 0 else 5
    notices = bundle.get('notices') or {}
    news_gaps, risks, catalysts = notice_review(notices, now)
    if notices.get('code') != identity:
        news_gaps.append('公告标的身份与行情不一致')
    gaps.extend(news_gaps)
    if not news_gaps:
        score += 5 + (5 if catalysts else 0)
    industry = context.get('industries', {}).get(row['code'], '')
    sector = context.get('sectors', {}).get(industry, {})
    if not sector:
        gaps.append('行业归属或板块样本不足')
    elif (fraction(sector.get('breadth')) is None or number(sector.get('median_change_pct')) is None
          or number(sector.get('members')) is None or number(sector['members']) < 5):
        gaps.append('行业广度、涨幅或样本数无效')
    elif fraction(sector['breadth']) < .5 or number(sector['median_change_pct']) <= 0:
        gaps.append('所属行业未形成多数上涨')
    else:
        score += 10
    intraday_gaps = execution_gaps(q, bundle.get('minutes', []), now, c)
    gaps.extend(intraday_gaps)
    if not intraday_gaps:
        score += 15
    calendar = context.get('calendar', {})
    if not calendar.get('verified') or calendar.get('session') != now.date().isoformat() or not calendar.get('is_session'):
        gaps.append('交易日历未确认当前开市日')
    next_session = session_date(calendar.get('next_session'))
    if next_session is None or next_session <= now.date():
        gaps.append('下一个交易日未确认')
    if not time.fromisoformat(c['start_time']) <= now.time().replace(tzinfo=None) <= time.fromisoformat(c['last_entry_time']):
        gaps.append('当前不在北京时间尾盘筛选窗口')
    if fraction(context.get('universe_coverage')) is None or fraction(context['universe_coverage']) < c['min_universe_coverage']:
        gaps.append('全市场快照分页覆盖不足')
    for key, label in [('valid_quote_coverage','全市场有效报价覆盖不足'),('dated_quote_coverage','全市场带日期行情覆盖不足')]:
        if fraction(context.get(key)) is None or fraction(context[key]) < c['min_universe_coverage']:
            gaps.append(label)
    if fraction(context.get('industry_coverage')) is None or fraction(context['industry_coverage']) < c['min_industry_coverage']:
        gaps.append('行业映射覆盖不足')
    snapshot_at = stamp(context.get('snapshot_fetched_at'))
    if snapshot_at is None or not 0 <= (now-snapshot_at).total_seconds() <= c['snapshot_max_age_seconds']:
        gaps.append('板块横截面采集过期')
    market_at = stamp(context.get('market_quote_at'))
    if market_at is None or not 0 <= (now-market_at).total_seconds() <= c['snapshot_max_age_seconds']:
        gaps.append('全市场源行情时间未核验或过期')
    indices = context.get('indices', [])
    if (not isinstance(indices,list) or len(indices) != 2 or any(not isinstance(x,dict) for x in indices)
            or {x.get('code') for x in indices} != {'000001.SH','399001.SZ'}
            or any(not x.get('source') or number(x.get('change_pct')) is None or not -100 < number(x['change_pct']) < 100 for x in indices)):
        gaps.append('上证与深证指数身份、来源或涨幅无效')
    elif any(not stamp(x.get('quote_at')) or not 0 <= (now-stamp(x['quote_at'])).total_seconds() <= 120 for x in indices):
        gaps.append('大盘指数报价未确认新鲜度')
    elif any(number(x['change_pct']) <= c['index_drawdown_limit_pct'] for x in indices):
        gaps.append('大盘处于急跌环境')
    if fraction(context.get('market_breadth')) is None or fraction(context['market_breadth']) < c['min_market_breadth']:
        gaps.append('全市场上涨家数占比过低')
    if score < c['min_score']:
        gaps.append('多维评分未达门槛')
    if replay:
        gaps.append('离线回放仅验证规则，不产生实时买入候选')
    if bundle.get('errors'):
        gaps.extend(bundle['errors'])
    gaps = sorted(set(gaps))
    plan = None
    if not gaps:
        reference = number(q['ask'])
        deadlines = [stamp(q['quote_at']) + timedelta(seconds=c['quote_max_age_seconds']),
                     cross_at + timedelta(seconds=c['quote_max_age_seconds']),
                     max(stamp(b['at']) for b in bundle['minutes'] if stamp(b['at']) <= now) + timedelta(seconds=c['minute_max_age_seconds']),
                     snapshot_at + timedelta(seconds=c['snapshot_max_age_seconds']),
                     market_at + timedelta(seconds=c['snapshot_max_age_seconds']),
                     datetime.combine(now.date(), time.fromisoformat(c['last_entry_time']), TZ)]
        deadlines.extend(stamp(x['quote_at']) + timedelta(seconds=120) for x in indices)
        deadline = min(deadlines)
        plan = {'reference_entry': round(reference, 2), 'entry_ceiling': floor_price(min(reference*1.003, number(q['vwap'])*1.025)),
                'stop_reference': round(reference*(1-c['stop_loss_pct']), 2),
                'target_reference': round(reference*(1+c['take_profit_pct']), 2),
                'valid_until': deadline.isoformat(), 'earliest_sell_session': calendar['next_session'],
                'time_exit': f"{calendar['next_session']} {c['next_day_exit_time']} +08:00",
                'rule': '参考价非成交价；成交后重算止盈止损。仅下一交易日起可卖，低开穿越止损按首个可成交价格退出；跌停可能无法成交。'}
    return {'code': row['code'], 'name': row.get('name'), 'price': row.get('price'),
            'quote_at': q.get('quote_at'), 'change_pct': row.get('change_pct'), 'industry': industry,
            'score': min(100, score), 'status': 'conditional' if not gaps else 'watch_only',
            'blockers': gaps, 'enhanced_filter_blockers':advanced['gaps'], 'metrics': tech['metrics'], 'signals': tech['reasons'],
            'financials': finance, 'notice_risks': risks, 'catalysts': catalysts, 'plan': plan,
            'source_urls': bundle.get('source_urls', [])}
