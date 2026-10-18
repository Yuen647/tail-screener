"""AKQuant's stock MA5/20 state rule and explicitly local liquidity allocation.

This reproduces its inequalities, including equality doing nothing. It does not
claim that the educational single-stock example validates a five-stock system.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import numpy as np
from .market_integrity import BAR_FIELDS, bar_integrity_mask, deduplicate_source_codes
from .decision_review import price_diagnostics
from .kline import review as kline_review
from .kline_catalog import build as build_kline_catalog

STRATEGY_ID = 'akquant-ma5-20-account'


def rule_identity():
    root = Path(__file__).resolve().parents[2]
    protocol = root / 'references/account-workflow/protocol.json'
    files = [protocol, protocol.with_name('pre-evaluation-affordability-clarification.json'),
             *sorted((root/'scripts').rglob('*.py'))]
    # Conservative until behavior-equivalence upgrades have an audited migration.
    return hashlib.sha256(b''.join(p.relative_to(root).as_posix().encode()+b'\0'+p.read_bytes()+b'\0'
                                   for p in files)).hexdigest()


def ma_state(closes):
    values = np.asarray(closes, dtype=float)
    if len(values) < 20 or not np.isfinite(values[-20:]).all() or (values[-20:] <= 0).any():
        return {'state': 'unknown', 'ma5': None, 'ma20': None}
    short, long = float(values[-5:].mean()), float(values[-20:].mean())
    return {'state': 'buy' if short > long else 'sell' if short < long else 'hold',
            'ma5': short, 'ma20': long}


def rank_rows(rows, top=6):
    """No code-based allocation tie break; code only stabilizes presentation."""
    rows = sorted(rows, key=lambda r: (-r['mean_amount20'], -r['amount_yuan'], r['code']))
    last = None
    for index, row in enumerate(rows):
        key = (row['mean_amount20'], row['amount_yuan'])
        if key != last: rank = index + 1
        row['rank'] = rank
        last = key
    return [row for row in rows if row['rank'] <= top]


def scan_account(store, session, excluded=None, top=6, *, config=None):
    excluded = excluded or {}
    capital_cents=config['initial_capital_cents'] if config else 10000000
    slots=config['max_positions'] if config else 6
    nominal_budget=capital_cents*95//(100*slots)
    last = store.session_index[session]
    research_pool=[]
    rows, signals, conditions, counts = [], {}, {}, {'universe': 0, 'excluded': 0, 'incomplete': 0,
                                   'evaluated': 0, 'bullish': 0, 'liquidity_excluded': 0, 'nominal_lot_excluded': 0,
                                   'chase_excluded':0}
    for identity in deduplicate_source_codes(store.codes(session), session):
        code = identity['stable_id']; counts['universe'] += 1
        conditions[code]={'state':'unknown','reason':'input_unverified'}
        try:
            age = last - np.searchsorted(store.sessions, min(a for a,b in store.intervals[identity['data_code']]))
            if age < 252:
                conditions[code]={'state':'invalid','reason':'listing_age'}
                signals[code]={'state':'unknown','ma5':None,'ma20':None}
                counts['liquidity_excluded'] += 1; continue
            f = {key: store.values(identity['data_code'], key, max(0, last-19), last) for key in BAR_FIELDS}
            signal = ma_state(f['close']); signals[code] = signal
            if code in excluded:
                conditions[code]={'state':'unknown' if any(x.get('category')=='data' for x in excluded[code]) else 'invalid',
                                  'reason':'eligibility_or_quote_check'}
                counts['excluded'] += 1; continue
            if last < 19 or not bar_integrity_mask(f)['valid'].all():
                counts['incomplete'] += 1; continue
            counts['evaluated'] += 1
            amount = float(f['amount'].mean()*1000)
            if age < 252 or amount < 10_000_000:
                conditions[code]={'state':'invalid','reason':'liquidity'}
                counts['liquidity_excluded'] += 1; continue
            from .account_engine import cents, fee
            raw_price=float(f['close'][-1]/f['factor'][-1])
            premium=(float(f['close'][-1])/signal['ma20']-1)*100
            minimum=200 if code.startswith('SH688') else 100
            gross=cents(minimum*raw_price)
            affordable=gross+sum(fee(gross,session,'buy').values())<=nominal_budget
            diagnostics=price_diagnostics(f['close'])
            candles=kline_review(f)
            candles['sessions']=list(store.sessions[max(0,last-19):last+1])
            failures=[]
            if signal['state']!='buy':failures.append('ma_condition')
            if config and premium>config['max_ma20_premium_pct']+1e-10:failures.append('ma20_premium')
            if affordable:
                research_pool.append({'code':code,'display_code':identity['display_code'],**signal,
                    'price':raw_price,'ma20_premium_pct':premium,'mean_amount20':amount,
                    'amount_yuan':float(f['amount'][-1]*1000),'screen_passed':not failures,
                    'price_diagnostics':diagnostics, 'kline_review':candles,
                    'screen_rejections':failures})
            if signal['state'] != 'buy':
                conditions[code]={'state':'unknown' if signal['state']=='unknown' else 'invalid','reason':'ma_condition'}
                continue
            if config and premium>config['max_ma20_premium_pct']+1e-10:
                conditions[code]={'state':'invalid','reason':'ma20_premium'}
                counts['chase_excluded']+=1;continue
            # Price ceiling is frozen with the signal, never raised after
            # seeing the next session's open. It is a research constraint.
            ceiling=(min(raw_price*(1+config['max_entry_gap_pct']/100),
                         signal['ma20']/float(f['factor'][-1])*(1+config['max_ma20_premium_pct']/100)) if config else None)
            if not affordable:
                conditions[code]={'state':'invalid','reason':'nominal_affordability'}
                counts['nominal_lot_excluded']+=1;continue
            counts['bullish'] += 1
            conditions[code]={'state':'valid','reason':'all_screen_conditions_passed'}
            rows.append({'code': code, 'display_code': identity['display_code'], **signal,
                         'mean_amount20': amount, 'amount_yuan': float(f['amount'][-1]*1000),
                         'price': raw_price,
                         'price_diagnostics':diagnostics, 'kline_review':candles,
                         'ma20_premium_pct':premium,'entry_price_ceiling':ceiling,
                         'holding_rule': ('收盘SMA5<SMA20或持有满'+str(config['max_holding_sessions'])+'个交易日后，下一开盘尝试退出；受阻延后'
                                          if config and config.get('max_holding_sessions') else '持有至收盘SMA5<SMA20后下一交易日尝试退出；受阻延后，未设固定天数')})
        except (ValueError, OSError, KeyError):
            signals[code] = {'state': 'unknown', 'ma5': None, 'ma20': None}
            counts['incomplete'] += 1
    selected = rank_rows(rows, top)
    counts['displayed'] = len(selected)
    return {'strategy_id': STRATEGY_ID, 'session': session, 'signals': signals, 'conditions':conditions,
            'candidates': selected, 'research_pool':research_pool, 'stats': counts,
            'kline_catalog':build_kline_catalog(rows,session)}


def precision_interval(adjclose, raw_close):
    """Same documented cent/float32 compatibility assumption as prior audit.

    A compatible interval is only a numerical scenario, never event evidence.
    """
    if not all(np.isfinite(x) and x > 0 for x in (adjclose, raw_close)): return None
    u = 2**-24
    da = .005 + u/(1-u)*abs(adjclose)
    dc = 2*u/(1-u)*abs(raw_close)
    if raw_close <= dc: return None
    return [(adjclose-da)/(raw_close+dc), (adjclose+da)/(raw_close-dc)]


def extra_adjclose(store, code, first, last):
    if hasattr(store, 'extra_adjclose'):
        return store.extra_adjclose(code,first,last)
    # Extra field is not silently added to the existing DailyStore public schema.
    from .daily_store import canonical_code
    code = canonical_code(code)
    path = store.root / 'features' / code.lower() / 'adjclose.day.bin'
    values = np.full(last-first+1, np.nan)
    if not path.is_file() or path.stat().st_size % 4: return values
    data = np.fromfile(path, dtype='<f4')
    if not len(data) or not np.isfinite(data[0]) or data[0] != int(data[0]): return values
    start = int(data[0]); left, right = max(first,start), min(last+1,start+len(data)-1)
    if start < 0 or start+len(data)-1 > len(store.sessions): return values
    if right > left: values[left-first:right-first] = data[1+left-start:1+right-start]
    return values


def read_bar(store, code, session):
    index = store.session_index[session]
    f = {key: store.values(code,key,index,index) for key in BAR_FIELDS}
    if not bar_integrity_mask(f)['valid'][0]: return {'valid': False, 'reason': 'bar_integrity'}
    raw = {key: float(f[key][0]/f['factor'][0]) for key in ('open','close','high','low')}
    previous = store.values(code,'close',max(0,index-1),max(0,index-1))[0]
    pf = store.values(code,'factor',max(0,index-1),max(0,index-1))[0]
    previous = previous/pf if np.isfinite(pf) and pf > 0 else None
    interval = precision_interval(extra_adjclose(store,code,index,index)[0],raw['close'])
    return {**raw, 'valid': True, 'previous_close': float(previous) if previous is not None and np.isfinite(previous) else None,
            'volume_shares': float(f['volume'][0]*f['factor'][0]*100),
            'adjustment_interval': interval, 'execution_evidence': 'conditional_daily_bar_scenario',
            'official_limits_verified': False, 'auction_allocation_verified': False}
