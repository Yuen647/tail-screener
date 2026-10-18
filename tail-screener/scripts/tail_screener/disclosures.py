"""Bounded announcement-body acquisition and inspectable, non-semantic screening.

The provider is an exchange-notice mirror, not an independent authentication
service. Text matching identifies passages to read; it never certifies safety.
"""
from __future__ import annotations
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from .engine import stamp
from .providers import SourceError, source_diagnostic

BODY_URL = 'https://np-cnotice-stock.eastmoney.com/api/content/ann'
BODY_URLS = (BODY_URL, 'https://np-cnotice-pc.eastmoney.com/api/content/ann')
MAX_BODY_PAGES = 128
TOPICS = {
    'delisting': ('退市', '风险警示'),
    'investigation': ('立案', '行政处罚'),
    'debt': ('债务逾期', '债务违约', '破产', '重整'),
    'pledge': ('质押', '冻结'),
    'sale': ('减持',),
    'earnings': ('业绩预告', '业绩快报', '亏损'),
    'contract': ('合同', '订单', '中标'),
    'distribution': ('权益分派', '除权', '配股', '送股', '转增'),
    'correction': ('更正', '撤销', '终止'),
}


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _save(folder, payload):
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    sha=_sha(raw);path=Path(folder)/(sha+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        if path.read_bytes()!=raw:raise ValueError('公告证据哈希冲突')
    else:
        with path.open('xb') as f:f.write(raw)
    return {'path':str(path.resolve()),'sha256':sha,'encoding':'canonical_json_utf8'}


def article_id(item):
    match=re.fullmatch(r'https://data\.eastmoney\.com/notices/detail/\d{6}/(AN\d+)\.html',item.get('url',''))
    if not match:raise ValueError('公告链接或编号无效')
    return match[1]


def parse_body_page(payload, item, code):
    data=payload.get('data') if isinstance(payload,dict) else None
    if not isinstance(payload,dict) or payload.get('success') != 1 or not isinstance(data,dict):raise ValueError('公告正文接口未成功')
    if data.get('art_code')!=article_id(item):raise ValueError('公告编号不匹配')
    securities=data.get('security') or []
    if code[:6] not in {r.get('stock') for r in securities if isinstance(r,dict)}:
        raise ValueError('公告正文证券身份不匹配')
    if re.sub(r'\s+','',data.get('notice_title',''))!=re.sub(r'\s+','',item['title']):
        raise ValueError('公告正文标题与目录不一致')
    if data.get('is_ai_summary') not in (0,'0'):raise ValueError('摘要不能代替公告正文')
    count=data.get('page_size')
    if isinstance(count,bool) or not isinstance(count,int) or not 1<=count<=MAX_BODY_PAGES:
        raise ValueError(f'公告分页数量缺失或超过{MAX_BODY_PAGES}页采集上限')
    content=data.get('notice_content')
    if not isinstance(content,str) or not content.strip() or len(content)>500_000:
        raise ValueError('公告正文为空或超过大小限制')
    # An issuer can date a notice tomorrow while it is disclosed tonight.
    # Keep both fields, and use the dated directory time for PIT selection.
    return data,count,content


def _body_page(source, item, code, page):
    """Cache only validated pages; retain the actual same-provider route used."""
    article=article_id(item)
    identity=_sha(json.dumps([article,item['title'],code],ensure_ascii=False).encode())
    def acquire():
        preferred=getattr(source,'_notice_body_route',None)
        routes=list(BODY_URLS)
        if isinstance(preferred,str) and preferred in routes:
            routes.remove(preferred);routes.insert(0,preferred)
        attempts=[]
        for index,url in enumerate(routes):
            try:
                payload=source.get(url,{'art_code':article,'client_source':'web','page_index':page})
                parse_body_page(payload,item,code)
            except SourceError as exc:
                attempts.append(source_diagnostic(exc,'notice_body'))
                cause=(exc.cause or {}).get('category',exc.category)
                # Access refusals and certificate errors are not outages to
                # evade. Invalid securities, titles or summaries also fail
                # closed above, without trying to find a more permissive page.
                if cause in ('http_denied','tls_error') or exc.status_code in (401,403):raise
                if index==len(routes)-1:
                    exc.route_attempts=attempts;raise
                continue
            source._notice_body_route=url
            return {'payload':payload,'request_url':url,'fetched_at':datetime.now().astimezone().isoformat(),
                    'fallback_attempts':attempts,'source_family':'eastmoney'}
        raise ValueError('公告正文没有可用路由')
    envelope=source.cached(f'notice-body-v2:{identity}:{page}',3600,acquire)
    if not isinstance(envelope,dict) or envelope.get('request_url') not in BODY_URLS:
        raise ValueError('公告正文缓存来源无效')
    parse_body_page(envelope.get('payload'),item,code)
    return envelope


def fetch_body(source, item, code, folder):
    article=article_id(item);parts=[];evidence=[];routes=[];first=None;count=1
    for page in range(1,MAX_BODY_PAGES+1):
        envelope=_body_page(source,item,code,page);payload=envelope['payload']
        data,n,content=parse_body_page(payload,item,code)
        if first is None:first=data;count=n
        elif n!=count or any(data.get(key)!=first.get(key) for key in ('notice_date','eitime','attach_url')):
            raise ValueError('公告分页期间内容身份发生变化')
        if content in parts:raise ValueError('公告分页重复，不能视为全文')
        parts.append(content);evidence.append(_save(folder,payload))
        routes.append({key:value for key,value in envelope.items() if key!='payload'})
        if sum(len(p) for p in parts)>2_000_000:raise ValueError('公告全文超过采集上限')
        if page==count:break
    body='\n'.join(parts)
    # Preserve whitespace and negations in excerpts; these are topic hits,
    # not assertions that a breach/contract or a profitable order exists.
    findings=[]
    for topic,terms in TOPICS.items():
        hits=[]
        for term in terms:
            match=re.search(re.escape(term),body)
            if match:
                hits.append({'term':term,'offset':match.start(),
                             'excerpt':body[max(0,match.start()-100):match.end()+180]})
        if hits:findings.append({'topic':topic,'hits':hits})
    return {'article_id':article,'title':item['title'],'published_at':item['published_at'],
            'document_date':first.get('notice_date'),'source_url':item['url'],
            'attachment_url':first.get('attach_url'),'source':'eastmoney:exchange_notice_mirror',
            'source_authentication':'mirror_only','status':'retrieved','page_count':count,
            'body':body,'body_sha256':_sha(body.encode()),'evidence':evidence,'body_routes':routes,'findings':findings,
            'semantic_review':'not_performed','orders_confirmed':False,'safe_to_buy':False}


def review_bodies(source, directory, code, now, folder, *, limit=30):
    start=now-timedelta(days=14);items=[];errors=[];seen=set()
    if not isinstance(directory,dict):directory={}
    entries=directory.get('items',[])
    if not isinstance(entries,list):
        errors.append({'stage':'directory','reason':'公告目录列表结构无效'});entries=[]
    for item in entries:
        if not isinstance(item,dict):
            errors.append({'stage':'directory','reason':'公告目录条目结构无效'});continue
        at=stamp(item.get('published_at'))
        if at is None:
            errors.append({'stage':'directory','reason':'公告披露时间未核验'});continue
        if at>now:
            errors.append({'stage':'directory','reason':'公告目录含未来披露时间'});continue
        if at>=start:
            try:article=article_id(item)
            except (ValueError,TypeError):
                errors.append({'stage':'directory','reason':'公告链接或编号无效'});continue
            if article in seen:
                errors.append({'stage':'directory','article_id':article,'reason':'公告目录含重复编号'});continue
            seen.add(article);items.append(item)
    documents=[]
    for item in items[:limit]:
        try:documents.append(fetch_body(source,item,code,folder))
        except Exception as exc:
            errors.append({'article_id':article_id(item),'stage':'body',**source_diagnostic(exc,'notice_body'),
                           'reason':str(exc) if isinstance(exc,ValueError) else '正文采集失败'})
    if len(items)>limit:errors.append({'stage':'limit','reason':f'近14日公告超过{limit}篇上限'})
    if directory.get('verified') is not True or directory.get('truncated') is not False:
        errors.append({'stage':'directory','reason':'近14日目录覆盖不完整'})
    return {'status':'incomplete' if errors else 'complete','window_start':start.isoformat(),
            'window_end':now.isoformat(),'required_count':len(items),'retrieved_count':len(documents),
            'documents':documents,'errors':errors,'source_authentication':'mirror_only',
            'semantic_review':'not_performed','safe_to_buy':False,
            'scope':'近14日公告镜像正文采集及词项定位；不认证合同真实性、财务准确性或消息面安全'}
