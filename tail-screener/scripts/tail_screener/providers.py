"""Bounded public adapters. Preserve source times; retrieval is not quote time."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import math
import re
import threading
import time
import uuid
from urllib.parse import urlencode, urlparse

import requests

from .engine import TZ, is_a_share, number, stamp


class BudgetExpired(RuntimeError):
    pass


class SourceError(RuntimeError):
    """Safe to put in reports: never contains query strings or proxy credentials."""
    def __init__(self, host, category, status_code=None, cause=None, *, request_failed=None, route_attempts=None):
        self.host, self.category, self.status_code = host, category, status_code
        self.cause = dict(cause) if cause else None
        self.request_failed = category not in ('circuit_open','budget_expired') if request_failed is None else request_failed
        self.route_attempts = list(route_attempts or [])
        suffix = f' (cause={self.cause["category"]})' if self.cause else ''
        super().__init__(f'{host}: {category}' + (f' (HTTP {status_code})' if status_code else '') + suffix)


def source_diagnostic(exc, source):
    if isinstance(exc, SourceError):
        detail={'source':source, 'host':exc.host, 'category':exc.category, 'status_code':exc.status_code}
        if exc.cause:
            detail['cause']=dict(exc.cause)
        if exc.route_attempts:
            detail.update(request_failed=exc.request_failed,route_attempts=[dict(r) for r in exc.route_attempts])
        return detail
    if isinstance(exc, BudgetExpired):
        category = 'budget_expired'
    elif isinstance(exc, requests.Timeout):
        category = 'timeout'
    elif isinstance(exc, requests.ConnectionError):
        category = 'connection_error'
    else:
        category = 'invalid_response' if isinstance(exc, (ValueError, TypeError, KeyError)) else 'source_error'
    detail={'source':source, 'category':category, 'error_type':type(exc).__name__}
    if getattr(exc,'route_attempts',None):
        detail.update(request_failed=exc.request_failed,route_attempts=[dict(r) for r in exc.route_attempts])
    return detail


def safe_source_error(exc):
    """Do not put arbitrary transport/parser exception text in saved artifacts."""
    if isinstance(exc, SourceError):
        return f'SourceError: {exc}'
    if isinstance(exc, BudgetExpired):
        return 'BudgetExpired: 采集预算用尽，缺项保留为未核验'
    return f'{type(exc).__name__}: {source_diagnostic(exc, "request")["category"]}'


def parallel(items, fn, workers):
    values, errors = {}, {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(fn, item): item for item in items}
        for future in as_completed(pending):
            item = pending[future]
            try:
                values[item] = future.result()
            except Exception as exc:
                errors[str(item)] = safe_source_error(exc)
    return values, errors


def tencent_volume_fields(raw_volume, symbol, source_field):
    """Normalize the endpoint's documented-by-observation board-specific contract.

    Tencent qt[6] and fqkline day[5] report STAR (sh688...) volume in shares;
    the other supported Shanghai/Shenzhen A-share boards report 100-share lots.
    Never infer or repair this unit from price, amount, or another provider.
    """
    multiplier=1 if symbol.startswith('sh688') else 100
    raw=number(raw_volume)
    return {'volume':raw*multiplier if raw is not None else None,
            'volume_raw':raw,'volume_unit':'shares',
            'source_volume_unit':'shares' if multiplier==1 else 'lots_100_shares',
            'volume_multiplier':multiplier,'source_volume_field':source_field}


def parse_quote(text, symbol):
    match = re.search(r'v_' + re.escape(symbol) + r'="([^"\r\n]*)";', text)
    if not match:
        raise ValueError('腾讯返回的股票代码不匹配')
    f = match[1].split('~')
    if len(f) < 49 or f[2] != symbol[2:]:
        raise ValueError('腾讯行情字段或身份无效')
    at = datetime.strptime(f[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
    fields = {'price':3, 'prev_close':4, 'open':5, 'volume':6, 'bid':9, 'bid_size':10,
              'ask':19, 'ask_size':20, 'change_pct':32, 'high':33, 'low':34, 'amount':37,
              'turnover_pct':38, 'limit_up':47, 'limit_down':48}
    out = {k: number(f[i]) for k,i in fields.items()}
    for required in ('price', 'prev_close', 'amount', 'volume', 'change_pct'):
        if out[required] is None:
            raise ValueError(f'报价缺字段 {required}')
    if out['price'] <= 0 or out['prev_close'] <= 0:
        raise ValueError('无有效成交价')
    out['amount'] *= 10000   # 腾讯A股成交额单位万元
    out.update(tencent_volume_fields(f[6],symbol,'qt[6]'))
    out['vwap'] = out['amount']/out['volume'] if out['volume'] else None
    out.update(code=f'{symbol[2:]}.{symbol[:2].upper()}', name=f[1], quote_at=at.isoformat(),
               source='tencent:qt', source_url=f'https://qt.gtimg.cn/q={symbol}')
    return out


def parse_minutes(payload, symbol):
    data = ((payload.get('data') or {}).get(symbol) or {}).get('data') or {}
    day = datetime.strptime(str(data.get('date')), '%Y%m%d').date()
    previous = 0.0
    rows = []
    for item in data.get('data') or []:
        fields = item.split()
        if len(fields) != 4:
            raise ValueError('分时结构变化，不能验证成交额')
        clock = datetime.strptime(fields[0], '%H%M').time()
        cumulative, price = number(fields[3]), number(fields[1])
        if cumulative is None or cumulative < previous or price is None or price <= 0:
            raise ValueError('分时累计成交额下降或价格无效')
        rows.append({'at':datetime.combine(day, clock, TZ).isoformat(), 'close':price,
                     'amount':cumulative-previous})
        previous = cumulative
    if len({r['at'] for r in rows}) != len(rows):
        raise ValueError('分时重复时间')
    if rows != sorted(rows,key=lambda r:r['at']):
        raise ValueError('分时顺序错误')
    return rows


def parse_sina_quote(text, symbol):
    match=re.search(r'(?:var\s+)?hq_str_'+re.escape(symbol)+r'="([^"\r\n]*)";',text)
    if not match:
        raise ValueError('新浪交叉报价身份不匹配')
    f=match[1].split(',')
    if len(f)<32:
        raise ValueError('新浪交叉报价字段不足')
    at=stamp(f[30]+'T'+f[31])
    out={k:number(f[i]) for k,i in {'price':3,'prev_close':2,'volume':8,'amount':9}.items()}
    if at is None or any(v is None or v<=0 for v in out.values()):
        raise ValueError('新浪交叉报价时间或量价无效')
    return dict(out,code=f'{symbol[2:]}.{symbol[:2].upper()}',name=f[0],quote_at=at.isoformat(),
                source='sina:hq',source_url='https://hq.sinajs.cn/list='+symbol)


def parse_notices(payload, code, now):
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('list'), list):
        raise ValueError('公告接口结构缺失')
    items = []
    for r in data['list']:
        if code.split('.')[0] not in {x.get('stock_code') for x in r.get('codes', [])}:
            raise ValueError('公告接口返回了其他股票')
        when = r.get('display_time') or r.get('eiTime') or str(r.get('notice_date',''))[:10]
        when = re.sub(r'(\d{2}:\d{2}:\d{2}):(\d{3})$', r'\1.\2', str(when))
        at = stamp(when)
        article = str(r.get('art_code') or '')
        if not isinstance(r.get('title'), str) or not r['title'].strip():
            raise ValueError('公告标题缺失')
        if not re.fullmatch(r'AN\d+', article):
            raise ValueError('缺少可追溯的公告编号')
        items.append({'title':str(r.get('title') or ''), 'published_at':at.isoformat() if at else None,
                      'url':f'https://data.eastmoney.com/notices/detail/{code.split(".")[0]}/{article}.html',
                      'source':'eastmoney:exchange_notice_mirror'})
    dates = [stamp(r['published_at']) for r in items if stamp(r['published_at'])]
    total = number(data.get('total_hits'))
    # A complete 14-day window needs an older boundary or a verified total count.
    complete = bool(dates and min(dates) <= now-timedelta(days=14)) or (total is not None and total == len(items))
    truncated = not complete
    return {'code':code, 'verified':bool(items) and not truncated,
            'source':'eastmoney:ann', 'items':items, 'truncated':truncated,
            'fetched_at':now.isoformat(), 'scope':'近14日公告标题筛查；重大事项需全文复核'}


from .calendar import calendar_context  # Public compatibility export; no network.


class PublicSources:
    def __init__(self, cache: Path, budget=240, workers=8):
        self.cache = cache
        self.workers = workers
        self.deadline = time.monotonic() + budget
        self.cache.mkdir(parents=True,exist_ok=True)
        self._lock = threading.Lock()
        self._request_counts = {}
        self._request_elapsed = {}
        self._timeout_profiles = {}
        self._host_failures = {}
        self._host_failure_causes = {}
        self._host_retry_at = {}
        self._host_probes = set()
        self._eastmoney_rows = None
        self._eastmoney_attempted = False
        self._session_local = threading.local()
        self._sessions = []
        self._fallback_events = []
        self._history_preference = None
        self._history_primary_probe = False

    @property
    def fallback_events(self):
        with self._lock:
            return [dict(event) for event in self._fallback_events]

    def history_health(self):
        """Read transport scheduling state without probing or resetting it.

        Both routes are Tencent, not independent sources. Explicit primary
        access/TLS refusals block fallback policy even if the mirror has not
        been attempted. Proxy errors retain their own category: they are not
        reclassified as timeouts or as proof of a permanent source outage.
        """
        now=time.monotonic()
        hosts=('web.ifzq.gtimg.cn','proxy.finance.qq.com')
        with self._lock:
            primary_cause=dict(self._host_failure_causes.get(hosts[0]) or {})
            policy_blocked=(primary_cause.get('category') in ('http_denied','tls_error')
                            or primary_cause.get('status_code') in (401,403))
            routes=[]
            for host in hosts:
                cause=dict(self._host_failure_causes.get(host) or {})
                delay=max(0.,self._host_retry_at.get(host,0)-now)
                blocked=(cause.get('category') in ('http_denied','tls_error')
                         or cause.get('status_code') in (401,403))
                if blocked or policy_blocked:
                    state='blocked'
                    if policy_blocked and not blocked:
                        cause=dict(primary_cause,policy='primary_refusal_blocks_fallback')
                    delay=0.
                elif host in self._host_probes:
                    state='probe_in_flight'
                    delay=max(.05,delay)
                elif self._host_failures.get(host,0)>=8 and delay>0:
                    state='cooldown'
                else:
                    state='ready'
                    delay=0.
                if host==hosts[0] and self._history_preference and state!='blocked':
                    preference=self._history_preference
                    preference_delay=max(0.,preference['retry_at']-now)
                    if self._history_primary_probe:
                        state,delay='probe_in_flight',max(.05,delay)
                    elif preference_delay>0:
                        state,delay='cooldown',max(delay,preference_delay)
                    if not cause:
                        cause=dict(preference['cause'])
                routes.append({'host':host,'state':state,'retry_after_seconds':round(delay,3),
                               'cause':cause,'source_family':'tencent'})
        usable=[route for route in routes if route['state']!='blocked']
        return {'routes':routes,'retry_after_seconds':min((r['retry_after_seconds'] for r in usable),default=0.),
                'all_routes_blocked':not usable,'source_family':'tencent',
                'independent_sources':1}

    def _history_route_choice(self):
        """Prefer a proven mirror briefly, with one scheduled primary probe."""
        now=time.monotonic()
        with self._lock:
            cause=dict(self._host_failure_causes.get('web.ifzq.gtimg.cn') or {})
            if cause.get('category') in ('http_denied','tls_error') or cause.get('status_code') in (401,403):
                raise SourceError('web.ifzq.gtimg.cn',cause.get('category','http_denied'),cause.get('status_code'),request_failed=False)
            preference=self._history_preference
            if preference is None:
                return False,False,None
            if now<preference['retry_at'] or self._history_primary_probe:
                return True,False,dict(preference)
            self._history_primary_probe=True
            return False,True,dict(preference)

    def _request(self, url, **kwargs):
        # requests.get creates and closes a new Session on every call. Reuse
        # connections within each worker without sharing mutable Sessions across
        # threads; keep the environment's proxy and certificate settings intact.
        session = getattr(self._session_local, 'session', None)
        if session is None:
            session = requests.Session()
            self._session_local.session = session
            with self._lock:
                self._sessions.append(session)
        return session.get(url, **kwargs)

    def close(self):
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            session.close()

    @property
    def diagnostics(self):
        with self._lock:
            return [dict(host=host, category=category, status_code=status, attempts=count,
                         elapsed_ms=round(self._request_elapsed.get((host,category,status),0)*1000,1),
                         timeout_profiles=[dict(connect_seconds=c,read_seconds=r,
                                                error_type=e,attempts=n)
                             for (h,k,s,c,r,e),n in self._timeout_profiles.items()
                             if (h,k,s)==(host,category,status)])
                    for (host,category,status),count in sorted(self._request_counts.items(), key=lambda x:str(x[0]))]

    def _record_request(self, host, category, status=None, elapsed=0, timeout=None, error_type=None):
        with self._lock:
            key = (host, category, status)
            self._request_counts[key] = self._request_counts.get(key, 0) + 1
            self._request_elapsed[key] = self._request_elapsed.get(key,0) + elapsed
            if timeout:
                profile=(*key,round(timeout[0],3),round(timeout[1],3),error_type)
                self._timeout_profiles[profile]=self._timeout_profiles.get(profile,0)+1

    def _admit_host(self, host):
        """Gate new logical requests, never their already-admitted retries."""
        now=time.monotonic()
        with self._lock:
            if self._host_failures.get(host,0) < 8:
                return 'normal'
            if host in self._host_probes or now < self._host_retry_at.get(host,0):
                return None
            self._host_probes.add(host)
            return 'probe'

    def _complete_host(self, host, succeeded, mode, error=None):
        with self._lock:
            if mode=='probe':
                self._host_probes.discard(host)
            if isinstance(error, BudgetExpired):
                return
            if succeeded:
                self._host_failures[host]=0
                self._host_retry_at.pop(host,None)
                self._host_failure_causes.pop(host,None)
            else:
                # Count only exhausted logical requests. A wave of eight first
                # timeouts must still receive all eight promised retries.
                self._host_failures[host]=self._host_failures.get(host,0)+1
                previous=self._host_failure_causes.get(host) or {}
                # Keep an explicit access/TLS refusal through the failure wave;
                # a concurrent timeout must not erase the reason for blocking.
                if isinstance(error,SourceError) and previous.get('category') not in ('http_denied','tls_error'):
                    self._host_failure_causes[host]={'category':error.category,'status_code':error.status_code}
                if self._host_failures[host]>=8:
                    self._host_retry_at[host]=time.monotonic()+1.0

    def _wait_for_recovery(self, host):
        with self._lock:
            retry_at=self._host_retry_at.get(host,0)
        now=time.monotonic()
        delay=max(0,min(1.0,retry_at-now))
        if delay and self.deadline-now > delay+.3:
            time.sleep(delay)

    @property
    def request_deadline(self):
        return min(self.deadline, getattr(self._session_local, 'deadline', self.deadline))

    @contextmanager
    def deadline_scope(self, deadline):
        """Cap this thread's requests without shortening other stages or workers."""
        previous = getattr(self._session_local, 'deadline', self.deadline)
        self._session_local.deadline = min(previous, deadline)
        try:
            yield
        finally:
            self._session_local.deadline = previous

    def get(self, url, params=None, text=False):
        host = urlparse(url).hostname or ''
        if self.request_deadline-time.monotonic() <= 0:
            self._record_request(host,'budget_expired')
            raise BudgetExpired('采集预算用尽，缺项保留为未核验')
        mode=self._admit_host(host)
        if mode is None:
            with self._lock:
                cause=dict(self._host_failure_causes.get(host) or {})
            self._record_request(host,'circuit_open',cause.get('status_code'))
            raise SourceError(host,'circuit_open',cause.get('status_code'),cause=cause)
        succeeded=False
        error=None
        try:
            result=self._get_with_retries(url,host,params,text)
            succeeded=True
            return result
        except (SourceError, BudgetExpired) as exc:
            error=exc
            raise
        finally:
            self._complete_host(host,succeeded,mode,error)

    def _get_with_retries(self, url, host, params, text):
        referer = ('https://data.eastmoney.com/' if host.endswith('.eastmoney.com') else
                   'https://gu.qq.com/' if host.endswith(('.gtimg.cn','.qq.com')) else 'https://finance.sina.com.cn/')
        for attempt in range(2):
            remaining = self.request_deadline - time.monotonic()
            if remaining <= 0:
                self._record_request(host,'budget_expired')
                raise BudgetExpired('采集预算用尽，缺项保留为未核验')
            status, retryable, error_type = None, False, None
            started = time.monotonic()
            response = None
            try:
                # A second attempt with the same short timeout repeats the same
                # failure on a reachable but slow route. Broaden only the retry,
                # and never allocate more than the remaining collection budget.
                connect, read = (4, 6) if attempt == 0 else (8, 10)
                scale = min(1, remaining/(connect+read))
                timeout = (connect*scale, read*scale)
                response = self._request(url, params=params,
                    timeout=timeout, stream=True,
                    headers={'User-Agent':'Mozilla/5.0', 'Referer':referer})
                response.raise_for_status()
                # Check bytes while downloading, not after .content has already
                # allocated an unlimited body. Socket timeouts still bound each
                # blocked read; deadline checks also stop streams between chunks.
                chunks, size = [], 0
                stream = iter(response.iter_content(chunk_size=16_384))
                while True:
                    if time.monotonic() >= self.request_deadline:
                        raise BudgetExpired('采集预算用尽，缺项保留为未核验')
                    try:
                        chunk = next(stream)
                    except StopIteration:
                        break
                    if time.monotonic() >= self.request_deadline:
                        raise BudgetExpired('采集预算用尽，缺项保留为未核验')
                    size += len(chunk)
                    if size > 8_000_000:
                        raise ValueError('行情返回体超出限制')
                    chunks.append(chunk)
                body = b''.join(chunks)
                result = body.decode('gbk') if text else json.loads(body)
            except BudgetExpired:
                self._record_request(host,'budget_expired',elapsed=time.monotonic()-started,timeout=timeout)
                raise
            except requests.exceptions.SSLError:
                category = 'tls_error'
            except requests.exceptions.ProxyError:
                category, retryable = 'proxy_error', True
            except requests.Timeout as exc:
                category, retryable = 'timeout', True
                error_type=type(exc).__name__
            except requests.ConnectionError:
                category, retryable = 'connection_error', True
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                category = 'http_denied' if status in (401,403) else 'http_error'
                retryable = status in (429,500,502,503,504)
            except (ValueError, UnicodeError):
                category = 'invalid_response'
            except requests.RequestException:
                category = 'request_error'
            else:
                self._record_request(host,'ok',elapsed=time.monotonic()-started,timeout=timeout)
                return result
            finally:
                if response is not None:
                    response.close()
            self._record_request(host,category,status,elapsed=time.monotonic()-started,
                                 timeout=timeout,error_type=error_type)
            if attempt == 0 and retryable and self.request_deadline-time.monotonic() > .3:
                time.sleep(.15)
                continue
            raise SourceError(host,category,status) from None

    def _catalog_pages(self, numbers, fetch, host):
        """One bounded low-concurrency recovery pass, preserving successful pages."""
        started = time.monotonic()
        numbers = list(numbers)
        pages, errors = parallel(numbers, fetch, self.workers)
        original_errors = dict(errors)
        transient = ('timeout', 'ReadTimeout', 'ConnectTimeout', 'connection_error',
                     'ConnectionError', 'proxy_error', 'circuit_open',
                     'HTTP 429', 'HTTP 500', 'HTTP 502', 'HTTP 503', 'HTTP 504')
        retry = [n for n in numbers if n not in pages and
                 any(token in errors.get(str(n), '') for token in transient)]
        recovered = {}
        if retry and self.deadline-time.monotonic() > .3:
            self._wait_for_recovery(host)
            # Let one recovery probe finish before releasing other requests;
            # otherwise an open circuit can discard the whole retry wave.
            recovered, failed = parallel(retry[:1], fetch, 1)
            with self._lock:
                circuit_closed = self._host_failures.get(host,0) < 8
            if recovered or circuit_closed:
                more, more_failed = parallel(retry[1:], fetch, min(2,self.workers))
                recovered.update(more)
                failed.update(more_failed)
            for n in recovered:
                errors.pop(str(n), None)
            errors.update(failed)
            pages.update(recovered)
        recovery = {'initial_failed_pages':len(original_errors),
                    'recovered_pages':len(recovered), 'remaining_failed_pages':len(errors),
                    'original_errors':original_errors,
                    'elapsed_seconds':round(time.monotonic()-started,3)}
        return pages, errors, recovery

    def cached(self, key, ttl, fn):
        path = self.cache / (hashlib.sha256(key.encode()).hexdigest()+'.json')
        now = datetime.now(TZ)
        try:
            stored = json.loads(path.read_text(encoding='utf-8'))
            fetched = stamp(stored.get('fetched_at'))
            if fetched and 0 <= (now-fetched).total_seconds() <= ttl:
                return stored['data']
        except (OSError, ValueError, KeyError):
            pass
        data = fn()
        tmp = path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        tmp.write_text(json.dumps({'fetched_at':now.isoformat(),'data':data},ensure_ascii=False),encoding='utf-8')
        tmp.replace(path)
        return data

    def _sina_catalog(self):
        base = 'https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.'
        total = int(self.get(base+'getHQNodeStockCount', {'node':'hs_a'}))
        if not 1000 < total < 12000:
            raise ValueError('全市场总数异常')
        def page(n):
            result = self.get(base+'getHQNodeData',{'page':n,'num':100,'sort':'symbol','asc':1,'node':'hs_a'})
            if not isinstance(result,list):
                raise ValueError('全市场分页失效')
            return result
        pages, errors, recovery = self._catalog_pages(
            range(1,math.ceil(total/100)+1),page,'vip.stock.finance.sina.com.cn')
        raw = [r for p in sorted(pages) for r in pages[p]]
        unique = {r.get('symbol'):r for r in raw if isinstance(r,dict) and re.fullmatch(r'(?:sh|sz|bj)\d{6}',str(r.get('symbol','')))}
        symbols=sorted(s for s in unique if is_a_share(f'{s[2:]}.{s[:2].upper()}'))
        invalid = len(raw)-sum(isinstance(r,dict) and bool(re.fullmatch(r'(?:sh|sz|bj)\d{6}',str(r.get('symbol','')))) for r in raw)
        duplicates = len(raw)-invalid-len(unique)
        complete = not errors and not invalid and not duplicates and len(unique) == total
        return symbols, {'source':'sina:symbols', 'reported_total':total, 'received_unique':len(unique),
                         'duplicates':duplicates, 'invalid_rows':invalid, 'errors':errors,
                         'complete':complete, 'received_coverage':min(1,len(unique)/total),
                         'page_recovery':recovery}

    def _eastmoney_catalog(self):
        self._eastmoney_attempted = True
        url='https://datacenter.eastmoney.com/securities/api/data/v1/get'
        params={'reportName':'RPT_F10_BASIC_ORGINFO','columns':'SECUCODE,EM2016,STR_CODEA,LISTING_DATE',
                'filter':'(SECURITY_TYPE_CODE="058001001")','pageSize':500,'pageNumber':1,
                'sortColumns':'SECUCODE','sortTypes':'1','source':'F10','client':'PC'}
        first=self.get(url,params)
        result=first.get('result') or {}
        total, page_count = result.get('count'), result.get('pages')
        if (not first.get('success') or type(total) is not int or not 1000 < total < 120000
                or type(page_count) is not int or not 1 <= page_count <= 250
                or not isinstance(result.get('data'),list) or not result['data']):
            raise ValueError('备用股票目录结构或总数异常')
        def page(n):
            d=self.get(url,{**params,'pageNumber':n})
            r=d.get('result') or {}
            if (not d.get('success') or r.get('count') != total or r.get('pages') != page_count
                    or not isinstance(r.get('data'),list) or not r['data']):
                raise ValueError('备用目录分页缺失或总数变化')
            return r['data']
        pages,errors,recovery=self._catalog_pages(
            range(2,page_count+1),page,'datacenter.eastmoney.com')
        raw=result['data']+[r for n in sorted(pages) for r in pages[n]]
        valid=[r for r in raw if isinstance(r,dict) and 'STR_CODEA' in r
               and re.fullmatch(r'[A-Z0-9_-]+\.[A-Z]{2,5}',str(r.get('SECUCODE','')))]
        unique={r['SECUCODE']:r for r in valid}
        invalid, duplicates = len(raw)-len(valid), len(valid)-len(unique)
        # This company catalog also contains delisted stocks and former codes.
        # STR_CODEA is its current A-share identity; historical aliases do not qualify.
        active={code:r for code,r in unique.items() if is_a_share(code) and r.get('STR_CODEA') == code[:6]}
        symbols=sorted(code[-2:].lower()+code[:6] for code in active)
        complete=not errors and not invalid and not duplicates and len(unique)==total and 1000<len(symbols)<12000
        if complete:
            self._eastmoney_rows=list(unique.values())
        return symbols, {'source':'eastmoney:RPT_F10_BASIC_ORGINFO:current_a_codes',
                         'reported_total':total,'received_unique':len(unique), 'duplicates':duplicates,
                         'invalid_rows':invalid,'errors':errors,'complete':complete,
                         'received_coverage':min(1,len(unique)/total),
                         'page_recovery':recovery,
                         'excluded_historical_a_codes':sum(is_a_share(code) and code not in active for code in unique),
                         'scope':'完整A股公司目录分页；仅保留当前STR_CODEA与沪深SECUCODE一致的代码'}

    def universe(self, *, allow_preopen=False, include_inactive=False):
        start = datetime.now(TZ)
        started = time.monotonic()
        catalogs, attempts = [], []
        for name, fetch in [('sina:symbols',self._sina_catalog),
                            ('eastmoney:RPT_F10_BASIC_ORGINFO:current_a_codes',self._eastmoney_catalog)]:
            try:
                symbols, catalog = fetch()
                catalogs.append((symbols,catalog))
                attempts.append(dict(catalog, eligible_symbols=len(symbols)))
                if catalog['complete']:
                    break
            except Exception as exc:
                attempts.append(dict(source_diagnostic(exc,name),complete=False))
        # No on-disk symbol or quote cache can stand in for a live, verified catalog.
        if catalogs:
            symbols,catalog=max(catalogs,key=lambda item:(item[1]['complete'],len(item[0])))
        else:
            symbols,catalog=[],{'source':'unavailable','reported_total':0,'received_unique':0,
                                'duplicates':0,'received_coverage':0,'complete':False,'errors':{}}
        errors=dict(catalog.get('errors') or {})
        if not catalog['complete']:
            errors['catalog']='股票目录未完整核验；本次仅可观察，不能认定全市场无合格股票'
        catalog_seconds = time.monotonic()-started
        # Catalogs only supply identities. Market statistics require dated live quotes.
        batches=[tuple(symbols[i:i+70]) for i in range(0,len(symbols),70)]
        def quote_batch(batch):
            text=self.get('https://qt.gtimg.cn/q='+','.join(batch),text=True)
            values, failures=[],{}
            for symbol in batch:
                try:
                    row=parse_quote(text,symbol)
                    if any(number(row.get(k)) is None for k in ('open','high','low','turnover_pct')):
                        raise ValueError('市场统计字段缺失')
                    at = stamp(row['quote_at'])
                    auction = bool(allow_preopen and at and (9, 15) <= (at.hour, at.minute) < (9, 30)
                                   and row['volume'] == 0 and row['amount'] == 0
                                   and row['open'] == row['high'] == row['low'] == 0)
                    inactive=bool(include_inactive and at and row['volume']==row['amount']==0
                                  and row['open']==row['high']==row['low']==0)
                    if row['amount'] < 0 or row['volume'] < 0 or (not auction and not inactive and not row['low'] <= row['price'] <= row['high']):
                        raise ValueError('市场报价量价结构异常')
                    if auction:
                        row.update(quote_phase='preopen_indication', has_completed_trade=False)
                    elif inactive:
                        row.update(quote_phase='no_completed_trade',has_completed_trade=False)
                    values.append(row)
                except (ValueError,TypeError) as exc:
                    failures[symbol]=str(exc)
            return values, failures
        quotes, quote_errors=parallel(batches,quote_batch,self.workers)
        failed_batches=[batch for batch in batches if batch not in quotes]
        recovery=[]
        if failed_batches and self.deadline-time.monotonic() > .3:
            # A reachable quote host can reject or time out on one large batch.
            # Retry only those identities in smaller chunks; retain the denominator.
            self._wait_for_recovery('qt.gtimg.cn')
            chunks=[batch[i:i+20] for batch in failed_batches for i in range(0,len(batch),20)]
            # Complete the first small chunk as a recovery probe before releasing
            # the rest. Otherwise they could all be rejected while it is in flight.
            recovered,recovery_errors=parallel(chunks[:1],quote_batch,1)
            rest,rest_errors=parallel(chunks[1:],quote_batch,self.workers)
            recovered.update(rest)
            recovery_errors.update(rest_errors)
            for batch in failed_batches:
                parts=[batch[i:i+20] for i in range(0,len(batch),20)]
                available=[recovered[part] for part in parts if part in recovered]
                if available:
                    quotes[batch]=([row for rows,_ in available for row in rows],
                                   {symbol:error for _,invalid in available for symbol,error in invalid.items()})
                missing=[part for part in parts if part not in recovered]
                original_error=quote_errors.pop(str(batch),None)
                if missing:
                    quote_errors[str(batch)]=f'小批次恢复后仍有 {sum(len(part) for part in missing)} 只报价采集失败'
                recovery.append({'symbols':len(batch),'chunk_size':20,'original_error':original_error,
                                 'recovered_symbols':sum(len(recovered[part][0]) for part in parts if part in recovered),
                                 'remaining_errors':{str(part):recovery_errors[str(part)] for part in missing}})
        # A successful HTTP response may still omit individual symbols. Treat
        # those identities as missing rather than silently accepting the batch;
        # retry only the missing fields once, leaving the denominator unchanged.
        invalid_before={s:e for batch in quotes.values() for s,e in batch[1].items()}
        if invalid_before and self.deadline-time.monotonic() > .3:
            missing_symbols=sorted(invalid_before)
            chunks=[tuple(missing_symbols[i:i+20]) for i in range(0,len(missing_symbols),20)]
            self._wait_for_recovery('qt.gtimg.cn')
            recovered,recovery_errors=parallel(chunks,quote_batch,min(2,self.workers))
            owner={symbol:batch for batch in batches for symbol in batch}
            for chunk,(good,bad) in recovered.items():
                for row in good:
                    symbol=row['code'][-2:].lower()+row['code'][:6]
                    if symbol in invalid_before:
                        quotes[owner[symbol]][0].append(row)
                        quotes[owner[symbol]][1].pop(symbol,None)
                for symbol,error in bad.items():
                    quotes[owner[symbol]][1][symbol]=error
            recovery.append({'symbols':len(missing_symbols),'chunk_size':20,
                             'reason':'partial_response',
                             'recovered_symbols':sum(len(v[0]) for v in recovered.values()),
                             'remaining_errors':recovery_errors})
        rows=[r for batch in batches if batch in quotes for r in quotes[batch][0]]
        invalid={s:e for batch in quotes.values() for s,e in batch[1].items()}
        end=datetime.now(TZ)
        fresh=[r for r in rows if stamp(r['quote_at']) and 0 <= (end-stamp(r['quote_at'])).total_seconds() <= 120]
        market_quote_at=min((r['quote_at'] for r in fresh),default=None)
        return rows, {'reported_total':catalog['reported_total'], 'received_unique':catalog['received_unique'],
                      'duplicates':catalog['duplicates'],'invalid_rows':catalog.get('invalid_rows',0),
                      'coverage':1 if catalog['complete'] else 0,'received_coverage':catalog['received_coverage'],
                      'catalog_complete':catalog['complete'],'catalog_attempts':attempts,
                      'fallback_used':catalog['source'].startswith('eastmoney:'),
                      'errors':errors, 'fetched_at':start.isoformat(),
                      'eligible_symbols':len(symbols),'valid_quote_coverage':len(rows)/max(1,len(symbols)),
                      'preopen_indication_count':sum(r.get('quote_phase') == 'preopen_indication' for r in rows),
                      'dated_quote_coverage':len(fresh)/max(1,len(symbols)), 'market_quote_at':market_quote_at,
                      'quote_errors':{str(k):v for k,v in quote_errors.items()},'invalid_quotes':invalid,
                      'quote_batch_recovery':recovery,
                      'stage_seconds':{'catalog':round(catalog_seconds,3),
                                       'quotes':round(time.monotonic()-started-catalog_seconds,3)},
                      'source':catalog['source']+'+tencent:dated_quotes',
                      'source_diagnostics':self.diagnostics,
                      'time_note':'全市场统计使用腾讯完整日期报价；有效报价覆盖与新鲜报价覆盖单独计算。'}

    def industries(self):
        def fetch_all():
            if self._eastmoney_rows is None:
                if self._eastmoney_attempted:
                    # Repeating a whole directory here cannot repair the quote
                    # universe already selected from the partial directory.
                    raise SourceError('datacenter.eastmoney.com','catalog_incomplete')
                _,catalog=self._eastmoney_catalog()
                if not catalog['complete']:
                    raise ValueError('行业映射目录未完整核验')
            mapping={r['SECUCODE']:'-'.join(r['EM2016'].split('-')[:2]) for r in self._eastmoney_rows
                     if r.get('EM2016') and is_a_share(r.get('SECUCODE')) and r.get('STR_CODEA')==r['SECUCODE'][:6]}
            return {'mapping':mapping,'fetched_at':datetime.now(TZ).isoformat(),'source':'eastmoney:RPT_F10_BASIC_ORGINFO'}
        return fetch_all() if self._eastmoney_rows is not None else self.cached('industries-v2',86400,fetch_all)

    def quote(self, code):
        symbol=code[-2:].lower()+code[:6]
        return parse_quote(self.get('https://qt.gtimg.cn/q='+symbol,text=True),symbol)

    def _tencent_timeseries(self, path, params, symbol, kind):
        """The public Tencent frontend mirror is the same source family.

        Its historical OHLC and raw volume were checked against the original
        endpoint's saved responses. It cannot substitute for independent Sina
        quotes, and it is never used to evade an explicit 401/403/TLS failure.
        """
        primary='https://web.ifzq.gtimg.cn'+path
        alternate='https://proxy.finance.qq.com/ifzqgtimg'+path
        url, route = primary, 'direct'
        event=None
        preferred,probe,preference=(self._history_route_choice() if kind=='history' else (False,False,None))
        fallback_cause=None
        primary_failure=None

        def request_alternate(reason, skipped=False):
            nonlocal url,route,event
            url,route=alternate,'official_frontend'
            event={'code':f'{symbol[2:]}.{symbol[:2].upper()}','kind':kind,
                   'primary_error':reason,
                   'alternate_url':alternate+'?'+urlencode(params),'outcome':'attempted',
                   'source_family':'tencent','independent_source':False}
            if skipped:
                event.update(primary_skipped=True,route_policy='recent_successful_fallback',
                             primary_retry_after_seconds=round(max(0.,preference['retry_at']-time.monotonic()),3))
            with self._lock:
                self._fallback_events.append(event)
            try:
                return self.get(alternate,params)
            except Exception as fallback_exc:
                if primary_failure is not None:
                    # Preserve failures that occurred during this call even
                    # when the final route was rejected locally by its circuit.
                    # A previous circuit cause is not a new network attempt.
                    last=source_diagnostic(fallback_exc,'history_route')
                    last['request_failed']=getattr(fallback_exc,'request_failed',not isinstance(fallback_exc,BudgetExpired))
                    fallback_exc.request_failed=primary_failure['request_failed'] or last['request_failed']
                    fallback_exc.route_attempts=[dict(primary_failure),last]
                with self._lock:
                    event.update(outcome='failed',error=safe_source_error(fallback_exc))
                raise

        try:
            if preferred:
                payload=request_alternate(preference['error'],skipped=True)
            else:
                try:
                    payload=self.get(primary,params)
                except SourceError as exc:
                    primary_failure=source_diagnostic(exc,'history_route')
                    primary_failure['request_failed']=exc.request_failed
                    cause=exc.cause or {}
                    if exc.category=='circuit_open' and (
                            cause.get('category') in ('http_denied','tls_error')
                            or cause.get('status_code') in (401,403)):
                        raise
                    transient=exc.category in ('timeout','connection_error','proxy_error','circuit_open')
                    retryable_status=exc.category=='http_error' and exc.status_code in (429,500,501,502,503,504)
                    if not (transient or retryable_status):
                        raise
                    fallback_cause=(dict(cause) if exc.category=='circuit_open' and cause else
                                    {'category':exc.category,'status_code':exc.status_code})
                    # At most one logical request to the public frontend;
                    # environment proxy and TLS settings remain unchanged.
                    payload=request_alternate(safe_source_error(exc))
            payload_data=payload.get('data') if isinstance(payload,dict) else None
            if not isinstance(payload_data,dict) or not isinstance(payload_data.get(symbol),dict):
                if event is not None:
                    with self._lock:
                        event.update(outcome='invalid_response')
                raise ValueError('腾讯时序数据结构或股票身份不匹配')
            if kind=='history':
                stock=payload_data[symbol]
                values=(stock.get('hfqday') if str(params.get('param','')).endswith(',hfq')
                        else stock.get('qfqday') or stock.get('day'))
                if not isinstance(values,list) or not values:
                    if event is not None:
                        with self._lock:
                            event.update(outcome='invalid_response')
                    raise ValueError('腾讯历史日线数据为空或结构无效')
            if event is not None:
                with self._lock:
                    event.update(outcome='response_received')
            if kind=='history':
                with self._lock:
                    if route=='direct':
                        self._history_preference=None
                    elif fallback_cause is not None:
                        # Only a successfully returned fallback establishes
                        # affinity. Mirror success during affinity never keeps
                        # extending the scheduled primary recovery time.
                        self._history_preference={'retry_at':time.monotonic()+30.,
                                                  'error':event['primary_error'],'cause':fallback_cause}
            return payload, {'source_url':url+'?'+urlencode(params),
                             'source_family':'tencent','source_route':route}
        finally:
            if probe:
                with self._lock:
                    self._history_primary_probe=False

    def minutes(self, code):
        symbol=code[-2:].lower()+code[:6]
        data,provenance=self._tencent_timeseries('/appstock/app/minute/query',{'code':symbol},symbol,'minutes')
        return [dict(row,**provenance) for row in parse_minutes(data,symbol)]

    @staticmethod
    def _annotate_history(rows, count):
        if not isinstance(rows,list) or not rows or any(not isinstance(row,dict) for row in rows):
            raise ValueError('历史日线缓存结构无效')
        result=[]
        for row in rows:
            # The recorded response field, not a URL requesting qfq, proves
            # actual adjustment. Legacy caches are annotated only in memory.
            volume_field=row.get('source_volume_field')
            field=('qfqday' if volume_field=='fqkline.qfqday[5]' else
                   'day' if volume_field=='fqkline.day[5]' else None)
            result.append(dict(row, adjustment_requested='qfq',
                               adjustment_actual='qfq' if field=='qfqday' else 'unadjusted' if field=='day' else 'unknown',
                               source_price_field='fqkline.'+field if field else None,
                               history_requested_count=count,
                               history_completed_count=len(rows)))
        return result

    def cached_history(self, code, *, count=300):
        """Read this day's exact-length cache, without network or any writes.

        Short server responses remain short: consumers validate indicator
        coverage. A 300-bar request never consumes a 100-bar legacy cache.
        """
        if isinstance(count,bool) or not isinstance(count,int) or not 1 <= count <= 1000:
            raise ValueError('历史日线请求数量须为1—1000的整数')
        now=datetime.now(TZ)
        today=now.date().isoformat()
        keys=[f'history-v4:{code}:{count}:{today}']
        if count==100:
            keys.append(f'history-v3:{code}:{today}')
        for key in keys:
            path=self.cache/(hashlib.sha256(key.encode()).hexdigest()+'.json')
            try:
                stored=json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(stored,dict):
                    continue
                fetched=stamp(stored.get('fetched_at'))
                if fetched is None or not 0 <= (now-fetched).total_seconds() <= 86400:
                    continue
                rows=stored['data']
                annotated=self._annotate_history(rows,count)
                if any(not isinstance(row.get('date'),str) or
                       not re.fullmatch(r'\d{4}-\d{2}-\d{2}',row['date']) or
                       stamp(row['date']) is None or row['date']>=today or
                       number(row.get('close')) is None or number(row.get('close'))<=0 or
                       row.get('history_requested_count',count)!=count
                       for row in rows):
                    continue
                dates=[row['date'] for row in rows]
                if dates!=sorted(set(dates)):
                    continue
                return annotated
            except (OSError,ValueError,KeyError,TypeError):
                continue
        return None

    def history(self, code, *, count=100):
        """Fetch up to ``count`` daily bars, retaining adjustment provenance.

        The current session is excluded, so ``count`` is a request size, not
        a guarantee of that many completed sessions. Consumers must check the
        actual history length for their indicators. A requested qfq response
        may contain unadjusted ``day`` rows; never label those as qfq.
        """
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
            raise ValueError('历史日线请求数量须为1—1000的整数')
        cached_rows=self.cached_history(code,count=count)
        if cached_rows is not None:
            return cached_rows
        symbol=code[-2:].lower()+code[:6]
        now=datetime.now(TZ)
        today=now.date().isoformat()

        def load():
            data,provenance=self._tencent_timeseries('/appstock/app/fqkline/get',
                {'param':f'{symbol},day,,,{count},qfq'},symbol,'history')
            stock=(data.get('data') or {}).get(symbol) or {}
            field='qfqday' if stock.get('qfqday') else 'day'
            values=stock.get(field) or []
            rows=[]
            for r in values:
                if len(r)<6 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(r[0])):
                    raise ValueError('日线结构无效')
                if r[0] >= today:
                    continue
                row=dict(zip(('date','open','close','high','low','volume'),r[:6]))
                for k in ('open','close','high','low','volume'):
                    row[k]=number(row[k])
                row.update(tencent_volume_fields(r[5],symbol,f'fqkline.{field}[5]'))
                row.update(provenance)
                rows.append(row)
            if not rows:
                raise ValueError('没有历史日线')
            return self._annotate_history(rows,count)

        # v1 had incorrect STAR volume; v2 lacked endpoint provenance; v3 did
        # not include the request length or explicit adjustment metadata.
        # Invalid cache payloads are misses, not an early collection failure.
        # Pure reads above can reuse the legacy default without writing it.
        data=load()
        key=f'history-v4:{code}:{count}:{today}'
        path=self.cache/(hashlib.sha256(key.encode()).hexdigest()+'.json')
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'fetched_at':now.isoformat(),'data':data},ensure_ascii=False),encoding='utf-8')
        tmp.replace(path)
        return data

    def financials(self, code):
        today=datetime.now(TZ).date().isoformat()
        def load():
            d=self.get('https://datacenter-web.eastmoney.com/api/data/v1/get',{
                'reportName':'RPT_DMSK_FN_INCOME','columns':'ALL','filter':f'(SECURITY_CODE="{code[:6]}")',
                'sortColumns':'REPORT_DATE','sortTypes':'-1','pageSize':6,'pageNumber':1})
            if not d.get('success'):
                raise ValueError('财报接口无有效结果')
            rows=(d.get('result') or {}).get('data') or []
            if any(r.get('SECUCODE') != code for r in rows):
                raise ValueError('财报代码不匹配')
            known=[r for r in rows if str(r.get('NOTICE_DATE') or '')[:10] < today and r.get('NOTICE_DATE')]
            if not known:
                raise ValueError('无可确认在当前时点已披露的财报')
            r=max(known,key=lambda r:r['REPORT_DATE'])
            uncertain=[x for x in rows if str(x.get('REPORT_DATE','')) >= r['REPORT_DATE']
                       and (not x.get('NOTICE_DATE') or str(x['NOTICE_DATE'])[:10] >= today)]
            if uncertain:
                raise ValueError('存在更新或更正财报但披露时点待核，不能用旧财报替代最新结果')
            return {'code':code,'report_date':r['REPORT_DATE'][:10],'published_at':r['NOTICE_DATE'][:10],
                    'net_profit':number(r.get('PARENT_NETPROFIT')),'core_profit':number(r.get('DEDUCT_PARENT_NETPROFIT')),
                    'revenue':number(r.get('TOTAL_OPERATE_INCOME')),'revenue_growth_pct':number(r.get('TOI_RATIO')),
                    'source':'eastmoney:RPT_DMSK_FN_INCOME',
                    'source_url':f'https://data.eastmoney.com/bbsj/{code[:6]}.html'}
        # Do not mask a newly published filing with an earlier cached quarter.
        return load()

    def notices(self, code):
        now=datetime.now(TZ)
        rows=[]
        # Bounded pagination: no silent assumption that the first 50 cover 14 days.
        for page in range(1,4):
            payload=self.get('https://np-anotice-stock.eastmoney.com/api/security/ann',{
                'stock_list':code[:6],'page_size':50,'page_index':page,'ann_type':'A','sr':-1,'client_source':'web'})
            if not isinstance(payload.get('data'),dict) or not isinstance(payload['data'].get('list'),list):
                raise ValueError('公告分页结构缺失')
            rows.extend(payload['data']['list'])
            merged={'data':{'list':rows,'total_hits':payload['data'].get('total_hits')}}
            result=parse_notices(merged,code,now)
            if result['verified'] or not payload['data']['list']:
                return result
        return result

    def enrich(self, code):
        bundle={'errors':[], 'error_details':{}}
        for key, fn in [('quote',self.quote),('financials',self.financials),('notices',self.notices)]:
            try:
                bundle[key]=fn(code)
            except Exception as exc:
                detail=source_diagnostic(exc,key)
                bundle['error_details'][key]=detail
                bundle['errors'].append(f'{key}采集失败：{detail["category"]}')
        return bundle

    def refresh(self, code):
        # Minute requests precede quotes; the final quote cannot predate the latest minute.
        symbol=code[-2:].lower()+code[:6]
        bundle={'errors':[], 'error_details':{}}
        def cross_quote(_code):
            return parse_sina_quote(self.get('https://hq.sinajs.cn/list='+symbol,text=True),symbol)
        for key,fn in [('minutes',self.minutes),('cross_quote',cross_quote),('quote',self.quote)]:
            try:
                bundle[key]=fn(code)
            except Exception as exc:
                detail=source_diagnostic(exc,key)
                bundle['error_details'][key]=detail
                bundle['errors'].append(f'{key}采集失败：{detail["category"]}')
        return bundle
