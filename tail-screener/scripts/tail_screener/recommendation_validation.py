"""Evidence gates for promoting a screen into a recommendation rule.

A working scanner, a famous author, or a positive selected subperiod cannot
substitute for economic evidence and dated execution data. This module evaluates
an explicit evidence record; it never estimates a probability from a ranking.
"""
from __future__ import annotations
import math

REQUIRED_PERIODS = ('2018-2020', '2021-2023', '2024-2026')
REQUIRED_DATA_CHECKS = ('historical_eligibility', 'actual_price_limits',
                        'corporate_actions', 'unresolved_exits', 'capacity',
                        'signal_execution_timing', 'complete_trial_disclosure')


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def assess_recommendation_evidence(evidence, *, current_rule_sha256=None):
    """Fail closed on omitted/unknown evidence, retaining separate failure causes.

    Passing these retrospective gates is not a guarantee of future profits or
    proof of a live track record. The independent-forward flag is reported
    separately; historical evidence cannot create it.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    checks, failures, missing = [], [], []
    def check(key, passed, reason):
        state = 'passed' if passed is True else 'failed' if passed is False else 'unverified'
        checks.append({'id': key, 'status': state, 'reason': reason})
        if passed is False: failures.append(key)
        elif passed is not True: missing.append(key)

    digest = evidence.get('rule_sha256')
    identity_known = isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest)
    check('rule_identity', (digest == current_rule_sha256) if identity_known and current_rule_sha256 else None,
          '结果必须对应本次实际运行规则的SHA256；旧规则或另一组合不能继承。')
    check('preregistered', evidence.get('registered_before_outcomes'),
          '参数与比较口径必须在本轮结果计算前固定。')
    full = evidence.get('later_periods') or {}
    n = full.get('resolved_weekly_cohorts')
    check('sample', n >= 100 if _finite(n) else None, '较晚时期至少有100个已解决周批次；重复个股不作为独立样本。')
    for field, label in [('net_mean', '净收益均值应为正'),
                         ('paired_excess_mean', '相对同端点指数价格变化的均值差应为正'),
                         ('adjusted_excess_lower_bound', '两项比较校正后的周块置信下界应高于零')]:
        value = full.get(field)
        check(field, value > 0 if _finite(value) else None, label+'。')
    stress = full.get('double_cost_net_mean')
    check('cost_stress', stress >= 0 if _finite(stress) else None, '双倍成本下净均值不应为负。')
    periods = evidence.get('periods') or {}
    for period in REQUIRED_PERIODS:
        value = (periods.get(period) or {}).get('paired_excess_mean')
        check('period_' + period, value > 0 if _finite(value) else None,
              period+'必须单独保留，相对指数均值差应为正；不只报告表现好的时期。')
    data = evidence.get('data_checks') or {}
    for field in REQUIRED_DATA_CHECKS:
        check(field, data.get(field), '交易数据与全量结果检查：'+field+'；未知不可视为通过。')
    eligible = not failures and not missing
    return {'status': 'historical_gates_passed' if eligible else 'rejected' if failures else 'insufficient_evidence',
            'eligible_for_recommendation': eligible,
            'independent_forward_evidence': evidence.get('independent_forward_evidence') is True,
            'checks': checks, 'failed_checks': failures, 'unverified_checks': missing,
            'meaning': '历史检验门槛，不是未来盈利承诺；实盘与事前记录必须单独核验。'}


def load_recommendation_validation(skill_root, current_strategy_id):
    """Read the shipped comparison and verify its referenced local evidence.

    Unavailable or changed evidence does not stop technical scanning; it removes
    recommendation eligibility and makes the validation gap visible.
    """
    import hashlib
    import json
    from pathlib import Path
    root = Path(skill_root).resolve()
    path = root / 'references/recommendation-validation.json'
    if not path.is_file():
        return None
    unavailable = {'scope': '本版推荐有效性核验',
                   'admission': {'level': 'not_validated', 'reason': '验证资料缺失、损坏或与当前代码不一致，不能据此认定推荐有效。'},
                   'strategies': [], 'gate_summary': ['本次仅保留技术候选；没有取得可使用的推荐资格证明。']}
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            return unavailable
        manifests = record.get('evidence_files')
        if not isinstance(manifests, dict) or not manifests:
            return unavailable
        for relative, expected in manifests.items():
            local = (root / relative).resolve()
            if not local.is_relative_to(root) or not local.is_file():
                return unavailable
            if hashlib.sha256(local.read_bytes()).hexdigest() != expected:
                return unavailable
        for row in record.get('strategies', []):
            if (row.get('id') not in ('weekly-winner-j2-k1', 'weekly-loser-j4-k4')
                    or (row.get('evidence') or {}).get('strategy_id') != row.get('id')):
                return unavailable
            rule_file = root / 'scripts/tail_screener/weekly_signals.py'
            identity = hashlib.sha256(rule_file.read_bytes()).hexdigest() if rule_file.is_file() else None
            result = assess_recommendation_evidence(row.get('evidence'), current_rule_sha256=identity)
            row['gate_result'] = result
            # A rendered admission may only be tightened by software; a shipped
            # label cannot promote missing economic/transaction evidence.
            row['admission'] = {'level': 'conditional' if result['eligible_for_recommendation'] else 'rejected',
                                'reason': ('通过登记的历史门槛，尚需事前记录验证；不是未来收益保证。'
                                           if result['eligible_for_recommendation'] else
                                           '未通过全部净收益与交易数据门槛，不能升级为买入推荐规则。')}
        selected = next((row for row in record.get('strategies', []) if row.get('id') == current_strategy_id), None)
        record['admission'] = (selected['admission'] if selected else
                               {'level': 'screening_only', 'reason': '当前趋势模板没有已通过的完整买卖有效性证据；下列新规则比较不改变原候选排名。'})
        return record
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return unavailable
