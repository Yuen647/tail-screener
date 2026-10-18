"""Append-only, hash-chained paper records; local integrity, not notarization."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, date, time
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .account_engine import new_account, advance_account, account_metrics
from .account_strategy import rule_identity, scan_account, read_bar

TZ=ZoneInfo('Asia/Shanghai')


def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(value):return hashlib.sha256(canonical(value)).hexdigest()
def _now():return datetime.now(TZ)


@contextmanager
def locked(directory):
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('账户目录不允许符号链接')
    directory.mkdir(parents=True,exist_ok=True)
    lock=directory/'.writer.lock'
    try:
        with lock.open('x') as f:f.write(str(os.getpid()))
    except FileExistsError as exc:raise ValueError('账户正在写入或上次写入未正常结束；本次不覆盖') from exc
    try:yield directory
    finally:lock.unlink(missing_ok=True)


def read_journal(directory):
    records=[];previous=None;previous_time=None
    for index,path in enumerate(sorted(Path(directory).glob('*.json'))):
        if path.is_symlink() or path.name!=f'{index:08d}.json':raise ValueError('账户记录缺失或顺序损坏')
        item=json.loads(path.read_text(encoding='utf-8'));p=item['payload']
        if item['sha256']!=digest(p) or p['previous_sha256']!=previous or p['sequence']!=index:
            raise ValueError('账户哈希链损坏，停止更新')
        stamp=datetime.fromisoformat(p['recorded_at'])
        if stamp.tzinfo is None or previous_time is not None and stamp<previous_time:raise ValueError('账户记录时间倒退')
        previous_time=stamp;previous=item['sha256'];records.append(item)
    return records


def append_record(directory, records, body):
    """Caller must hold writer lock. Caller cannot choose record time or hash."""
    directory=Path(directory);now=_now()
    if now.tzinfo is None:raise ValueError('记录时钟缺少时区')
    if records and now<datetime.fromisoformat(records[-1]['payload']['recorded_at']):raise ValueError('本地时钟倒退')
    body=__import__('copy').deepcopy(body)
    if body.get('plan',{}).get('timing',{}).get('prospective'):
        plan=body['plan'];plan['timing']=timing(plan['signal_session'],plan['timing']['entry_session'],now)
    payload={**body,'sequence':len(records),'previous_sha256':records[-1]['sha256'] if records else None,'recorded_at':now.isoformat()}
    item={'payload':payload,'sha256':digest(payload)}
    temp=directory/f'.{len(records):08d}.tmp';destination=directory/f'{len(records):08d}.json'
    try:
        with temp.open('xb') as f:f.write(canonical(item));f.flush();os.fsync(f.fileno())
        os.link(temp,destination)
    finally:temp.unlink(missing_ok=True)
    return item


def timing(signal_session, next_session, now):
    close=datetime.combine(date.fromisoformat(signal_session),time(15),TZ)
    if now<close:return {'prospective':False,'reason':'signal_not_closed','entry_session':None}
    if next_session is None:return {'prospective':False,'reason':'next_session_unverified','entry_session':None}
    if next_session<=signal_session:raise ValueError('下一交易日无效')
    opening=datetime.combine(date.fromisoformat(next_session),time(9,30),TZ)
    if now>=opening:return {'prospective':False,'reason':'entry_window_missed_no_backfill','entry_session':None}
    return {'prospective':True,'reason':'recorded_before_next_open','entry_session':next_session}


def update_forward_account(store, scan, evidence, directory, *, next_session=None, entry_block_reasons=None, config=None, action_loader=None):
    """Advance only actual archived sessions; entry lists require prior records.

    Exit rule is registered as a standing mechanical instruction. If the user
    does not invoke daily, later data reconstructs conditional paper exits; it
    does not pretend the source was downloaded or a real order sent that day.
    """
    current=scan['session'];identity=rule_identity()
    if config:
        from .account_config import config_identity
        identity=config_identity(identity,config)
    with locked(directory) as folder:
        records=read_journal(folder)
        if records:
            state=records[-1]['payload']['account']
            if state['rule_sha256']!=identity:raise ValueError('策略/账户代码版本变化，必须保存为独立账户，不接续旧成绩')
            if state['last_session']>current:raise ValueError('行情日期早于已有账户，不回滚')
        else:state=new_account(rule_sha256=identity,**({'capital':config['initial_capital_cents']/100,
                              'max_positions':config['max_positions'], 'max_holding_sessions':config['max_holding_sessions']} if config else {}))
        plans={}
        for item in records:
            p=item['payload'];plan=p['plan']
            if plan['timing']['prospective']:
                target=plan['timing']['entry_session']
                if datetime.fromisoformat(p['recorded_at'])>=datetime.combine(date.fromisoformat(target),time(9,30),TZ):
                    raise ValueError('伪造的事前入场标记')
                plans.setdefault(target,plan['candidates'])
        snapshots=[];inputs=[];action_evidence={}
        first=(store.session_index[state['last_session']]+1) if state['last_session'] else store.session_index[current]
        if action_loader and first<=store.session_index[current]:
            needed=set(state['positions'])|{r['code'] for day,rows in plans.items()
                                           if store.sessions[first]<=day<=current for r in rows}
            for code in sorted(needed):
                action_evidence[code]=action_loader(code,store.sessions[first],current)
        for idx in range(first,store.session_index[current]+1):
            session=store.sessions[idx]
            # Only held securities need standing exit signals; no retroactive
            # whole-market entry selection is performed for skipped days.
            held=list(state['positions']);entry=plans.get(session,[])
            codes=set(held)|{r['code'] for r in entry}
            bars={};signals={}
            from .account_strategy import ma_state
            for code in codes:
                try:
                    bars[code]=read_bar(store,code,session)
                    signals[code]=ma_state(store.values(code,'close',max(0,idx-19),idx))
                except (OSError,ValueError,KeyError):
                    bars[code]={'valid':False,'reason':'read_error'};signals[code]={'state':'unknown'}
            from copy import deepcopy
            source_bars=deepcopy(bars)
            actions=[];event_blockers={}
            for code in codes:
                package=action_evidence.get(code)
                if package is None:continue
                todays=[a for a in package.get('actions',[]) if a['ex_session']==session and code in held]
                unresolved=[e for e in package.get('events',[]) if e['ex_session']==session and e['status']=='unresolved']
                acquisition_failed=any('ex_session' not in e for e in package.get('errors',[]))
                if unresolved or acquisition_failed:
                    event_blockers[code]='distribution_unresolved' if unresolved else 'distribution_source_unavailable'
                    if code in held:
                        # Unknown coverage can be repaired on a fresh replay;
                        # never release cash or clear an old unresolved event.
                        state['positions'][code]['quarantine']=state['positions'][code].get('quarantine') or event_blockers[code]
                    bars[code]={**bars.get(code,{}),'valid':False,'reason':event_blockers[code]}
                elif code in held and state['positions'][code].get('quarantine') and todays:
                    event_blockers[code]='historical_event_requires_replay'
                else:actions.extend(todays)
            state,snap=advance_account(state,session,bars,signals,entries=entry,actions=actions,
                                      previous_session=store.sessions[idx-1] if idx else None)
            snapshots.append(snap);inputs.append({'session':session,'bars':bars,'source_bars':source_bars,'signals':signals,
                                                  'entry_orders':entry,'actions':actions,'action_blockers':event_blockers})
        now=_now();when=timing(current,next_session,now)
        blockers=list(entry_block_reasons or [])
        if any(p.get('errors') for p in action_evidence.values()):blockers.append('account_distribution_evidence_incomplete')
        if state['nav_cents'] is None:blockers.append('account_nav_unverified')
        if blockers:
            when={'prospective':False,'reason':'data_incomplete_no_entry_plan','entry_session':None,
                  'blockers':list(dict.fromkeys(blockers))}
        if when['entry_session'] in plans:
            when={'prospective':False,'reason':'entry_day_already_frozen_first_plan_retained','entry_session':None}
        frozen_history={}
        idx=store.session_index[current]
        for row in scan['candidates']:
            history=store.history(row['code'],current,count=20)
            # Preserve exact primitive inputs for selected names; the complete
            # universe source is identified by archive hash, not duplicated.
            frozen_history[row['code']]={'dates':list(history.index),
                'fields':{key:[float(x) if __import__('math').isfinite(float(x)) else None for x in history[key]]
                          for key in ('open','high','low','close','volume','factor','vwap','amount')}}
        body={'schema_version':1,'kind':'conditional_paper_account_update','rule_sha256':identity,
              'configuration':config,
              'account':state,'snapshots':snapshots,'execution_inputs':inputs,
              'plan':{'signal_session':current,'timing':when,'candidates':scan['candidates'],
                      'candidate_inputs':frozen_history,'universe_stats':scan['stats']},
              'source_evidence':evidence,'corporate_action_evidence':action_evidence,
              'limit':'Local hash chain detects changes; not independent timestamp proof, real fills or complete PIT data.'}
        # Recheck the real wall clock at publication, never trust report time.
        body['plan']['timing']=timing(current,next_session,_now()) if when['prospective'] else when
        item=append_record(folder,records,body)
        all_snapshots=[s for r in records for s in r['payload']['snapshots']]+snapshots
        return {'record':item,'record_path':str(folder/f'{len(records):08d}.json'),
                'account':state,'metrics':account_metrics(state,all_snapshots),
                'records':len(records)+1,'prospective_entry_plans':sum(r['payload']['plan']['timing']['prospective'] for r in records)+int(item['payload']['plan']['timing']['prospective']),
                'new_events':[e for s in snapshots for e in s['events']],
                'history_snapshots':all_snapshots}
