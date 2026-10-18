"""Registered offline reconstruction using production signals and accounting.

This initial contract supplies reproducible development evidence. Statistical
promotion and independent forward validation are separate, unimplemented gates.
"""
from collections import Counter
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path
import re

from .account_config import account_config
from .account_engine import new_account, advance_account, account_metrics
from .account_strategy import STRATEGY_ID, rule_identity, scan_account, read_bar, ma_state
from .calendar import TZ
from .recent_daily import RecentDailyStore, _digest


def _sha(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value):
        raise ValueError('缺少有效SHA256')
    return value


def _spec(spec):
    required={'schema_version','strategy_id','rule_sha256','snapshot_sha256','coverage_sha256',
              'config','start','end','cost_multiplier','data_class','criteria'}
    if not isinstance(spec,dict) or set(spec)!=required: raise ValueError('验证登记字段缺失或多余')
    if type(spec['schema_version']) is not int or spec['schema_version']!=1 or spec['strategy_id']!=STRATEGY_ID:
        raise ValueError('仅支持当前MA账户验证契约，旧尾盘或周策略证据不能代入')
    for field in ('rule_sha256','snapshot_sha256','coverage_sha256'): _sha(spec[field])
    if date.fromisoformat(spec['start']).isoformat()!=spec['start'] or date.fromisoformat(spec['end']).isoformat()!=spec['end'] or spec['start']>spec['end']:
        raise ValueError('验证区间无效')
    if type(spec['cost_multiplier']) is not int or spec['cost_multiplier'] not in (1,2):
        raise ValueError('成本倍数仅支持1或2')
    if spec['data_class'] not in ('synthetic_fixture','retrospective_development'):
        raise ValueError('此入口只接受合成样例或事后开发数据，不生成前向资格')
    config=spec['config']
    if type(config.get('initial_capital_cents')) is not int:raise ValueError('初始资金分值须为整数')
    normal=account_config(config['initial_capital_cents']/100,config['max_positions'],config['max_holding_sessions'],
        max_entry_gap_pct=config['max_entry_gap_pct'],max_ma20_premium_pct=config['max_ma20_premium_pct'])
    if config.get('data_source') in ('recent','archive'): normal['data_source']=config['data_source']
    if config!=normal or type(config['max_holding_sessions']) is not int:
        raise ValueError('账户配置必须与生产规范一致')
    criteria=spec['criteria']
    if not isinstance(criteria,dict) or set(criteria)!={'min_sessions','min_closed_trades','max_drawdown_pct'}:
        raise ValueError('研究门槛必须事先明确样本和回撤要求')
    if any(type(criteria[k]) is not int or criteria[k]<1 for k in ('min_sessions','min_closed_trades')):
        raise ValueError('最小样本数无效')
    if isinstance(criteria['max_drawdown_pct'],bool) or not isinstance(criteria['max_drawdown_pct'],(int,float)) or not 0<criteria['max_drawdown_pct']<=100:
        raise ValueError('研究回撤阈值无效')
    return spec


