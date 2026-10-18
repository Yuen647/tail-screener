"""Independent development reconstructions from saved retrospective candidates.

The candidates are historical research outputs, never actual pre-open orders.
Repairing inputs can change subsequent simulated trades. Original accounts and
forecasts are read-only and the current whole-market selector is not validated.
"""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path

from .account_engine import new_account,advance_account,account_metrics
from .account_strategy import read_bar,ma_state,rule_identity
from .historical_recovery import digest,COVERAGE_FIELDS,verified_sessions
from .recent_daily import RecentDailyStore


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def file_sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def merge_history(paths):
    rows={};listing={};sessions=set();sources=[];basis=None
    for path in paths:
        path=Path(path);envelope=read(path);data=envelope['data']
        if digest(data)!=envelope['sha256']:raise ValueError('历史快照哈希不符')
        current=data['metadata'].get('adjustment_basis')
        if current is None or basis is not None and current!=basis:raise ValueError('不能混合复权口径')
        basis=current;sessions.update(data['sessions'])
        for code,day in data['listing_dates'].items():
            if code in listing and listing[code]!=day:raise ValueError('上市日期冲突')
            listing[code]=day
        for code,observations in data['rows'].items():
            target=rows.setdefault(code,{})
            for day,row in observations.items():
                if day in target and target[day]!=row:raise ValueError('同证券同日历史快照冲突')
                target[day]=deepcopy(row)
        sources.append({'path':str(path.resolve()),'file_sha256':file_sha(path),'snapshot_sha256':envelope['sha256']})
    if not sessions:raise ValueError('没有可合并的历史输入')
    data={'schema_version':1,'sessions':sorted(sessions),'listing_dates':listing,'rows':rows,
          'metadata':{'source':'merged_explicit_recovery','adjustment_basis':basis,'sources':sources,
                      'historical_eligibility_verified':False,'corporate_actions_audited':False,
                      'scope':'legacy_retrospective_candidate_inputs_not_historical_market'}}
    return {'sha256':digest(data),'data':data}


def reconstruct(forecasts,snapshot,action_loader,*,cost_multiplier=1,capital=100000,max_positions=5):
    if not forecasts:raise ValueError('名单为空')
    store=RecentDailyStore(snapshot['data'],Path('.'));days=[r['entry_session'] for r in forecasts]
    if days!=sorted(set(days)):raise ValueError('名单日期重复或倒退')
    _,expected=verified_sessions(days[0],days[-1])
    if days!=expected:raise ValueError('名单不是连续已核验交易日')
    for row in forecasts:
        idx=store.session_index[row['entry_session']]
        if idx==0 or row['signal_session']!=store.sessions[idx-1]:raise ValueError('名单信号日不是实际前一交易日')
    identity=rule_identity();state=new_account(capital=capital,rule_sha256=identity,cost_multiplier=cost_multiplier,
                                            max_positions=max_positions,max_holding_sessions=None)
    packages={};snaps=[];gaps=[];execution=[];needed_windows={}
    for row in forecasts:
        day=row['entry_session'];idx=store.session_index[day];held=set(state['positions'])
        needed=held|{r['code'] for r in row['candidates']};bars={};signals={};actions=[];blockers={}
        for code in sorted(needed):
            if code not in packages:packages[code]=action_loader(code,days[0],days[-1])
            package=packages[code]
            bars[code]=read_bar(store,code,day)
            signals[code]=ma_state(store.values(code,'close',max(0,idx-19),idx))
            required=store.sessions[max(0,idx-19):idx+1]
            for d in required:
                if d not in store.rows.get(code,{}):needed_windows.setdefault(code,set()).add(d)
            if not bars[code].get('valid') or signals[code]['state']=='unknown':
                gaps.append({'code':code,'session':day,'reason':'bar_or_signal_window_missing'})
            failed=any('ex_session' not in e for e in package.get('errors',[]))
            unresolved=any(e['ex_session']==day and e['status']=='unresolved' for e in package.get('events',[]))
            if failed or unresolved:
                reason='distribution_source_unavailable' if failed else 'distribution_unresolved'
                blockers[code]=reason;bars[code]={**bars[code],'valid':False,'reason':reason}
                if code in held:state['positions'][code]['quarantine']=state['positions'][code].get('quarantine') or reason
            elif code in held and not state['positions'][code].get('quarantine'):
                actions.extend(a for a in package.get('actions',[]) if a['ex_session']==day)
        previous=store.sessions[idx-1]
        try:
            after,snap=advance_account(state,day,bars,signals,entries=row['candidates'],actions=actions,previous_session=previous)
        except ValueError as exc:
            if not actions or not str(exc).startswith(('无法处理未知或零碎股','复权变化不能','现金分红缺少')):raise
            # Incompatible or fractional entitlements stay unknown. Never
            # force a round-number allocation or clear a prior quarantine.
            for action in actions:
                code=action['code'];blockers[code]=str(exc);state['positions'][code]['quarantine']='distribution_model_unresolved'
            gaps.append({'session':day,'reason':str(exc),'codes':[a['code'] for a in actions]})
            after,snap=advance_account(state,day,bars,signals,entries=row['candidates'],previous_session=previous)
            actions=[]
        state=after;snaps.append(snap);execution.append({'session':day,'actions':actions,'blockers':blockers})
    if identity!=rule_identity():raise ValueError('重建期间代码发生变化')
    complete=not gaps and not any(r['blockers'] for r in execution)
    return {'schema_version':1,'kind':'retrospective_existing_candidate_reconstruction','input_complete':complete,
        'rule_sha256':identity,'snapshot_sha256':snapshot['sha256'],'entry_policy':'saved_retrospective_candidates_no_reranking',
        'configuration':{'capital':capital,'max_positions':max_positions,'max_holding_sessions':None,'cost_multiplier':cost_multiplier},
        'account':state,'snapshots':snaps,'metrics':account_metrics(state,snaps),'data_gaps':gaps,'execution':execution,
        'additional_history_demand':{c:sorted(d) for c,d in sorted(needed_windows.items())},
        'action_evidence':{c:{'status':p.get('status'),'events':len(p.get('events',[])),'actions':len(p.get('actions',[]))} for c,p in packages.items()},
        'historical_eligibility_verified':False,'corporate_actions_audited':False,'real_fills_verified':False,
        'independent_forward_evidence':False,'current_strategy_validated':False,'recommendation_eligible':False,
        'meaning':'New prices/actions may change simulated fills. This does not repair the original immutable accounts or validate historical market rankings.'}


def write_demand(result,path):
    with Path(path).open('x',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f);writer.writerow(['code','session',*COVERAGE_FIELDS])
        for code,days in result['additional_history_demand'].items():
            for day in days:writer.writerow([code,day,*(['missing']*len(COVERAGE_FIELDS))])
