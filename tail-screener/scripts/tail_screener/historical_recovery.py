"""Bounded free historical acquisition, overlap checks and field-level coverage.

Raw and explicit Tencent HFQ are retained separately. This is retrospective
research data, not PIT eligibility, a corporate-action ledger or fill evidence.
"""
import csv
from datetime import date,datetime
import hashlib
import json
import math
from pathlib import Path
import time

from .calendar import TZ,_load_backend,calendar_context
from .daily_store import canonical_code
from .fallback_daily import TENCENT_PATH,parse_sina_factors
from .market_integrity import bar_integrity_mask
from .providers import PublicSources,SourceError,safe_source_error
from .recovery_queue import RecoveryQueue,canonical
from .suspensions import explain_missing

VERSION='tencent_explicit_hfq_v1'
SINA_VERSION='sina_dated_hfq_factor_v1'
PRICE_FIELDS=('open','high','low','close')
RAW_FIELDS=(*PRICE_FIELDS,'volume_shares','amount_yuan')
NORMAL_FIELDS=(*PRICE_FIELDS,'volume','factor','vwap','amount','adjclose')
COVERAGE_FIELDS=tuple('raw_'+k for k in RAW_FIELDS)+('hfq_close',)+tuple('normalized_'+k for k in NORMAL_FIELDS)


def digest(value):return hashlib.sha256(canonical(value)).hexdigest()


def verified_sessions(start,end):
    if date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end or start>end:
        raise ValueError('历史范围无效')
    now=datetime.now(TZ);context=calendar_context(now)
    latest=context['session'] if context.get('verified') and context.get('is_session') and now.hour>=15 else context.get('previous_session')
    if not context.get('verified') or end>latest:raise ValueError('历史结束日期超过最新完整交易日或日历未核验')
    xcals,pd=_load_backend();cal=xcals.get_calendar('XSHG')
    if start<cal.first_session.date().isoformat() or end>cal.last_session.date().isoformat():raise ValueError('超出已核验日历范围')
    days=cal.sessions_in_range(start,end).strftime('%Y-%m-%d').tolist()
    if not days:raise ValueError('范围内没有交易日')
    prefix=cal.sessions[cal.sessions<=pd.Timestamp(days[0])][-253:-1].strftime('%Y-%m-%d').tolist()
    if len(prefix)<252:raise ValueError('日历缺少上市年龄判断所需前置窗口')
    return prefix+days,days


def create_plan(scope,start,end,queue,*,chunk_sessions=40,overlap=3,calendar=None,adjustment_basis='tencent_hfq'):
    if adjustment_basis not in ('tencent_hfq','sina_hfq_factor'):raise ValueError('历史复权口径不支持')
    if type(chunk_sessions) is not int or type(overlap) is not int or not 1<=overlap<chunk_sessions<=50 or chunk_sessions+overlap>60:
        raise ValueError('切片须为2至50个交易日，重叠小于切片且合计不超过60')
    if not isinstance(scope,dict) or type(scope.get('schema_version')) is not int or scope['schema_version']!=1 or not isinstance(scope.get('securities'),list):
        raise ValueError('历史范围文件格式无效')
    if not 1<=len(scope['securities'])<=256:raise ValueError('每个范围支持1至256只证券，请分批规划')
    sessions,requested=calendar if calendar is not None else verified_sessions(start,end)
    if sessions!=sorted(set(sessions)) or requested!=sorted(set(requested)) or any(d not in sessions for d in requested):
        raise ValueError('交易日历无效')
    securities={}
    for item in scope['securities']:
        code=canonical_code(item['code']);listing=item['listing_date']
        if date.fromisoformat(listing).isoformat()!=listing or listing>end:raise ValueError('上市日期无效或晚于范围')
        if code in securities:raise ValueError('范围包含重复证券')
        securities[code]={'listing_date':listing,'listing_evidence':item.get('listing_evidence','supplied_unverified')}
        if 'required_sessions' in item:
            required=item['required_sessions']
            if (not isinstance(required,list) or not required or any(not isinstance(d,str) for d in required)
                    or required!=sorted(set(required)) or any(d not in requested or d<listing for d in required)):
                raise ValueError('逐证券所需交易日须唯一、有序且位于请求范围及上市日之后')
            securities[code]['required_sessions']=required
    if sum(len(i.get('required_sessions',[d for d in requested if d>=i['listing_date']])) for i in securities.values())>250000:
        raise ValueError('单个导出最多25万证券交易日，请缩小范围')
    tasks=[]
    for code,item in sorted(securities.items()):
        days=item.get('required_sessions',[day for day in requested if day>=item['listing_date']])
        # Never join sparse windows into a count-limited HTTP request: a large
        # calendar gap could truncate its early observations at the provider.
        indices={day:i for i,day in enumerate(sessions)};runs=[]
        for day in days:
            if not runs or indices[day]!=indices[runs[-1][-1]]+1:runs.append([])
            runs[-1].append(day)
        for run in runs:
            for offset in range(0,len(run),chunk_sessions):
                chunk=run[max(0,offset-overlap):offset+chunk_sessions]
                task=RecoveryQueue.spec(code,chunk[0],chunk[-1],'historical_daily',SINA_VERSION if adjustment_basis=='sina_hfq_factor' else None)
                task_id=queue.enqueue(**task,priority=10000000+indices[chunk[0]])
                tasks.append({'id':task_id,**task,'sessions':chunk})
    plan={'schema_version':1,'normalization':SINA_VERSION if adjustment_basis=='sina_hfq_factor' else VERSION,
          'adjustment_basis':adjustment_basis,'start':start,'end':end,'sessions':sessions,
          'requested_sessions':requested,'securities':securities,'tasks':tasks,
          'chunk_sessions':chunk_sessions,'overlap_sessions':overlap,'scope_sha256':digest(scope),
          'historical_eligibility_verified':False,'corporate_actions_audited':False}
    identity=digest(plan);directory=queue.root/'plans';directory.mkdir(exist_ok=True)
    path=directory/(identity+'.json');body=canonical({'sha256':identity,'data':plan})
    try:
        with path.open('xb') as handle:handle.write(body)
    except FileExistsError:
        if path.read_bytes()!=body:raise ValueError('恢复计划身份冲突')
    return {'sha256':identity,'data':plan,'path':str(path.resolve())}