def register_experiment(spec, directory):
    spec=_spec(spec)
    if spec['rule_sha256']!=rule_identity(): raise ValueError('登记规则不是当前实现')
    body={'spec':spec,'registered_at':datetime.now(TZ).isoformat(),
          'validator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          'scope':'offline_development_not_independent_forward_or_recommendation'}
    envelope={'data':body,'sha256':_digest(body)}
    folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
    target=folder/(envelope['sha256']+'.json')
    with target.open('x',encoding='utf-8') as handle:json.dump(envelope,handle,ensure_ascii=False,indent=2,allow_nan=False)
    return target


def validate_account(registration, snapshot, coverage):
    if _digest(registration['data'])!=registration['sha256']: raise ValueError('登记文件哈希损坏')
    spec=_spec(registration['data']['spec'])
    if registration['data']['validator_sha256']!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('验证器版本已变，须重新登记')
    if spec['rule_sha256']!=rule_identity(): raise ValueError('规则版本已变，旧登记不适用')
    if _digest(snapshot['data'])!=snapshot['sha256'] or snapshot['sha256']!=spec['snapshot_sha256']:
        raise ValueError('行情快照身份不符')
    if _digest(coverage)!=spec['coverage_sha256']: raise ValueError('覆盖证据版本不符')
    if type(coverage.get('schema_version')) is not int or coverage['schema_version']!=1:
        raise ValueError('覆盖证据格式无效')
    store=RecentDailyStore(snapshot['data'],Path('.'))
    if spec['start'] not in store.session_index or spec['end'] not in store.session_index:
        raise ValueError('登记日期不在快照交易日中')
    sessions=store.sessions[store.session_index[spec['start']]:store.session_index[spec['end']]+1]
    config=spec['config']
    account=new_account(capital=config['initial_capital_cents']/100,rule_sha256=spec['rule_sha256'],
                        max_positions=config['max_positions'],max_holding_sessions=config['max_holding_sessions'],
                        cost_multiplier=spec['cost_multiplier'])
    pending=[];snapshots=[];decisions=[];gaps=[];audit_gaps=[]
    for day in sessions:
        idx=store.session_index[day];evidence=coverage.get('sessions',{}).get(day,{})
        scan=scan_account(store,day,evidence.get('excluded',{}),top=config['max_positions'],config=config)
        input_complete=evidence.get('screen_inputs_complete') is True and scan['stats']['incomplete']==0 and scan['stats']['universe']>0
        if not input_complete: gaps.append({'session':day,'reason':'screen_inputs_or_scope_incomplete'})
        for field in ('historical_eligibility_verified','corporate_action_coverage_verified','official_price_limits_verified'):
            if evidence.get(field) is not True: audit_gaps.append({'session':day,'field':field})
        codes=set(account['positions'])|{r['code'] for r in pending}
        bars={};signals={}
        for code in codes:
            try:
                bars[code]=read_bar(store,code,day)
                signals[code]=ma_state(store.values(code,'close',max(0,idx-19),idx))
            except (ValueError,OSError,KeyError):
                bars[code]={'valid':False,'reason':'missing_input'};signals[code]={'state':'unknown'}
            if evidence.get('corporate_action_coverage_verified') is not True or code in evidence.get('unresolved_actions',[]):
                bars[code]={**bars[code],'valid':False,'reason':'corporate_action_coverage_unknown'}
                if code in account['positions']:account['positions'][code]['quarantine']='corporate_action_coverage_unknown'
        actions=[a for a in evidence.get('actions',[]) if a.get('code') in account['positions']]
        account,snap=advance_account(account,day,bars,signals,entries=pending,actions=actions,
                                     previous_session=store.sessions[idx-1] if idx else None)
        snapshots.append(snap)
        next_entries=scan['candidates'] if input_complete else []
        decisions.append({'signal_session':day,'execution_session':store.sessions[idx+1] if idx+1<len(store.sessions) else None,
                          'candidates':next_entries,'screen_stats':scan['stats'],'entries_attempted':pending,
                          'decision_origin':'historical_reconstruction_not_actual_past_record'})
        pending=next_entries
    metric=account_metrics(account,snapshots)
    if gaps or audit_gaps:
        metric={**metric,'total_return':None,'max_drawdown':None,'valuation_complete':False}
    reasons=Counter(event.get('reason',event['type']) for s in snapshots for event in s['events'])
    prices=coverage.get('benchmark',{}).get('prices',{})
    benchmark=None
    try:
        first,last=float(prices[sessions[0]]),float(prices[sessions[-1]])
        if first>0 and last>0 and math.isfinite(first+last):benchmark=last/first-1
    except (KeyError,TypeError,ValueError,OverflowError):pass
    if spec['rule_sha256']!=rule_identity():raise ValueError('计算期间规则发生变化')
    criteria=spec['criteria']
    return {'schema_version':1,'registration_sha256':registration['sha256'],'rule_sha256':spec['rule_sha256'],
            'snapshot_sha256':snapshot['sha256'],'coverage_sha256':spec['coverage_sha256'],
            'data_class':spec['data_class'],'account':account,'snapshots':snapshots,'decisions':decisions,
            'metrics':metric,'data_gaps':gaps,'audit_gaps':audit_gaps,'event_counts':dict(reasons),
            'benchmark':{'kind':coverage.get('benchmark',{}).get('kind','unverified'),'return':benchmark,
                         'paired_excess':metric['total_return']-benchmark if metric['total_return'] is not None and benchmark is not None else None},
            'research_checks':{'sample_sessions':len(sessions)>=criteria['min_sessions'],
                'sample_closed_trades':len(account['closed_trades'])>=criteria['min_closed_trades'],
                'within_drawdown_limit':metric['max_drawdown']>=-criteria['max_drawdown_pct']/100 if metric['max_drawdown'] is not None else None},
            'statistical_promotion':'not_implemented','recommendation_eligible':False,
            'real_fills_verified':False,'independent_forward_evidence':False,
            'meaning':'Conditional retrospective development reconstruction; no profitability qualification.'}
