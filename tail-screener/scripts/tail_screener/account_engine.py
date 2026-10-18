"""Finite-cash, long-only conditional paper account. No broker integration.

All money is integer cents. Missing marks/actions never release cash or silently
liquidate stock. This engine consumes preplanned entries, not future rankings.
"""
from __future__ import annotations
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from datetime import date
import math
import re
from .daily_store import canonical_code
from .dividend_tax import MODEL as DIVIDEND_TAX_MODEL, TRANSFER_MODEL, reserve as tax_reserve, settle as settle_tax, tax_cents


def cents(value):
    if isinstance(value,bool) or not math.isfinite(float(value)):
        raise ValueError('金额必须为有限数值')
    return int((Decimal(str(value))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))


def new_account(*, capital=100000, rule_sha256, cost_multiplier=1, max_positions=5, max_holding_sessions=None):
    from .account_config import account_config
    config=account_config(capital,max_positions,max_holding_sessions)
    if cost_multiplier not in (1,2):
        raise ValueError('账户参数不在登记范围')
    return {'schema_version':1,'rule_sha256':rule_sha256,'initial_cash_cents':cents(capital),
            'cash_cents':cents(capital),'nav_cents':cents(capital),'last_session':None,
            'cost_multiplier':cost_multiplier,'max_positions':max_positions,
            'max_holding_sessions':config['max_holding_sessions'],
            'positions':{},'receivables':[],'closed_trades':[], 'processed_action_ids':[],
            'dividend_tax_lots':[], 'dividend_tax_reserve_cents':0,
            'last_marked_nav_cents':cents(capital),'sessions_processed':0,
            'certainty':'conditional_paper_account_not_live'}


def fee(notional_cents, day, side, multiplier=1):
    value=Decimal(notional_cents)/100
    commission=max(Decimal('5'),value*Decimal('.0003'))
    transfer=value*Decimal('.00002' if day<'2022-04-29' else '.00001')
    stamp=value*Decimal('.001' if day<'2023-08-28' else '.0005') if side=='sell' else Decimal(0)
    slip=value*Decimal('.001')
    return {key:int((v*100*multiplier).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
            for key,v in [('commission',commission),('transfer',transfer),('stamp',stamp),('slippage',slip)]}


def _blocked(bar):
    if not isinstance(bar,dict) or not bar.get('valid'):return 'bar_unverified'
    for key in ('open','high','low','close','previous_close','volume_shares'):
        value=bar.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
            return 'bar_unverified'
    if bar['low']>min(bar['open'],bar['close']) or bar['high']<max(bar['open'],bar['close']):return 'bar_geometry'
    if bar.get('halted'):return 'halted'
    if bar['high']==bar['low']:return 'one_price_day'
    reference=bar.get('documented_ex_reference_close',bar['previous_close'])
    if abs(bar['open']/reference-1)>=.045:return 'assumed_4_5pct_open_gap'
    return None


def _interval(bar):
    bounds=(bar or {}).get('adjustment_interval')
    if not isinstance(bounds,list) or len(bounds)!=2:return None
    if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>0 for v in bounds):return None
    return bounds if bounds[0]<=bounds[1] else None


def _quantity(code, budget_cents, price, day, multiplier):
    step,minimum=(1,200) if code.startswith('SH688') else (100,100)
    quantity=int(budget_cents/(price*100))//step*step
    while quantity>=minimum:
        gross=cents(quantity*price)
        costs=fee(gross,day,'buy',multiplier)
        if gross+sum(costs.values())<=budget_cents:return quantity,gross,costs
        quantity-=step
    return 0,0,{}