def parse_series(payload,code,start,end,*,adjusted):
    field='hfqday' if adjusted else 'day';symbol=code.lower()
    if not isinstance(payload,dict) or payload.get('code')!=0 or not isinstance(payload.get('data',{}).get(symbol),dict):
        raise ValueError('历史响应证券身份不符')
    lines=payload['data'][symbol].get(field)
    if not isinstance(lines,list) or not lines:raise ValueError('explicit_'+field+'_missing')
    result={};last=None
    for line in lines:
        if not isinstance(line,list) or len(line)<6:raise ValueError('historical_row_schema')
        day=line[0]
        if date.fromisoformat(day).isoformat()!=day or last is not None and day<=last or day>end:
            raise ValueError('historical_date_or_order')
        last=day
        if day<start:continue
        row={}
        pairs=[('open',1),('close',2),('high',3),('low',4),('volume_shares',5)]
        if not adjusted:pairs.append(('amount_yuan',8))
        for key,index in pairs:
            try:
                value=float(line[index])
                if isinstance(line[index],bool) or not math.isfinite(value) or value<=0:raise ValueError()
                if key=='volume_shares':value*=1 if code.startswith('SH688') else 100
                if key=='amount_yuan':value*=10000
                row[key]=value
            except (ValueError,TypeError,IndexError):row[key]=None
        if all(row[k] is not None for k in PRICE_FIELDS):
            if row['low']>min(row['open'],row['close']) or row['high']<max(row['open'],row['close']):
                raise ValueError('historical_price_geometry')
        result[day]=row
    return result


def normalize_rows(raw,adjusted,days):
    rows={};issues={}
    for day in days:
        r=raw.get(day,{});a=adjusted.get(day,{})
        missing=['raw_'+key for key in RAW_FIELDS if r.get(key) is None]
        if a.get('close') is None:missing.append('hfq_close')
        if missing:issues[day]=missing;continue
        if a.get('volume_shares') is not None and abs(a['volume_shares']-r['volume_shares'])>1.01:
            issues[day]=['raw_adjusted_volume_conflict'];continue
        factor=a['close']/r['close']
        row={key:r[key]*factor for key in PRICE_FIELDS}
        row.update(factor=factor,volume=r['volume_shares']/100/factor,amount=r['amount_yuan']/1000,
                   vwap=r['amount_yuan']/r['volume_shares']*factor,adjclose=a['close'])
        if not bool(bar_integrity_mask(row)['valid']):issues[day]=['bar_integrity'];continue
        rows[day]=row
    return rows,issues


