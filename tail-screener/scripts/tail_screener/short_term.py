"""Separate short-term quote checks from closed-session selection and fills."""
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import math

from .calendar import TZ
from .research_status import notice_status


def refresh_candidate_quotes(source,rows,now,calendar,*,clock=None):
    """Refresh only selected quotes after slow history/enrichment requests.

    Keep the selection-time quote separately. A failed refresh never falls
    back to that older quote and cannot silently preserve a passing status.
    """
    from .candidate_risk import instrument_eligibility
    from .daily_store import canonical_code
    from .providers import safe_source_error
    phase=market_phase(now,calendar)
    result={'phase':phase,'status':'not_needed','requested':0,'updated':0,'failed':{}}
    if phase!='continuous_trading':return result
    clock=clock or (lambda:datetime.now(TZ))
    result.update(status='complete',requested=len(rows))
    def fetch(row):
        try:
            quote=source.quote(row['code'][2:]+'.'+row['code'][:2]);checked=clock()
            if not isinstance(quote,dict) or canonical_code(quote.get('code'))!=row['code']:
                raise ValueError('refreshed_quote_identity_mismatch')
            check=instrument_eligibility(quote,asof=checked,expected_session=checked.astimezone(TZ).date().isoformat())
            if not check['eligible']:raise ValueError('refreshed_quote_ineligible:'+','.join(i['code'] for i in check['issues']))
            stamp=datetime.fromisoformat(quote['quote_at'])
            if (checked-stamp).total_seconds()>120:raise ValueError('refreshed_quote_stale')
            return quote,{'status':'updated','checked_at':checked.isoformat()},None
        except Exception as exc:
            error=str(exc) if isinstance(exc,ValueError) else safe_source_error(exc)
            return None,{'status':'failed','error':error},error
    with ThreadPoolExecutor(max_workers=4) as pool:values=list(pool.map(fetch,rows))
    for row,(quote,check,error) in zip(rows,values):
        row['selection_quote']=row.get('quote');row['quote']=quote;row['quote_refresh']=check
        if error is None:result['updated']+=1
        else:result['failed'][row['code']]=error
    if result['failed']:result['status']='incomplete'
    return result


def market_phase(now, calendar):
    if now.tzinfo is None:raise ValueError('观察时点必须包含时区')
    now=now.astimezone(TZ)
    if not calendar.get('verified'):return 'calendar_unverified'
    if not calendar.get('is_session'):return 'non_trading_day'
    minute=now.hour*60+now.minute
    if minute<570:return 'pre_open'
    if minute<690:return 'continuous_trading'
    if minute<780:return 'lunch_break'
    if minute<900:return 'continuous_trading'
    return 'post_close'


def trading_context(report, now, calendar, *, mode='auto'):
    """A fresh quote never certifies fills, company actions or expected profit."""
    phase=market_phase(now,calendar)
    current=now.astimezone(TZ)
    active=phase=='continuous_trading'
    context={'requested_mode':mode,'phase':phase,'checked_at':current.isoformat(),
             'signal_session':report.get('data_session'),'actionable':False,
             'selection_basis':'latest_verified_closed_session',
             'execution_evidence':'daily_bar_scenario_only',
             'unverified':['strategy_profitability','disclosure_semantics','corporate_actions','auction_fills'],
             'status':'research_only','candidates':[]}
    context['notice_bodies_status']=notice_status(report)
    if context['notice_bodies_status'] not in ('mirror_bodies_collected','not_applicable'):
        context['unverified'].append('notice_bodies')
    complete=report.get('run_status',{}).get('data_status')=='complete'
    if not complete:context['status']='blocked_data'
    elif mode=='live' and not active:context['status']='outside_continuous_trading'
    for row in report.get('candidates',[]):
        quote=row.get('quote') or {};reasons=[];age=None;price=None
        try:
            stamp=datetime.fromisoformat(quote['quote_at'])
            if stamp.tzinfo is None:raise ValueError()
            stamp=stamp.astimezone(TZ);age=(current-stamp).total_seconds()
            if age<0:reasons.append('future_quote')
            if active and (stamp.date()!=current.date() or age>120):reasons.append('quote_not_current')
            price=float(quote['price'])
            if not math.isfinite(price) or price<=0:raise ValueError()
        except (KeyError,TypeError,ValueError,OverflowError):
            reasons.append('quote_unverified');price=None
        ceiling=row.get('entry_price_ceiling')
        if price is not None and ceiling is not None and price>ceiling+1e-8:
            reasons.append('above_frozen_price_ceiling')
        if not complete:reasons.append('universe_or_account_incomplete')
        if not active:reasons.append('not_continuous_trading')
        item={'code':row['code'],'quote_at':quote.get('quote_at'),'quote_age_seconds':age,
              'observed_price':price,'entry_price_ceiling':ceiling,
              'change_from_signal_close_pct':(price/row['price']-1)*100 if price is not None else None,
              'ma20_premium_at_signal_pct':row.get('ma20_premium_pct'),
              'status':'blocked' if reasons else 'quote_checks_passed_research_only',
              'reasons':reasons,'actionable':False}
        context['candidates'].append(item)
        row['short_term_check']=item
    return context
