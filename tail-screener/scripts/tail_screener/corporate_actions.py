"""Free distribution schedule + matching implementation-body evidence.

Only the explicit standard A-share cash-only table is automatically interpreted.
Everything else remains unresolved; provider absence is never a no-event proof.
"""
from __future__ import annotations
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import math
import re
from .disclosures import _save, fetch_body
from .providers import parse_notices, source_diagnostic
from .engine import stamp
from .dividend_tax import MODEL,TRANSFER_MODEL

SCHEDULE_URL='https://datacenter-web.eastmoney.com/api/data/v1/get'


def _day(value):
    if not isinstance(value,str):raise ValueError('公司行动日期缺失')
    result=value[:10]
    if date.fromisoformat(result).isoformat()!=result:raise ValueError('公司行动日期无效')
    return result


def parse_schedule(payload, code):
    if not isinstance(payload,dict) or payload.get('success') is not True:raise ValueError('分红目录接口失败')
    result=payload.get('result')
    if not isinstance(result,dict) or not isinstance(result.get('data'),list):raise ValueError('分红目录结构缺失')
    count=result.get('count');rows=result['data']
    if isinstance(count,bool) or not isinstance(count,int) or count!=len(rows):raise ValueError('分红目录分页不完整')
    events=[];seen=set()
    for r in rows:
        if r.get('SECUCODE')!=code or r.get('SECURITY_CODE')!=code[:6]:raise ValueError('分红目录证券身份不匹配')
        if r.get('ASSIGN_PROGRESS')!='实施分配':continue
        if not r.get('EX_DIVIDEND_DATE'):continue  # pending proposal, not an entitlement
        ex=_day(r['EX_DIVIDEND_DATE']);record=_day(r.get('EQUITY_RECORD_DATE'))
        if record>=ex or ex in seen:raise ValueError('分红日期冲突或同日重复实施记录')
        seen.add(ex)
        def value(key):
            x=r.get(key)
            if x is None:return None
            if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<0:raise ValueError('分红数值无效')
            return str(x)
        events.append({'code':code[-2:]+code[:6],'record_session':record,'ex_session':ex,
            'gross_cash_per_10':value('PRETAX_BONUS_RMB'),'bonus_per_10':value('BONUS_RATIO'),
            'transfer_per_10':value('IT_RATIO'),'total_shares_per_10':value('BONUS_IT_RATIO'),
            'notice_date':_day(r.get('NOTICE_DATE')),'report_date':_day(r.get('REPORT_DATE')),
            'profile':r.get('IMPL_PLAN_PROFILE'),'status':'document_required'})
    return events


