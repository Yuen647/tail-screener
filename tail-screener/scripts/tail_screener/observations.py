"""Append-only observations, separate from frozen orders and account returns."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

from .calendar import TZ


def _canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False)


def _digest(value): return hashlib.sha256(_canonical(value).encode()).hexdigest()


def update_observations(directory, *, series, session, sessions, candidates, conditions,
                        complete, snapshot_sha256, horizon=5):
    if session not in sessions or sessions!=sorted(set(sessions)) or type(horizon) is not int or horizon<1:
        raise ValueError('观察交易日或期限无效')
    if type(complete) is not bool: raise ValueError('观察完整性须为布尔值')
    folder=Path(directory);folder.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(folder/'observations.sqlite3',timeout=10)
    db.row_factory=sqlite3.Row
    try:
        db.executescript('''CREATE TABLE IF NOT EXISTS runs(
          sequence INTEGER PRIMARY KEY,input_id TEXT UNIQUE NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS cycles(code TEXT PRIMARY KEY,payload TEXT NOT NULL);''')
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT * FROM runs ORDER BY sequence DESC LIMIT 1').fetchone()
        # Verify the complete small ledger rather than trusting mutable cached state.
        last_hash=None
        for row in db.execute('SELECT * FROM runs ORDER BY sequence'):
            payload=json.loads(row['payload'])
            if _digest(payload)!=row['sha256'] or payload['previous_sha256']!=last_hash:
                raise ValueError('观察记录哈希链损坏')
            last_hash=row['sha256']
        old=json.loads(previous['payload']) if previous else None
        if old and (old['series']!=series or old['session']>session): raise ValueError('观察版本不符或日期倒退')
        tracked={row['code']:json.loads(row['payload']) for row in db.execute('SELECT * FROM cycles')}
        if old and tracked!=old['result']['cycles']: raise ValueError('观察缓存与哈希链不一致')
        selected={r['code']:r for r in candidates}
        watched=set(tracked)|set(selected)
        relevant={code:conditions.get(code,{'state':'unknown','reason':'not_evaluated'}) for code in sorted(watched)}
        inputs={'series':series,'session':session,'snapshot':snapshot_sha256,'complete':complete,
                'selected':{code:{k:row.get(k) for k in ('rank','entry_price_ceiling')} for code,row in selected.items()},
                'conditions':relevant,'horizon':horizon}
        input_id=_digest(inputs)
        existing=db.execute('SELECT payload FROM runs WHERE input_id=?',(input_id,)).fetchone()
        if existing:
            db.rollback()
            return {**json.loads(existing['payload'])['result'],'idempotent':True}
        # First complete daily observation stays frozen. Later source revisions are retained separately.
        frozen=db.execute('SELECT payload FROM runs ORDER BY sequence DESC').fetchall()
        same_complete=next((json.loads(r['payload']) for r in frozen
                            if json.loads(r['payload'])['session']==session and json.loads(r['payload'])['complete']),None)
        changes=[];index={day:i for i,day in enumerate(sessions)}
        if same_complete:
            changes.append({'type':'same_session_revision','reason':'first_complete_observation_retained'})
        else:
            for code in sorted(watched):
                condition=relevant[code];state=condition.get('state','unknown') if complete else 'unknown'
                cycle=tracked.get(code);prior=cycle.copy() if cycle else None
                if cycle and cycle['state']=='expired':
                    cycle['in_display']=code in selected;cycle['last_checked_session']=session
                    if state=='invalid': cycle.update(state='invalid',reason=condition.get('reason','screen_condition_failed'))
                    tracked[code]=cycle
                    continue
                if cycle and cycle['state']=='invalid':
                    if state!='valid' or code not in selected: continue
                    cycle=None
                if cycle is None:
                    if state!='valid' or code not in selected: continue
                    cycle={'code':code,'cycle_id':_digest([series,code,session]),'first_signal_session':session,
                           'last_confirmed_session':None,'confirmed_sessions':0,'consecutive_confirmed_sessions':0,
                           'state':'watching','gaps':[]}
                    changes.append({'code':code,'type':'first_seen'})
                cycle['in_display']=code in selected
                if prior and prior.get('in_display')!=cycle['in_display']:
                    changes.append({'code':code,'type':'entered_display' if cycle['in_display'] else 'left_display'})
                if cycle.get('last_checked_session') and cycle['last_checked_session'] in index:
                    missing=sessions[index[cycle['last_checked_session']]+1:index[session]]
                    cycle['gaps']=sorted(set(cycle['gaps'])|set(missing))
                if state=='unknown':
                    cycle['state']='unknown';cycle['reason']=condition.get('reason','data_incomplete')
                    cycle['consecutive_confirmed_sessions']=0
                elif state!='valid':
                    cycle['state']='invalid';cycle['reason']=condition.get('reason','screen_condition_failed')
                elif index[session]-index.get(cycle['first_signal_session'],index[session])>=horizon:
                    cycle['state']='expired';cycle['reason']='observation_horizon_reached'
                else:
                    previous_confirmed=cycle['last_confirmed_session']
                    if previous_confirmed!=session:
                        contiguous=previous_confirmed in index and index[session]-index[previous_confirmed]==1 and cycle['state']!='unknown'
                        cycle['confirmed_sessions']+=1
                        cycle['consecutive_confirmed_sessions']=cycle['consecutive_confirmed_sessions']+1 if contiguous else 1
                    cycle['last_confirmed_session']=session;cycle['state']='watching'
                    cycle['reason']='screen_valid_research_only'
                cycle['last_checked_session']=session;tracked[code]=cycle
                if prior and prior['state']!=cycle['state']:
                    changes.append({'code':code,'type':'state_changed','from':prior['state'],'to':cycle['state']})
        result={'status':'complete' if complete else 'data_unknown','session':session,'cycles':tracked,
                'changes':changes,'observation_horizon_sessions':horizon,'idempotent':False,
                'same_session_revision':bool(same_complete),'real_fills_verified':False}
        body={'series':series,'session':session,'complete':complete,'inputs':inputs,'result':result,
              'recorded_at':datetime.now(TZ).isoformat(),'previous_sha256':last_hash}
        db.execute('INSERT INTO runs VALUES(?,?,?,?)', (previous['sequence']+1 if previous else 0,input_id,_canonical(body),_digest(body)))
        for code,cycle in tracked.items():
            db.execute('INSERT OR REPLACE INTO cycles VALUES(?,?)',(code,_canonical(cycle)))
        db.commit();return result
    except BaseException:
        db.rollback();raise
    finally: db.close()
