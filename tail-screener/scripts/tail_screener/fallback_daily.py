"""Public daily fallbacks with explicit price, adjustment and unit contracts.

Sohu's published history table labels volume as lots and amount as 10,000 RMB.
Tencent newfqkline supplies qfq OHLC, raw volume and amount. Its STAR volume
is shares; other supported A shares use lots. Never infer units by fitting a
desired answer. Both series must reconcile before use. Tencent raw and qfq
are one source family; agreement is not independent price confirmation.
"""
import json
import threading
import math
import re
from copy import deepcopy
from datetime import datetime

from .calendar import TZ
from .daily_refresh import ENDPOINT, FIELDS2, parse_daily
from .daily_store import canonical_code
from .providers import SourceError, safe_source_error

SOHU='https://q.stock.sohu.com/hisHq'
TENCENT_PATH='/appstock/app/newfqkline/get'


def parse_pair(pair,code,start,end):
    if pair.get('source') in ('tencent_raw+sina_qfq_factor','sohu_raw+sina_qfq_factor'):
        raw=(_parse_sohu(pair['sohu'],code,start,end) if pair['source'].startswith('sohu')
             else _parse_tencent(pair['tencent_raw'],code,start,end,adjusted=False))
        factors=parse_sina_factors(pair['sina_factors'],code)
        adjusted={}
        for day,row in raw.items():
            eligible=[(date,factor) for date,factor in factors if date<=day]
            if not eligible:raise ValueError('sina_factor_history_missing')
            factor=max(eligible)[1]
            adjusted[day]={**row,**{key:row[key]/factor for key in ('open','high','low','close')}}
        return raw,adjusted,.01,.01
    if pair.get('source')=='tencent_raw_qfq':
        return (_parse_tencent(pair['tencent_raw'],code,start,end,adjusted=False),
                _parse_tencent(pair['tencent_qfq'],code,start,end,adjusted=True),.01,100.)
    if pair.get('source')!='sohu_raw+tencent_qfq':
        return parse_daily(pair['raw'],code,start,end),parse_daily(pair['qfq'],code,start,end),.01,.01
    raw=_parse_sohu(pair['sohu'],code,start,end)
    adjusted=_parse_tencent(pair['tencent'],code,start,end,adjusted=True)
    return raw,adjusted,1.01,10000.


def _parse_sohu(source,code,start,end):
    code=canonical_code(code)
    if not isinstance(source,list) or len(source)!=1:raise ValueError('sohu_daily_schema')
    item=source[0]
    if item.get('status')!=0 or item.get('code')!='cn_'+code[2:]:raise ValueError('sohu_daily_identity')
    rows=item.get('hq')
    if not isinstance(rows,list) or not rows:raise ValueError('sohu_daily_empty')
    lines=[];last=None
    for row in rows:
        if not isinstance(row,list) or len(row)<9:raise ValueError('sohu_daily_schema')
        day=row[0]
        if last is not None and day>=last:raise ValueError('sohu_daily_unordered')
        last=day
        lines.append(','.join(map(str,[day,row[1],row[2],row[6],row[5],row[7],float(row[8])*10000])))
    return parse_daily({'data':{'code':code[2:],'klines':lines[::-1]}},code,start,end)


def _parse_tencent(payload,code,start,end,*,adjusted):
    code=canonical_code(code);symbol=code.lower();field='qfqday' if adjusted else 'day'
    stock=(payload.get('data') or {}).get(symbol) or {}
    if payload.get('code')!=0 or not isinstance(stock.get(field),list) or not stock[field]:
        raise ValueError('tencent_actual_qfq_missing' if adjusted else 'tencent_actual_raw_missing')
    lines=[];last=None
    for row in stock[field]:
        if not isinstance(row,list) or len(row)<9:raise ValueError('tencent_new_daily_schema')
        day=row[0]
        if datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day or last is not None and day<=last:
            raise ValueError('tencent_daily_date_or_order')
        last=day
        if day>end:raise ValueError('tencent_daily_after_target')
        # newfqkline may return count bars before the requested start.
        if day<start:continue
        lots=float(row[5])/(100 if code.startswith('SH688') else 1)
        lines.append(','.join(map(str,[day,*row[1:5],lots,float(row[8])*10000])))
    return parse_daily({'data':{'code':code[2:],'klines':lines}},code,start,end)


