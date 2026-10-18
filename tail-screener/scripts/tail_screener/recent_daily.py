"""Small current-market snapshots; historical archives are optional research data.

Listing dates come from the current company catalog, not from an invented
252-day history. Raw prices/volume/amount and adjusted closes are kept apart.
Snapshot hashes retain the normalization anchor across repeated daily runs.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import nullcontext
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

import numpy as np

from .calendar import _load_backend, calendar_context, TZ
from .daily_store import DailyStore, FIELDS, canonical_code
from .daily_refresh import ENDPOINT, FIELDS2, parse_daily
from .market_integrity import bar_integrity_mask
from .providers import safe_source_error, SourceError
from .bulk_data import download_lock
from .fallback_daily import DailyPairSource,parse_pair
from .suspensions import explain_missing
from .candidate_risk import instrument_eligibility


class MissingWindow(ValueError):
    def __init__(self,days,reason='recent_indicator_window_missing'):
        super().__init__(reason);self.days=list(days)


class RecentDailyStore(DailyStore):
    def __init__(self, snapshot, root):
        self.root=Path(root)
        self.snapshot=snapshot
        self.sessions=snapshot['sessions']
        if not self.sessions or self.sessions!=sorted(set(self.sessions)):raise ValueError('recent_calendar_invalid')
        self.session_index={day:i for i,day in enumerate(self.sessions)}
        self.intervals={code:[(day,self.sessions[-1])] for code,day in snapshot['listing_dates'].items()}
        self.rows=snapshot['rows']

    def codes(self,session=None):
        if session is not None and session not in self.session_index:raise ValueError('recent_unknown_session')
        return sorted(code for code,intervals in self.intervals.items()
                      if session is None or intervals[0][0]<=session<=intervals[0][1])

    def values(self,code,field,first,last):
        code=canonical_code(code)
        if field not in FIELDS or not 0<=first<=last<len(self.sessions):raise ValueError('recent_field_or_range')
        rows=self.rows.get(code,{})
        return np.array([rows.get(day,{}).get(field,np.nan) for day in self.sessions[first:last+1]],dtype=float)

    def extra_adjclose(self,code,first,last):
        rows=self.rows.get(canonical_code(code),{})
        return np.array([rows.get(day,{}).get('adjclose',np.nan) for day in self.sessions[first:last+1]],dtype=float)


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def load_recent(cache):
    cache=Path(cache);pointer=cache/'latest.json'
    if not pointer.is_file():return None
    name=json.loads(pointer.read_text(encoding='utf-8'))['snapshot']
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}-[0-9a-f]{64}\.json',name):raise ValueError('recent_pointer_invalid')
    envelope=json.loads((cache/name).read_text(encoding='utf-8'))
    if _digest(envelope['data'])!=envelope['sha256'] or not name.endswith(envelope['sha256']+'.json'):
        raise ValueError('recent_snapshot_hash_mismatch')
    return RecentDailyStore(envelope['data'],cache)


def _calendar(target):
    at=datetime.fromisoformat(target+'T16:00:00+08:00')
    if not calendar_context(at).get('verified'):raise ValueError('recent_calendar_unverified')
    xcals,pd=_load_backend();calendar=xcals.get_calendar('XSHG')
    end=calendar.sessions.get_loc(pd.Timestamp(target))
    days=[x.date().isoformat() for x in calendar.sessions[max(0,end-299):end+1]]
    if len(days)<253:raise ValueError('recent_calendar_too_short')
    return days


def collect_recent(source, quotes, target, cache, evidence_dir, *, budget=600, workers=12, progress=None):
    # Serialize normalization and publication as one transaction; a later run
    # must never overwrite the anchor used by an earlier in-flight run.
    with download_lock(Path(cache)):
        return _collect_recent(source,quotes,target,cache,evidence_dir,budget=budget,workers=workers,progress=progress)


def _collect_recent(source, quotes, target, cache, evidence_dir, *, budget, workers, progress):
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    previous=load_recent(cache)
    if previous and previous.sessions[-1]>target:raise ValueError('recent_snapshot_is_from_future')
    sessions=_calendar(target);wanted=sessions[-20:];start=sessions[-40]
    # This verified directory supplies listing dates even when Sina supplied
    # the live quote universe. Missing dates are explicit acquisition failures.
    symbols,catalog=source._eastmoney_catalog()
    if not catalog.get('complete'):raise ValueError('recent_listing_catalog_incomplete')
    listings={};listing_errors={}
    for item in source._eastmoney_rows or []:
        try:code=canonical_code(item.get('SECUCODE'))
        except ValueError:continue
        if item.get('STR_CODEA')!=code[2:]:continue
        day=str(item.get('LISTING_DATE') or '')[:10]
        try:
            if datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day or day>target:raise ValueError()
        except ValueError:
            listing_errors[code]='listing_date_missing_or_invalid';day=sessions[0]
        listings[code]=day
    by_code={}
    for quote in quotes:
        try:by_code[canonical_code(quote['code'])]=quote
        except (KeyError,ValueError):continue
    # A current quote omitted from the company directory cannot silently vanish.
    for code in set(by_code)-set(listings):
        listings[code]=sessions[0];listing_errors[code]='listing_catalog_quote_mismatch'
    output=Path(evidence_dir);output.mkdir(parents=True,exist_ok=False)
    metadata={'source':'current_listing_dates+validated_public_raw_qfq_daily','requested_session':target,
              'status':'incomplete','updated':0,'failed':dict(listing_errors),'excluded_young':[],
              'files':{},'recovery':[],'missing_sessions':{},'corporate_actions_verified':False,'historical_pit_metadata':False}
    rows={};deadline=min(source.deadline,time.monotonic()+budget);daily_source=DailyPairSource(source)
    candidates=[]
    for code,listed in listings.items():
        if code in listing_errors:continue
        if len(sessions)-1-np.searchsorted(sessions,listed)<252:
            metadata['excluded_young'].append(code);continue
        candidates.append(code)
    def cache_key(code):return f'closed-daily-pair-v1:{code}:{start}:{target}'
    if isinstance(getattr(source,'cache',None),Path):
        # Read cached inputs first. An unavailable first network symbol must
        # not hide still-valid cached responses for the rest of the universe.
        candidates.sort(key=lambda code:(not (source.cache/(hashlib.sha256(cache_key(code).encode()).hexdigest()+'.json')).is_file(),code))

    def fetch_unscoped(code):
        if time.monotonic()>=deadline:raise ValueError('recent_budget_exhausted')
        quote=by_code.get(code)
        if quote is None:raise ValueError('recent_quote_missing')
        at=datetime.fromisoformat(quote['quote_at'])
        if at.tzinfo is None:raise ValueError('recent_quote_timezone_missing')
        at=at.astimezone(TZ)
        if at.date().isoformat()<target or at>datetime.now(TZ):raise ValueError('recent_quote_date_invalid')
        same_day=at.date().isoformat()==target
        if same_day and at.hour<15:raise ValueError('recent_quote_not_closed')
        if same_day and quote.get('quote_phase')=='no_completed_trade':raise MissingWindow([target],'recent_no_completed_trade')
        def download():
            if time.monotonic()>=deadline:raise ValueError('recent_budget_exhausted')
            return daily_source.fetch(code,start,target)
        def validated_pair(pair):
            pair=daily_source.complete_adjustment(pair,code,target)
            raw,adj,vt,atol=parse_pair(pair,code,start,target)
            if target in raw and same_day:
                for field,key,tolerance,multiplier in [('close','price',.011,1),('open','open',.011,1),('high','high',.011,1),('low','low',.011,1),('volume_lots','volume',100,100),('amount_yuan','amount',10000,1)]:
                    value=float(quote[key]);expected=raw[target][field]*multiplier
                    if not np.isfinite(value) or abs(expected-value)>max(tolerance,abs(expected)*2e-6):raise ValueError('recent_quote_daily_conflict')
            return pair,raw,adj,vt,atol
        # Keys contain both window dates. Completed bars can be reused for
        # 24h, but are re-parsed and checked against the fresh dated quote on
        # every invocation. A new target date always requests a new window.
        # This avoids re-downloading 5,000 unchanged closes every hour.
        downloaded=False
        def fresh():
            nonlocal downloaded
            downloaded=True
            pair=download();validated_pair(pair)
            return pair
        try:
            pair=source.cached(cache_key(code),86400,fresh)
            pair,raw,adj,volume_tolerance,amount_tolerance=validated_pair(pair)
        except (ValueError,KeyError,TypeError):
            if downloaded:raise
            # One bounded refresh repairs a malformed or conflicting cache;
            # a failed fresh response never falls back to the rejected cache.
            pair=source.cached(cache_key(code),-1,fresh)
            pair,raw,adj,volume_tolerance,amount_tolerance=validated_pair(pair)
        missing=[day for day in wanted if day not in raw or day not in adj]
        if missing:raise MissingWindow(missing)
        scale=1.;proxy_scale=1.;anchor=None
        old_rows=previous.rows.get(code,{}) if previous else {}
        anchors=sorted(day for day in old_rows if day in raw and day in adj and day<=target)
        if old_rows and not anchors:raise ValueError('recent_normalization_anchor_missing')
        if anchors:
            anchor=anchors[-1];old=old_rows[anchor]
            for field in ('open','high','low','close'):
                if abs(old[field]/old['factor']-raw[anchor][field])>.011:raise ValueError('recent_anchor_raw_conflict')
            scale=old['close']/adj[anchor]['close'];proxy_scale=old['adjclose']/adj[anchor]['close']
        normalized={}
        for day in sorted(set(raw)&set(adj)):
            r=raw[day]
            if abs(r['volume_lots']-adj[day]['volume_lots'])>volume_tolerance or abs(r['amount_yuan']-adj[day]['amount_yuan'])>amount_tolerance:raise ValueError('recent_raw_adjusted_conflict')
            factor=adj[day]['close']*scale/r['close']
            row={field:r[field]*factor for field in ('open','high','low','close')}
            row.update(factor=factor,volume=r['volume_lots']/factor,amount=r['amount_yuan']/1000,
                       vwap=r['amount_yuan']/(r['volume_lots']*100)*factor)
            if not bool(bar_integrity_mask(row)['valid']):raise ValueError('recent_bar_integrity')
            row['adjclose']=adj[day]['close']*proxy_scale
            normalized[day]=row
        evidence={'code':code,'source':pair.get('source','eastmoney_raw_qfq'),
                  'source_urls':pair.get('source_urls',[ENDPOINT]),'listing_date':listings[code],'pair':pair,
                  'quote':quote,'quote_daily_crosscheck':'matched' if same_day else 'different_session_not_compared',
                  'response_reused':not downloaded,'response_cache_max_age_seconds':86400,
                  'anchor_session':anchor,'normalized_rows':normalized}
        body=json.dumps(evidence,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
        with (output/(code+'.json')).open('xb') as handle:handle.write(body)
        return normalized,hashlib.sha256(body).hexdigest(),pair.get('source','eastmoney_raw_qfq')

    def scoped():
        scope=getattr(source,'deadline_scope',None)
        return scope(deadline) if callable(scope) else nullcontext()

    def fetch(code):
        with scoped():
            return fetch_unscoped(code)

    started=time.monotonic();last_progress=started
    def checkpoint(completed,total,wave,force=False):
        nonlocal last_progress
        now=time.monotonic()
        if not force and completed%100 and now-last_progress<5:return
        last_progress=now
        state={'stage':'daily_inputs','requested_session':target,'wave':wave+1,'checked_this_wave':completed,
               'total_this_wave':total,'validated_inputs':len(metadata['files']),
               'unresolved':len(metadata['failed']),'elapsed_seconds':round(now-started,1),
               'remaining_seconds':round(max(0,deadline-now),1),'workers':workers,
               'updated_at':datetime.now(TZ).isoformat()}
        temporary=output/'progress.tmp'
        temporary.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8')
        temporary.replace(output/'progress.json')
        if progress:progress(f'日线输入核验 {completed}/{total}；已核验 {len(metadata["files"])}，待解决 {len(metadata["failed"])}；本阶段已用 {state["elapsed_seconds"]} 秒，剩余预算 {state["remaining_seconds"]} 秒（非入选数量）')

    # Keep only a bounded rolling set in flight. Consecutive source failures
    # stop new dispatch, while every already-started future is still collected.
    queue=list(candidates)
    checkpoint(0,len(queue),0,True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for wave in range(4):
            retry=[];pending={};offset=0;completed=0;source_failures=0;halt=False
            while pending or (offset<len(queue) and not halt):
                while not halt and offset<len(queue) and len(pending)<workers and time.monotonic()<deadline:
                    code=queue[offset];offset+=1
                    pending[pool.submit(fetch,code)]=code
                if not pending:break
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    code=pending.pop(future);completed+=1
                    try:
                        rows[code],metadata['files'][code],origin=future.result()
                        metadata.setdefault('source_counts',{})[origin]=metadata.get('source_counts',{}).get(origin,0)+1
                        metadata['failed'].pop(code,None);source_failures=0
                    except Exception as exc:
                        metadata['failed'][code]=str(exc) if isinstance(exc,ValueError) else safe_source_error(exc)
                        if isinstance(exc,MissingWindow):metadata['missing_sessions'][code]=exc.days
                        if isinstance(exc,SourceError):
                            source_failures+=1
                            cause=exc.cause or {};category=cause.get('category',exc.category)
                            retryable=category in ('proxy_error','connection_error','timeout','circuit_open') or (category=='http_error' and exc.status_code in (429,500,502,503,504))
                            if retryable:retry.append(code)
                        else:source_failures=0
                    if source_failures>=workers:halt=True
                    checkpoint(completed,len(queue),wave)
            untouched=queue[offset:]
            reason='recent_budget_exhausted' if time.monotonic()>=deadline else 'recent_source_unavailable_not_requested'
            metadata['failed'].update({code:reason for code in untouched})
            if halt and retry:retry.extend(untouched)
            checkpoint(completed,len(queue),wave,True)
            if not retry or wave==3 or time.monotonic()>=deadline:break
            delay=(2,5,15)[wave]
            if deadline-time.monotonic()<=delay:break
            metadata['recovery'].append({'wave':wave+1,'pending':len(retry),'wait_seconds':delay})
            if progress:progress(f'日线源暂不可用，保留已核验输入；{delay}秒后进行第{wave+1}次有限恢复尝试。')
            time.sleep(delay)
            queue=list(dict.fromkeys(retry))
    if metadata['missing_sessions']:
        with scoped():
            explained,checks=explain_missing(source,metadata['missing_sessions'],output/'suspensions')
        for code,detail in list(explained.items()):
            quote=by_code.get(code,{})
            if target in detail['covered_sessions'] and quote.get('volume',0)>0 and quote.get('amount',0)>0:
                # A positive observed trade contradicts a full-day halt claim.
                del explained[code]
        metadata['explained_exclusions']=explained;metadata['suspension_checks']=checks
        for code in explained:metadata['failed'].pop(code,None)
    # Retain prior valid anchors for recovery, but do not pretend they are
    # current bars: every target-day missing value stays NaN.
    if previous:
        for code,old in previous.rows.items():
            if code in listings and code not in rows:
                rows[code]={day:row for day,row in old.items() if day in sessions and day<target}
    successes=len(metadata['files'])
    scope_exclusions={code:check['name_markers'] for code,quote in by_code.items()
                      if (check:=instrument_eligibility(quote))['name_markers']}
    metadata['scope_excluded_gaps']={code:reason for code,reason in metadata['failed'].items() if code in scope_exclusions}
    metadata['required_failed']={code:reason for code,reason in metadata['failed'].items() if code not in scope_exclusions}
    metadata.update(updated=successes,requested_codes=len(listings),status='complete' if not metadata['required_failed'] else 'incomplete',
                    acquisition_status='complete' if not metadata['failed'] else 'incomplete',
                    data_session=target if successes else None,evidence_directory=str(output.resolve()))
    snapshot={'schema_version':1,'sessions':sessions,'listing_dates':listings,'rows':rows,'metadata':dict(metadata)}
    digest=_digest(snapshot);name=target+'-'+digest+'.json'
    with (cache/name).open('x',encoding='utf-8') as handle:json.dump({'sha256':digest,'data':snapshot},handle,ensure_ascii=False,allow_nan=False)
    temporary=cache/('pointer-'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps({'snapshot':name}),encoding='utf-8');temporary.replace(cache/'latest.json')
    metadata['snapshot_path']=str((cache/name).resolve());metadata['snapshot_sha256']=digest
    with (output/'manifest.json').open('x',encoding='utf-8') as handle:json.dump(metadata,handle,ensure_ascii=False,indent=2)
    return RecentDailyStore(snapshot,cache),metadata
