"""Full Shanghai/Shenzhen daily trend screening using anonymous bulk history.

The technical shortlist is deliberately distinct from an order or an author's
complete trading system. Data status and economic validation are separate.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import time

from .bulk_data import ensure_bulk_dataset, BulkDataError
from .calendar import calendar_context
from .daily_store import DailyStore, canonical_code
from .engine import TZ
from .paths import data_root
from .providers import PublicSources, source_diagnostic
from .market_integrity import deduplicate_source_codes, source_identity

VERSION = '3.1.2'
STRATEGY_ID = 'minervini-trend'


def latest_complete_session(now, calendar):
    if not calendar.get('verified'):
        return None
    if calendar.get('is_session') and (now.hour, now.minute) >= (15, 0):
        return calendar['session']
    return calendar.get('previous_session')


def _save(path, value):
    with Path(path).open('x', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)


def _progress(message):
    print('tail-screener：' + message, flush=True)


def _strategy(skill_root):
    manifest = json.loads((skill_root / 'references/stock-trend-template-source.json').read_text())
    history_path = skill_root / 'references/trend-historical-check/summary.json'
    history = json.loads(history_path.read_text()) if history_path.is_file() else None
    return {
        'name': '米内尔维尼趋势模板 · 沪深A股免费数据适配',
        'source_url': 'https://www.marketsmith.hk/v2/blog/the-holy-grail-in-investing-mark-minervini-trend-mode-cn?lang=zh-hans',
        'source_manifest': manifest,
        'rule_descriptions': [
            '收盘价高于150日和200日均线', '150日均线高于200日均线',
            '200日均线高于120个交易日前的值（本地形式化）',
            '50日均线高于150日和200日均线', '收盘价高于50日均线',
            '收盘价至少比52周最低价高30%', '收盘价不低于52周最高价的75%',
            '沪深A股相对强度代理达到70（免费替代项）',
        ],
        'adaptations': [
            '相对强度使用沪深A股252个交易日复权收益率的横截面百分位，非欧奈尔专有RS评级。',
            '120日上升定义为均线端点比较；不声称120天每天都上涨。',
            '模板仅筛选趋势资格；基本面、公告、买点及退出属于后续独立核查。',
        ],
        'holding_period': '本模板未定义固定持有天数或完整退出规则；入选不等于立即买入，不预设隔夜卖出。',
        'validation': {'formula': '独立实现与测试', 'profitability': '2018—2020年固定规则观察未支持稳定短线优势；尚无通过执行约束的收益验证',
                       'scope': '个股技术候选筛选，不是完整SEPA交易系统'},
        'historical_observation': history,
    }


def _enrich(source, code):
    exchange_code = code[2:] + '.' + code[:2]
    result = {'review_gaps': []}
    for key in ('financials', 'notices'):
        try:
            result[key] = getattr(source, key)(exchange_code)
            if key == 'notices' and (not result[key].get('verified') or result[key].get('truncated')):
                result['review_gaps'].append('公告目录覆盖未完整核验，不能据此排除遗漏公告')
            if key == 'financials' and any(result[key].get(field) is None for field in ('net_profit', 'core_profit', 'revenue')):
                result['review_gaps'].append('已取得财报记录，但关键财务数值存在缺项')
        except Exception as exc:
            result.setdefault('errors', {})[key] = source_diagnostic(exc, key)
            result['review_gaps'].append(('财务披露' if key == 'financials' else '公告目录') + '本次未取得')
    result['review_gaps'].append('公告正文与机构资金身份未核验')
    return result


def _publish(output, report, snapshot):
    from .trend_report import render_report
    digest = hashlib.sha256()
    scripts = Path(__file__).resolve().parents[1]
    for path in sorted(scripts.rglob('*.py')):
        digest.update(str(path.relative_to(scripts)).encode('utf-8'))
        digest.update(path.read_bytes())
    report['code_sha256'] = snapshot['code_sha256'] = digest.hexdigest()
    _save(output / 'snapshot.json', snapshot)
    report['evidence'] = {'snapshot_sha256': hashlib.sha256((output / 'snapshot.json').read_bytes()).hexdigest(),
                          'snapshot_scope': '运行参数、来源版本、报价与已计算指标；原始日线由固定数据包哈希定位'}
    _save(output / 'report.json', report)
    with (output / 'index.html').open('x', encoding='utf-8') as handle:
        handle.write(render_report(report))
    print(json.dumps({'report': str((output / 'report.json').resolve()),
                      'html': str((output / 'index.html').resolve()),
                      'strategy_id': STRATEGY_ID, 'data_session': report.get('data_session'),
                      'stats': report['stats'], 'run_status': report['run_status']}, ensure_ascii=False))
    return report


def run(args, *, now=None):
    from .candidate_risk import instrument_eligibility, validate_candidate
    from .trend_template import scan_trend
    started = time.monotonic()
    now = now or datetime.now(TZ)
    skill_root = Path(__file__).resolve().parents[2]
    root = data_root(skill_root)
    output = Path(args.output) if args.output else root / 'reports' / now.strftime('trend_%Y%m%d_%H%M%S_%f')
    # An explicit destination is accepted only when it contains no prior report.
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ('report.json', 'snapshot.json', 'index.html')):
        raise ValueError('输出目录已有报告，请选择新目录；历史结果不会覆盖')
    calendar = calendar_context(now)
    requested = latest_complete_session(now, calendar)
    report = {'version': VERSION, 'strategy_id': STRATEGY_ID, 'generated_at': now.isoformat(),
              'data_session': None, 'requested_session': requested, 'analysis_mode': 'closed_daily',
              'strategy': _strategy(skill_root), 'calendar': calendar,
              'stats': {'universe': 0, 'history_complete': 0, 'matched': 0, 'displayed': 0,
                        'excluded': 0, 'incomplete': 0}, 'candidates': [], 'warnings': [],
              'selection': {'top_n': args.top, 'portfolio_positions': None,
                            'description': '合格技术候选按相对强度代理降序；边界精确并列全部展示。展示数量不代表持仓数。'}}
    from .recommendation_validation import load_recommendation_validation
    validation = load_recommendation_validation(skill_root, STRATEGY_ID)
    if validation is not None:
        report['recommendation_validation'] = validation
    from .accuracy_evidence import load_accuracy_evidence
    report['accuracy_evidence'] = load_accuracy_evidence(skill_root)
    if requested is None:
        report['warnings'].append('交易日历未覆盖当前日期，无法确认应使用的最近完整交易日。')
    try:
        data = ensure_bulk_dataset(args.data_cache or root / '.cache/bulk', target_session=requested,
                                   deadline=time.monotonic() + 600, progress=_progress)
    except BulkDataError as exc:
        report['run_status'] = {'data_status': 'failed', 'message': str(exc)}
        report['warnings'].append('历史数据未准备完成；本次未评估市场候选数量。')
        return _publish(output, report, {'strategy_id': STRATEGY_ID, 'generated_at': now.isoformat(), 'failure': str(exc)})
    store = DailyStore(data['directory'])
    session = data['metadata']['data_session']
    report.update(data_session=session, data_source=data['metadata'])
    if session > now.date().isoformat() or (session == now.date().isoformat() and (now.hour, now.minute) < (15, 0)):
        raise ValueError('数据包包含本次生成时点尚未收盘的日线，不能使用')
    if requested and session > requested:
        raise ValueError('数据包实际日期晚于已核验的最近完整交易日')
    if data['metadata']['stale']:
        report['warnings'].append(f'免费数据源尚未提供{requested or "应有交易日"}完整包，本报告仅分析实际已有的{session}。')
    _progress(f'日线交易日 {session}；采集沪深股票目录和带时间报价')
    source = PublicSources(root / '.cache/trend-public', budget=180, workers=4)
    quotes, catalog = [], {}
    try:
        try:
            quotes, catalog = source.universe(allow_preopen=True)
        except Exception as exc:
            catalog = {'catalog_complete': False, 'error': source_diagnostic(exc, 'universe')}
        by_code = {}
        for quote in quotes:
            try: supplied_code = canonical_code(quote['code'])
            except (ValueError, KeyError): continue
            identity = source_identity(supplied_code, session)
            code = identity['stable_id']
            # Prefer the actually effective display code when a provider sends
            # both aliases. They cannot create two universe/ranking positions.
            if code in by_code and supplied_code != identity['display_code']:
                continue
            by_code[code] = quote
        active = [item['stable_id'] for item in deduplicate_source_codes(store.codes(session), session)]
        exclusion, security_checks, cross_checks = {}, {}, {}
        cross, errors = store.cross_section(session)
        cross_unavailable = (set(active) - set(cross.index)) | (set(active) & set(errors))
        observed_at = datetime.now(TZ)
        expected_quote_session = (calendar.get('session') if calendar.get('is_session')
                                  and (observed_at.hour, observed_at.minute) >= (9, 30)
                                  else calendar.get('previous_session'))
        for code in active:
            quote = by_code.get(code)
            # Auction quotes may already carry today's date. Neither they nor
            # yesterday's completed bar demonstrate today's eventual execution.
            expected = expected_quote_session
            if (calendar.get('is_session') and (observed_at.hour, observed_at.minute) >= (9, 15)
                    and str((quote or {}).get('quote_at', ''))[:10] == calendar.get('session')):
                expected = calendar['session']
            check = instrument_eligibility(quote, asof=observed_at, expected_session=expected)
            security_checks[code] = check
            preopen = calendar.get('is_session') and (observed_at.hour, observed_at.minute) < (9, 30)
            blocking = [item for item in check['issues']
                        if not (preopen and item['code'] == 'no_quote_trading')]
            same_session = str((quote or {}).get('quote_at', ''))[:10] == session
            if blocking:
                exclusion[code] = blocking
            elif same_session and code in cross_unavailable:
                # A damaged amount/vwap file can remove this row while the
                # strategy's separate OHLCV reader still succeeds. Missing
                # evidence must not silently bypass same-day reconciliation.
                exclusion[code] = [{'code': 'daily_cross_section_unavailable', 'category': 'data',
                                    'message': '信号日原始量价截面缺失或读取失败，无法完成同日两源核验'}]
            elif same_session:
                cross_checks[code] = validate_candidate(quote, cross.loc[code], session=session,
                                                       daily_session=session, asof=observed_at)
                if not cross_checks[code]['data_verified']:
                    exclusion[code] = cross_checks[code]['issues']
        _progress(f'正在按固定模板核验 {len(active)} 只沪深A股；历史数据在本地批量计算')
        screening = scan_trend(store, session, excluded_codes=exclusion, top_n=args.top)
        report.update({key: screening[key] for key in ('all_evaluated', 'ranked_matches', 'candidates')})
        report['stats'].update(screening['stats'])
        report['stats'].update(history_complete=screening['stats']['formula_evaluated_count'],
                               matched=screening['stats']['matched_count'],
                               incomplete=screening['stats']['history_incomplete_count'],
                               excluded=screening['stats']['excluded_count'])
        report['methodology'] = screening.get('methodology', {})
        report['stats'].update(universe=len(active), displayed=len(report['candidates']),
                               quotes=len(by_code), quote_missing=len(set(active) - set(by_code)),
                               live_catalog_not_in_daily=len(set(by_code) - set(active)),
                               daily_cross_section_unavailable=len(cross_unavailable))
        # Names and source timestamps are evidence for current security status.
        for row in report['ranked_matches']:
            row['name'] = by_code.get(row['code'], {}).get('name', row['code'])
        for row in report['candidates']:
            code = row['code']; quote = by_code.get(code, {})
            row['name'] = quote.get('name', code)
            row['quote'] = quote
            if code in cross.index and code not in cross_unavailable:
                amount = float(cross.loc[code, 'raw_amount_yuan'])
                if math.isfinite(amount) and amount > 0:
                    row['metrics']['amount_yuan'] = amount
            row['instrument_check'] = security_checks[code]
            if code in cross_checks:
                row['risk'] = cross_checks[code]
            else:
                quote_session = str(quote.get('quote_at', ''))[:10]
                different_sessions = quote_session != session
                risk_issues = (['当日报价与历史收盘日不同，不进行跨日价格一致性比较']
                               if different_sessions else ['同日两源核验未完成'])
                if code in cross_unavailable:
                    risk_issues.append('信号日原始量价截面缺失或读取失败；仅保留已完成的技术公式，成交额与两源核验未完成')
                row['risk'] = {'eligible': security_checks[code]['eligible'], 'data_verified': False,
                               'issues': risk_issues, 'quote_session': quote_session,
                               'daily_session': session,
                               'cross_source_status': 'different_sessions' if different_sessions else 'unavailable',
                               'daily_cross_section_status': 'unavailable' if code in cross_unavailable else 'available'}
            row['holding_period'] = report['strategy']['holding_period']
            row['selection_reason'] = '通过全部模板条件；在完整历史与证券资格可核验的沪深A股范围按相对强度代理排名。'
        _progress(f'模板计算完成，正在补充前列 {len(report["candidates"])} 只的财务披露和公告目录')
        with ThreadPoolExecutor(max_workers=4) as pool:
            enriched = list(pool.map(lambda row: _enrich(source, row['code']), report['candidates']))
        cutoff = datetime.now(TZ).isoformat()
        for row, evidence in zip(report['candidates'], enriched):
            row.update(evidence)
            if row['code'] in cross_unavailable:
                row['review_gaps'].append('信号日原始量价截面缺失，成交额及两源一致性未核验；技术公式完成不代表这些资料齐全')
            row['review_flags'] = []
            financials = row.get('financials') or {}
            if isinstance(financials.get('net_profit'), (int, float)) and financials['net_profit'] < 0:
                row['review_flags'].append('最新已披露报告期归母净利润为负；技术入选不代表经营质量通过')
            if isinstance(financials.get('core_profit'), (int, float)) and financials['core_profit'] < 0:
                row['review_flags'].append('最新已披露报告期扣非归母净利润为负')
            if not row['instrument_check']['observed_activity']:
                row['review_flags'].append('处于盘前报价阶段，尚无当日成交记录；不能按指示价格认定可成交')
            notices = row.get('notices') or {}
            if notices.get('items'):
                notices['items'] = [item for item in notices['items']
                                    if item.get('published_at') and item['published_at'] <= cutoff]
                window_start = (datetime.now(TZ) - timedelta(days=14)).isoformat()
                notices['recent_items'] = [item for item in notices['items'] if item['published_at'] >= window_start]
                notices['window_start'] = window_start
                notices['window_end'] = cutoff
                for item in notices['recent_items']:
                    if any(word in item['title'] for word in ('减持', '立案', '处罚', '退市', '风险警示', '业绩预亏')):
                        row['review_flags'].append('需核对公告正文：' + item['title'])
            row['review_gaps'].append('模板未定义完整买点、持有期限及退出计划')
        report['diagnostics'] = {'catalog': catalog, 'source_requests': source.diagnostics,
                                  'cross_section_errors': errors}
    finally:
        source.close()
    data_exclusions = sum(any(item.get('category') == 'data' for item in reasons)
                          for reasons in exclusion.values())
    report['stats']['data_verification_excluded'] = data_exclusions
    report['stats']['same_session_cross_checks'] = len(cross_checks)
    incomplete = bool(data['metadata']['stale'] or not requested or not catalog.get('catalog_complete')
                      or report['stats']['quote_missing'] or report['stats']['live_catalog_not_in_daily']
                      or report['stats']['incomplete'] or data_exclusions or cross_unavailable)
    report['run_status'] = {'data_status': 'incomplete' if incomplete else 'complete',
                            'execution_status': 'completed',
                            'message': (f'生成{len(report["candidates"])}只技术候选；数据覆盖与后续交易核查见报告。'
                                        if report['candidates'] else '本次未取得可展示的合格候选；须结合数据覆盖区分未核验与不满足条件。')}
    report['warnings'].append('相对强度排名不代表胜率或预期收益；五只名单没有独立组合收益验证。')
    if report['candidates']:
        from .selection_ledger import freeze_selection, evaluate_ledger
        try:
            signal_calendar = calendar_context(datetime.fromisoformat(session + 'T16:00:00+08:00'))
            next_signal_session = signal_calendar.get('next_session') if signal_calendar.get('verified') else None
            ledger = root / '.cache/selection-ledger'
            frozen = freeze_selection(report, ledger, next_session=next_signal_session)
            _save(output / 'selection-record.json', json.loads(Path(frozen['path']).read_text()))
            observations = evaluate_ledger(ledger, store)
            _save(output / 'observations.json', observations)
            report['monitoring'] = {'record': frozen, 'stats': observations['stats'],
                                    'observation_label': observations['observation_label'],
                                    'record_file': 'selection-record.json', 'observation_file': 'observations.json'}
        except (OSError, ValueError, TypeError, KeyError) as exc:
            report['monitoring'] = {'status': 'unavailable', 'error_type': type(exc).__name__}
            report['warnings'].append('本次候选跟踪记录未完成；不影响上述公式结果，也未生成任何已验证收益。')
    report['completed_at'] = datetime.now(TZ).isoformat()
    report['elapsed_seconds'] = round(time.monotonic() - started, 3)
    snapshot = {'strategy_id': STRATEGY_ID, 'version': VERSION, 'generated_at': now.isoformat(),
                'data_session': session, 'parameters': {'top_n': args.top, 'mode': 'closed_daily'},
                'data_source': data['metadata'], 'quotes': quotes,
                'security_exclusions': exclusion, 'all_evaluated': report['all_evaluated'],
                'methodology': report['methodology'], 'candidates': report['candidates'],
                'replay_scope': '指标及证据冻结；不含完整原始历史包，不冒充自足的离线公式重算快照'}
    return _publish(output, report, snapshot)


def main(argv=None):
    parser = argparse.ArgumentParser(description='免费沪深A股趋势模板选股；随时使用最近完整交易日日线')
    parser.add_argument('--strategy', choices=[STRATEGY_ID], default=STRATEGY_ID)
    parser.add_argument('--top', type=int, default=5)
    parser.add_argument('--mode', choices=['auto', 'post-close'], default='auto')
    parser.add_argument('--output')
    parser.add_argument('--data-cache', type=Path, help='已核验批量数据缓存位置；通常自动管理')
    args = parser.parse_args(argv)
    if not 1 <= args.top <= 50:
        parser.error('--top 必须在1到50之间')
    return run(args)
