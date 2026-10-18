"""Descriptive price evidence and a fail-closed recommendation boundary.

The default MA state/liquidity strategy has no implemented promotion validator.
Neither price strength, a successful run nor a caller-supplied boolean may
promote its observations into buy recommendations. No selection is changed here.
"""
from math import fsum, isfinite


def research_preselection(report):
    """Preserve useful screening output independently of buy eligibility.

    The labels describe research questions, not a new alpha filter or ranking.
    Incomplete runs may expose partial observations but never a complete shortlist.
    """
    run = report.get('run_status') or {}
    complete = (run.get('execution_status') == 'completed'
                and run.get('data_status') == 'complete'
                and bool(report.get('data_session'))
                and report.get('data_session') == report.get('requested_session')
                and not report.get('data_issues'))
    failed = run.get('execution_status') == 'failed' or run.get('data_status') == 'failed'
    rows = []
    flag_labels = {'negative_5_session_return': '近5交易日下跌',
                   'close_below_ma5': '收盘低于MA5',
                   'close_below_ma20': '收盘低于MA20', 'ma5_falling': 'MA5下行'}
    for row in report.get('candidates') or []:
        diagnostics = row.get('price_diagnostics') or {}
        flags = diagnostics.get('risk_flags', [])
        if diagnostics.get('status') != 'computed':
            state, focus = 'evidence_needed', '补齐近期走势证据'
        elif 'close_below_ma20' in flags and 'close_below_ma5' in flags:
            state, focus = 'price_recovery_needed', '核查下跌原因，等待价格恢复证据'
        elif flags:
            state, focus = 'mixed_price_evidence', '核查局部走弱是否影响短期机会'
        else:
            state, focus = 'no_weakness_flags', '核查是否存在与持有期限匹配的具体机会'
        opposition = [flag_labels[x] for x in flags if x in flag_labels]
        if diagnostics.get('status') != 'computed':
            opposition.append('近期走势未核验，不能视为没有走弱风险')
        candle=row.get('kline_review') or {}
        if candle.get('status')=='computed':
            patterns=candle.get('patterns',[])
            opposition.extend(x for x in patterns if x in ('阴实体覆盖前一阳实体','上破前5日高点后收回','上影占当日振幅至少一半'))
            if candle.get('position')=='收盘跌破前5日低点':opposition.append(candle['position'])
            if '上破前5日高点后收回' in patterns:focus='核查突破回落及量能，比较后续能否重新收在区间上沿之外'
            elif candle.get('position')=='收盘跌破前5日低点':focus='核查区间下沿失守原因与恢复条件'
        rows.append({'code': row['code'], 'name': row.get('name', row['code']),
                     'liquidity_rank': row.get('rank'), 'research_state': state,
                     'research_focus': focus, 'observed_concerns': opposition,
                     'screen_basis': '通过原筛选门槛；按成交额排列，未按预期收益排序',
                     'ma5': row.get('ma5'), 'ma20': row.get('ma20'),
                     'mean_amount20': row.get('mean_amount20'),
                     'price_diagnostics': diagnostics,
                     'kline_review': row.get('kline_review',{'status':'unknown'}),
                     'buy_eligible': False,
                     'open_questions': ['与持有期限匹配的机会及反对证据', '公告内容与事件时点',
                                        '入场触发与判断失效条件', '可执行退出、成本与损失情景']})
    return {'schema_version': 1,
            'status': 'complete' if complete else 'failed' if failed else 'partial',
            'count': len(rows) if complete else 0,
            'partial_count': len(rows) if not complete else 0,
            'candidates': rows, 'ranking_basis': 'liquidity_not_expected_return',
            'note': '研究预选独立于买入资格；未验证盈利不清空候选，也不把候选变成下单建议。'}


