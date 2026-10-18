"""Reconstruct a saved paper journal in a new artifact, never amend its history.

Entries remain those actually frozen before the corresponding opening. Newly
available event evidence can repair valuation, but is labelled retrospective.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
from .account_engine import new_account,advance_account,account_metrics
from .account_journal import read_journal,locked,TZ,digest
from .account_strategy import rule_identity


def replay_journal(directory, output, action_loader):
    directory=Path(directory).resolve();output=Path(output).resolve()
    if output==directory or output.is_relative_to(directory):raise ValueError('重放结果必须写入原账户以外的新目录')
    if output.exists() and any(output.iterdir()):raise ValueError('重放输出目录必须为空')
    # Snapshot the validated local chain under its ordinary writer lock.
    with locked(directory):records=deepcopy(read_journal(directory))
    if not records:raise ValueError('没有可重放账户记录')
    old=records[-1]['payload']['account'];inputs=[];plans={}
    for record in records:
        p=record['payload'];plan=p['plan'];when=plan['timing']
        if when['prospective']:
            opening=datetime.fromisoformat(when['entry_session']+'T09:30:00+08:00')
            if datetime.fromisoformat(p['recorded_at'])>=opening:raise ValueError('原计划缺少开盘前记录')
            plans.setdefault(when['entry_session'],plan['candidates'])
        for row in p['execution_inputs']:
            if row['entry_orders']!=plans.get(row['session'],[]):raise ValueError('执行输入不匹配原先冻结的名单')
            if inputs and row['session']<=inputs[-1]['session']:raise ValueError('原执行日期重复或倒退')
            inputs.append(row)
    if not inputs:raise ValueError('缺少原始逐日执行输入，不能从账户摘要反推价格或交易')
    identity=rule_identity();state=new_account(capital=old['initial_cash_cents']/100,rule_sha256=identity,
        cost_multiplier=old['cost_multiplier'],max_positions=old['max_positions'],max_holding_sessions=old.get('max_holding_sessions'))
    packages={};snaps=[];replayed=[]
    for row in inputs:
        session=row['session'];held=set(state['positions']);entries=row['entry_orders']
        needed=held|{r['code'] for r in entries}
        bars=deepcopy(row.get('source_bars',row['bars']));actions=[];blockers={}
        for code in sorted(needed):
            if code not in packages:packages[code]=action_loader(code,inputs[0]['session'],inputs[-1]['session'])
            package=packages[code]
            failed=any('ex_session' not in e for e in package.get('errors',[]))
            unresolved=any(e['ex_session']==session and e['status']=='unresolved' for e in package.get('events',[]))
            if failed or unresolved:
                blockers[code]='distribution_source_unavailable' if failed else 'distribution_unresolved'
                if code in held:state['positions'][code]['quarantine']=state['positions'][code].get('quarantine') or blockers[code]
                bars[code]={'valid':False,'reason':blockers[code]}
            elif code in held:
                if not state['positions'][code].get('quarantine'):
                    actions.extend(a for a in package.get('actions',[]) if a['ex_session']==session)
            if code not in bars:
                bars[code]={'valid':False,'reason':'not_in_original_execution_inputs'}
                blockers[code]='not_in_original_execution_inputs'
        previous=state['last_session']
        state,snapshot=advance_account(state,session,bars,row['signals'],entries=entries,actions=actions,previous_session=previous)
        snaps.append(snapshot);replayed.append({'session':session,'actions':actions,'blockers':blockers})
    if rule_identity()!=identity:raise ValueError('重放期间代码发生变化')
    result={'kind':'retrospective_paper_journal_reconstruction','generated_at':datetime.now(TZ).isoformat(),
        'source_directory':str(directory),'source_chain_head':records[-1]['sha256'],
        'source_rule_sha256':old['rule_sha256'],'replay_rule_sha256':identity,
        'entry_policy':'original_frozen_orders_only_no_new_rankings','recommendation_eligible':False,
        'resumable_live_journal':False,'account':state,'snapshots':snaps,'metrics':account_metrics(state,snaps),
        'action_evidence':packages,'replay_inputs':replayed,
        'limit':'Retrospective reconstruction using newly retrieved evidence; not prospective strategy validation or broker reconciliation.'}
    output.mkdir(parents=True,exist_ok=True)
    with (output/'replay.json').open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2,allow_nan=False)
    with (output/'source-chain.json').open('x',encoding='utf-8') as f:json.dump(records,f,ensure_ascii=False,allow_nan=False)
    return result
