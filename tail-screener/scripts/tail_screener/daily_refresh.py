"""Closed-session daily updates with immutable inputs and explicit missingness.

Eastmoney raw bars supply shares/RMB; its qfq close series supplies the entire
MA window. Rebase that window at the archive anchor, never append a raw quote
to adjusted history. Neither adjustment ratios nor this adapter certify cash
dividends, share distributions, or executable trades.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import time

import numpy as np

from .calendar import calendar_context, TZ
from .daily_store import DailyStore, FIELDS, canonical_code
from .market_integrity import bar_integrity_mask

ENDPOINT = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'
FIELDS2 = 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61'


def parse_daily(payload, code, start, end):
    code=canonical_code(code)
    data=payload.get('data') if isinstance(payload,dict) else None
    if not isinstance(data,dict) or data.get('code')!=code[2:]:
        raise ValueError('daily_identity_mismatch')
    values=data.get('klines')
    if not isinstance(values,list) or not values:raise ValueError('daily_empty')
    rows={};last=None
    for value in values:
        if not isinstance(value,str):raise ValueError('daily_schema')
        parts=value.split(',')
        if len(parts)<7:raise ValueError('daily_schema')
        day=parts[0]
        if datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day or not start<=day<=end:
            raise ValueError('daily_date_outside_request')
        if last and day<=last:raise ValueError('daily_duplicate_or_unordered')
        last=day
        row=dict(zip(('open','close','high','low','volume_lots','amount_yuan'),map(float,parts[1:7])))
        if not all(math.isfinite(v) and v>0 for v in row.values()):raise ValueError('daily_invalid_value')
        if row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):
            raise ValueError('daily_geometry')
        rows[day]=row
    return rows


def required_sessions(anchor, target, *, max_gap=10):
    """Use the verified exchange calendar, never a weekday approximation."""
    days=[];current=anchor
    while current<target and len(days)<max_gap:
        context=calendar_context(datetime.fromisoformat(current+'T16:00:00+08:00'))
        if not context.get('verified') or not context.get('next_session'):
            raise ValueError('refresh_calendar_unverified')
        following=context['next_session']
        if following<=current or following>target:raise ValueError('refresh_target_not_session')
        days.append(following);current=following
    if current!=target:raise ValueError('refresh_gap_exceeds_limit')
    return days


def reconcile_window(base, code, target, raw, adjusted, quote, added):
    """Replace the complete indicator window from one dated adjusted series."""
    anchor=base.sessions[-1];anchor_index=len(base.sessions)-1
    if anchor not in raw or anchor not in adjusted:raise ValueError('refresh_anchor_missing')
    sessions=base.sessions+added
    wanted=sessions[-20:]
    if len(wanted)<20 or any(day not in raw or day not in adjusted for day in set(wanted+added)):
        raise ValueError('refresh_window_missing')
    for day in set(raw)&set(adjusted):
        if abs(raw[day]['volume_lots']-adjusted[day]['volume_lots'])>.01 or abs(raw[day]['amount_yuan']-adjusted[day]['amount_yuan'])>.01:
            raise ValueError('refresh_raw_adjusted_volume_conflict')
    old=base.history(code,anchor,count=1).iloc[-1]
    if not bool(old['valid_ohlcv']):raise ValueError('refresh_invalid_anchor')
    for field in ('open','high','low','close'):
        if abs(float(old['raw_'+field])-raw[anchor][field])>.011:
            raise ValueError('refresh_anchor_price_conflict')
    if abs(float(old['raw_volume_shares'])-raw[anchor]['volume_lots']*100)>max(100,raw[anchor]['volume_lots']*100*2e-6):
        raise ValueError('refresh_anchor_volume_conflict')
    if abs(float(old['raw_amount_yuan'])-raw[anchor]['amount_yuan'])>max(10000,raw[anchor]['amount_yuan']*2e-6):
        raise ValueError('refresh_anchor_amount_conflict')
    at=datetime.fromisoformat(quote['quote_at'])
    if at.tzinfo is None or at.astimezone(TZ).date().isoformat()!=target or (at.hour,at.minute)<(15,0):
        raise ValueError('refresh_quote_not_closed')
    if canonical_code(quote['code'])!=code:raise ValueError('refresh_quote_identity')
    for field,key in [('close','price'),('open','open'),('high','high'),('low','low')]:
        value=quote.get(key)
        if isinstance(value,bool) or value is None or not math.isfinite(float(value)) or abs(raw[target][field]-float(value))>.011:
            raise ValueError('refresh_quote_price_conflict')
    for field,key,tolerance in [('volume_lots','volume',100),('amount_yuan','amount',10000)]:
        expected=raw[target][field]*(100 if field=='volume_lots' else 1)
        value=quote.get(key)
        if value is None or not math.isfinite(float(value)) or abs(expected-float(value))>max(tolerance,expected*2e-6):
            raise ValueError('refresh_quote_activity_conflict')
    scale=float(old['close'])/adjusted[anchor]['close']
    # Preserve the original adjustment proxy's scale for conditional accounts.
    # Missing proxy evidence stays missing and will block simulated fills.
    from .account_strategy import extra_adjclose
    proxy=extra_adjclose(base,code,anchor_index,anchor_index)[0]
    proxy_scale=proxy/adjusted[anchor]['close'] if np.isfinite(proxy) and proxy>0 else None
    rows={}
    for day in sorted(set(wanted+added+[anchor])):
        r=raw[day];close=adjusted[day]['close']*scale;factor=close/r['close']
        row={field:r[field]*factor for field in ('open','high','low','close')}
        row.update(factor=factor,volume=r['volume_lots']/factor,amount=r['amount_yuan']/1000,
                   vwap=r['amount_yuan']/(r['volume_lots']*100)*factor)
        if not bool(bar_integrity_mask(row)['valid']):raise ValueError('refresh_bar_integrity')
        row['adjclose']=adjusted[day]['close']*proxy_scale if proxy_scale else None
        rows[day]=row
    return rows


class UpdatedDailyStore(DailyStore):
    """Read-through immutable overlay; archive bytes are never edited."""
    def __init__(self, base, added, updates, codes):
        self.base=base;self.root=base.root
        self.sessions=base.sessions+list(added)
        self.session_index={day:i for i,day in enumerate(self.sessions)}
        self.updates=updates;self.current_codes=sorted(set(codes))
        self.intervals=dict(base.intervals)
        for code in self.current_codes:
            if code in self.intervals:
                self.intervals[code]=list(self.intervals[code])+[(added[0],added[-1])]
            else:self.intervals[code]=[(added[0],added[-1])]

    def codes(self,session=None):
        if session is None:return sorted(self.intervals)
        if session in self.base.session_index:return self.base.codes(session)
        if session not in self.session_index:raise ValueError('unknown_overlay_session')
        return self.current_codes

    def values(self,code,field,first,last):
        code=canonical_code(code)
        if field not in FIELDS or not 0<=first<=last<len(self.sessions):raise ValueError('overlay_field_or_range')
        result=np.full(last-first+1,np.nan)
        old_last=min(last,len(self.base.sessions)-1)
        if old_last>=first:result[:old_last-first+1]=self.base.values(code,field,first,old_last)
        for day,row in self.updates.get(code,{}).items():
            index=self.session_index[day]
            if first<=index<=last:result[index-first]=row[field]
        return result

    def extra_adjclose(self,code,first,last):
        from .account_strategy import extra_adjclose
        result=np.full(last-first+1,np.nan)
        old_last=min(last,len(self.base.sessions)-1)
        if old_last>=first:result[:old_last-first+1]=extra_adjclose(self.base,code,first,old_last)
        for day,row in self.updates.get(code,{}).items():
            index=self.session_index[day]
            if first<=index<=last:result[index-first]=row['adjclose'] if row['adjclose'] is not None else np.nan
        return result


def refresh_daily_store(base, source, quotes, target, output, *, workers=4, budget=240, progress=None):
    """Persist each completed pair before use; failures remain explicit gaps."""
    metadata={'requested_session':target,'base_session':base.sessions[-1], 'status':'not_needed',
              'updated':0,'failed':{},'source':'eastmoney:raw-and-qfq-daily',
              'indicator_window':'last20 replaced from one qfq series; rebased at archive anchor',
              'corporate_actions_verified':False}
    if not target or target<=base.sessions[-1]:return base,metadata
    try:added=required_sessions(base.sessions[-1],target)
    except ValueError as exc:
        metadata.update(status='blocked',reason=str(exc));return base,metadata
    by_code={canonical_code(q['code']):q for q in quotes}
    codes=sorted(set(base.codes(base.sessions[-1]))|set(by_code))
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    start=base.sessions[max(0,len(base.sessions)-40)]
    deadline=min(source.deadline,time.monotonic()+budget)
    updates={};files={}

    def fetch(code):
        if time.monotonic()>=deadline:raise ValueError('refresh_budget_exhausted')
        if code not in by_code:raise ValueError('refresh_quote_missing')
        if code not in base.intervals:raise ValueError('refresh_archive_history_missing')
        payloads={};parsed={}
        for adjust,label in [(0,'raw'),(1,'qfq')]:
            if time.monotonic()>=deadline:raise ValueError('refresh_budget_exhausted')
            params={'fields1':'f1,f2,f3,f4,f5,f6','fields2':FIELDS2,'klt':'101','fqt':str(adjust),
                    'secid':('1' if code.startswith('SH') else '0')+'.'+code[2:],
                    'beg':start.replace('-',''),'end':target.replace('-','')}
            payloads[label]=source.get(ENDPOINT,params)
            parsed[label]=parse_daily(payloads[label],code,start,target)
        rows=reconcile_window(base,code,target,parsed['raw'],parsed['qfq'],by_code[code],added)
        evidence={'code':code,'requested_session':target,'fetched_at':datetime.now(TZ).isoformat(),
                  'source_url':ENDPOINT,'raw':payloads['raw'],'qfq':payloads['qfq'],
                  'quote':by_code[code],'normalized_rows':rows}
        body=json.dumps(evidence,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
        path=output/(code+'.json')
        with path.open('xb') as handle:handle.write(body)
        return rows,hashlib.sha256(body).hexdigest()

    # Bounded chunks avoid submitting thousands of tasks after a failed source.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0,len(codes),workers):
            if time.monotonic()>=deadline:
                metadata['failed'].update({code:'refresh_budget_exhausted' for code in codes[offset:]});break
            pending={pool.submit(fetch,code):code for code in codes[offset:offset+workers]}
            for future in as_completed(pending):
                code=pending[future]
                try:updates[code],files[code]=future.result()
                except Exception as exc:
                    from .providers import safe_source_error
                    metadata['failed'][code]=str(exc) if isinstance(exc,ValueError) else safe_source_error(exc)
            completed=min(offset+workers,len(codes))
            if progress and (completed%100<workers or completed==len(codes)):
                progress(f'日线补齐已检查 {completed}/{len(codes)}，成功 {len(updates)}，缺项 {len(metadata["failed"])}')
    metadata.update(status='complete' if not metadata['failed'] else 'incomplete',updated=len(updates),
                    requested_codes=len(codes),added_sessions=added,files=files,
                    evidence_directory=str(output.resolve()))
    with (output/'manifest.json').open('x',encoding='utf-8') as handle:
        json.dump(metadata,handle,ensure_ascii=False,indent=2,allow_nan=False)
    # Even partial refreshes use the target session; failed symbols have NaNs,
    # not yesterday's values relabelled as today. The caller must gate plans.
    return (UpdatedDailyStore(base,added,updates,codes) if updates else base),metadata
