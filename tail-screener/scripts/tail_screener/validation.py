"""Offline validation of frozen, prospectively recorded signal cohorts.

This does not generate hypothetical fills or optimize a strategy. File hashes
detect inconsistency; they cannot prove when a user originally saved a file.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, time
import hashlib
import json
from math import isfinite, sqrt
from pathlib import Path

from .engine import TZ, is_a_share, stamp
from .providers import calendar_context


def config_digest(config):
    raw = json.dumps(config, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def full_stamp(value, field):
    parsed = stamp(value)
    if not isinstance(value, str) or len(value) <= 10 or parsed is None:
        raise ValueError(f'{field} 需要完整日期时间')
    return parsed


def load_reports(paths):
    """Validate report identity and retain zero-signal days in the cohort."""
    files = set()
    for value in paths:
        path = Path(value)
        files.update(path.rglob('report.json') if path.is_dir() else [path])
    reports, signals = [], {}
    run_ids, days = set(), set()
    for path in sorted(files):
        report = json.loads(path.read_text(encoding='utf-8'))
        if report.get('strategy_id') not in (None,'legacy_tail_v2_1'):
            raise ValueError(f'{path}: 此验证器仅支持旧版尾盘固定退出队列；作者策略的动态持有须独立记录，不能混为零信号日或合并收益')
        if report.get('mode') != '实时采集' or report.get('analysis_mode','live') != 'live':
            raise ValueError(f'{path}: 回放或未知模式不能作为事前信号')
        for field in ('run_id', 'config_hash', 'code_version'):
            if not isinstance(report.get(field), str) or not report[field].strip():
                raise ValueError(f'{path}: 缺少 {field}；旧报告不能补造事前身份')
        if report['config_hash'] != config_digest(report['config']):
            raise ValueError(f'{path}: 配置哈希不匹配')
        snapshot_path = path.parent/'snapshot.json'
        if snapshot_path.exists():
            snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
            if config_digest(snapshot) != report['run_id']:
                raise ValueError(f'{path}: 冻结快照哈希与 run_id 不匹配')
        elif report.get('version', 0) >= 2:
            raise ValueError(f'{path}: v2 报告必须提供同目录 snapshot.json')
        as_of = full_stamp(report.get('as_of'), 'as_of')
        cal = calendar_context(as_of)
        if not cal.get('verified') or not cal.get('is_session'):
            raise ValueError(f'{path}: 主扫描日期不是可核验交易日')
        if not time(14, 30) <= as_of.time().replace(tzinfo=None) <= time(14, 55):
            raise ValueError(f'{path}: 主扫描不在尾盘窗口，不能作为事前零信号日')
        day = as_of.date().isoformat()
        if report['run_id'] in run_ids or day in days:
            raise ValueError('重复运行或同日多份报告；仅输入事前指定的每日一次主扫描，不能事后挑选')
        run_ids.add(report['run_id'])
        days.add(day)
        report['_at'], report['_day'], report['_file'] = as_of, day, str(path)
        reports.append(report)
        conditional_count = sum(pick.get('status') == 'conditional' for pick in report['picks'])
        reported_count = report.get('stats', {}).get('conditional_count')
        if reported_count is not None and reported_count != conditional_count:
            raise ValueError(f'{path}: 条件候选统计与 picks 不一致')
        codes = set()
        for pick in report['picks']:
            if pick.get('status') != 'conditional':
                continue
            sid = pick.get('signal_id')
            if not isinstance(sid, str) or not sid or sid in signals:
                raise ValueError('条件候选缺少唯一 signal_id 或存在重复')
            if not is_a_share(pick.get('code', '')):
                raise ValueError(f'{sid}: 不属于沪深 A 股')
            expected_id = hashlib.sha256(f"{report['run_id']}:{pick['code']}:{report['config_hash']}".encode('utf-8')).hexdigest()
            if sid != expected_id:
                raise ValueError(f'{sid}: signal_id 与运行、股票或配置身份不匹配')
            if pick['code'] in codes:
                raise ValueError('同一主扫描重复股票，不能重复计入分母')
            codes.add(pick['code'])
            plan = pick.get('plan') or {}
            until = full_stamp(plan.get('valid_until'), 'plan.valid_until')
            if not as_of <= until or until.date() != as_of.date():
                raise ValueError(f'{sid}: 入场计划有效期异常')
            if until.time().replace(tzinfo=None) > time(14, 55):
                raise ValueError(f'{sid}: 入场有效期不得超过14:55')
            if plan.get('earliest_sell_session') != cal['next_session']:
                raise ValueError(f'{sid}: 最早卖出日与交易日历不一致')
            if not time(14, 30) <= as_of.time().replace(tzinfo=None) <= time(14, 55):
                raise ValueError(f'{sid}: 条件信号不在尾盘窗口')
            signals[sid] = dict(pick, signal_at=as_of, day=day, run_id=report['run_id'],
                                config_hash=report['config_hash'], code_version=report['code_version'],
                                config=report['config'])
    return sorted(reports, key=lambda r: r['_at']), signals


def validate_outcome(row, signal, now, costs):
    # Reuse the existing tested A-share calendar, T+1 and cost accounting.
    from evaluate_trades import evaluate_trade
    if row.get('code') and row['code'] != signal['code']:
        raise ValueError('结果股票代码与冻结信号不一致')
    state = row.get('status')
    state = 'closed' if state == 'filled' else state
    if state not in ('closed', 'open', 'skipped', 'unfilled'):
        raise ValueError('status 必须为 closed/open/skipped/unfilled')
    recorded = full_stamp(row.get('recorded_at'), 'recorded_at')
    if not signal['signal_at'] <= recorded <= now:
        raise ValueError('记录时间早于信号或晚于验证时点')
    outcome = dict(signal_id=row['signal_id'], status=state, recorded_at=recorded,
                   day=signal['day'], code=signal['code'], deviations=[])
    if state in ('skipped', 'unfilled'):
        if not row.get('reason', '').strip():
            raise ValueError('跳过/未成交必须保留原因')
        if any(row.get(k) for k in ('entry_at', 'entry_price', 'exit_at', 'exit_price', 'shares')):
            raise ValueError('跳过/未成交不能同时包含成交字段')
        outcome['reason'] = row['reason']
        return outcome
    if row.get('kind') not in ('actual', 'paper'):
        raise ValueError('成交 kind 必须为 actual 或 paper')
    entry = full_stamp(row.get('entry_at'), 'entry_at')
    price, shares = float(row['entry_price']), int(row['shares'])
    if not isfinite(price) or price <= 0 or shares <= 0:
        raise ValueError('入场价及股数必须有效')
    if not signal['signal_at'] <= entry <= recorded or entry.date() != signal['signal_at'].date():
        raise ValueError('成交必须在信号之后、当日入场，且不得晚于记录时间')
    cal = calendar_context(entry)
    if not cal.get('verified') or not cal.get('is_session'):
        raise ValueError('入场交易日无法核验')
    clock = entry.time().replace(tzinfo=None)
    if not time(14, 30) <= clock <= time(15):
        raise ValueError('成交时间不属于当日尾盘')
    if entry > stamp(signal['plan']['valid_until']):
        outcome['deviations'].append('超过信号有效期入场，实际亏损仍保留')
    ceiling = signal['plan'].get('entry_ceiling')
    if ceiling is not None and price > ceiling:
        outcome['deviations'].append('成交价超过计划追价上限')
    outcome.update(kind=row['kind'], entry_at=entry, entry_price=price, shares=shares)
    if state == 'open':
        if row.get('exit_at') or row.get('exit_price'):
            raise ValueError('open 状态不能包含退出成交')
        return outcome
    exit_at = full_stamp(row.get('exit_at'), 'exit_at')
    if exit_at > recorded:
        raise ValueError('退出成交晚于记录时间')
    accounting = evaluate_trade(dict(row, code=signal['code']), **costs)
    target_exit = datetime.combine(datetime.fromisoformat(cal['next_session']).date(),
                                   time.fromisoformat(signal['config'].get('next_day_exit_time', '10:30')), TZ)
    if exit_at > target_exit:
        outcome['deviations'].append('超过下一交易日时间退出要求，损益仍保留')
    outcome.update(accounting, exit_at=exit_at)
    return outcome


def wilson_interval(wins, count):
    if not count:
        return None
    z = 1.959963984540054
    proportion = wins / count
    denominator = 1 + z*z/count
    center = (proportion + z*z/(2*count)) / denominator
    half = z * sqrt(proportion*(1-proportion)/count + z*z/(4*count*count)) / denominator
    return [max(0.0, center-half), min(1.0, center+half)]


def summary(outcomes):
    closed = [o for o in outcomes if o['status'] == 'closed']
    wins = sum(o['net_pnl'] > 0 for o in closed)
    dates = defaultdict(list)
    for o in closed:
        dates[o['day']].append(o)
    return {'states': dict(Counter(o['status'] for o in outcomes)), 'closed_trades': len(closed),
            'closed_trade_net_pnl': round(sum(o['net_pnl'] for o in closed), 2),
            'fees': round(sum(o['fees'] for o in closed), 2),
            'closed_trade_win_rate': wins/len(closed) if closed else None,
            'win_rate_wilson_95_descriptive': wilson_interval(wins, len(closed)),
            'deviating_trades': sum(bool(o.get('deviations')) for o in outcomes),
            'entry_date_closed_trade_returns': [
                {'date': day, 'closed_trades': len(items),
                 'net_pnl': round(sum(o['net_pnl'] for o in items), 2),
                 'net_return_pct': round(100*sum(o['net_pnl'] for o in items)/sum(o['entry_notional'] for o in items), 6)}
                for day, items in sorted(dates.items())]}


def expanding_folds(reports, outcomes, warmup_days=20, test_days=5, gap_days=1):
    """Keep complete dates together; purge labels unresolved at the test boundary."""
    if warmup_days < 1 or test_days < 1 or gap_days < 0:
        raise ValueError('warmup_days/test_days 必须为正整数，gap_days 不得为负')
    days = [r['_day'] for r in reports]
    folds = []
    for start in range(warmup_days+gap_days, len(days), test_days):
        testing = set(days[start:start+test_days])
        training = set(days[:start-gap_days])
        boundary = reports[start]['_at']
        prior = [o for o in outcomes if o['day'] in training]
        # Both realized outcome and its recording must predate the test start.
        usable = [o for o in prior if o['status'] == 'closed'
                  and o['exit_at'] < boundary and o['recorded_at'] < boundary]
        testing_outcomes = [o for o in outcomes if o['day'] in testing]
        folds.append({'train_start': days[0], 'train_end': days[start-gap_days-1],
                      'test_start': days[start], 'test_end': days[min(start+test_days, len(days))-1],
                      'embargo_observed_report_dates': days[start-gap_days:start],
                      'training_closed_available': len(usable),
                      'training_unavailable_or_purged': len(prior)-len(usable),
                      'test_by_kind': {kind: summary([o for o in testing_outcomes if o.get('kind') == kind])
                                       for kind in ('actual', 'paper')},
                      'test_states': dict(Counter(o['status'] for o in testing_outcomes))})
    return folds


def validate_cohort(reports, signals, rows, *, costs=None, now=None, warmup_days=20, test_days=5, gap_days=1):
    costs = costs or {}
    now = now or datetime.now(TZ)
    if any(not isfinite(v) or v < 0 for v in costs.values()) or costs.get('paper_slippage_bps', 10) >= 10000:
        raise ValueError('费用/滑点必须非负且有限，滑点不得达到10000bp')
    errors, outcomes, seen = [], [], set()
    # Duplicate outcomes invalidate the cohort rather than allowing a winning row to overwrite a loss.
    counts = Counter(row.get('signal_id') for row in rows)
    for line, row in enumerate(rows, 2):
        sid = row.get('signal_id')
        try:
            if not sid or sid not in signals:
                raise ValueError('无法匹配冻结条件信号')
            if counts[sid] != 1:
                raise ValueError('同一 signal_id 存在多行结果，不允许选择性覆盖')
            outcomes.append(validate_outcome(row, signals[sid], now, costs))
            seen.add(sid)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            errors.append({'line': line, 'signal_id': sid, 'error': str(exc)})
    missing = sorted(set(signals)-seen)
    states = Counter(o['status'] for o in outcomes)
    versions = {(r['config_hash'], r['code_version']) for r in reports}
    folds = expanding_folds(reports, outcomes, warmup_days, test_days, gap_days)
    issues = []
    acquisition_gaps = [{'day': r['_day'], 'status': r['run_status']['data_status']}
                        for r in reports if r.get('run_status', {}).get('data_status') in ('failed', 'incomplete')]
    if acquisition_gaps:
        issues.append('存在扫描失败或数据不完整的主扫描日；保留这些日期，不能按正常零候选日评价策略')
    if any(r['_at'] > now for r in reports):
        issues.append('存在晚于验证时点的报告')
    if not reports or not signals:
        issues.append('缺少条件信号样本')
    if missing or errors:
        issues.append('信号结果不完整或存在错误，不能只按成功闭合交易下结论')
    if states['open']:
        issues.append('仍有未退出持仓，无法给出完整队列收益')
    if states['skipped']:
        issues.append('有人为跳过信号，只能评价实际执行子集')
    if len(versions) != 1:
        issues.append('包含不同配置/代码版本，不能合并为单一固定策略表现')
    if any(o.get('deviations') for o in outcomes):
        issues.append('存在偏离入场/退出计划的成交')
    if not folds:
        issues.append('观察日期不足以形成前向测试分段')
    actual_paper = {o.get('kind') for o in outcomes if o.get('kind')}
    if len(actual_paper) > 1:
        issues.append('实盘与模拟成交分开列示，禁止合并胜率')
    by_version = []
    for config_hash, code_version in sorted(versions):
        subset = [o for o in outcomes if (signals[o['signal_id']]['config_hash'],
                                        signals[o['signal_id']]['code_version']) == (config_hash, code_version)]
        by_version.append({'config_hash': config_hash, 'code_version': code_version,
                           'states': dict(Counter(o['status'] for o in subset)),
                           'by_kind': {kind: summary([o for o in subset if o.get('kind') == kind])
                                       for kind in ('actual', 'paper')}})
    return {'method': '冻结规则、按观察日期扩展的前向分段诊断；未调参、未重建历史成交',
            'report_days': len(reports), 'selected_signals': len(signals), 'valid_outcomes': len(outcomes),
            'acquisition_gap_days': acquisition_gaps,
            'outcome_coverage': len(outcomes)/len(signals) if signals else None,
            'states': dict(states), 'missing_signal_ids': missing, 'errors': errors,
            'versions': [{'config_hash': c, 'code_version': v} for c, v in sorted(versions)],
            'by_kind': {kind: summary([o for o in outcomes if o.get('kind') == kind])
                        for kind in ('actual', 'paper')} if len(versions) == 1 else {},
            'by_version': by_version,
            'folds': folds if len(versions) == 1 else [], 'blocking_issues': issues,
            'complete_observed_cohort': not issues,
            'profitability_proven': False,
            'limitations': ['仅覆盖提交的主扫描报告，无法证明没有遗漏其他交易日；同日只允许事前指定的一次主扫描。',
                            '哈希校验用于发现文件不一致，不是第三方时间戳，无法证明文件确实事前冻结。',
                            'Wilson 区间只描述已闭合成交，股票和交易日相关性使独立样本假设不成立，不能当作未来胜率置信保证。',
                            '按入场日期汇总的已闭合成交本金收益不是逐日账户净值；无未平仓估值，不计算账户回撤或年化 Sharpe。',
                            '分段按已提交报告日期计数，零候选日也须保存；不是等间隔交易日回测。',
                            '未成交样本保留在分母但不虚构收益；没有盘口/逐笔撮合证据时不推断其本可成交。']}