def price_diagnostics(closes):
    """Use one adjusted-close basis, ending at the signal session only."""
    missing = {'status': 'unverified', 'basis': 'adjusted_close',
               'return_5_sessions_pct': None, 'close_vs_ma5_pct': None,
               'close_vs_ma20_pct': None, 'ma5_change_pct': None,
               'drawdown_from_20_session_close_high_pct': None,
               'risk_flags': ['price_history_unverified']}
    try:
        values = [float(x) for x in closes][-20:]
    except (TypeError, ValueError, OverflowError):
        return missing
    if len(values) != 20 or not all(isfinite(x) and x > 0 for x in values):
        return missing
    ma5, ma20 = fsum(values[-5:]) / 5, fsum(values) / 20
    previous_ma5 = fsum(values[-6:-1]) / 5
    result = {'status': 'computed', 'basis': 'adjusted_close',
              'return_5_sessions_pct': (values[-1] / values[-6] - 1) * 100,
              'close_vs_ma5_pct': (values[-1] / ma5 - 1) * 100,
              'close_vs_ma20_pct': (values[-1] / ma20 - 1) * 100,
              'ma5_change_pct': (ma5 / previous_ma5 - 1) * 100,
              'drawdown_from_20_session_close_high_pct': (values[-1] / max(values) - 1) * 100}
    result['risk_flags'] = [label for key, label in (
        ('return_5_sessions_pct', 'negative_5_session_return'),
        ('close_vs_ma5_pct', 'close_below_ma5'),
        ('close_vs_ma20_pct', 'close_below_ma20'),
        ('ma5_change_pct', 'ma5_falling')) if result[key] < 0]
    return result


def decision_summary(report):
    """Recompute at publication/render time; never trust a stored eligibility flag."""
    config = report.get('account_config') or {}
    reasons = ['strategy_profitability_unvalidated',
               'holding_horizon_unvalidated',
               'candidate_trade_theses_unvalidated']
    run = report.get('run_status') or {}
    if (run.get('execution_status') != 'completed' or run.get('data_status') != 'complete'
            or not report.get('data_session')
            or report.get('data_session') != report.get('requested_session')
            or report.get('data_issues')):
        reasons.insert(0, 'required_data_unverified')
    if (report.get('evidence_dimensions') or {}).get('disclosure_semantics') != 'reviewed':
        reasons.append('disclosure_semantics_unverified')
    capital, slots = config.get('initial_capital_cents'), config.get('max_positions')
    budget = (capital * 95 // (100 * slots)
              if type(capital) is int and capital > 0 and type(slots) is int and slots > 0 else None)
    shortlist = research_preselection(report)
    if shortlist['status'] == 'complete' and shortlist['count']:
        conclusion = f"本次有{shortlist['count']}只研究预选；买入依据尚未验证。"
    elif shortlist['status'] == 'complete':
        conclusion = '本次没有满足筛选条件的研究预选，不凑满名额。'
    else:
        conclusion = '本次未形成完整研究预选；已有观察结果与缺项分别保留。'
    return {'schema_version': 1, 'status': 'research_only',
            'recommendation_eligible': False, 'recommendation_count': 0,
            'preselection_count': shortlist['count'], 'preselection_status': shortlist['status'],
            'recommended_codes': [], 'reason_codes': reasons,
            'conclusion': conclusion,
            'ranking_basis': 'mean_amount20_desc_then_signal_amount_desc',
            'ranking_meaning': '流动性顺序，不是买入优先级',
            'display_count': len(report.get('candidates') or []),
            'max_positions': slots, 'holding_sessions': config.get('max_holding_sessions'),
            'horizon_validation': 'not_validated',
            'nominal_slot_budget_cents': budget,
            'allocation_assumption': '95% of initial capital divided by max_positions; not a user allocation instruction',
            'exit_timing': '持有上限含买入日；满指定交易日后下一开盘尝试退出，受阻延后。',
            'loss_limit': 'not_defined_by_holding_limit_or_entry_ceiling',
            'promotion_validator': 'not_implemented'}


def apply_decision_review(report):
    """Annotate evidence without changing candidate order, prices or paper plans."""
    decision = decision_summary(report)
    report['decision_review'] = decision
    report['research_preselection'] = research_preselection(report)
    report['recommendation_eligible'] = False
    report['recommendations'] = []
    for row in report.get('candidates') or []:
        row['recommendation_eligible'] = False
        row['trade_review'] = {
            'status': 'unvalidated', 'buy_thesis': None,
            'entry_trigger': None, 'invalidation_condition': None,
            'expected_return': None, 'loss_estimate': None,
            'reason_codes': list(decision['reason_codes']),
            'note': '均线与成交额只解释入选原因；入场优势、失效条件和损失范围尚未验证。'}
    return decision
