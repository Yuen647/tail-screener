"""Separate sound individual setups from a currently unsuitable environment.

This annotation never changes the evaluator's admission rules, execution plan,
or original blockers. An environmental watch is a checklist for a fresh scan,
not a trade signal, a forecast, or evidence of strategy profitability.
"""
from __future__ import annotations

from copy import deepcopy

from .engine import fraction, number


ENVIRONMENT_BLOCKERS = {
    'live': frozenset({
        '全市场上涨家数占比过低', '大盘处于急跌环境', '所属行业未形成多数上涨',
    }),
    'post_close': frozenset({
        '全市场收盘上涨家数占比过低', '数据交易日大盘收盘处于急跌环境',
        '所属行业收盘未形成多数上涨',
    }),
}
RESEARCH_NOTE = '盘后研究仅供观察，须下一交易日按实时规则重新筛选'


def _usable_context(context, config, item, mode):
    """Do not interpret a missing observation as a weak environment.

    In particular, the live evaluator uses the same breadth blocker for a
    missing breadth value and a below-threshold breadth value.
    """
    coverage = [('universe_coverage', 'min_universe_coverage'),
                ('valid_quote_coverage', 'min_universe_coverage'),
                ('industry_coverage', 'min_industry_coverage')]
    if mode == 'live':
        coverage.append(('dated_quote_coverage', 'min_universe_coverage'))
    for key, threshold in coverage:
        value, minimum = fraction(context.get(key)), fraction(config.get(threshold))
        if value is None or minimum is None or value < minimum:
            return False
    if fraction(context.get('market_breadth')) is None:
        return False
    indices = context.get('indices')
    if (not isinstance(indices, list) or len(indices) != 2
            or any(not isinstance(index, dict) for index in indices)
            or {index.get('code') for index in indices} != {'000001.SH', '399001.SZ'}
            or any(not index.get('source') or number(index.get('change_pct')) is None
                   or not -100 < number(index['change_pct']) < 100 for index in indices)):
        return False
    sector = context.get('sectors', {}).get(item.get('industry'), {})
    if (fraction(sector.get('breadth')) is None or number(sector.get('median_change_pct')) is None
            or number(sector.get('members')) is None or number(sector['members']) < 5):
        return False
    if mode == 'post_close':
        return context.get('research', {}).get('verified') is True
    return context.get('calendar', {}).get('verified') is True


def _conditions(item, context, config, mode):
    indices = {index['code']: index for index in context['indices']}
    sector = context['sectors'][item['industry']]
    conditions = [
        {'id': 'index_' + code, 'label': label,
         'observed': number(indices[code]['change_pct']),
         'required': {'operator': '>', 'value': config['index_drawdown_limit_pct']}, 'unit': 'percent'}
        for code, label in [('000001.SH', '上证指数涨跌幅'), ('399001.SZ', '深证成指涨跌幅')]]
    conditions.extend([
        {'id': 'market_breadth', 'label': '全市场上涨家数占比',
         'observed': fraction(context['market_breadth']),
         'required': {'operator': '>=', 'value': config['min_market_breadth']}, 'unit': 'fraction'},
        {'id': 'sector_breadth', 'label': '所属行业上涨家数占比',
         'observed': fraction(sector['breadth']),
         'required': {'operator': '>=', 'value': .5}, 'unit': 'fraction'},
        {'id': 'sector_median', 'label': '所属行业涨跌幅中位数',
         'observed': number(sector['median_change_pct']),
         'required': {'operator': '>', 'value': 0}, 'unit': 'percent'},
        {'id': 'fresh_scan', 'label': '下一交易日重新筛选' if mode == 'post_close' else '尾盘窗口重新筛选',
         'observed': '尚未进行后续重扫',
         'required': {'operator': 'all_pass', 'value': [
             '按当时最新行情重新检查全部个股、行业与市场规则',
             '核验双源报价、分钟数据和公告正文',
             '仅实时规则全部通过后才生成带有效期的价格计划']}, 'unit': 'checklist'},
    ])
    return conditions