def parse_cash_implementation(event, document):
    """Fail closed on differentials, ambiguous amounts, other share classes.

    The cash amount is matched only in the opening 'per-share distribution'
    section. Annual total and already-paid interim amounts are not this event.
    """
    text=re.sub(r'\s+','',document['body']).replace('Ａ','A')
    title=document['title']
    if not implementation_title(title) or any(w in title for w in ('更正','撤销','取消','转增')) or not re.search(r'证券代码[:：]'+event['code'][2:]+r'(?:\D|$)',text):
        raise ValueError('不是该证券的权益分派实施正文')
    if not re.search(r'差异化分红送转[:：]否',text):raise ValueError('差异化分配状态未明确排除')
    for key in ('bonus_per_10','transfer_per_10','total_shares_per_10'):
        if event[key] is not None and Decimal(event[key])!=0:raise ValueError('送转股份及税务尚需单独核验')
    opening=text.split('相关日期',1)[0]
    if re.search(r'每股(?:送|转增)|每10股(?:送|转增)',opening):raise ValueError('正文含非现金分配')
    # Banks use cash-dividend synonyms and may insert the distribution period.
    # Keep the A-share and gross-tax qualification local to the matched clause.
    period=r'(?:\d{4}年(?:半年度|年度|中期|第[一二三四]季度))?'
    amounts={Decimal(v) for v in re.findall(r'A股每股(?:派发)?'+period+r'现金(?:红利|股息)(?:人民币)?([0-9]+(?:\.[0-9]+)?)元[（(](?:含税|税前)[）)]',opening)}
    if len(amounts)!=1:raise ValueError('本次A股每股税前现金金额缺失或冲突')
    gross=amounts.pop()
    if gross<=0 or event['gross_cash_per_10'] is None or gross*10!=Decimal(event['gross_cash_per_10']):
        raise ValueError('正文每股金额与目录每十股金额不匹配')
    # The supported cash-only template has no bonus-listing column. We do not
    # assign a meaning to a date merely because it occurs near a keyword.
    pattern=(r'股份类别股权登记日最后交易日除权[（(]息[）)]日现金红利发放日'
             r'A股(\d{4}/\d{1,2}/\d{1,2})[－—-](\d{4}/\d{1,2}/\d{1,2})(\d{4}/\d{1,2}/\d{1,2})')
    tables=re.findall(pattern,text)
    def iso(v):return date(*map(int,v.split('/'))).isoformat()
    dates={tuple(iso(v) for v in row) for row in tables}
    if len(dates)!=1:raise ValueError('A股登记、除息、发放日表格缺失或冲突')
    record,ex,pay=dates.pop()
    if record!=event['record_session'] or ex!=event['ex_session'] or pay<ex:
        raise ValueError('正文日期与目录不匹配')
    if not re.search(r'暂不扣缴(?:个人所得税|股息红利税)',text) or '2015' not in text or '101' not in text:
        raise ValueError('境内自然人延期扣税条款未核验')
    return {'event_id':event['code']+':'+ex+':'+document['article_id'],
            'code':event['code'],'record_session':record,'ex_session':ex,'pay_session':pay,
            'gross_cash_per_share':float(gross),'share_multiplier':1,'tax_status':MODEL,
            'verified':True,'source_sha256':document['body_sha256'],
            'source_url':document['source_url'],'source_authentication':'mirror_only',
            'real_withholding_verified':False,'certainty':'conditional_resident_cash_dividend_model'}


def implementation_title(title):
    return any(t in title for t in ('权益分派实施公告','分红派息及转增股本实施公告','分红派息实施公告','利润分配实施公告'))


