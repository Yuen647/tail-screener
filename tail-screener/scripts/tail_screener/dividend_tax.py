"""Conditional resident-individual cash-dividend tax model, not broker evidence.

Scope: ordinary unrestricted Shanghai/Shenzhen shares acquired on the market
after 2015-09-08, a single lot, no bonus shares or partial sales. Sources:
https://www.chinatax.gov.cn/n810341/n810755/c1797427/content.html
https://shanghai.chinatax.gov.cn/tax/zcfw/zcjd/201211/t400944.html
"""
from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

MODEL='cn_resident_cash_model'
TRANSFER_MODEL='cn_resident_documented_capital_transfer_model'


def rate(entry, exit_day):
    start=date.fromisoformat(entry);end=date.fromisoformat(exit_day)
    if entry<'2015-09-08' or end<start:raise ValueError('股息税模型日期超出支持范围')
    y,m=(start.year+1,1) if start.month==12 else (start.year,start.month+1)
    month=date(y,m,min(start.day,monthrange(y,m)[1]))
    year=date(start.year+1,start.month,min(start.day,monthrange(start.year+1,start.month)[1]))
    return Decimal('.20') if end<=month else Decimal('.10') if end<=year else Decimal('0')


def tax_cents(gross, entry, exit_day):
    return int((Decimal(gross)*rate(entry,exit_day)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def reserve(state, session):
    return sum(tax_cents(d['gross_cents'],d['entry_session'],d.get('exit_session') or session)
               for d in state.get('dividend_tax_lots',[]) if not d.get('settled'))


def settle(state, session, events):
    for d in state.get('dividend_tax_lots',[]):
        if d.get('settled') or not d.get('exit_session') or session<d['pay_session']:continue
        tax=tax_cents(d['gross_cents'],d['entry_session'],d['exit_session'])
        if state['cash_cents']<tax:raise ValueError('模拟股息税扣收资金不足')
        state['cash_cents']-=tax;d.update(settled=True,tax_cents=tax)
        events.append({'type':'modeled_dividend_tax','code':d['code'],'event_id':d['event_id'],
                       'tax_cents':tax,'tax_status':d.get('tax_status',MODEL),'real_withholding_verified':False})