def _invalidation(item, config):
    metrics = item.get('metrics') or {}
    return [
        {'id': 'ma20', 'label': '价格跌破重算的20日均线或偏离过大时移出观察',
         'observed': {'price': number(item.get('price')), 'ma20': number(metrics.get('ma20'))},
         'required': {'operator': 'price_over_recomputed_ma20_between',
                      'minimum': 1, 'maximum': 1 + config['max_ma20_premium']}, 'unit': 'ratio'},
        {'id': 'atr', 'label': '重算的ATR14相对波动超限时移出观察',
         'observed': number(metrics.get('atr14_pct')),
         'required': {'operator': '<=', 'value': config['max_atr_pct']}, 'unit': 'fraction'},
        {'id': 'base_width', 'label': '近10日整理区间重算后过宽时移出观察',
         'observed': number(metrics.get('consolidation_width_10d')),
         'required': {'operator': '<=', 'value': config['max_base_width_pct']}, 'unit': 'fraction'},
        {'id': 'new_risk', 'label': '出现财务或公告风险时移出观察',
         'observed': '本次未发现现行规则规定的风险阻断项',
         'required': {'operator': 'all_pass', 'value': [
             '最新财报净利润及扣非净利润大于0、营收增速不低于-20%',
             '公告正文核查无新增风险或未决事项']}, 'unit': 'checklist'},
        {'id': 'data', 'label': '数据缺失、过期或双源冲突时暂停观察资格',
         'observed': '本次数据完整性检查通过',
         'required': {'operator': 'all_pass', 'value': [
             f"股票目录和有效报价覆盖率均不低于{config['min_universe_coverage']:.0%}",
             f"行业映射覆盖率不低于{config['min_industry_coverage']:.0%}",
             '其他数据完整性、时效与一致性规则全部通过']}, 'unit': 'checklist'},
    ]


def annotate_opportunities(evaluated, context, config, analysis_mode, run_status):
    """Copy evaluator rows and identify cases blocked *only* by environment.

    Unknown blockers fail closed. Only completed collection can produce an
    environmental watch. Existing statuses, plans and blockers are preserved.
    """
    result = deepcopy(evaluated)
    allowed = ENVIRONMENT_BLOCKERS.get(analysis_mode, frozenset())
    complete = isinstance(run_status, dict) and run_status.get('data_status') == 'complete'
    for item in result:
        raw = item.get('blockers')
        valid = isinstance(raw, list) and all(isinstance(gap, str) for gap in raw)
        blockers = set(raw) if valid else {'拒绝原因缺失或结构无效'}
        if analysis_mode == 'post_close':
            blockers.discard(RESEARCH_NOTE)
        environment = sorted(blockers & allowed)
        other = sorted(blockers - allowed)
        item.update(watch_eligible=False, watch_class=None, environment_blockers=environment,
                    other_blockers=other, watch_reason=None, watch_conditions=[],
                    watch_invalidation=[], watch_basis=None)
        score, minimum = number(item.get('score')), number(config.get('min_score'))
        eligible = (complete and bool(environment) and not other and valid
                    and item.get('status') == 'watch_only' and item.get('plan') is None
                    and item.get('research_status') != 'incomplete'
                    and score is not None and minimum is not None and score >= minimum
                    and not item.get('notice_risks')
                    and _usable_context(context, config, item, analysis_mode))
        if not eligible:
            continue
        research = context.get('research') or {}
        data_session = (research.get('data_session') if analysis_mode == 'post_close'
                        else (context.get('calendar') or {}).get('session'))
        item.update(
            watch_eligible=True, watch_class='environment_wait',
            watch_reason='个股现行规则已通过，等待市场或行业环境改善后重新筛选',
            watch_conditions=_conditions(item, context, config, analysis_mode),
            watch_invalidation=_invalidation(item, config),
            watch_basis={'data_session': data_session, 'quote_at': item.get('quote_at'),
                         'score_is_probability': False, 'entry_eligible': False,
                         'rule': '数值仅为本次观测；下次扫描须重算，不是固定买入价、卖出价或止损价。'})
    return result


def summarize_opportunities(evaluated):
    """One-run counts only; repeating the same session adds no return evidence."""
    return {
        'environment_wait_count': sum(item.get('watch_eligible') is True for item in evaluated),
        'other_blocked_count': sum(bool(item.get('other_blockers')) for item in evaluated),
    }