def parse_sina_factors(text,code,*,adjustment='qfq'):
    if adjustment not in ('qfq','hfq'):raise ValueError('sina_factor_adjustment')
    prefix='var '+canonical_code(code).lower()+adjustment+'='
    if not isinstance(text,str) or not text.startswith(prefix):raise ValueError('sina_factor_identity')
    value,consumed=json.JSONDecoder().raw_decode(text[len(prefix):])
    tail=text[len(prefix)+consumed:].strip().lstrip(';').strip()
    if tail and not re.fullmatch(r'/\*[\s\S]*\*/',tail):raise ValueError('sina_factor_trailing_data')
    rows=value.get('data')
    if not isinstance(rows,list) or not rows or value.get('total')!=len(rows) or len(rows)>2000:
        raise ValueError('sina_factor_schema')
    result=[];seen=set();last=None
    for row in rows:
        if adjustment=='hfq' and set(row)!={'d','f'}:raise ValueError('sina_hfq_unsupported_fields')
        if isinstance(row.get('f'),bool):raise ValueError('sina_factor_value')
        day=row['d'];factor=float(row['f'])
        if datetime.strptime(day,'%Y-%m-%d').date().isoformat()!=day or day in seen or last is not None and day>=last:
            raise ValueError('sina_factor_date_or_order')
        if not math.isfinite(factor) or factor<=0:raise ValueError('sina_factor_value')
        result.append((day,factor));seen.add(day);last=day
    return result


class DailyPairSource:
    def __init__(self,source):
        self.source=source;self.lock=threading.Lock();self.fallback_reason=None

    def complete_adjustment(self,pair,code,end):
        if pair.get('source') not in ('tencent_raw_qfq','sohu_raw+tencent_qfq'):return pair
        symbol=canonical_code(code).lower()
        stock=(pair.get('tencent_qfq',pair.get('tencent',{})).get('data') or {}).get(symbol) or {}
        if stock.get('qfqday'):return pair
        if not stock.get('day'):return pair
        pair=deepcopy(pair);url=f'https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js'
        factor_text=self.source.cached(f'sina-qfq-factor:{symbol}:{end}',3600,lambda:self.source.get(url,text=True))
        parse_sina_factors(factor_text,code)
        pair.update(source=('sohu_raw' if pair['source'].startswith('sohu') else 'tencent_raw')+'+sina_qfq_factor',sina_factors=factor_text,
                    adjustment_basis='raw_OHLC_divided_by_dated_sina_qfq_factor')
        pair['source_urls'].append(url)
        return pair

    def fetch(self,code,start,end):
        code=canonical_code(code)
        with self.lock:reason=self.fallback_reason
        if reason is None:
            try:
                pair={'source':'eastmoney_raw_qfq'}
                for adjust,label in [(0,'raw'),(1,'qfq')]:
                    pair[label]=self.source.get(ENDPOINT,{'fields1':'f1,f2,f3,f4,f5,f6','fields2':FIELDS2,
                        'klt':'101','fqt':str(adjust),'secid':('1' if code.startswith('SH') else '0')+'.'+code[2:],
                        'beg':start.replace('-',''),'end':end.replace('-','')})
                pair['fetched_at']=datetime.now(TZ).isoformat()
                return pair
            except SourceError as exc:
                category=(exc.cause or {}).get('category',exc.category)
                if category in ('http_denied','tls_error') or exc.status_code in (401,403):raise
                if category not in ('timeout','connection_error','proxy_error','circuit_open','http_error'):raise
                reason=safe_source_error(exc)
                with self.lock:self.fallback_reason=reason
        # Tencent also publishes raw amounts. Use its explicit raw and qfq
        # fields when Sohu is unavailable; this is one source family, not
        # independent confirmation. Quote agreement is still checked upstream.
        symbol=code.lower();pair={'source':'tencent_raw_qfq','source_urls':[],
            'independent_price_crosscheck':False,'fallback_reason':reason,
            'unit_contract':{'volume':'shares' if code.startswith('SH688') else 'lots','amount':'10000_RMB'}}
        for suffix,adjust in [('raw',''),('qfq','qfq')]:
            params={'param':f'{symbol},day,{start},{end},60,{adjust}'}
            payload,provenance=self.source._tencent_timeseries(TENCENT_PATH,params,symbol,'history')
            pair['tencent_'+suffix]=payload;pair['source_urls'].append(provenance['source_url'])
        pair['fetched_at']=datetime.now(TZ).isoformat()
        return self.complete_adjustment(pair,code,end)
