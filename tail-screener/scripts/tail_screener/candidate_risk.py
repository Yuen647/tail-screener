"""Current SH/SZ A-share data and name-risk checks, without an alpha filter.

The quote contract is ``providers.parse_quote``; the daily contract is a
``DailyStore.cross_section`` row with its *actual* session supplied separately.
Neither a provider name nor a matching price proves audited issuer health.
Missing historical ST, listing or suspension metadata is never invented.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, time
from math import isfinite
import re
import unicodedata
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')
_SCOPE = re.compile(r'^(SH(?:60\d{4}|688\d{3})|SZ(?:00\d{4}|30\d{4}))$')
# These tolerances cover the archive float32 round trip and Tencent's public
# display precision. They are data reconciliation tolerances, not alpha knobs.
PRICE_TOLERANCE_YUAN = 0.011
VOLUME_TOLERANCE_SHARES = 100.0
VOLUME_RELATIVE_TOLERANCE = 2e-7
AMOUNT_TOLERANCE_YUAN = 10_000.0
KNOWN_DAILY_SOURCE = 'investment_data:qlib_bin'


def canonical_candidate_code(value):
    code = str(value).strip().upper()
    if re.fullmatch(r'\d{6}\.(SH|SZ)', code):
        code = code[-2:] + code[:6]
    if not _SCOPE.fullmatch(code):
        raise ValueError('仅支持沪深A股，排除ETF、B股、北交所及境外证券')
    return code


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if isfinite(value) else None


def _session(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return None


def _timestamp(value):
    # An undated quote or a date-only string is not an observed quote time.
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) > 10:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(TZ)


def instrument_eligibility(quote, *, asof=None, expected_session=None):
    """Screen the *current quote* independently of a prior closed daily bar.

    Use this when the strategy uses yesterday's completed bar while a quote
    belongs to today's open session. Do not compare those two dates' prices.
    ``expected_session`` is supplied by the caller's actual trading calendar;
    without it this function makes no freshness assertion. An active quote
    only proves observed trading, not official suspension or executable orders.
    """
    quote = {} if quote is None else dict(quote)
    issues = []

    def issue(code, message, category='data'):
        issues.append({'code': code, 'message': message, 'category': category})

    code = None
    try:
        code = canonical_candidate_code(quote.get('code'))
    except ValueError:
        issue('outside_scope', '报价证券不属于沪深A股', 'scope')
    name = str(quote.get('name') or '').strip()
    normalized_name = unicodedata.normalize('NFKC', name).upper().replace(' ', '')
    markers = []
    if not name:
        issue('name_missing', '当前名称缺失，无法核验当前风险简称')
    else:
        if 'ST' in normalized_name:
            markers.append('ST')
        if '退' in name:
            markers.append('delisting_name')
        if normalized_name.startswith(('N', 'C')):
            markers.append('new_listing_name')
        if markers:
            issue('name_risk', '当前简称存在ST、退市或N/C新股特殊阶段标记', 'scope')
    quote_at = _timestamp(quote.get('quote_at'))
    reference_at = _timestamp(asof) if asof is not None else None
    if quote_at is None:
        issue('quote_timestamp_invalid', '报价缺少有效且含时区的时间')
    if asof is not None and reference_at is None:
        issue('asof_invalid', '核验时点必须包含有效时区')
    if quote_at is not None and reference_at is not None and quote_at > reference_at:
        issue('future_quote', '报价时刻晚于核验时点')
    wanted_day = _session(expected_session) if expected_session is not None else None
    if expected_session is not None and (wanted_day is None or quote_at is None or quote_at.date() != wanted_day):
        issue('quote_session_mismatch', '报价不属于交易日历要求的最近行情交易日')
    if quote.get('source') not in ('tencent:qt', 'sina:hq'):
        issue('quote_source_unverified', '当前报价供应管道未识别')
    values = {k: _number(quote.get(k)) for k in ('price', 'volume', 'amount')}
    observed_activity = all(v is not None and v > 0 for v in values.values())
    if not observed_activity:
        inactive = any(values[k] == 0 for k in ('volume', 'amount'))
        issue('no_quote_trading' if inactive else 'quote_fields_invalid',
              '报价无有效成交，停牌原因尚未独立确认' if inactive else '报价量价缺失或无效')
    gaps = ['官方停复牌状态、财务与公告正文风险未核验',
            '当前名称标记不能回填历史ST状态，也不能证明完整上市日期',
            '有效成交报价不保证下一时点可买入或卖出']
    if expected_session is None:
        gaps.append('未传入交易日历要求的行情日期；未声称报价为最新交易日')
    if asof is None:
        gaps.append('未传入核验时点；未检查报价是否晚于观察时间')
    return {'code': code, 'name': name, 'quote_at': quote.get('quote_at'),
            'expected_session': expected_session,
            'scope_eligible': not any(i['category'] == 'scope' for i in issues),
            'data_verified': not any(i['category'] == 'data' for i in issues),
            'eligible': not any(i['category'] in ('scope', 'data') for i in issues),
            'observed_activity': observed_activity,
            'name_markers': markers, 'buy_eligibility': 'not_evaluated',
            'issues': issues, 'unverified_risks': gaps}


def validate_candidate(quote, daily, *, session, daily_session=None, asof=None,
                       require_closed_session=True,
                       daily_source=KNOWN_DAILY_SOURCE):
    """Reconcile one current candidate using actual, same-session evidence.

    ``quote`` uses code, name, quote_at, source, price/open/high/low, volume
    (shares), amount (RMB), and limit_up/limit_down from ``Providers.quote``.
    ``daily`` uses raw_open/high/low/close, raw_volume_shares and
    raw_amount_yuan from ``DailyStore``. ``daily_session`` must be the date
    passed to that store; missing dates fail verification.

    ``eligible`` means eligible for strategy ranking under these checks, not
    verified buy eligibility. Negative financial/announcement risks, actual
    exchange status and future order execution require separate evidence.
    Current names only screen current risk designations; do not reuse these
    names to backfill historical ST status.
    """
    quote = {} if quote is None else dict(quote)
    daily_series_name = getattr(daily, 'name', None)
    daily = {} if daily is None else dict(daily)
    issues, checks = [], []

    def issue(code, message, category='data'):
        issues.append({'code': code, 'category': category, 'message': message})

    def check(code, passed, **evidence):
        checks.append({'code': code, 'passed': bool(passed), **evidence})

    code = None
    try:
        code = canonical_candidate_code(quote.get('code'))
    except ValueError:
        issue('outside_scope', '报价证券不属于沪深A股', 'scope')
    supplied_daily_code = daily.get('code')
    if supplied_daily_code is None:
        # A Series index can be SH600000; a plain mapping has no inferred ID.
        series_name = daily_series_name
        if isinstance(series_name, str) and _SCOPE.fullmatch(series_name.upper()):
            supplied_daily_code = series_name
    if supplied_daily_code is None:
        issue('daily_identity_missing', '日线证券身份缺失，不能仅凭数值匹配报价')
    if supplied_daily_code is not None:
        try:
            daily_code = canonical_candidate_code(supplied_daily_code)
        except ValueError:
            daily_code = None
        check('same_security', daily_code == code and code is not None,
              quote_code=code, daily_code=daily_code)
        if code is None or daily_code != code:
            issue('security_mismatch', '日线与报价证券身份不一致')

    name = str(quote.get('name') or '').strip()
    upper_name = unicodedata.normalize('NFKC', name).upper().replace(' ', '')
    if not name:
        issue('name_missing', '名称缺失，无法核验当前风险简称')
    else:
        markers = []
        if 'ST' in upper_name:
            markers.append('ST')
        if '退' in name:
            markers.append('delisting_name')
        if upper_name.startswith(('N', 'C')):
            markers.append('new_listing_name')
        check('current_name_risk', not markers, name=name, markers=markers,
              source=quote.get('source'), observed_at=quote.get('quote_at'))
        if markers:
            issue('name_risk', '当前简称存在ST、退市或N/C新股特殊阶段标记', 'scope')

    wanted_day, history_day = _session(session), _session(daily_session)
    quote_at = _timestamp(quote.get('quote_at'))
    reference_at = _timestamp(asof) if asof is not None else None
    if wanted_day is None:
        issue('session_invalid', '目标交易日格式无效')
    if history_day is None or history_day != wanted_day:
        issue('daily_session_mismatch', '日线缺少实际日期或不属于目标交易日')
    if quote_at is None or quote_at.date() != wanted_day:
        issue('quote_session_mismatch', '报价时间缺失、不含时区或不属于目标交易日')
    if asof is not None and reference_at is None:
        issue('asof_invalid', '核验时点必须包含有效时区')
    if reference_at is not None:
        if quote_at is not None and quote_at > reference_at:
            issue('future_quote', '报价时刻晚于核验时点')
        if wanted_day is not None and wanted_day > reference_at.date():
            issue('future_session', '目标交易日晚于核验时点')
        if require_closed_session and wanted_day == reference_at.date() and reference_at.time() < time(15):
            issue('session_not_closed', '当日尚未收盘，不能确认完整日线')
    if require_closed_session and quote_at is not None and quote_at.time() < time(15):
        issue('quote_before_close', '该报价尚未反映收盘，不能与完整日线作收盘核验')
    check('same_session', wanted_day is not None and history_day == wanted_day
          and quote_at is not None and quote_at.date() == wanted_day,
          requested_session=session, daily_session=daily_session,
          quote_at=quote.get('quote_at'))

    q_values = {k: _number(quote.get(k)) for k in ('price', 'open', 'high', 'low', 'volume', 'amount')}
    d_values = {k: _number(daily.get(k)) for k in
                ('raw_close', 'raw_open', 'raw_high', 'raw_low', 'raw_volume_shares', 'raw_amount_yuan')}
    q_valid = all(v is not None and v > 0 for v in q_values.values())
    d_valid = all(v is not None and v > 0 for v in d_values.values())
    if not q_valid:
        inactive = any(q_values[k] == 0 for k in ('volume', 'amount'))
        issue('no_quote_trading' if inactive else 'quote_fields_invalid',
              '报价无有效成交，停牌原因尚未独立确认' if inactive else '报价OHLC、成交量或成交额缺失/无效')
    if not d_valid:
        issue('daily_fields_invalid', '日线原始OHLC、成交量或成交额缺失/无效；不得用归一化价格替代')
    for label, values, keys in (
        ('quote', q_values, ('open', 'high', 'low', 'price')),
        ('daily', d_values, ('raw_open', 'raw_high', 'raw_low', 'raw_close')),
    ):
        op, hi, lo, cl = (values[k] for k in keys)
        valid = all(v is not None and v > 0 for v in (op, hi, lo, cl))
        ordered = valid and lo <= min(op, cl) <= max(op, cl) <= hi
        check(label + '_ohlc', ordered)
        if valid and not ordered:
            issue(label + '_ohlc_invalid', '报价或日线OHLC顺序不成立')

    independent = daily_source == KNOWN_DAILY_SOURCE and quote.get('source') == 'tencent:qt'
    check('separate_provider_lineage', independent, daily_source=daily_source,
          quote_source=quote.get('source'),
          scope='社区日线当前上游Tushare与腾讯公开报价；不保证两者无共同交易所来源')
    if not independent:
        issue('source_lineage_unverified', '没有核验为不同供应管道，不能记作独立报价交叉验证')
    compared = []
    # No false numerical mismatches across dates or provisional intraday quotes.
    same_close = (wanted_day is not None and history_day == wanted_day
                  and quote_at is not None and quote_at.date() == wanted_day
                  and quote_at.time() >= time(15))
    if same_close and q_valid and d_valid:
        pairs = [('price', 'raw_close', PRICE_TOLERANCE_YUAN),
                 ('open', 'raw_open', PRICE_TOLERANCE_YUAN),
                 ('high', 'raw_high', PRICE_TOLERANCE_YUAN),
                 ('low', 'raw_low', PRICE_TOLERANCE_YUAN),
                 ('volume', 'raw_volume_shares', VOLUME_TOLERANCE_SHARES +
                  abs(q_values['volume']) * VOLUME_RELATIVE_TOLERANCE),
                 ('amount', 'raw_amount_yuan', AMOUNT_TOLERANCE_YUAN)]
        for q_key, d_key, tolerance in pairs:
            difference = abs(q_values[q_key] - d_values[d_key])
            matched = difference <= tolerance
            compared.append({'field': q_key, 'quote': q_values[q_key], 'daily': d_values[d_key],
                             'absolute_difference': difference, 'tolerance': tolerance, 'matched': matched})
            if not matched:
                issue('cross_source_' + q_key + '_mismatch', '同日两源原始' + q_key + '不一致')
    elif not require_closed_session:
        issue('intraday_cross_check_unavailable', '完整日线不能与盘中累计量价直接核验')
    check('cross_source_ohlcv', len(compared) == 6 and all(r['matched'] for r in compared),
          comparisons=compared)

    price = q_values['price']
    for field, flag, label in [('limit_up', 'at_limit_up', '报价处于涨停价，不能据此假设可买到'),
                              ('limit_down', 'at_limit_down', '报价处于跌停价，不能据此假设可卖出')]:
        limit = _number(quote.get(field))
        if price is not None and limit is not None and limit > 0 and abs(price - limit) < 0.005:
            issue(flag, label, 'execution')
    gaps = ['财务及公告正文风险未由本核验函数检查',
            '当前简称不能证明完整历史ST状态、上市日期或官方停复牌状态',
            '匹配收盘量价不能保证下一交易时点可成交']
    if asof is None:
        gaps.append('未提供核验时点；仅核验所属交易日，未验证相对当前时间的时效')
    return {
        'code': code, 'name': name, 'session': session,
        'scope_eligible': not any(r['category'] == 'scope' for r in issues),
        'data_verified': not any(r['category'] == 'data' for r in issues),
        'eligible': not any(r['category'] in ('scope', 'data') for r in issues),
        'buy_eligibility': 'not_evaluated',
        'issues': issues, 'checks': checks, 'unverified_risks': gaps,
    }


def validate_universe(quotes, daily_rows, *, session, daily_session, asof=None,
                      daily_source=KNOWN_DAILY_SOURCE):
    """Keep input coverage and exclusions separate from strategy signal counts.

    ``daily_rows`` is a mapping of canonical or exchange-suffixed codes to
    DailyStore row dictionaries. Duplicate quote identities are rejected, not
    silently replaced with whichever quote happened to be iterated last.
    """
    quotes = list(quotes)
    normalized = {}
    duplicate_daily = set()
    for code, row in daily_rows.items():
        key = canonical_candidate_code(code)
        if key in normalized:
            duplicate_daily.add(key)
        normalized[key] = dict(row, code=key)
    ids = []
    for quote in quotes:
        try:
            ids.append(canonical_candidate_code(quote.get('code')))
        except ValueError:
            ids.append(None)
    counts = Counter(c for c in ids if c is not None)
    results = []
    for quote, code in zip(quotes, ids):
        result = validate_candidate(quote, normalized.get(code), session=session,
                                    daily_session=daily_session, asof=asof, daily_source=daily_source)
        if counts[code] > 1 or code in duplicate_daily:
            result['issues'].append({'code': 'duplicate_security', 'category': 'data',
                                     'message': '同一证券有重复输入，未静默选择其中一条'})
            result['data_verified'] = result['eligible'] = False
        results.append(result)
    issue_counts = Counter(issue['code'] for row in results for issue in row['issues'])
    return {'results': results,
            'summary': {'input_quotes': len(quotes), 'unique_quote_codes': len(counts),
                        'daily_codes': len(normalized),
                        'eligible': sum(row['eligible'] for row in results),
                        'scope_excluded': sum(not row['scope_eligible'] for row in results),
                        'data_unverified': sum(not row['data_verified'] for row in results),
                        'issue_counts': dict(sorted(issue_counts.items()))}}