def parse_implementation(event, document):
    """Interpret bounded exchange templates, retaining shareholder and ex-price bases.

    Shenzhen's resident tax table is checked against gross cash, never the
    provider's generic after-tax field. Bonus shares and fractional allocation
    remain unsupported. Capital transfers need an explicit no-tax statement or
    the complete resident supplementary-tax schedule with no extra share tax.
    """
    if hashlib.sha256(document['body'].encode()).hexdigest()!=document.get('body_sha256'):
        raise ValueError('实施正文哈希不符')
    if not implementation_title(document['title']):raise ValueError('不是支持的实施公告标题')
    text=re.sub(r'\s+','',document['body']).replace('Ａ','A')
    if not re.search(r'(?:证券|股票)代码(?:[（(]A/H[）)])?[:：]'+event['code'][2:]+r'(?:\D|$)',text):raise ValueError('实施正文证券身份不匹配')
    if any(w in document['title'] for w in ('更正','撤销','取消')):raise ValueError('更正实施文件须人工核对最终方案')
    cash_error=None
    if event['code'].startswith('SH'):
        try:return parse_cash_implementation(event,document)
        except ValueError as exc:cash_error=str(exc)
    number=r'([0-9]+(?:\.[0-9]+)?)'
    def unique(pattern,required=True):
        values={Decimal(v) for v in re.findall(pattern,text)}
        if not values and not required:return None
        if len(values)!=1:raise ValueError('正文数值缺失或不唯一：'+pattern)
        return values.pop()
    def date_value(pattern,required=True):
        values={date(*map(int,r)).isoformat() for r in re.findall(pattern,text)}
        if not values and not required:return None
        if len(values)!=1:raise ValueError('正文日期缺失或冲突：'+pattern)
        return values.pop()
    for key in ('bonus_per_10',):
        if event.get(key) is not None and Decimal(event[key])!=0:raise ValueError('送红股税基及持有期尚未支持')
    if re.search(r'每(?:10)?股送(?:红股)?[1-9]',text):raise ValueError('正文含尚未支持的送红股')
    gross=None;transfer=None;record=None;ex=None;pay=None;sellable=None
    if event['code'].startswith('SZ'):
        gross=unique(r'每10股派(?:发)?'+number+r'元人民币现金[（(]含税',False)
        if gross is None:gross=unique(r'每10股派发现金股利人民币'+number+r'元[（(]含税')
        gross/=10
        transfer=unique(r'(?:以|用)资本公积金向全体股东每10股转增'+number+r'股',False)
        transfer=(transfer or Decimal(0))/10
        ymd=r'(\d{4})年(\d{1,2})月(\d{1,2})日'
        record=date_value(r'(?:本次(?:A股)?权益分派(?:A股)?股权登记日|A股股权登记日|[1１][.、]股权登记日)(?:为)?[:：]?'+ymd)
        ex=date_value(r'(?:A股)?除(?:权除息|息)日(?:为)?[:：]?'+ymd)
        pay=date_value(r'代派的(?:A股股东)?现金红利将于'+ymd+r'通过股东托管证券公司')
        if transfer:
            sellable=date_value(r'新增(?:可流通股份|无限售条件流通股|无限售流通股)上市日为[:：]'+ymd)
            credited=date_value(r'(?:本次转增股份|本次所送[（(]转[）)]股)于'+ymd+r'直接记入股东证券账户')
            if credited>sellable:raise ValueError('新股到账日晚于可售日')
        # Rounding at six decimals is explicitly used in many implementation
        # notices. Compare to the published per-ten-share tax, not whole-account
        # cash; the engine rounds account amounts to cents separately.
        short=unique(r'持股1个月[（(]含1个月[）)]以内，每10股补缴税款(?:人民币)?'+number+r'元')
        long=unique(r'持股1个月以上至1年[（(]含1年[）)]的，每10股补缴税款(?:人民币)?'+number+r'元')
        if abs(short-gross*10*Decimal('.2'))>Decimal('.000001') or abs(long-gross*10*Decimal('.1'))>Decimal('.000001'):
            raise ValueError('自然人补税表与本次税前现金不一致，不能解释转增税务')
        if '暂不扣缴个人所得税' not in text or not re.search(r'持股超过1年的，不[需须]补缴税款',text):
            raise ValueError('自然人延期扣税及一年以上条款缺失')
    else:
        differential_flag=re.search(r'差异化分红送转[:：](是|否)',text)
        if not differential_flag:raise ValueError('上交所差异化分配状态缺失')
        opening=text.split('相关日期',1)[0]
        amounts={Decimal(v) for v in re.findall(r'(?:A股)?每股(?:派发)?现金红利(?:人民币)?'+number+r'元',opening)}
        if len(amounts)>1:raise ValueError('上交所本次每股现金金额冲突')
        gross=amounts.pop() if amounts else Decimal(0)
        transfer=unique(r'每股转增(?:股份)?'+number+r'股',False) or Decimal(0)
        if gross:
            qualified={Decimal(v) for v in re.findall(r'每股(?:派发)?现金红利(?:人民币)?'+number+r'元[（(](?:含税|税前)[）)]',text)}
            if gross not in qualified:raise ValueError('上交所正文税前现金条款不明确或冲突')
            if not re.search(r'暂不扣缴(?:个人所得税|股息红利税)',text) or '2015' not in text or '101' not in text:
                raise ValueError('境内自然人延期扣税条款未核验')
        if gross and not transfer:
            header=r'股份类别股权登记日最后交易日除权[（(]息[）)]日现金红利发放日'
        elif transfer and not gross:
            if not re.search(r'股票(?:发行溢价|溢价发行)收入所形成的资本公积金',text) or not re.search(r'本次资本公积金转增股本不扣税',text):
                raise ValueError('转增免税来源与条款不明确')
            header=r'新增无限售条股份类别股权登记日最后交易日除权[（(]息[）)]日件流通股份上市日'
        elif transfer and gross:
            if differential_flag[1]!='否':raise ValueError('上交所差异化现金与转增尚未支持')
            if not re.search(r'股票(?:发行溢价|溢价发行)收入所形成的资本公积金',text) or not re.search(r'转增股本不(?:扣|征)税',text):
                raise ValueError('转增免税来源与条款不明确')
            header=r'股份类别股权登记日最后交易日除权[（(]息[）)]日新增无限售条件流通股现金红利份上市日发放日'
        else:raise ValueError('实施正文现金或转增模板未识别；现金模板核验：'+str(cash_error))
        # Exact header/column order, rather than keyword-nearest dates.
        table_dates=r'A股(\d{4}/\d{1,2}/\d{1,2})[－—-](\d{4}/\d{1,2}/\d{1,2})(\d{4}/\d{1,2}/\d{1,2})'
        if gross and transfer:table_dates+=r'(\d{4}/\d{1,2}/\d{1,2})'
        tables=re.findall(header+table_dates,text)
        dates={tuple(date(*map(int,v.split('/'))).isoformat() for v in row) for row in tables}
        if len(dates)!=1:raise ValueError('上交所转增登记、除权、可售日表格不明确')
        row=dates.pop();record,ex,last=row[:3]
        if gross and transfer:sellable=last;pay=row[3]
        elif gross:pay=last
        else:sellable=last
    if record!=event['record_session'] or ex!=event['ex_session'] or (pay is not None and pay<ex) or (sellable is not None and sellable<ex):
        raise ValueError('正文登记、除权、到账或上市日期与目录冲突')
    if (event.get('gross_cash_per_10') is not None and gross*10!=Decimal(event['gross_cash_per_10'])) or (gross and event.get('gross_cash_per_10') is None):
        raise ValueError('正文现金金额与目录不匹配')
    if (event.get('transfer_per_10') is not None and transfer*10!=Decimal(event['transfer_per_10'])) or (transfer and event.get('transfer_per_10') is None):
        raise ValueError('正文转增数量与目录不匹配')
    if not gross and not transfer:raise ValueError('无经济分配权益')
    # Differential ex-prices caused by excluded treasury shares require an
    # explicit formula. Shareholder entitlement must never use this average.
    reference_cash=gross;reference_ratio=1+transfer
    sections=re.split(r'[一二]、(?:本次实施的)?权益分派方案',text)
    actual=re.split(r'[二三四]、',sections[-1],maxsplit=1)[0]
    differential=bool(re.search(r'(?:剔除|扣除|减去)(?:公司)?(?:回购|已回购)',actual)) or event['code'].startswith('SH') and differential_flag[1]=='是'
    excluded_counts=re.findall(r'剔除已回购股份([0-9,]+(?:\.[0-9]+)?)股后的',actual)
    if excluded_counts and all(Decimal(n.replace(',',''))==0 for n in excluded_counts):differential=False
    if differential:
        formulas=re.findall(r'除权除息价格?=[（(]股权登记日收盘价-'+number+r'元[）)](?:[（(]?/|/[（(]?)[（(]?1\+'+number+r'[）)]',text)
        if formulas:
            if len(set(formulas))!=1:raise ValueError('差异化除权公式冲突')
            reference_cash,reference_transfer=map(Decimal,formulas[0]);reference_ratio=1+reference_transfer
        elif not transfer:
            reference_cash=unique(r'(?:除权除息价格?=股权登记日收盘价-|除权除息参考价格=前收盘价格-)'+number+r'元(?:/股)?',False)
            if reference_cash is None:
                reference_cash=unique(r'(?:每股现金红利应以|除权除息[^。；]{0,30}每股现金红利为)'+number+r'元/股',False)
            if reference_cash is None:
                reference_cash=unique(r'除权除息参考价格=权益分派股权登记日收盘价[－-]每股现金红利=权益分派股权登记日收盘价-'+number+r'元/股',False)
            if reference_cash is None:raise ValueError('回购股份差异化除权参考金额缺失')
        else:raise ValueError('转增含回购股份，缺少明确的差异化除权公式')
    return {'event_id':event['code']+':'+ex+':'+document['article_id'],'code':event['code'],
        'record_session':record,'ex_session':ex,'pay_session':pay,'shares_sellable_on':sellable,
        'gross_cash_per_share':float(gross),'share_multiplier':float(1+transfer),
        'ex_price_cash_per_share':float(reference_cash),'ex_price_share_multiplier':float(reference_ratio),
        'tax_status':TRANSFER_MODEL if transfer else MODEL,'verified':True,'source_sha256':document['body_sha256'],
        'source_url':document['source_url'],'source_authentication':'mirror_only',
        'tax_evidence':'explicit_implementation_resident_tax_table' if event['code'].startswith('SZ') else 'explicit_share_premium_transfer_exemption',
        'real_withholding_verified':False,'certainty':'conditional_resident_documented_distribution_model'}


