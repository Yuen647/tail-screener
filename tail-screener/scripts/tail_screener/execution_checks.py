"""Audit claimed fills against cash, T+1 and observed market capacity.

This is an offline consistency audit, not an order simulator or a performance
certificate. Inputs must use *raw shares and RMB*, never Qlib normalized units.
No historical ST status or exchange price limit is inferred from a code/name.
Unknown evidence remains unverified; existing returns are never recomputed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json


def _number(value, *, positive=False):
    if isinstance(value, bool) or value is None:
        raise ValueError('missing or invalid number')
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('invalid number') from exc
    if not number.is_finite() or number < 0 or (positive and number == 0):
        raise ValueError('number must be finite and nonnegative')
    return number


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('trade_date must be YYYY-MM-DD')
    return value


def audit_fills(fills, daily_bars, *, initial_cash_yuan, initial_positions=None):
    """Return an inspectable audit without changing the supplied ledger.

    ``fills`` are in execution order: trade_date, code, side (buy/sell),
    quantity_shares, price_yuan, fee_yuan. ``daily_bars`` are dated records with
    code, volume_shares, amount_yuan, high_yuan, low_yuan; documented actual
    limit_up_yuan/limit_down_yuan and limit_source establish known limits.
    A documented ``limit_status='none'`` covers sessions without price limits.
    Missing fees or market fields are unverified, never assumed verified zero.

    ``initial_positions`` maps code to raw shares acquired *before* the first
    fill date. Pass {} to declare no positions; None means unknown inventory.
    Callers must not put same-day purchases into this inventory.
    Cash checks follow supplied within-day order; no cash is borrowed. Sellable
    inventory resets only on a later date, enforcing A-share T+1. This checks
    date order, not whether supplied dates are exchange trading sessions.
    Corporate actions are not modeled: split the ledger at an action and use
    verified new opening cash/inventory for the next audit segment.

    Same-direction daily fills are aggregated to catch split overfills. The
    sum of both sides above daily volume is flagged for verification rather
    than automatically called impossible: exchange volume counts each matched
    trade once, while an account may participate on both sides across trades.
    """
    fills, daily_bars = list(fills), list(daily_bars)
    inventory_known = initial_positions is not None
    initial_positions = dict(initial_positions or {})
    errors, unverified = [], []
    zero = Decimal(0)
    share_epsilon, money_epsilon = Decimal('0.000001'), Decimal('0.01')
    price_epsilon = Decimal('0.005')  # half a one-fen tick; float serialization tolerance

    def issue(target, code, message, **context):
        target.append({'code': code, 'message': message, **context})

    if not fills:
        issue(unverified, 'no_fills_to_audit', 'No executions were evaluated.')
    if not inventory_known:
        issue(unverified, 'initial_inventory_unknown', 'Cannot verify ownership or T+1 without starting inventory.')

    cash = None
    if initial_cash_yuan is None:
        issue(unverified, 'initial_cash_unknown', 'Cannot verify cash without starting cash.')
    else:
        try:
            cash = _number(initial_cash_yuan)
        except ValueError:
            issue(errors, 'invalid_initial_cash', 'Starting cash must be finite and nonnegative.')
    holdings = {}
    for code, quantity in initial_positions.items():
        try:
            holdings[code] = _number(quantity)
        except ValueError:
            issue(errors, 'invalid_initial_position', 'Invalid starting raw share quantity.', stock=code)
    available = dict(holdings)
    bars = {}
    for index, record in enumerate(daily_bars):
        try:
            day, code = _day(record['trade_date']), str(record['code'])
            if not code:
                raise ValueError('empty code')
            key = (day, code)
            if key in bars:
                issue(errors, 'duplicate_daily_bar', 'More than one bar for a stock/date.', trade_date=day, stock=code)
                continue
            bars[key] = record
        except (KeyError, TypeError, ValueError):
            issue(errors, 'invalid_daily_bar', 'Bar needs a valid date and code.', bar_index=index)

    aggregates = defaultdict(lambda: {'buy_shares': zero, 'sell_shares': zero,
                                      'buy_amount': zero, 'sell_amount': zero})
    checked_market = set()
    previous_day, fees_known, parsed_fills = None, True, 0
    net_cash_change = zero
    for index, record in enumerate(fills):
        context = {'fill_index': index}
        try:
            day, code, side = _day(record['trade_date']), str(record['code']), record['side']
            if not code or side not in ('buy', 'sell'):
                raise ValueError('invalid code or side')
            quantity = _number(record['quantity_shares'], positive=True)
            price = _number(record['price_yuan'], positive=True)
        except (KeyError, TypeError, ValueError):
            issue(errors, 'invalid_fill', 'Fill requires date, code, side, positive raw shares and RMB price.', **context)
            continue
        context.update(trade_date=day, stock=code, side=side)
        parsed_fills += 1
        try:
            fee = _number(record['fee_yuan'])
        except KeyError:
            fee, fees_known = zero, False
            issue(unverified, 'fee_unknown', 'Fee missing; zero is used only as a lower bound for cash checks.', **context)
        except ValueError:
            issue(errors, 'invalid_fee', 'Fee must be finite and nonnegative.', **context)
            fee, fees_known = zero, False

        if previous_day is not None and day < previous_day:
            issue(errors, 'nonchronological_fills', 'Fills must remain in actual execution order.', **context)
        elif previous_day != day:
            available = dict(holdings)
            previous_day = day
        held, sellable = holdings.get(code, zero), available.get(code, zero)
        value = quantity * price
        if side == 'sell':
            if inventory_known and quantity > held + share_epsilon:
                issue(errors, 'oversell', 'Sale exceeds all owned shares.', owned_shares=float(held), **context)
            elif inventory_known and quantity > sellable + share_epsilon:
                issue(errors, 't_plus_one_violation', 'Sale uses shares bought on the same date.', sellable_shares=float(sellable), **context)
            holdings[code] = held - quantity
            available[code] = sellable - quantity
            cash_change = value - fee
        else:
            holdings[code] = held + quantity
            cash_change = -value - fee
        net_cash_change += cash_change
        if cash is not None:
            cash += cash_change
            if cash < -money_epsilon:
                issue(errors, 'negative_cash', 'Execution requires undeclared borrowing or unavailable cash.', cash_yuan=float(cash), **context)

        key = (day, code)
        aggregates[key][side + '_shares'] += quantity
        aggregates[key][side + '_amount'] += value
        bar = bars.get(key)
        if bar is None:
            if key not in checked_market:
                issue(unverified, 'daily_bar_missing', 'Cannot verify price, volume or actual limits.', trade_date=day, stock=code)
            checked_market.add(key)
            continue
        try:
            high, low = _number(bar['high_yuan'], positive=True), _number(bar['low_yuan'], positive=True)
            if high < low:
                raise ValueError('reversed prices')
        except (KeyError, ValueError):
            high = low = None
            if key not in checked_market:
                issue(unverified, 'daily_price_range_unknown', 'Cannot verify observed execution range.', trade_date=day, stock=code)
        if high is not None and (price > high + price_epsilon or price < low - price_epsilon):
            issue(errors, 'fill_outside_daily_range', 'Execution price lies outside the observed daily range.', **context)

        documented = bool(bar.get('limit_source'))
        no_limit = bar.get('limit_status') == 'none' and documented
        upper = lower = None
        if not no_limit:
            try:
                upper = _number(bar['limit_up_yuan'], positive=True)
                lower = _number(bar['limit_down_yuan'], positive=True)
                if not documented or lower >= upper:
                    raise ValueError('undocumented or invalid limits')
            except (KeyError, ValueError):
                upper = lower = None
                if key not in checked_market:
                    issue(unverified, 'actual_limits_unknown', 'Actual dated exchange limits are not established; no board/ST limits inferred.', trade_date=day, stock=code)
        if upper is not None:
            if price > upper + price_epsilon or price < lower - price_epsilon:
                issue(errors, 'fill_outside_actual_limits', 'Execution price violates supplied documented daily limits.', **context)
            if high is not None and abs(high - low) <= price_epsilon:
                blocked = (side == 'buy' and abs(high - upper) <= price_epsilon) or (side == 'sell' and abs(low - lower) <= price_epsilon)
                if blocked:
                    issue(errors, 'one_price_limit_fill_unverified', 'Daily data cannot establish queue allocation for this one-price limit execution; do not accept a simulated fill.', **context)
        checked_market.add(key)

    for (day, code), totals in aggregates.items():
        bar = bars.get((day, code))
        if bar is None:
            continue
        for field, suffix, tolerance in [('volume_shares', 'shares', share_epsilon), ('amount_yuan', 'amount', money_epsilon)]:
            try:
                capacity = _number(bar[field])
            except (KeyError, ValueError):
                issue(unverified, 'daily_' + suffix + '_unknown', 'Daily market capacity is missing or invalid.', trade_date=day, stock=code)
                continue
            for side in ('buy', 'sell'):
                filled = totals[side + '_' + suffix]
                if filled > capacity + tolerance:
                    issue(errors, 'daily_' + suffix + '_exceeded', 'Same-direction claimed fills exceed the entire observed daily market.', trade_date=day, stock=code, side=side, filled=float(filled), market_capacity=float(capacity))
            if (totals['buy_' + suffix] <= capacity + tolerance
                    and totals['sell_' + suffix] <= capacity + tolerance
                    and totals['buy_' + suffix] + totals['sell_' + suffix] > capacity + tolerance):
                issue(unverified, 'combined_' + suffix + '_requires_trade_evidence', 'Both sides combined exceed reported volume; transaction-level evidence is needed.', trade_date=day, stock=code)

    fingerprint = hashlib.sha256(json.dumps({'fills': fills, 'daily_bars': daily_bars,
        'initial_cash_yuan': initial_cash_yuan, 'initial_positions': initial_positions if inventory_known else None},
        sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
    return {
        'schema_version': 1, 'status': 'invalid' if errors else ('unverified' if unverified else 'passed'),
        'execution_consistency_verified': not errors and not unverified,
        'input_sha256': fingerprint,
        'fill_count': len(fills), 'parsed_fill_count': parsed_fills,
        'errors': errors, 'unverified': unverified,
        'ending_cash_yuan': float(cash) if cash is not None and fees_known else None,
        'net_cash_change_yuan': float(net_cash_change) if fees_known else None,
        'ending_positions_raw_shares': {code: float(quantity) for code, quantity in holdings.items()} if inventory_known else None,
        'limitations': [
            'An audit pass is not proof of fill availability, strategy profitability or future performance.',
            'Inputs and initial positions require source verification; this function cannot authenticate broker records.',
            'No queue, intraday liquidity, calendar, slippage, delisting recovery or historical ST state is invented.',
            'Corporate actions require a new audit segment with verified adjusted opening inventory and cash.',
            'The original return series is not modified by this audit.',
        ],
    }
