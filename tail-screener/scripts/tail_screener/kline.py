"""Completed-bar candle geometry; descriptive evidence, never a probability.

Inputs share one adjustment basis. Time is axis 0; no future bars are read.
"""
from __future__ import annotations
import numpy as np

SHAPE = ('body_open','range_open','signed_body_range','upper_open','upper_range',
         'lower_open','lower_range','close_balance_open','close_balance_range')
FEATURE_NAMES = tuple(f'k_{name}_lag{lag}' for lag in range(3) for name in SHAPE) + (
    'k_inside','k_outside','k_bull_body_engulf','k_bear_body_engulf',
    'k_close_prior5high_atr','k_close_prior5low_atr','k_close_prior19high_atr',
    'k_close_prior19low_atr','k_range5_over15','k_amount_over_prior5',
    'k_close_progress3_atr','k_high_rejection5','k_low_recovery5')

def _divide(a,b):
    a,b=np.broadcast_arrays(a,b)
    return np.divide(a,b,out=np.zeros(a.shape,dtype=float),where=b!=0)

def features(fields):
    """Return (...,40) features for the last 20 completed bars; raise on bad OHLC."""
    a={k:np.asarray(fields[k],dtype=float)[-20:] for k in ('open','high','low','close','amount')}
    o,h,l,c,v=(a[k] for k in ('open','high','low','close','amount'))
    if len(c)!=20 or any(x.shape!=c.shape for x in a.values()):raise ValueError('20 aligned bars required')
    if any(not np.isfinite(x).all() for x in a.values()):raise ValueError('Nonfinite candle input')
    # Daily input integrity is checked upstream; allow tiny representation noise only.
    tol=np.maximum(h,1)*1e-7
    if (np.minimum.reduce([o,h,l,c])<=0).any() or (v<=0).any() or (h+tol<np.maximum(o,c)).any() or (l-tol>np.minimum(o,c)).any() or (h+tol<l).any():raise ValueError('Invalid candle input')
    span=np.maximum(h-l,0);body=c-o;upper=np.maximum(h-np.maximum(o,c),0);lower=np.maximum(np.minimum(o,c)-l,0)
    out=[]
    for lag in range(3):
        j=-1-lag
        out.extend([body[j]/o[j],span[j]/o[j],_divide(body[j],span[j]),upper[j]/o[j],_divide(upper[j],span[j]),lower[j]/o[j],_divide(lower[j],span[j]),(2*c[j]-h[j]-l[j])/o[j],_divide(2*c[j]-h[j]-l[j],span[j])])
    prior_high=h[-6:-1].max(axis=0);prior_low=l[-6:-1].min(axis=0)
    tr=np.maximum.reduce([span[1:],np.abs(h[1:]-c[:-1]),np.abs(l[1:]-c[:-1])]);atr=tr[-14:].mean(axis=0)
    bull=(body[-1]>0)&(body[-2]<0)&(o[-1]<=c[-2])&(c[-1]>=o[-2])
    bear=(body[-1]<0)&(body[-2]>0)&(o[-1]>=c[-2])&(c[-1]<=o[-2])
    out.extend([(h[-1]<h[-2])&(l[-1]>l[-2]),(h[-1]>h[-2])&(l[-1]<l[-2]),bull,bear,
        _divide(c[-1]-prior_high,atr),_divide(c[-1]-prior_low,atr),
        _divide(c[-1]-h[:-1].max(axis=0),atr),_divide(c[-1]-l[:-1].min(axis=0),atr),
        _divide(span[-5:].mean(axis=0),span[-20:-5].mean(axis=0)),v[-1]/v[-6:-1].mean(axis=0),
        _divide(c[-1]-c[-4],atr),(h[-1]>prior_high)&(c[-1]<prior_high),(l[-1]<prior_low)&(c[-1]>prior_low)])
    return np.stack(out,axis=-1)

def review(fields):
    """Scalar public result, prices rebased to the signal-day raw-price basis."""
    try:
        values=features(fields)
        if values.ndim!=1:raise ValueError('Single security required')
        factors=np.asarray(fields['factor'],dtype=float)[-20:]
        if factors.shape!=(20,) or not np.isfinite(factors).all() or (factors<=0).any():raise ValueError('20 aligned positive factors required')
        factor=float(factors[-1])
        a={k:np.asarray(fields[k],dtype=float)[-20:] for k in ('open','high','low','close','amount')}
        o,h,l,c,v=(a[k] for k in ('open','high','low','close','amount'))
        metrics=dict(zip(FEATURE_NAMES,map(float,values)))
        flags=[]
        for key,label in [('k_inside','内包日'),('k_outside','外包日'),('k_bull_body_engulf','阳实体覆盖前一阴实体'),('k_bear_body_engulf','阴实体覆盖前一阳实体'),('k_high_rejection5','上破前5日高点后收回'),('k_low_recovery5','跌破前5日低点后收回')]:
            if metrics[key]:flags.append(label)
        if metrics['k_upper_range_lag0']>=.5:flags.append('上影占当日振幅至少一半')
        if metrics['k_lower_range_lag0']>=.5:flags.append('下影占当日振幅至少一半')
        if h[-1]>l[-1] and abs(metrics['k_signed_body_range_lag0'])<=.1:flags.append('小实体：不超过振幅一成')
        upper=float(h[-6:-1].max()/factor);lower=float(l[-6:-1].min()/factor)
        position='收盘突破前5日高点' if c[-1]>h[-6:-1].max() else '收盘跌破前5日低点' if c[-1]<l[-6:-1].min() else '收盘位于前5日高低区间内'
        return {'status':'computed','basis':'adjusted_window_rebased_to_signal_raw_price','bars_used':20,
            'position':position,'patterns':flags,'body_range_ratio':abs(metrics['k_signed_body_range_lag0']),
            'upper_shadow_ratio':metrics['k_upper_range_lag0'],'lower_shadow_ratio':metrics['k_lower_range_lag0'],
            'amount_vs_previous5':metrics['k_amount_over_prior5'],'prior5_high':upper,'prior5_low':lower,
            'range5_over_previous15':metrics['k_range5_over15'],
            'candles':[{'open':float(o[i]/factor),'high':float(h[i]/factor),'low':float(l[i]/factor),'close':float(c[i]/factor),'amount':float(v[i]*1000)} for i in range(20)]}
    except (ValueError,KeyError,TypeError,IndexError,OverflowError):
        return {'status':'unknown','reason':'20_aligned_valid_completed_bars_required','patterns':[]}