def collect_actions(source, code, start, end, now, folder):
    result={'code':code,'start':start,'end':end,'status':'complete','events':[], 'actions':[],
            'errors':[],'evidence':[],'all_corporate_actions_audited':False,
            'scope':'已实施分红送转目录；不覆盖配股、合并、退市及所有非分配公司行动'}
    exchange=code[2:]+'.'+code[:2]
    try:
        # Fresh on every invocation. Past cache is not proof of no new event.
        payload=source.get(SCHEDULE_URL,{'reportName':'RPT_SHAREBONUS_DET','columns':'ALL',
            'filter':f'(SECURITY_CODE="{code[2:]}")','sortColumns':'REPORT_DATE','sortTypes':'-1',
            'pageSize':500,'pageNumber':1,'source':'WEB','client':'WEB'})
        result['evidence'].append(_save(folder,payload))
        events=parse_schedule(payload,exchange)
        result['events']=[e for e in events if start<=e['ex_session']<=end]
        for event in result['events']:
            try:
                begin=(date.fromisoformat(event['notice_date'])-timedelta(days=3)).isoformat()
                finish=(date.fromisoformat(event['notice_date'])+timedelta(days=1)).isoformat()
                listing=source.get('https://np-anotice-stock.eastmoney.com/api/security/ann',{
                    'stock_list':code[2:],'page_size':100,'page_index':1,'ann_type':'A','sr':-1,
                    'client_source':'web','begin_time':begin,'end_time':finish})
                result['evidence'].append(_save(folder,listing))
                notices=parse_notices(listing,exchange,now)
                if notices['truncated']:raise ValueError('实施公告检索目录未取全')
                if any(any(word in i['title'] for word in ('更正','撤销','取消')) and
                       any(word in i['title'] for word in ('分派','分红','分配')) for i in notices['items']):
                    raise ValueError('检索到分配更正或撤销，需要核对最终实施方案')
                candidates=[i for i in notices['items'] if implementation_title(i['title'])
                            and begin<=i['published_at'][:10]<=finish]
                if len(candidates)!=1:raise ValueError('实施公告不唯一或缺失，需核对更正和撤销记录')
                if stamp(candidates[0]['published_at']) is None or stamp(candidates[0]['published_at'])>now:
                    raise ValueError('实施公告尚未在采集时点披露')
                document=fetch_body(source,candidates[0],exchange,folder)
                event['document']=document
                action=parse_implementation(event,document)
                event.update(status='distribution_model_supported',action=action);result['actions'].append(action)
            except Exception as exc:
                event['status']='unresolved';event['error']=source_diagnostic(exc,'implementation_body')
                if isinstance(exc,ValueError):event['error']['reason']=str(exc)
                result['errors'].append({'ex_session':event['ex_session'],**event['error']})
    except Exception as exc:result['errors'].append(source_diagnostic(exc,'distribution_schedule'))
    if result['errors']:result['status']='incomplete'
    return result
