"""Explicit paper-account configuration; no parameter implies profitability."""
from decimal import Decimal, InvalidOperation
import hashlib
import json


def account_config(capital=100000, max_positions=6, max_holding_sessions=5, *, max_entry_gap_pct=3, max_ma20_premium_pct=8):
    try:value=Decimal(str(capital))
    except InvalidOperation as exc:raise ValueError('初始资金必须是金额') from exc
    if isinstance(capital,bool) or not value.is_finite() or value<=0 or value>Decimal('1000000000') or value!=value.quantize(Decimal('.01')):
        raise ValueError('初始资金须大于0、不超过10亿元且最多两位小数')
    if isinstance(max_positions,bool) or not isinstance(max_positions,int) or not 1<=max_positions<=50:
        raise ValueError('持仓席位须为1至50的整数')
    if max_holding_sessions is not None and (isinstance(max_holding_sessions,bool) or not isinstance(max_holding_sessions,int) or not 1<=max_holding_sessions<=60):
        raise ValueError('最大持有周期须为1至60个交易日')
    limits={}
    for name,raw in [('max_entry_gap_pct',max_entry_gap_pct),('max_ma20_premium_pct',max_ma20_premium_pct)]:
        try:limit=Decimal(str(raw))
        except InvalidOperation as exc:raise ValueError('追价约束必须是数值') from exc
        if isinstance(raw,bool) or not limit.is_finite() or not 0<=limit<=100:
            raise ValueError('追价约束须为0至100的百分数')
        limits[name]=float(limit)
    return {'initial_capital_cents':int(value*100),'max_positions':max_positions,
            **limits,
            'max_holding_sessions':max_holding_sessions,'reserve_fraction':.05,
            'exit_policy':'MA5<MA20 or holding limit, attempt next open; blocked exits persist',
            'parameter_validation':'research_configuration_not_profitability_validated'}


def config_identity(rule_sha256,config):
    body=json.dumps(config,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    return hashlib.sha256(rule_sha256.encode()+b'\n'+body).hexdigest()