def adjusted_from_factors(raw,text,code):
    factors=parse_sina_factors(text,code,adjustment='hfq');rows={}
    for day,row in raw.items():
        eligible=[(d,f) for d,f in factors if d<=day]
        if not eligible:continue
        factor=max(eligible)[1]
        rows[day]={**row,**{key:row[key]*factor if row.get(key) is not None else None for key in PRICE_FIELDS}}
    return rows


class HistoricalFetcher:
    def __init__(self,source,sessions,*,refresh=False,adjustment_basis='tencent_hfq'):
        if adjustment_basis not in ('tencent_hfq','sina_hfq_factor'):raise ValueError('历史复权口径不支持')
        self.source=source;self.sessions=sessions;self.refresh=refresh;self.adjustment_basis=adjustment_basis
        self.factor_cache={}
        self.parser_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def __call__(self,spec,deadline):
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=self.parser_sha256:raise ValueError('恢复期间解析器发生变化')
        if spec['kind']!='historical_daily':return {'state':'unsupported','error':'not_a_historical_task'}
        code=canonical_code(spec['code']);days=[d for d in self.sessions if spec['start']<=d<=spec['end']]
        if not days or len(days)>60:return {'state':'unsupported','error':'historical_chunk_exceeds_contract'}
        if (spec.get('variant')==SINA_VERSION)!=(self.adjustment_basis=='sina_hfq_factor'):
            return {'state':'unsupported','error':'adjustment_variant_mismatch'}
        responses={};errors={};parsed={};denied=False;invalid=False
        self.source.deadline=min(self.source.deadline,deadline)
        requested=(('raw',''),('hfq_factor','hfq')) if self.adjustment_basis=='sina_hfq_factor' else (('raw',''),('hfq','hfq'))
        for label,adjust in requested:
            if time.monotonic()>=deadline:errors[label]='budget_exhausted';break
            try:
                if label=='hfq_factor':
                    if code not in self.factor_cache:
                        url=f'https://finance.sina.com.cn/realstock/company/{code.lower()}/hfq.js'
                        def factor_download():
                            text=self.source.get(url,text=True);parse_sina_factors(text,code,adjustment='hfq')
                            return {'text':text,'provenance':{'source_url':url},'fetched_at':datetime.now(TZ).isoformat()}
                        self.factor_cache[code]=self.source.cached(f'{SINA_VERSION}:{code}:factors',-1 if self.refresh else 3600,factor_download)
                    responses[label]=self.factor_cache[code]
                    parsed['hfq']=adjusted_from_factors(parsed.get('raw',{}),responses[label]['text'],code)
                    continue
                def download():
                    payload,provenance=self.source._tencent_timeseries(TENCENT_PATH,
                        {'param':f'{code.lower()},day,{spec["start"]},{spec["end"]},60,{adjust}'},code.lower(),'history')
                    checked=parse_series(payload,code,spec['start'],spec['end'],adjusted=bool(adjust))
                    if set(checked)-set(days):raise ValueError('historical_non_session_observation')
                    return {'payload':payload,'provenance':provenance,'fetched_at':datetime.now(TZ).isoformat()}
                response=self.source.cached(f'{VERSION}:{code}:{spec["start"]}:{spec["end"]}:{label}',
                                             -1 if self.refresh else 86400,download)
                responses[label]=response
                parsed[label]=parse_series(response['payload'],code,spec['start'],spec['end'],adjusted=bool(adjust))
                if set(parsed[label])-set(days):raise ValueError('historical_non_session_observation')
            except Exception as exc:
                parsed.pop('hfq' if label=='hfq_factor' else label,None)
                errors[label]=str(exc) if isinstance(exc,ValueError) else safe_source_error(exc)
                invalid=invalid or isinstance(exc,ValueError)
                cause=getattr(exc,'cause',None) or {}
                if getattr(exc,'category',None) in ('http_denied','tls_error') or cause.get('category') in ('http_denied','tls_error'):
                    denied=True;break
        rows,issues=normalize_rows(parsed.get('raw',{}),parsed.get('hfq',{}),days)
        state=('unsupported' if denied else 'conflict' if invalid else 'retry' if errors
               else 'conflict' if any('conflict' in reason for values in issues.values() for reason in values)
               else 'missing' if issues else 'succeeded')
        package={'schema_version':1,'normalization':SINA_VERSION if self.adjustment_basis=='sina_hfq_factor' else VERSION,'spec':spec,'expected_sessions':days,
                 'responses':responses,'rows':rows,'issues':issues,'source_errors':errors,'result_state':state,
                 'adjustment_basis':self.adjustment_basis,'raw_units':{'volume':'shares','amount':'RMB'},
                 'source_family':'tencent+sina_factor' if self.adjustment_basis=='sina_hfq_factor' else 'tencent','independent_source_crosscheck':False,
                 'historical_eligibility_verified':False,'corporate_actions_audited':False,
                 'parser_sha256':self.parser_sha256}
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=self.parser_sha256:raise ValueError('恢复期间解析器发生变化')
        return {'state':state,'payload':canonical(package),'source':self.adjustment_basis,
                'error':canonical(errors or issues).decode() if errors or issues else None,'retry_after':60}


