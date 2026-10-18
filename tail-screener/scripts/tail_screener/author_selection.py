"""Attribute and rank Connors matches without altering their entry rules.

Alvarez's published ranking is ascending RSI(2). Exact boundary ties remain
equally ranked and are all displayed; stock codes only stabilize display order
inside such ties. No rounded indicator, code, or input order is an alpha signal.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from math import isfinite


ALVAREZ_RSI2_POLICY = {
    'id': 'alvarez_rsi2_ascending_v1',
    'name': 'Cesar Alvarez：RSI(2)由低到高排序',
    'metric': 'rsi2', 'direction': 'ascending',
    'source': {
        'author': 'Cesar Alvarez',
        'title': 'The ABCs of Creating a Mean Reversion Strategy – Part 2',
        'published_date': '2017-11-01',
        'url': 'https://alvarezquanttrading.com/blog/the-abcs-of-creating-a-mean-reversion-strategy-part-2/',
        'section': 'Ranking of signals',
    },
    'entry_source': {
        'author': 'Larry Connors',
        'title': 'How To Enter and Exit a Position on the Close',
        'published_date': '2009-08-21',
        'url': 'https://tradingmarkets.com/recent/how_to_enter_and_exit_a_position_on_the_close-677551',
        'rule': '收盘价>SMA200 且 RSI(2)<5；RSI(2)>70收盘退出',
    },
    'tie_policy': ('使用未经显示取整的RSI(2)；精确相等时同优先级，名次采用1、1、3。'
                   '名额边界的同分标的全部展示；股票代码仅稳定同分组的显示顺序。'),
    'interpretation': '这是信号优先级，不是预期收益、胜率或盈利保证；不会改变原入场条件。',
    'adaptation': ('Connors入场规则与Alvarez排序原则分别归因；沪深A股个股应用未经本地收益验证。'
                   '边界同分全部展示属于本工具的展示政策，不宣称是作者规定的仓位分配。'),
}


def _finite_number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _verified_value(row):
    """Check the provided match flag against finite native rule metrics."""
    if not isinstance(row, dict):
        return None, '评估记录结构无效'
    if row.get('raw_entry_match') is False:
        return None, '未触发原入场规则'
    if row.get('raw_entry_match') is not True:
        return None, '原入场信号未评估或标识无效'
    if not isinstance(row.get('code'), str) or not row['code'].strip():
        return None, '股票代码缺失'
    if row.get('data_gaps') != []:
        return None, '数据缺项或完整性记录缺失'
    if row.get('confirmation_status') == 'incomplete':
        return None, '信号确认状态与完整性冲突'
    metrics = row.get('metrics')
    if not isinstance(metrics, dict):
        return None, '规则指标缺失'
    rsi, price, sma = (_finite_number(metrics.get(key)) for key in ('rsi2', 'price', 'sma200'))
    if (rsi is None or price is None or sma is None
            or not 0 <= rsi < 5 or not 0 < sma < price):
        return None, '原入场标识与指标缺失、非有限数或门槛不一致'
    if 'conditions' in row:
        conditions = row['conditions']
        if (not isinstance(conditions, list)
                or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in conditions)):
            return None, '原规则条件结构无效'
        by_id = {item.get('id'): item for item in conditions}
        for key, observed, operator, threshold in [
                ('above_sma200', price, '>', sma), ('rsi2_below_5', rsi, '<', 5)]:
            condition = by_id.get(key, {})
            if (condition.get('passed') is not True or condition.get('operator') != operator
                    or _finite_number(condition.get('observed')) != observed
                    or _finite_number(condition.get('threshold')) != threshold):
                return None, '原规则条件与指标不一致'
    return rsi, None


def rank_connors_matches(evaluated, top_n):
    """Rank valid matches, preserving every exact tie at the requested cutoff.

    Returned rows are deep copies. ``selected`` points to those same returned
    row objects in ``ranked_matches``. Non-matches and inconsistent inputs never
    enter the ranked pool. This function does no network/file I/O.
    """
    if not isinstance(top_n, int) or isinstance(top_n, bool) or not 1 <= top_n <= 20:
        raise ValueError('top_n须为1—20的整数')
    if not isinstance(evaluated, (list, tuple)):
        raise ValueError('evaluated须为已核验结果列表')
    candidates, excluded = [], Counter()
    for row in evaluated:
        value, reason = _verified_value(row)
        if reason is not None:
            excluded[reason] += 1
        else:
            candidates.append((value, row['code'], row))
    codes = Counter(code for _, code, _ in candidates)
    unique_candidates = []
    for candidate in candidates:
        if codes[candidate[1]] != 1:
            excluded['同一股票重复出现，无法确认唯一排名输入'] += 1
        else:
            unique_candidates.append(candidate)
    # The second component only orders *exactly equal* RSI values for display.
    unique_candidates.sort(key=lambda entry: (entry[0], entry[1]))
    cutoff = (unique_candidates[min(top_n, len(unique_candidates))-1][0]
              if unique_candidates else None)
    ranked, selected = [], []
    previous_value = None
    rank = 0
    for position, (value, _, original) in enumerate(unique_candidates, 1):
        if position == 1 or value != previous_value:
            rank = position
        chosen = value <= cutoff
        row = deepcopy(original)
        row.update(selection_rank=rank, ranking_value=value, selected=chosen,
                   selection_reason=(
                       f'数据完整且满足Connors入场规则；按Alvarez公开原则，以未取整RSI(2)由低到高排第{rank}。'
                       + ('进入本次展示范围；精确同分者同优先级。' if chosen
                          else '位于本次展示边界之后，保留在全部触发表。')))
        ranked.append(row)
        if chosen:
            selected.append(row)
        previous_value = value
    boundary_ties = sum(row['ranking_value'] == cutoff for row in ranked) if cutoff is not None else 0
    summary = {
        'input_count': len(evaluated), 'request_count': top_n,
        'qualified_count': len(ranked), 'excluded_count': len(evaluated)-len(ranked),
        'not_matched_count': excluded.get('未触发原入场规则', 0),
        'unevaluated_count': excluded.get('原入场信号未评估或标识无效', 0),
        'selected_count': len(selected), 'cutoff_value': cutoff,
        'boundary_tie_count': boundary_ties, 'tie_extra_count': max(0, len(selected)-top_n),
        'has_boundary_ties': boundary_ties > 1,
        'excluded_reasons': dict(sorted(excluded.items())),
    }
    return {'policy': deepcopy(ALVAREZ_RSI2_POLICY), 'ranked_matches': ranked,
            'selected': selected, 'summary': summary}