def _action(action,session,previous_session):
    """Validate an explicitly settled event supplied by a trusted caller.

    This contract does not verify documents or turn a provider's generic
    after-tax dividend into the account holder's final tax settlement.
    Missing economic fields must never mean zero or clear an unresolved event.
    """
    if not isinstance(action,dict):raise ValueError('公司行动必须是显式核验记录')
    event_id=action.get('event_id');sha=action.get('source_sha256')
    if not isinstance(event_id,str) or not event_id.strip() or len(event_id)>200:
        raise ValueError('公司行动事件身份缺失或无效')
    if action.get('verified') is not True or not isinstance(sha,str) or not re.fullmatch('[0-9a-f]{64}',sha):
        raise ValueError('公司行动必须有明确核验标记及SHA256证据身份')
    code=canonical_code(action.get('code'))
    def day(key,required=True):
        value=action.get(key)
        if value is None and not required:return None
        try:
            if not isinstance(value,str) or date.fromisoformat(value).isoformat()!=value:raise ValueError()
        except (ValueError,TypeError):raise ValueError('公司行动日期缺失或无效：'+key) from None
        return value
    ex=day('ex_session');record=day('record_session')
    if ex!=session or record>=ex or record!=previous_session:
        raise ValueError('公司行动须对应当前除权日及实际前一交易日的权益登记持仓')
    modeled=action.get('tax_status') in (DIVIDEND_TAX_MODEL,TRANSFER_MODEL)
    cash_key='gross_cash_per_share' if modeled else 'net_cash_per_share'
    numbers={}
    for key in (cash_key,'share_multiplier'):
        value=action.get(key)
        if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)) or not math.isfinite(float(value)):
            raise ValueError('公司行动经济字段必须显式提供有限数值：'+key)
        numbers[key]=Decimal(str(value))
    cash=numbers[cash_key];ratio=numbers['share_multiplier']
    if cash<0 or ratio<=0:raise ValueError('配股缴款或未知股份数量不能按分红处理')
    if cash==0 and ratio==1:raise ValueError('无经济变动的记录不能解除公司行动待核验状态')
    if modeled and (ex<'2015-09-08' or ratio<1 or action['tax_status']==DIVIDEND_TAX_MODEL and (ratio!=1 or cash<=0)):
        raise ValueError('自然人模型仅支持2015年9月8日后的现金或已核对转增')
    if action.get('tax_status') not in ('final_net_verified',DIVIDEND_TAX_MODEL,TRANSFER_MODEL):
        raise ValueError('股息及送股相关税款尚未最终核验，不能使用统一扣税后字段记账')
    pay=day('pay_session',required=cash>0)
    sellable=day('shares_sellable_on',required=ratio!=1)
    if pay is not None and pay<ex:raise ValueError('分红到账日不能早于除权日')
    if sellable is not None and sellable<ex:raise ValueError('新增股份可卖日期不能早于除权日')
    reference={}
    for key,default in (('ex_price_cash_per_share',cash),('ex_price_share_multiplier',ratio)):
        value=action.get(key,default)
        if isinstance(value,bool) or not isinstance(value,(int,float,Decimal)) or not math.isfinite(float(value)):
            raise ValueError('除权参考口径必须是有限数值')
        reference[key]=float(value)
    if reference['ex_price_cash_per_share']<0 or reference['ex_price_share_multiplier']<=0:raise ValueError('除权参考口径无效')
    if action.get('tax_status')==TRANSFER_MODEL and action.get('tax_evidence') not in ('explicit_implementation_resident_tax_table','explicit_share_premium_transfer_exemption'):
        raise ValueError('转增缺少明确免税或完整自然人补税表证据')
    return {'event_id':event_id,'code':code,'record_session':record,'ex_session':ex,
            'net_cash_per_share':cash,'share_multiplier':ratio,'pay_session':pay,'modeled_tax':modeled,
            'shares_sellable_on':sellable,'source_sha256':sha,'tax_status':action['tax_status'],**reference}


