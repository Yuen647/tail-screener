"""Frozen study06 numerical features, retained for explicit experimental ranking."""
import numpy as np
from .account_engine import cents

def fees(g,day,sell=False):
    # Exact integer rational rates, half-up per component.
    g=np.asarray(g,dtype=np.float64)
    return np.maximum(500,(g*3+5000)//10000)+(g*(2 if day<'2022-04-29' else 1)+50000)//100000+(g+500)//1000+(((g+500)//1000 if day<'2023-08-28' else (g+1000)//2000) if sell else 0)

def money(values):
    values=np.asarray(values,dtype=np.float64);scaled=values*100
    result=np.floor(scaled+.5)
    boundary=np.isfinite(values)&(np.abs(scaled-np.floor(scaled)-.5)<1e-7)
    # Decimal(str(value)) only at half-cent boundaries; same semantics as cents().
    for k in np.flatnonzero(boundary):result.flat[k]=cents(float(values.flat[k]))
    return result

def quantity(codes,budget,price,day):
    star=np.char.startswith(codes,'SH688');step=np.where(star,1,100);minimum=np.where(star,200,100)
    q=np.floor(budget/(price*100)/step)*step
    q=np.where(np.isfinite(q),q,0)
    while True:
        g=money(q*price);f=fees(g,day)
        over=(q>=minimum)&(g+f>budget)
        if not over.any():break
        q[over]-=step[over]
    q=np.where(q>=minimum,q,0);g=np.where(q>0,money(q*price),0)
    return q,g,np.where(q>0,fees(g,day),0)

def percentiles(v):
    order=np.argsort(v,kind='stable');s=v[order];starts=np.r_[0,np.flatnonzero(np.diff(s)!=0)+1];ends=np.r_[starts[1:],len(v)]
    rank=np.empty(len(v));rank[order]=np.repeat((starts+ends)/(2*len(v)),ends-starts)
    return rank*2-1

FEATURES=['r1','r3','r5','r19','vol19','location','up_amount5','amount5_20','premium20','drawdown20']
FEATURES=FEATURES+['rank_'+x for x in FEATURES]+['log_amount','log_price','round_trip_cost','invested_fraction','intraday_return','opening_gap','daily_range','market_r5','market_breadth5']
FEATURES+=['ma5_over20','ma5_change','rank_ma5_over20','rank_ma5_change']

def research_rows(a,valid,active,days,codes,t,budget):
    c=a['close'][t-19:t+1];amt=a['amount'][t-19:t+1]*1000;ma5=c[-5:].mean(axis=0);ma20=c.mean(axis=0);meanamt=amt.mean(axis=0)
    price=c[-1]/a['factor'][t];q,g,fc=quantity(codes,budget,price,str(days[t]))
    mask=active[t]&valid[t-19:t+1].all(axis=0)&(meanamt>=1e7)&(c[-1]/ma20<=1.08+1e-10)&(q>0)
    idx=np.flatnonzero(mask)
    if not len(idx):return idx,np.empty((0,len(FEATURES))),np.array([]),np.array([]),np.array([],bool)
    cc=c[:,idx];aa=amt[:,idx];r=np.diff(cc,axis=0)/cc[:-1];ranges=a['high'][t,idx]-a['low'][t,idx]
    loc=np.divide(a['close'][t,idx]-a['low'][t,idx],ranges,out=np.full(len(idx),.5),where=ranges>0)
    v=np.column_stack([cc[-1]/cc[-2]-1,cc[-1]/cc[-4]-1,cc[-1]/cc[-6]-1,cc[-1]/cc[0]-1,r.std(axis=0,ddof=1),loc,((r[-5:]>0)*aa[-5:]).sum(axis=0)/aa[-5:].sum(axis=0),aa[-5:].mean(axis=0)/aa.mean(axis=0),cc[-1]/ma20[idx]-1,cc[-1]/cc.max(axis=0)-1])
    market=active[t]&valid[t-19:t+1].all(axis=0);market_r=c[-1,market]/c[-6,market]-1
    extra=np.column_stack([np.log(meanamt[idx]),np.log(price[idx]),(fc[idx]+fees(g[idx],str(days[t]),True))/budget,g[idx]/budget,cc[-1]/a['open'][t,idx]-1,a['open'][t,idx]/cc[-2]-1,ranges/cc[-2],np.full(len(idx),np.median(market_r)),np.full(len(idx),np.mean(market_r>0))])
    mgap=ma5[idx]/ma20[idx]-1;mslope=ma5[idx]/c[-6:-1,idx].mean(axis=0)-1
    x=np.column_stack([v,*[percentiles(v[:,k]) for k in range(v.shape[1])],extra,mgap,mslope,percentiles(mgap),percentiles(mslope)])
    ceiling=np.minimum(price[idx]*1.03,ma20[idx]/a['factor'][t,idx]*1.08)
    return idx,x,ceiling,meanamt[idx],ma5[idx]>ma20[idx]
