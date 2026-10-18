"""Explain missing observations using dated public suspension records.

These are provider-reported intervals, not an assertion of official exchange
verification. Only a full continuous session inside an explicit interval can
explain a missing daily bar; a predicted resume date never closes an interval.
"""
from datetime import datetime,time
import hashlib
import json
from pathlib import Path

from .calendar import TZ
from .daily_store import canonical_code
from .providers import safe_source_error

URL='https://datacenter-web.eastmoney.com/api/data/v1/get'


def covered_codes(payload,day):
    result=payload.get('result') or {}
    rows=result.get('data')
    if payload.get('success') is not True or not isinstance(rows,list) or result.get('count')!=len(rows):
        raise ValueError('suspension_listing_incomplete')
    opening=datetime.fromisoformat(day+'T09:30:00+08:00')
    closing=datetime.fromisoformat(day+'T15:00:00+08:00')
    output={}
    for row in rows:
        try:code=canonical_code(row['SECUCODE'])
        except (KeyError,ValueError):continue
        if row.get('SECURITY_CODE')!=code[2:]:raise ValueError('suspension_identity_conflict')
        start=datetime.fromisoformat(row['SUSPEND_START_TIME']).replace(tzinfo=TZ)
        finish=datetime.fromisoformat(row['SUSPEND_END_TIME']).replace(tzinfo=TZ) if row.get('SUSPEND_END_TIME') else None
        if finish and finish<start:raise ValueError('suspension_interval_invalid')
        if start<=opening and (finish is None or finish>=closing):output[code]=row
    return output


def explain_missing(source,missing,output):
    """One evidenced full-session halt fails the required continuous window.

    Other missing dates remain explicitly unverified, not imputed or certified.
    This is a selection exclusion, never permission to value a held position.
    """
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    checks={};evidence={}
    for day in sorted({d for days in missing.values() for d in days}):
        try:
            def collect():
                params={'sortColumns':'SUSPEND_START_DATE','sortTypes':'-1','pageSize':'500','pageNumber':'1',
                    'reportName':'RPT_CUSTOM_SUSPEND_DATA_INTERFACE','columns':'ALL','source':'WEB','client':'WEB',
                    'filter':f'(MARKET="全部")(DATETIME=\'{day}\')'}
                first=source.get(URL,params);result=first.get('result') or {}
                pages=result.get('pages');total=result.get('count')
                if type(pages) is not int or not 1<=pages<=50 or type(total) is not int:raise ValueError('suspension_pagination_invalid')
                payloads=[first];rows=list(result.get('data') or [])
                for page in range(2,pages+1):
                    p=source.get(URL,{**params,'pageNumber':str(page)});r=p.get('result') or {}
                    if p.get('success') is not True or r.get('pages')!=pages or r.get('count')!=total:raise ValueError('suspension_pagination_changed')
                    payloads.append(p);rows.extend(r.get('data') or [])
                merged={'success':first.get('success'),'result':{'data':rows,'count':total}}
                covered_codes(merged,day)
                return {'query_session':day,'source_url':URL,'pages':payloads,'merged':merged,'fetched_at':datetime.now(TZ).isoformat()}
            payload=source.cached('suspensions:'+day,3600,collect)
            evidence[day]=covered_codes(payload['merged'],day)
            body=json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False).encode('utf-8')
            path=output/(day+'.json');path.write_bytes(body)
            checks[day]={'status':'verified_provider_response','path':str(path.resolve()),'sha256':hashlib.sha256(body).hexdigest()}
        except Exception as exc:
            checks[day]={'status':'unverified','error':str(exc) if isinstance(exc,ValueError) else safe_source_error(exc)}
    explained={}
    for code,days in missing.items():
        covered=[day for day in days if code in evidence.get(day,{})]
        if covered:
            explained[code]={'reason':'reported_suspension_prevents_required_complete_window','missing_sessions':days,
                'covered_sessions':covered,'unverified_missing_sessions':[day for day in days if day not in covered],
                'scope':'provider_reported_intervals_not_official_exchange_verification',
                'evidence':{day:checks[day] for day in covered}}
    return explained,checks