def export_history(plan_envelope,queue,output,*,source=None):
    plan=plan_envelope['data']
    if digest(plan)!=plan_envelope['sha256'] or plan['normalization'] not in (VERSION,SINA_VERSION):raise ValueError('历史计划版本或哈希不符')
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()):raise ValueError('历史导出目录须为空')
    tasks={t['id']:t for t in queue.tasks()};raw_by_code={};adjusted_by_code={};rows_by_code={};conflicts={};sources=[];unavailable=[];chunks={};halt_candidates={}
    for expected in plan['tasks']:
        task=tasks.get(expected['id'])
        if not task or not task.get('latest_sha256') or task['state'] not in ('succeeded','missing'):
            unavailable.append({'task_id':expected['id'],'state':task['state'] if task else 'absent'});continue
        payload=(queue.blobs/(task['latest_sha256']+'.bin')).read_bytes()
        if hashlib.sha256(payload).hexdigest()!=task['latest_sha256']:raise ValueError('恢复证据损坏')
        package=json.loads(payload)
        spec={key:expected[key] for key in ('code','start','end','kind','variant') if key in expected}
        if package.get('spec')!=spec or package.get('normalization')!=plan['normalization'] or package.get('expected_sessions')!=expected['sessions']:
            raise ValueError('恢复证据与计划不符')
        code=spec['code'];responses=package['responses']
        raw=parse_series(responses['raw']['payload'],code,spec['start'],spec['end'],adjusted=False) if 'raw' in responses else {}
        if plan['normalization']==SINA_VERSION:
            adjusted=adjusted_from_factors(raw,responses['hfq_factor']['text'],code) if 'hfq_factor' in responses else {}
        else:adjusted=parse_series(responses['hfq']['payload'],code,spec['start'],spec['end'],adjusted=True) if 'hfq' in responses else {}
        if (set(raw)|set(adjusted))-set(expected['sessions']):raise ValueError('历史证据出现非交易日日线')
        normalized,issues=normalize_rows(raw,adjusted,expected['sessions'])
        if normalized!=package['rows'] or issues!=package['issues']:raise ValueError('保存的标准化结果与原始响应不符')
        if not package.get('source_errors'):
            halt_candidates.setdefault(code,set()).update(d for d in expected['sessions'] if d not in raw)
        chunks[expected['id']]={'raw':raw,'adjusted':adjusted}
        sources.append({'task_id':expected['id'],'sha256':task['latest_sha256'],
                                                'state':task['state'],'code':code,'sessions':expected['sessions']})
        raw_store=raw_by_code.setdefault(code,{});adj_store=adjusted_by_code.setdefault(code,{})
        for day,row in raw.items():
            old=raw_store.get(day)
            if old:
                for key in RAW_FIELDS:
                    tolerance=100.01 if key=='amount_yuan' else 1.01 if key=='volume_shares' else .011
                    if old.get(key) is not None and row.get(key) is not None and abs(old[key]-row[key])>tolerance:
                        conflicts.setdefault(code,{}).setdefault(day,[]).append('raw_'+key)
            raw_store.setdefault(day,row)
        for day,row in adjusted.items():
            old=adj_store.get(day)
            if old and old.get('close') is not None and row.get('close') is not None and abs(old['close']-row['close'])>.011:
                conflicts.setdefault(code,{}).setdefault(day,[]).append('hfq_close')
            adj_store.setdefault(day,row)
        target=rows_by_code.setdefault(code,{})
        for day,row in normalized.items():target.setdefault(day,row)
    overlap_gaps={};previous={}
    for task in plan['tasks']:
        code=task['code'];prior=previous.get(code);previous[code]=task
        if prior is None:continue
        shared=set(prior['sessions'])&set(task['sessions'])
        left=chunks.get(prior['id'],{});right=chunks.get(task['id'],{})
        unverified=[day for day in shared if any(package.get(kind,{}).get(day,{}).get('close') is None
                    for package in (left,right) for kind in ('raw','adjusted'))]
        if unverified:overlap_gaps.setdefault(code,[]).extend(sorted(unverified))
    # Unverified/disagreeing adjustment boundaries cannot form a single series.
    for code in overlap_gaps:rows_by_code[code]={}
    for code,days in conflicts.items():
        if any('hfq_close' in fields for fields in days.values()):rows_by_code[code]={}
        else:
            for day in days:rows_by_code.get(code,{}).pop(day,None)
    explained={};halt_checks={}
    if source is not None:
        candidate_days=sorted({d for code,days in halt_candidates.items() for d in days
                               if d not in raw_by_code.get(code,{})})[:60]
        missing={code:sorted(d for d in days if d in candidate_days and d not in raw_by_code.get(code,{}))
                 for code,days in halt_candidates.items()}
        missing={code:days for code,days in missing.items() if days}
        if missing:explained,halt_checks=explain_missing(source,missing,out/'suspensions')
    missing_cells=0;unresolved_cells=0;halt_bars=0;valid_bars=0;expected_bars=0
    with (out/'coverage.csv').open('x',encoding='utf-8-sig',newline='') as handle:
        writer=csv.writer(handle);writer.writerow(['code','session',*COVERAGE_FIELDS,'reason'])
        for code,info in sorted(plan['securities'].items()):
            for day in info.get('required_sessions',plan['requested_sessions']):
                if day<info['listing_date']:continue
                expected_bars+=1;r=raw_by_code.get(code,{}).get(day,{});a=adjusted_by_code.get(code,{}).get(day,{})
                normalized=rows_by_code.get(code,{}).get(day,{})
                states=[('present' if r.get(k) is not None else 'missing') for k in RAW_FIELDS]
                states+=['present' if a.get('close') is not None else 'missing']
                states+=[('present' if normalized.get(k) is not None else 'missing') for k in NORMAL_FIELDS]
                is_conflict=day in conflicts.get(code,{}) or any('hfq_close' in x for x in conflicts.get(code,{}).values())
                if is_conflict:states=['conflict' if k.startswith('normalized_') or k in conflicts.get(code,{}).get(day,[]) else s for k,s in zip(COVERAGE_FIELDS,states)]
                halted=not r and not normalized and not is_conflict and day in explained.get(code,{}).get('covered_sessions',[])
                if halted:states=['reported_suspension']*len(states);halt_bars+=1
                missing_cells+=sum(s!='present' for s in states);valid_bars+=bool(normalized)
                unresolved_cells+=sum(s not in ('present','reported_suspension') for s in states)
                reason='reported_suspension_no_bar_imputed' if halted else 'source_overlap_conflict' if is_conflict else 'adjustment_overlap_unverified' if code in overlap_gaps else 'required_fields_missing' if not normalized else ''
                writer.writerow([code,day,*states,reason])
    snapshot={'schema_version':1,'sessions':plan['sessions'],
              'listing_dates':{code:i['listing_date'] for code,i in plan['securities'].items()},
              'rows':rows_by_code,'metadata':{'source':'historical_recovery','normalization':plan['normalization'],
                 'adjustment_basis':plan.get('adjustment_basis','tencent_hfq'),'historical_eligibility_verified':False,
                 'corporate_actions_audited':False,'scope':'explicit_securities_not_full_historical_market',
                 'plan_sha256':plan_envelope['sha256'],'reported_suspensions':explained}}
    snapshot_id=digest(snapshot)
    with (out/'snapshot.json').open('x',encoding='utf-8') as handle:json.dump({'sha256':snapshot_id,'data':snapshot},handle,ensure_ascii=False,allow_nan=False)
    result={'schema_version':1,'plan_sha256':plan_envelope['sha256'],'snapshot_sha256':snapshot_id,
            'price_data_complete':missing_cells==0 and expected_bars>0,'required_bars':expected_bars,'valid_bars':valid_bars,
            'acquisition_complete':unresolved_cells==0 and expected_bars>0,'reported_suspension_bars':halt_bars,
            'unresolved_field_cells':unresolved_cells,'suspension_checks':halt_checks,'valuation_ready':False,
            'missing_or_conflicting_field_cells':missing_cells,'conflicts':conflicts,'overlap_gaps':overlap_gaps,'unavailable_tasks':unavailable,
            'sources':sources,'historical_eligibility_verified':False,'corporate_actions_audited':False,
            'recommendation_eligible':False,'queue':queue.summary(),
            'coverage_file_sha256':hashlib.sha256((out/'coverage.csv').read_bytes()).hexdigest()}
    with (out/'coverage.json').open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2)
    return result
