"""Independent, source-attributed Connors RSI(2) research pipeline.

Only operational data checks and the source strategy are applied. The legacy
tail scanner's ranking, momentum, liquidity and industry gates are not used.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, time as clock_time
import hashlib
import json
from pathlib import Path
import time

from .engine import TZ, is_a_share, number, stamp
from .history_collection import collect_histories
from .paths import data_root
from .providers import PublicSources, calendar_context, parallel, parse_quote, safe_source_error
from .research import closed_quote_matches, research_context, resolve_analysis
from .runtime import current_runtime_info


SKILL_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_FORMAT = 'tail-screener-author-snapshot-v1'
OPERATIONAL_KEYS = ('top_n', 'workers', 'budget_seconds', 'quote_max_age_seconds',
                    'snapshot_max_age_seconds', 'min_universe_coverage')
LEGACY_SELECTION_POLICY = {
    'id': 'legacy_code_order_v3_0_0', 'name': 'v3.0.0代码顺序展示（无策略优先级）',
    'metric': 'code', 'direction': 'ascending',
    'interpretation': '仅为保留旧冻结运行的展示顺序，不属于收益或信号强弱排名。',
    'tie_policy': '按代码展示原配置指定数量；原版本没有信号排名或边界并列政策。',
}


def canonical_hash(value):
    # Preserve the original canonical byte representation without allocating
    # both a full JSON string and its UTF-8 copy for large market snapshots.
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False)
    for chunk in encoder.iterencode(value):
        digest.update(chunk.encode('utf-8'))
    return digest.hexdigest()


def code_version():
    digest = hashlib.sha256()
    for path in sorted((SKILL_ROOT / 'scripts').rglob('*.py')):
        digest.update(str(path.relative_to(SKILL_ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _eligible_quote(row):
    if not isinstance(row, dict) or not is_a_share(row.get('code')):
        return False, '不属于沪深A股范围'
    name = str(row.get('name') or '').strip().upper()
    if not name or 'ST' in name or '退' in name or name.startswith(('N', 'C')):
        return False, '名称缺失或属于ST、退市、新股特殊交易阶段'
    price, previous = number(row.get('price')), number(row.get('prev_close'))
    if price is None or previous is None or price <= 0 or previous <= 0:
        return False, '报价或昨收缺失、非有限值或非正数'
    if not row.get('source'):
        return False, '报价来源缺失'
    return True, None


def _quote_gaps(row, now, analysis, config):
    at = stamp(row.get('quote_at'))
    gaps = []
    if at is None or len(str(row.get('quote_at'))) <= 10 or at > now:
        return ['报价缺少有效完整时间，或时间晚于冻结时点']
    if at.date().isoformat() != analysis.get('data_session'):
        gaps.append('报价交易日与本次分析交易日不一致')
    freshness_clock = now
    calendar = analysis.get('calendar_data_session') or {}
    if analysis['mode'] == 'live' and _session_phase(now, calendar) == 'midday_break':
        freshness_clock = now.replace(hour=11, minute=30, second=0, microsecond=0)
    if analysis['mode'] == 'live' and (freshness_clock - at).total_seconds() > config['quote_max_age_seconds']:
        gaps.append('盘中报价已超过有效时限，不能当作当前预备信号')
    if analysis['mode'] == 'post_close' and not closed_quote_matches(row, now, analysis.get('data_session')):
        gaps.append('尚未核验本次交易日的最终收盘报价')
    return gaps


def _session_phase(now, calendar):
    clock = now.time().replace(tzinfo=None)
    if not calendar.get('verified') or not calendar.get('is_session') or calendar.get('session') != now.date().isoformat():
        return 'non_session_or_unverified'
    if clock < clock_time(9, 15):
        return 'pre_market_closed'
    if clock < clock_time(9, 30):
        return 'pre_open'
    if clock_time(11, 30) <= clock < clock_time(13):
        return 'midday_break'
    if clock >= clock_time(15):
        return 'post_close'
    return 'continuous_or_closing_auction'


def _resolve_author_analysis(now, mode, calendar):
    requested = mode.replace('-', '_')
    # At the opening auction the source may already return today's quote.
    # Do not relabel it as yesterday's close merely to avoid missing data.
    analysis = resolve_analysis(now, requested, calendar=calendar)
    analysis['session_phase'] = _session_phase(now, calendar)
    return analysis


def _operational_config(config):
    result = {key: config[key] for key in OPERATIONAL_KEYS}
    for key in ('top_n', 'workers'):
        if isinstance(result[key], bool) or not isinstance(result[key], int):
            raise ValueError(f'{key} 必须为整数')
    if not 1 <= result['top_n'] <= 20 or not 1 <= result['workers'] <= 16:
        raise ValueError('作者策略展示数量需1—20，并发数需1—16')
    for key in ('budget_seconds', 'quote_max_age_seconds', 'snapshot_max_age_seconds', 'min_universe_coverage'):
        if number(result[key]) is None:
            raise ValueError(f'{key} 必须为有限数字')
    if not 1 <= result['budget_seconds'] <= 1200:
        raise ValueError('采集预算需1—1200秒')
    if not 1 <= result['quote_max_age_seconds'] <= 120 or not 1 <= result['snapshot_max_age_seconds'] <= 300:
        raise ValueError('报价/快照有效期超过支持范围')
    if not .95 <= result['min_universe_coverage'] <= 1:
        raise ValueError('目录覆盖要求需介于95%与100%')
    return result


def _refresh_quotes(source, rows, operational, analysis, deadline):
    """Refresh existing identities in Tencent's normal 70-symbol quote batches.

    The original directory and its denominator stay frozen. Neither fetching a
    response now nor a successful HTTP status can renew an old quote's time.
    """
    originals = {row['code']: row for row in rows}
    codes = sorted(originals)
    batches = [tuple(codes[i:i + 70]) for i in range(0, len(codes), 70)]
    attempted, responses, errors = [], {}, {}
    started = datetime.now(TZ).isoformat()

    def fetch(batch):
        symbols = [code[-2:].lower() + code[:6] for code in batch]
        payload = source.get('https://qt.gtimg.cn/q=' + ','.join(symbols), text=True)
        quotes, failures = {}, {}
        for code, symbol in zip(batch, symbols):
            try:
                quotes[code] = parse_quote(payload, symbol)
            except Exception as exc:
                failures[code] = safe_source_error(exc)
        return quotes, failures

    # Limit queued work to one worker wave: no thousands of individual quote
    # calls and no new directory request consumes the reserved refresh budget.
    for offset in range(0, len(batches), operational['workers']):
        if time.monotonic() >= deadline:
            break
        wave = batches[offset:offset + operational['workers']]
        attempted.extend(code for batch in wave for code in batch)
        values, failures = parallel(wave, fetch, operational['workers'])
        for batch in wave:
            if batch in values:
                quotes, bad = values[batch]
                responses.update(quotes)
                errors.update(bad)
            else:
                for code in batch:
                    errors[code] = failures.get(str(batch), '刷新请求未完成')
    finished = datetime.now(TZ)
    accepted = {}
    for code, quote in responses.items():
        valid, reason = _eligible_quote(quote)
        gaps = _quote_gaps(quote, finished, analysis, operational)
        original_at, refreshed_at = stamp(originals[code].get('quote_at')), stamp(quote.get('quote_at'))
        if quote.get('code') != code:
            gaps.append('刷新报价标的身份不一致')
        if original_at is not None and refreshed_at is not None and refreshed_at < original_at:
            gaps.append('刷新报价时间早于原始行情')
        if not valid:
            gaps.append(reason)
        if gaps:
            errors[code] = '；'.join(dict.fromkeys(gaps))
        else:
            accepted[code] = quote
    attempted_set = set(attempted)
    return accepted, {'started_at': started, 'finished_at': finished.isoformat(),
                      'source': 'tencent:qt', 'batch_size': 70, 'directory_refetched': False,
                      'requested_codes': attempted, 'responses': responses,
                      'refreshed_codes': sorted(accepted),
                      'not_attempted_codes': [code for code in codes if code not in attempted_set],
                      'errors': errors}


def collect_author(config, mode='auto'):
    """Attempt every eligible symbol in stable order within the actual budget."""
    from .author_rules import CONNORS_SOURCE, CONNORS_STRATEGY_ID
    from .author_selection import ALVAREZ_RSI2_POLICY
    operational = _operational_config(config)
    started = time.monotonic()
    started_at = datetime.now(TZ)
    calendar = calendar_context(started_at)
    analysis = _resolve_author_analysis(started_at, mode, calendar)
    if analysis['mode'] == 'live':
        analysis['data_session'] = started_at.date().isoformat() if calendar.get('verified') and calendar.get('is_session') else None
        analysis['calendar_data_session'] = calendar
    source = PublicSources(data_root(SKILL_ROOT) / '.cache', operational['budget_seconds'], operational['workers'])
    warnings, histories, history_errors = [], {}, {}
    try:
        print('CONNORS RSI(2) · ETF公式迁移实验 · 正在采集沪深A股实际行情', flush=True)
        try:
            universe, meta = source.universe()
        except Exception as exc:
            universe, meta = [], {'coverage': 0, 'valid_quote_coverage': 0,
                                  'fetched_at': datetime.now(TZ).isoformat(),
                                  'errors': {'universe': safe_source_error(exc)}}
            warnings.append('股票目录或全市场行情采集失败，本次没有完成市场扫描。')
        catalog_elapsed = round(time.monotonic() - started, 3)
        duplicate_codes = {code for code, count in Counter(row.get('code') for row in universe
                                                           if isinstance(row, dict)).items() if count > 1}
        eligible, excluded = [], []
        for row in universe:
            valid, reason = _eligible_quote(row)
            if valid and row['code'] in duplicate_codes:
                valid, reason = False, '同一股票出现重复报价，无法确定唯一输入'
            if valid:
                eligible.append(row)
            else:
                excluded.append({'code': row.get('code') if isinstance(row, dict) else None, 'reason': reason})
        eligible.sort(key=lambda row: row['code'])
        print(f'有效报价 {len(universe)} 只，策略范围 {len(eligible)} 只；逐批取得300日历史，不使用旧版初筛。', flush=True)
        deadline = min(started + operational['budget_seconds'], getattr(source, 'deadline', float('inf')))
        live = analysis['mode'] == 'live'
        refresh_reserve = min(60., max(0., deadline - time.monotonic()) * .25) if live else 0.
        history_deadline = deadline - refresh_reserve
        # Network calls themselves honor the earlier deadline, so a final
        # historical request wave cannot consume all reserved quote time.
        source.deadline = history_deadline
        try:
            collection = collect_histories(source, [row['code'] for row in eligible], count=300,
                                           workers=operational['workers'], deadline=history_deadline)
            histories, history_errors = collection['histories'], collection['errors']
        finally:
            source.deadline = deadline
        history_elapsed = round(time.monotonic() - started - catalog_elapsed, 3)
        initial_quotes = {row['code']: row for row in eligible}
        quote_refresh = None
        if live and histories:
            print(f'刷新 {len(histories)} 只已取得历史的盘中报价（70只/批），保留源时间…', flush=True)
            refreshed, quote_refresh = _refresh_quotes(source, [row for row in eligible if row['code'] in histories],
                                                       operational, analysis, deadline)
            eligible = [refreshed.get(row['code'], row) for row in eligible]
            quote_refresh['reserved_seconds'] = round(refresh_reserve, 3)
            if quote_refresh['errors'] or quote_refresh['not_attempted_codes']:
                warnings.append('部分盘中报价未能刷新；继续按原始源报价时间核验，过期输入不会获得新的有效期。')
        now = datetime.now(TZ)
        end_calendar = calendar_context(now)
        analysis['session_phase'] = _session_phase(now, end_calendar)
        analysis['freshness_note'] = ('午休时以当日11:30作为有效交易时间截止点核验报价；'
                                     '保留实际源时间和冻结时间，13:00起恢复按实际时间核验。'
                                     if analysis['mode'] == 'live' and analysis['session_phase'] == 'midday_break' else
                                     '盘中按实际冻结时点核验源报价时间；盘后核验指定交易日的已完成收盘。')
        if analysis['mode'] == 'live' and analysis['session_phase'] == 'pre_open':
            warnings.append('当前处于开盘前/集合竞价阶段，仅当日有效报价可用于预备观察；'
                            '旧报价不会充当当日行情，未取得有效报价时保留未评估状态，不生成执行资格。')
        quote_gaps = {row['code']: gaps for row in eligible
                      if (gaps := _quote_gaps(row, now, analysis, operational))}
        research = (dict(research_context(universe, now, config, analysis))
                    if analysis['mode'] == 'post_close' else None)
        if research is not None:
            research.pop('score_basis', None)
            research.pop('minute_scope', None)
            research['formula_basis'] = CONNORS_SOURCE['native_entry']
        context = {'calendar': end_calendar, 'analysis': analysis, 'research': research,
                   'universe_coverage': meta.get('coverage', 0),
                   'valid_quote_coverage': meta.get('valid_quote_coverage', 0),
                   'dated_quote_coverage': meta.get('dated_quote_coverage', 0),
                   'quote_data_gaps': quote_gaps}
        pending = [row['code'] for row in eligible if row['code'] not in histories]
        attempted = collection['attempted_codes']
        not_attempted = collection['not_attempted_codes']
        if pending:
            warnings.append(f'采集范围尚未完成：{len(pending)} 只未取得历史，其中 {len(not_attempted)} 只尚未提交；'
                            '本次匹配只代表实际核验部分，不是全市场前N名。')
        if history_errors:
            warnings.append(f'历史采集有实际请求或解析错误且仍未恢复 {len(history_errors)} 只；'
                            '本地熔断拦截、未提交与公式未触发分别记录。')
        if quote_gaps:
            warnings.append(f'{len(quote_gaps)} 只报价的交易日、收盘状态或时效未通过核验。')
        return {'format': SNAPSHOT_FORMAT, 'strategy_id': CONNORS_STRATEGY_ID,
                'strategy_version': '3.0.2', 'selection_policy': deepcopy(ALVAREZ_RSI2_POLICY),
                'as_of': now.isoformat(), 'started_at': started_at.isoformat(),
                'analysis_mode': analysis['mode'], 'data_session': analysis.get('data_session'),
                'screening_config': dict(config), 'config_hash': canonical_hash(config),
                'screening_code_version': code_version(), 'runtime': current_runtime_info(),
                'universe': universe, 'eligible_codes': [row['code'] for row in eligible],
                'excluded_quotes': excluded, 'context': context, 'universe_metadata': meta,
                'bundles': [{'quote': row, 'initial_quote': initial_quotes[row['code']],
                             'history': histories[row['code']]} for row in eligible if row['code'] in histories],
                'quote_refresh': quote_refresh,
                'history_collection': collection['diagnostics'],
                'history_unfinished_labels': collection['unfinished_labels'],
                'history_errors': history_errors, 'unassessed_codes': pending,
                'not_attempted_codes': not_attempted,
                'source_diagnostics': list(getattr(source, 'diagnostics', [])),
                'source_fallbacks': list(getattr(source, 'fallback_events', [])),
                'stats': {'universe': len(universe), 'eligible': len(eligible), 'history_checked': len(histories),
                          'history_failed': len(history_errors), 'history_attempted': len(attempted),
                          'history_deferred': collection['diagnostics'].get('deferred_count', 0),
                          'not_attempted': len(not_attempted), 'unassessed': len(pending),
                          'quotes_refreshed': len(quote_refresh['refreshed_codes']) if quote_refresh else 0,
                          'quote_refresh_failed': len(quote_refresh['errors']) if quote_refresh else 0,
                          'elapsed_seconds': round(time.monotonic() - started, 3)},
                'stage_seconds': {'universe': catalog_elapsed, 'history': history_elapsed,
                                  'quote_refresh': round(time.monotonic() - started - catalog_elapsed - history_elapsed, 3)},
                'warnings': warnings}
    finally:
        source.close()


def load_author_replay(path):
    from .author_rules import CONNORS_STRATEGY_ID
    path = Path(path)
    with path.open(encoding='utf-8') as handle:
        snapshot = json.load(handle)
    if snapshot.get('format') != SNAPSHOT_FORMAT or snapshot.get('strategy_id') != CONNORS_STRATEGY_ID:
        raise ValueError('该快照不是独立作者策略冻结输入；旧版筛选快照不能冒充原策略复现')
    original = path.parent / 'report.json'
    if not original.is_file():
        raise ValueError('作者策略回放需要同目录原 report.json，以校验冻结输入的运行哈希')
    with original.open(encoding='utf-8') as handle:
        report = json.load(handle)
    if report.get('strategy_id') != CONNORS_STRATEGY_ID or report.get('run_id') != canonical_hash(snapshot):
        raise ValueError('作者策略冻结输入与原报告身份或运行哈希不一致')
    config = snapshot.get('screening_config')
    if (not isinstance(config, dict) or snapshot.get('config_hash') != canonical_hash(config)
            or report.get('config_hash') != canonical_hash(config) or report.get('config') != config):
        raise ValueError('作者策略原参数缺失或配置哈希不一致')
    return snapshot, dict(config)


def _strategy_metadata(source, sample=None):
    source = source if isinstance(source, dict) else {'citation': source}
    return {'id': 'connors_rsi2_a_share_research', 'name': source.get('name', 'Connors RSI(2) 均值回归研究'),
            'author': source.get('author', 'Larry Connors'),
            'source_url': source.get('url') or source.get('source_url'), 'source': source,
            'native_market': source.get('native_market', '原始研究为美国 ETF；本实现迁移至沪深 A 股个股'),
            'native_entry': source.get('native_entry', '收盘价 > SMA200 且 RSI(2) < 5'),
            'native_exit': source.get('native_exit', '持有至收盘 RSI(2) > 70，不规定固定持有天数'),
            'holding_period': (sample or {}).get('holding_period', {'type': 'dynamic', 'label': '持有至收盘 RSI(2)>70，允许多日'}),
            'validation': {
                'status': 'experimental_market_transfer',
                'label': 'ETF公式迁移实验；A股有效性未验证',
                'recommendation_ready': False,
                'a_share_backtest_completed': False,
                'a_share_out_of_sample_validated': False,
                'a_share_execution_validated': False,
                'source_scope': '原文是ETF入退出测试示例，不是这套A股股票池、排序与成交方案的验证报告。',
                'decision': '保留可核查的公式研究入口；不作为已经验证的A股荐股策略。',
            },
            'adaptations': ['原ETF研究迁移到沪深个股，尚未验证本市场收益。',
                            '股票范围限定沪深A股，排除ST、退市以及名称带N/C的新股特殊交易阶段；这是本地范围限制。',
                            '盘中报价代入仅为预备观察，最终收盘后才能确认作者的收盘规则。',
                            'A股买入当日不能卖出；收盘后确认信号不能假定已经按同一收盘价成交。',
                            '只展示公式研究，不生成自动执行、固定止盈止损或次日强制退出计划。']}


def _select_matches(snapshot, matches, top_n, rerank):
    from .author_selection import ALVAREZ_RSI2_POLICY, rank_connors_matches
    frozen_policy = snapshot.get('selection_policy')
    if frozen_policy is None and snapshot.get('strategy_version') == '3.0.0':
        frozen_policy = LEGACY_SELECTION_POLICY
    if not isinstance(frozen_policy, dict) or frozen_policy.get('id') not in (
            LEGACY_SELECTION_POLICY['id'], ALVAREZ_RSI2_POLICY['id']):
        raise ValueError('冻结快照的排序政策缺失或未知，不能静默替换排序规则')
    source_policy = deepcopy(frozen_policy)
    if rerank or source_policy['id'] == ALVAREZ_RSI2_POLICY['id']:
        selection = rank_connors_matches(matches, top_n)
        if not rerank:
            # Keep the recorded attribution/policy, even when current code is
            # used to recompute it. Code-version changes are disclosed below.
            selection['policy'] = deepcopy(source_policy)
        return selection, source_policy
    ordered = sorted(matches, key=lambda row: row['code'])
    for index, row in enumerate(ordered):
        row.update(selection_rank=None, ranking_value=None, selected=index < top_n,
                   selection_reason='保留v3.0.0冻结运行的代码展示顺序；此顺序不表示策略优先级。')
    selected = ordered[:top_n]
    return {'policy': deepcopy(source_policy), 'ranked_matches': ordered, 'selected': selected,
            'summary': {'input_count': len(matches), 'request_count': top_n, 'qualified_count': len(ordered),
                        'excluded_count': 0, 'selected_count': len(selected), 'cutoff_value': None,
                        'boundary_tie_count': 0, 'tie_extra_count': 0, 'has_boundary_ties': False,
                        'excluded_reasons': {}}}, source_policy


def finish_author(snapshot, config, output=None, replay=False, rerank=False):
    from .author_rules import CONNORS_SOURCE, CONNORS_STRATEGY_ID, evaluate_connors
    from .author_report import write_author_report
    if snapshot.get('strategy_id') != CONNORS_STRATEGY_ID or snapshot.get('format') != SNAPSHOT_FORMAT:
        raise ValueError('不是独立作者策略冻结输入')
    if rerank and not replay:
        raise ValueError('排序对照仅适用于冻结快照离线回放')
    operational = _operational_config(config)
    if snapshot.get('screening_config') != config or snapshot.get('config_hash') != canonical_hash(config):
        raise ValueError('冻结输入与本次作者策略参数不一致')
    frozen_at = stamp(snapshot.get('as_of'))
    if frozen_at is None:
        raise ValueError('作者策略冻结输入缺少有效完整时间')
    context = snapshot['context']
    analysis = context['analysis']
    calendar = analysis.get('calendar_data_session') or context.get('calendar') or {}
    post_close = snapshot['analysis_mode'] == 'post_close'
    confirmed = bool(post_close and (context.get('research') or {}).get('verified'))
    evaluated = []
    for bundle in snapshot['bundles']:
        quote = bundle['quote']
        result = evaluate_connors(bundle['history'], quote, snapshot.get('data_session'), confirmed,
                                  previous_session=calendar.get('previous_session'))
        result = dict(result)
        gaps = list(dict.fromkeys(list(result.get('data_gaps', []))
                                  + context.get('quote_data_gaps', {}).get(quote['code'], [])))
        if not calendar.get('verified') or not calendar.get('is_session'):
            gaps.append('行情所属交易日历未核验')
        if post_close and not confirmed:
            gaps.extend((context.get('research') or {}).get('gaps') or ['收盘数据时点未完成核验'])
        if gaps:
            result.update(data_gaps=list(dict.fromkeys(gaps)), raw_entry_match=None, confirmation_status='incomplete')
            result['exit_trigger'] = dict(result.get('exit_trigger') or {}, triggered=None, confirmation_status='incomplete')
        if not post_close and result.get('confirmation_status') == 'confirmed_close':
            result['confirmation_status'] = 'provisional'
        metrics = result.get('metrics') or {}
        result.update(code=quote['code'], name=quote.get('name'), price=quote.get('price'), quote_at=quote.get('quote_at'),
                      rsi2=metrics.get('rsi2'), sma200=metrics.get('sma200'), confirmed_close=result.get('confirmation_status') == 'confirmed_close',
                      entry_eligible=False, status='watch_only', plan=None,
                      execution_note='公式研究不等于可执行入场；后续交易时机须重新核验，未生成执行计划。')
        evaluated.append(result)
    evaluated.sort(key=lambda row: row['code'])
    matches = [row for row in evaluated if row.get('raw_entry_match') is True and not row.get('data_gaps')]
    selection, source_policy = _select_matches(snapshot, matches, operational['top_n'], rerank)
    ranked_matches, candidates = selection['ranked_matches'], selection['selected']
    ranked_by_code = {row['code']: row for row in ranked_matches}
    evaluated = [ranked_by_code.get(row['code'], row) for row in evaluated]
    unassessed = sorted(set(snapshot.get('unassessed_codes', []))
                        | {row['code'] for row in evaluated if row.get('raw_entry_match') is None})
    meta = snapshot.get('universe_metadata') or {}
    issues = []
    if meta.get('catalog_complete') is False:
        issues.append('股票目录明确标记为未完成，覆盖百分比不能替代完整性核验')
    if number(meta.get('coverage')) is None or meta['coverage'] < operational['min_universe_coverage']:
        issues.append('股票目录覆盖未达到核验要求')
    if number(meta.get('valid_quote_coverage')) is None or meta['valid_quote_coverage'] < operational['min_universe_coverage']:
        issues.append('有效行情覆盖未达到核验要求')
    if not context.get('calendar', {}).get('verified'):
        issues.append('交易日历未通过核验')
    if unassessed:
        issues.append(f'{len(unassessed)} 只未取得可完成原规则判断的输入')
    if post_close and not confirmed:
        issues.append('目标交易日收盘时点未通过核验')
    if not post_close and context.get('quote_data_gaps'):
        issues.append('部分盘中行情时间未通过核验')
    if selection['summary']['excluded_count']:
        issues.append(f"{selection['summary']['excluded_count']} 只原触发记录未通过排序输入核验，未用代码顺序补位")
    stats = dict(snapshot['stats'], unassessed=len(unassessed), raw_matches=len(matches),
                 confirmed_matches=sum(row['confirmation_status'] == 'confirmed_close' for row in matches),
                 provisional_matches=sum(row['confirmation_status'] == 'provisional' for row in matches),
                 displayed=len(candidates), conditional_count=0,
                 formula_evaluated=sum(row.get('raw_entry_match') is not None and not row.get('data_gaps')
                                       for row in evaluated),
                 evaluated=len(evaluated), data_incomplete=sum(bool(row.get('data_gaps')) for row in evaluated))
    failed = stats['universe'] == 0
    incomplete = failed or bool(issues)
    status = {'code': 'collection_failed' if failed else 'incomplete' if incomplete else 'completed',
              'label': '采集未完成' if failed else '部分范围研究' if incomplete else '公式计算已完成（策略待验证）',
              'data_status': 'failed' if failed else 'incomplete' if incomplete else 'complete',
              'timing_status': 'replay' if replay else 'post_close' if post_close else 'intraday_provisional',
              'reasons': issues,
              'summary': ('仅代表已取得完整证据的部分股票，不能称为全市场前N名。' if incomplete else
                          '已完成本次可核验范围的原规则计算；公式触发不等于执行条件已经核验。')}
    run_id = canonical_hash(snapshot)
    config_hash = canonical_hash(config)
    for row in evaluated:
        row['signal_id'] = hashlib.sha256(f'{run_id}:{row["code"]}:{config_hash}'.encode()).hexdigest()
    current_code = code_version()
    warnings = list(snapshot.get('warnings', []))
    warnings.append('原ETF策略迁移至沪深个股尚未获得本市场样本外收益验证。')
    if selection['policy']['id'] == LEGACY_SELECTION_POLICY['id']:
        warnings.append('保留旧冻结快照的代码顺序展示政策；它没有信号优先级。只有显式--rerank才做当前排序对照。')
    else:
        warnings.append('按Alvarez公开原则以未经取整RSI(2)由低到高排列已核验触发信号；'
                        '边界精确并列全部展示。优先级不表示预期收益或全市场最佳股票。')
    if selection['summary']['excluded_count']:
        warnings.append('部分触发记录缺少有效排序依据，已记录排序排除原因，未按代码补足展示数量。')
    if rerank:
        warnings.append('本次为显式离线排序对照：保留原冻结输入与原参数，应用当前排序政策，不冒充原展示结果。')
    if not post_close:
        warnings.append('本次按盘中预备模式计算，未完成收盘确认流程；即使采集跨过收盘，仍须另行核验最终收盘结果。')
    if replay:
        warnings.append('本次只复核冻结输入，未采集当前行情；不生成当前交易资格。')
        if snapshot.get('screening_code_version') != current_code:
            warnings.append('回放代码与原运行不同；代码哈希不是源码归档，不能视为原实现完全重现。')
    report = {'version': 3, 'strategy_version': '3.0.2', 'strategy_id': CONNORS_STRATEGY_ID,
              'strategy': _strategy_metadata(CONNORS_SOURCE, evaluated[0] if evaluated else None),
              'source': CONNORS_SOURCE, 'run_id': run_id, 'config_hash': config_hash, 'code_version': current_code,
              'as_of': snapshot['as_of'], 'generated_at': datetime.now(TZ).isoformat(),
              'mode': '离线排序对照' if rerank else '离线回放' if replay else '盘后公式迁移实验' if post_close else '盘中预备观察',
              'analysis_mode': snapshot['analysis_mode'], 'data_session': snapshot.get('data_session'),
              'confirmed_close': confirmed, 'config': config, 'operational_config': operational,
              'legacy_parameters_applied': False, 'context': context, 'runtime': snapshot.get('runtime'),
              'stats': stats, 'run_status': status, 'candidates': candidates, 'picks': candidates,
              'selection': {'policy': selection['policy'], 'summary': selection['summary']},
              'ranked_matches': ranked_matches,
              'all_evaluated': evaluated, 'unassessed_codes': unassessed,
              'universe_metadata': meta, 'source_diagnostics': snapshot.get('source_diagnostics', []),
              'source_fallbacks': snapshot.get('source_fallbacks', []),
              'quote_refresh': snapshot.get('quote_refresh'),
              'history_collection': snapshot.get('history_collection'),
              'history_unfinished_labels': snapshot.get('history_unfinished_labels', {}),
              'history_errors': snapshot.get('history_errors', {}), 'warnings': warnings,
              'replay_provenance': {'source_run_id': run_id, 'source_config_hash': config_hash,
                                    'original_config_used': True,
                                    'source_strategy_version': snapshot.get('strategy_version'),
                                    'source_selection_policy': source_policy,
                                    'selection_policy': selection['policy'],
                                    'execution_selection_policy': selection['policy'],
                                    'selection_changed': source_policy != selection['policy'],
                                    'rerank_requested': bool(rerank),
                                    'source_code_version': snapshot.get('screening_code_version'),
                                    'same_code_version': snapshot.get('screening_code_version') == current_code} if replay else None}
    target_dir = Path(output) if output else data_root(SKILL_ROOT) / 'reports' / ('author_' + datetime.now(TZ).strftime('%Y%m%d_%H%M%S_%f'))
    target_dir.mkdir(parents=True, exist_ok=True)
    if (target_dir / 'report.json').exists() or (target_dir / 'snapshot.json').exists():
        raise ValueError('报告目录已有冻结记录；请使用新目录，不能覆盖历史输入')
    with (target_dir / 'snapshot.json').open('w', encoding='utf-8') as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, allow_nan=False)
    return report, write_author_report(report, target_dir)


def run_author_scan(config, output=None, mode='auto', replay_path=None, rerank=False):
    if rerank and replay_path is None:
        raise ValueError('--rerank 需要作者策略 --replay 冻结快照')
    if replay_path is not None:
        snapshot, original_config = load_author_replay(replay_path)
        return finish_author(snapshot, original_config, output, replay=True, rerank=rerank)
    snapshot = collect_author(config, mode)
    return finish_author(snapshot, config, output)