def advance_account(account, session, bars, signals, *, entries=(), actions=(), previous_session=None):
    """Process one actual exchange session; caller supplies the real calendar.

    ``entries`` were fixed before this session's opening. Daily range/volume
    checks audit a hypothetical fill after the day; they do not certify auction
    allocation. Unknown action events block the affected position and NAV.
    """
    state=deepcopy(account);bars=deepcopy(bars); events=[]
    for bar in bars.values():
        if isinstance(bar,dict):bar.pop('documented_ex_reference_close',None)
    if state['last_session'] is not None and (session<=state['last_session'] or previous_session!=state['last_session']):
        raise ValueError('账户交易日必须连续且不可重复执行')
    if len({r['code'] for r in entries})!=len(entries):raise ValueError('重复入场指令')
    for row in entries:canonical_code(row['code'])
    positions=state['positions'];prior_nav=state['nav_cents'];multiplier=state['cost_multiplier']
    state['sessions_processed']+=1
    # Validate the entire batch before processing any event, including events
    # for stocks not held. Two events on one security need an audited combined
    # entitlement; sequential multiplication would use the wrong base quantity.
    validated=[_action(a,session,previous_session) for a in actions]
    ids=[a['event_id'] for a in validated];codes=[a['code'] for a in validated]
    if len(ids)!=len(set(ids)) or set(ids)&set(state['processed_action_ids']):raise ValueError('重复公司行动')
    if len(codes)!=len(set(codes)):raise ValueError('同股同日多项公司行动须先核验合并权益，不能串乘')
    for action in validated:
        code=action['code']
        if code not in positions:continue
        p=positions[code];q=p['quantity']
        if p['entry_session']>action['record_session']:raise ValueError('没有权益登记日持仓，不能取得分配权益')
        if p.get('quarantine'):raise ValueError('历史未解决公司行动须补齐并重放，不能由新事件清除')
        if action['modeled_tax']:
            current_bar=bars.get(code) or {};old_interval=p.get('adjustment_interval');new_interval=_interval(current_bar)
            previous=current_bar.get('previous_close');cash=action['ex_price_cash_per_share']
            if (isinstance(previous,bool) or not isinstance(previous,(int,float)) or not math.isfinite(previous)
                    or previous<=cash or not old_interval or not new_interval):
                raise ValueError('现金分红缺少可核对的前收盘及复权证据')
            reference=(previous-cash)/action['ex_price_share_multiplier']
            factor=previous/reference
            if max(old_interval[0]*factor,new_interval[0])>min(old_interval[1]*factor,new_interval[1]):
                raise ValueError('复权变化不能由本次分配除权参考口径解释，不能清除其他未知事件')
            current_bar['documented_ex_reference_close']=reference
        ratio=action['share_multiplier'];newq=Decimal(q)*ratio
        if newq!=newq.to_integral_value():
            raise ValueError('无法处理未知或零碎股处置')
        payout=cents(Decimal(q)*action['net_cash_per_share'])
        if action['modeled_tax'] and payout:
            # Gross cash is paid first. Reserve tax using a hypothetical sale
            # today until the actual conditional exit fixes the holding period.
            tax_cents(payout,p['entry_session'],session)
            state.setdefault('dividend_tax_lots',[]).append({'event_id':action['event_id'],
                'code':code,'entry_session':p['entry_session'],'gross_cents':payout,
                'pay_session':action['pay_session'],'settled':False,'tax_status':action['tax_status']})
        if payout:
            state['receivables'].append({'event_id':action['event_id'],'code':code,'cents':payout,'due':action['pay_session']})
            p['income_cents']+=payout
        p['quantity']=int(newq)
        if ratio!=1:p['sellable_on']=max(p['sellable_on'],action['shares_sellable_on'])
        p['quarantine']=None;p['adjustment_interval']=_interval(bars.get(code))
        state['processed_action_ids'].append(action['event_id'])
        events.append({'type':'company_action','code':code,'event_id':action['event_id'],'receivable_cents':payout,
                       'record_session':action['record_session'],'source_sha256':action['source_sha256'],
                       'tax_status':action['tax_status'],'prior_quantity':q,'new_quantity':p['quantity']})
    remaining=[]
    for receivable in state['receivables']:
        if receivable['due']<=session:
            state['cash_cents']+=receivable['cents'];events.append({'type':'distribution_paid',**receivable})
        else:remaining.append(receivable)
    state['receivables']=remaining
    settle_tax(state,session,events)
    for code,p in list(positions.items()):
        p['holding_sessions']+=1
        interval=_interval(bars.get(code));old=p.get('adjustment_interval')
        p['data_gap']=None
        if interval is None or old is None:
            p['data_gap']='adjustment_evidence_missing'
        elif max(old[0],interval[0])>min(old[1],interval[1]):
            p['quarantine']=p.get('quarantine') or 'company_action_unresolved'
        elif not p.get('quarantine'):
            p['adjustment_interval']=[max(old[0],interval[0]),min(old[1],interval[1])]
        if p.get('pending_exit'):
            reason=p.get('quarantine') or p.get('data_gap') or _blocked(bars.get(code))
            if p['entry_session']>=session or session<p.get('sellable_on',session):reason='T_plus_1_or_share_lock'
            if not reason and p['quantity']>bars[code]['volume_shares']*.01:reason='daily_capacity_unverified'
            if reason:
                events.append({'type':'exit_deferred','code':code,'reason':reason});continue
            gross=cents(p['quantity']*bars[code]['open']);costs=fee(gross,session,'sell',multiplier)
            state['cash_cents']+=gross-sum(costs.values())
            dividend_tax=0
            for lot in state.get('dividend_tax_lots',[]):
                if lot['code']==code and lot['entry_session']==p['entry_session'] and not lot.get('exit_session'):
                    lot['exit_session']=session
                    dividend_tax+=tax_cents(lot['gross_cents'],lot['entry_session'],session)
            settle_tax(state,session,events)
            trade={'code':code,'entry_session':p['entry_session'],'exit_session':session,
                   'quantity':p['quantity'],'holding_sessions':p['holding_sessions'],
                   'cost_basis_cents':p['cost_basis_cents'],'exit_value_cents':gross,
                   'net_pnl_including_verified_receivables_cents':gross-sum(costs.values())+p['income_cents']-p['cost_basis_cents']-dividend_tax,
                   'modeled_dividend_tax_cents':dividend_tax,
                   'exit_costs_cents':costs,'certainty':state['certainty']}
            state['closed_trades'].append(trade);events.append({'type':'sell',**trade});del positions[code]
    # Entries are grouped by exact allocation rank. An oversized tied group is
    # not arbitrarily truncated or replaced by a lower-ranking future winner.
    groups={}
    for row in entries:groups.setdefault(row['rank'],[]).append(row)
    for rank,group in sorted(groups.items()):
        fresh=[r for r in group if r['code'] not in positions]
        for r in group:
            if r['code'] in positions:events.append({'type':'entry_skipped','code':r['code'],'reason':'already_held'})
        if len(fresh)>state['max_positions']-len(positions):
            events.extend({'type':'entry_skipped','code':r['code'],'reason':'slot_limit_or_ambiguous_tie'} for r in fresh)
            break
        for row in fresh:
            code=row['code'];bar=bars.get(code);reason=_blocked(bar)
            ceiling=row.get('entry_price_ceiling')
            if ceiling is not None:
                if isinstance(ceiling,bool) or not isinstance(ceiling,(int,float)) or not math.isfinite(ceiling) or ceiling<=0:
                    raise ValueError('冻结入场价格上限无效')
                if not reason and bar['open']>ceiling+1e-8:reason='entry_price_ceiling_exceeded'
            if prior_nav is None:reason='previous_nav_unverified'
            if not _interval(bar):reason=reason or 'adjustment_evidence_missing'
            if reason:events.append({'type':'entry_rejected','code':code,'reason':reason});continue
            cap=float(row['mean_amount20'])*.01
            if not math.isfinite(cap) or cap<=0:raise ValueError('入场前流动性证据无效')
            weight=.95/state['max_positions']
            budget=min(int(prior_nav*weight),state['cash_cents']-tax_reserve(state,session)-int(prior_nav*.05),cents(cap))
            quantity,gross,costs=_quantity(code,max(0,budget),bar['open'],session,multiplier)
            if not quantity:reason='cash_or_minimum_lot'
            elif quantity>bar['volume_shares']*.01:reason='daily_capacity_unverified'
            if reason:events.append({'type':'entry_rejected','code':code,'reason':reason});continue
            cost=gross+sum(costs.values());state['cash_cents']-=cost
            positions[code]={'quantity':quantity,'entry_session':session,'sellable_on':session,
                'holding_sessions':0,'cost_basis_cents':cost,'income_cents':0,'pending_exit':None,
                'mark':bar['close'],'quarantine':None,'adjustment_interval':_interval(bar)}
            events.append({'type':'buy','code':code,'quantity':quantity,'gross_cents':gross,
                           'costs_cents':costs,'cash_after_cents':state['cash_cents']})
    unknown=[]
    for code,p in positions.items():
        bar=bars.get(code)
        if not bar or not bar.get('valid') or p.get('quarantine') or p.get('data_gap'):
            unknown.append({'code':code,'reason':p.get('quarantine') or p.get('data_gap') or 'mark_missing'})
        else:p['mark']=bar['close']
        signal=signals.get(code,{}).get('state','unknown')
        limit=state.get('max_holding_sessions')
        if limit is not None and p['holding_sessions']+1>=limit and not p['pending_exit']:
            p['pending_exit']=session
            events.append({'type':'holding_limit_exit_scheduled','code':code,'holding_sessions_including_entry':p['holding_sessions']+1})
        if signal=='sell' and not p['pending_exit']:p['pending_exit']=session
        if signal=='unknown':events.append({'type':'signal_unknown','code':code})
    state['dividend_tax_reserve_cents']=tax_reserve(state,session)
    indicative=state['cash_cents']+sum(cents(p['quantity']*p['mark']) for p in positions.values())+sum(r['cents'] for r in state['receivables'])-state['dividend_tax_reserve_cents']
    state['nav_cents']=None if unknown else indicative
    state['last_marked_nav_cents']=indicative;state['last_session']=session
    assert state['cash_cents']>=0 and len(positions)<=state['max_positions']
    snapshot={'session':session,'cash_cents':state['cash_cents'],'nav_cents':state['nav_cents'],
              'indicative_value_cents':indicative,'position_count':len(positions),
              'unverified_positions':unknown,'receivables_cents':sum(r['cents'] for r in state['receivables']),
              'events':events,'certainty':state['certainty']}
    snapshot['dividend_tax_reserve_cents']=state['dividend_tax_reserve_cents']
    return state,snapshot


def account_metrics(account, snapshots):
    values=[r['nav_cents'] for r in snapshots]
    complete=bool(values) and all(v is not None for v in values)
    result={'valuation_complete':complete,'sessions':len(values),
            'unverified_nav_sessions':sum(v is None for v in values),'closed_trades':len(account['closed_trades']),
            'open_positions':len(account['positions']),'total_return':None,'max_drawdown':None,
            'recommendation_eligible':False,'certainty':'conditional historical/paper account, not live performance',
            'corporate_actions_audited':False,
            'dividend_tax_model':DIVIDEND_TAX_MODEL if account.get('dividend_tax_lots') else None,
            'real_withholding_verified':False,
            'valuation_basis':'conditional_daily_marks_and_factor_compatibility_not_complete_event_audit'}
    if complete:
        peak=account['initial_cash_cents'];drawdown=0
        for value in values:peak=max(peak,value);drawdown=min(drawdown,value/peak-1)
        result.update(total_return=values[-1]/account['initial_cash_cents']-1,max_drawdown=drawdown)
    return result
