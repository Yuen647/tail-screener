"""Mechanical stock trend template, with an explicitly non-proprietary RS proxy.

This is a candidate screen, not the author's complete trading system. It does
not define entries, exits, position sizes, holding times or expected returns.
All calculations use source-adjusted daily prices at an actual data session.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

import numpy as np

from .daily_store import canonical_code
from .market_integrity import BAR_FIELDS, bar_integrity_mask, source_identity


STRATEGY_ID = "minervini-trend"
SOURCE_URL = (
    "https://www.marketsmith.hk/v2/blog/"
    "the-holy-grail-in-investing-mark-minervini-trend-mode-cn?lang=zh-hans"
)
FORMULA_SESSIONS = 320
RS_RETURN_SESSIONS = 252
CONDITION_NAMES = (
    "close_above_ma150_and_ma200",
    "ma150_above_ma200",
    "ma200_above_120_sessions_ago",
    "ma50_above_ma150_and_ma200",
    "close_above_ma50",
    "close_at_least_130pct_52w_low",
    "close_at_least_75pct_52w_high",
    "rs_proxy_at_least_70",
)


def methodology():
    """Return a fresh JSON-serializable definition, including local adaptations."""
    return {
        "strategy_id": STRATEGY_ID,
        "source_url": SOURCE_URL,
        "source_date": "2023-08-08",
        "scope": "Shanghai and Shenzhen A shares only",
        "formula_history_sessions": FORMULA_SESSIONS,
        "price_basis": "source-adjusted OHLC; constant rescaling does not alter conditions",
        "display_price_basis": "历史价按数据源复权，统一换算为目标日人民币尺度；非历史未复权价均值",
        "year_window": "252 actual trading sessions; daily highs and lows, not closes",
        "ma200_rise_definition": (
            "MA200(t) > MA200(t-120 trading sessions); endpoint comparison, "
            "not a claim of 120 consecutive daily increases; local implementation choice"
        ),
        "rs_proxy": {
            "label": "沪深A股252交易日收益率横截面百分位代理（1–99）",
            "is_ibd_rs_rating": False,
            "return_formula": "adjusted_close(t)/adjusted_close(t-252)-1",
            "universe": (
                "all in-scope, non-excluded stocks with 253 complete positive OHLCV "
                "and independently consistent factor/VWAP/amount observations, "
                "before trend-template conditions; not only template matches"
            ),
            "rank_formula": "1 + 98 * (average_ascending_rank - 1)/(N - 1)",
            "singleton_score": 50.0,
            "ties": "equal unrounded returns receive average rank; all equal returns score 50",
            "limitation": "free local return-rank proxy; not the proprietary IBD/MarketSmith RS formula",
        },
        "selection": {
            "order": "descending unrounded RS proxy (equivalently 252-session return)",
            "boundary": "include every exact tie at the final requested position",
            "code_order": "stable display order within ties only; never excludes a boundary tie",
            "adaptation": "local candidate display priority, not an author-validated optimal portfolio",
        },
        "holding_period": "not defined by the trend template alone",
        "source_integrity": {
            "fields": list(BAR_FIELDS),
            "scope": "only the needed 320-session formula window; RS uses its final 253 sessions",
            "checks": "finite positive fields, OHLC geometry, VWAP within bar with fixed rounding allowance, amount/volume/VWAP consistency",
            "unit_correction": "no inferred division/multiplication repairs; inconsistent observations are incomplete",
            "identity": "documented SH601313/SH601360 aliases counted once; read verified SH601360 series, preserve date-correct display code",
        },
        "entry_exit": "not defined by the trend template alone",
        "validation": "mechanical screening; does not establish A-share investment performance",
    }


def _exclusions(excluded_codes, session):
    if excluded_codes is None:
        return {}
    items = excluded_codes.items() if isinstance(excluded_codes, Mapping) else (
        (code, "caller_exclusion") for code in excluded_codes
    )
    result = {}
    for code, reasons in items:
        code = source_identity(code, session)["stable_id"]
        if isinstance(reasons, (tuple, list, set)):
            value = [str(reason) for reason in reasons] or ["caller_exclusion"]
        else:
            value = [str(reasons)] if reasons is not None else ["caller_exclusion"]
        result.setdefault(code, []).extend(value)
    return result


def _ohlcv_valid(fields):
    prices = np.stack([fields[key] for key in ("open", "high", "low", "close")])
    return (
        np.isfinite(prices).all(axis=0) & (prices > 0).all(axis=0)
        & (fields["high"] >= prices.max(axis=0))
        & (fields["low"] <= prices.min(axis=0))
        & np.isfinite(fields["volume"]) & (fields["volume"] > 0)
    )


def _history_reasons(store, code, first, last, fields):
    reasons = []
    if last + 1 < FORMULA_SESSIONS:
        reasons.append("insufficient_calendar_history")
    # An interval's first date is a data-availability date, not proof of IPO date.
    intervals = getattr(store, "intervals", {}).get(code, [])
    if intervals and min(interval[0] for interval in intervals) > store.sessions[first]:
        reasons.append("insufficient_recorded_history")
    prices = np.stack([fields[key] for key in ("open", "high", "low", "close")])
    if not np.isfinite(prices).all():
        reasons.append("historical_ohlc_gap")
    if (np.isfinite(prices) & (prices <= 0)).any():
        reasons.append("nonpositive_historical_price")
    finite_prices = np.isfinite(prices).all(axis=0)
    inconsistent = (
        (fields["high"] < prices.max(axis=0)) | (fields["low"] > prices.min(axis=0))
    )
    if (finite_prices & inconsistent).any():
        reasons.append("inconsistent_historical_ohlc")
    volume = fields["volume"]
    if not np.isfinite(volume).all():
        reasons.append("historical_volume_gap")
    if (volume == 0).any():
        # Zero volume is observed; suspension itself needs separate evidence.
        reasons.append("zero_volume_sessions")
    if (volume < 0).any():
        reasons.append("negative_historical_volume")
    return reasons


def _integrity_reasons(fields, integrity):
    """Add independent source defects without relabeling old OHLCV failures."""
    reasons = []
    for field in ("factor", "vwap", "amount"):
        values = fields[field]
        if not np.isfinite(values).all():
            reasons.append(f"historical_{field}_gap")
        if (np.isfinite(values) & (values <= 0)).any():
            reasons.append(f"nonpositive_historical_{field}")
    for reason in ("vwap_outside_bar", "amount_volume_vwap_mismatch"):
        if integrity["reason_masks"][reason].any():
            reasons.append("historical_" + reason)
    return reasons


def _rs_scores(returns):
    """Exact-tie average ranks on 1..99; never round before ranking."""
    ordered = sorted(returns.items(), key=lambda item: (item[1], item[0]))
    count = len(ordered)
    scores = {}
    start = 0
    while start < count:
        end = start + 1
        while end < count and ordered[end][1] == ordered[start][1]:
            end += 1
        average_rank = (start + 1 + end) / 2
        score = 50.0 if count == 1 else 1 + 98 * (average_rank - 1) / (count - 1)
        for code, _ in ordered[start:end]:
            scores[code] = float(score)
        start = end
    return scores


def scan_trend(store, session, excluded_codes=None, top_n=5):
    """Evaluate full in-scope history and return auditable JSON-ready records.

    ``excluded_codes`` may be an iterable of stock codes or a code/reason mapping.
    Exclusions are applied before the RS comparison universe is constructed.
    Missing histories are incomplete, not failed conditions or zero-return rows.
    The caller is responsible for source freshness and current risk evidence.
    """
    if session not in store.session_index:
        raise ValueError("所需日期没有实际交易日线，不能用其他日期替代")
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError("top_n 必须是正整数")
    exclusions = _exclusions(excluded_codes, session)
    last = store.session_index[session]
    first = max(0, last - FORMULA_SESSIONS + 1)
    records, returns = [], {}
    identities, source_codes = {}, {}
    source_count = 0
    for supplied_code in store.codes(session):
        # Defence in depth: a malformed store cannot admit ETFs or another market.
        try:
            supplied_code = canonical_code(supplied_code)
        except ValueError:
            continue
        source_count += 1
        identity = source_identity(supplied_code, session)
        code = identity["stable_id"]
        identities.setdefault(code, identity)
        source_codes.setdefault(code, []).append(supplied_code)
    for code, identity in identities.items():
        data_code = identity["data_code"]
        row = {
            "code": code, "session": session, "status": "incomplete",
            "source_identity": dict(identity), "display_code": identity["display_code"],
            "source_codes": source_codes[code],
            "formula_complete": False, "matched": False,
            "conditions": dict.fromkeys(CONDITION_NAMES), "metrics": {},
            "reason_codes": [], "data_gaps": [], "rs_eligible": False,
        }
        records.append(row)
        if code in exclusions:
            row.update(status="excluded", reason_codes=["caller_exclusion"],
                       exclusion_reasons=exclusions[code])
            continue
        try:
            fields = {
                key: store.values(data_code, key, first, last)
                for key in BAR_FIELDS
            }
            if any(np.shape(values) != (last - first + 1,) for values in fields.values()):
                raise ValueError("历史字段长度与交易日历不符")
            current_factor = float(fields["factor"][-1])
        except (OSError, ValueError, KeyError) as exc:
            row["reason_codes"] = ["history_read_error"]
            row["data_gaps"] = ["history_read_error"]
            row["error_type"] = type(exc).__name__
            continue

        ohlcv_valid = _ohlcv_valid(fields)
        integrity = bar_integrity_mask(fields)
        valid = integrity["valid"]
        close = fields["close"]
        row["metrics"].update(
            history_window_sessions=int(len(close)),
            valid_ohlcv_sessions=int(ohlcv_valid.sum()),
            source_integrity_valid_sessions=int(valid.sum()),
        )
        row["source_integrity"] = {
            "verified": bool(valid.all()),
            "invalid_sessions": int((~valid).sum()),
            "reason_counts": {reason: int(mask.sum()) for reason, mask in integrity["reason_masks"].items()
                              if mask.any()},
        }
        rs_valid = len(close) >= RS_RETURN_SESSIONS + 1 and valid[-253:].all()
        if rs_valid:
            annual_return = float(close[-1] / close[-253] - 1)
            if np.isfinite(annual_return):
                returns[code] = annual_return
                row["rs_eligible"] = True
                row["metrics"]["return_252_sessions"] = annual_return

        reasons = _history_reasons(store, data_code, first, last, fields)
        reasons.extend(_integrity_reasons(fields, integrity))
        if not np.isfinite(current_factor) or current_factor <= 0:
            reasons.append("invalid_current_factor")
        if reasons:
            row["reason_codes"] = reasons
            row["data_gaps"] = reasons.copy()
            continue
        # 200 observations ending at t-120 require exactly 320 total observations.
        current = float(close[-1])
        ma50 = float(close[-50:].mean())
        ma150 = float(close[-150:].mean())
        ma200 = float(close[-200:].mean())
        ma200_120_ago = float(close[-320:-120].mean())
        high_52w = float(fields["high"][-252:].max())
        low_52w = float(fields["low"][-252:].min())
        row["metrics"].update(
            adjusted_close=current, ma50=ma50, ma150=ma150, ma200=ma200,
            ma200_120_sessions_ago=ma200_120_ago, high_52w=high_52w, low_52w=low_52w,
            above_52w_low_pct=(current / low_52w - 1) * 100,
            below_52w_high_pct=(1 - current / high_52w) * 100,
            price=current / current_factor,
            sma50=ma50 / current_factor, sma150=ma150 / current_factor,
            sma200=ma200 / current_factor,
            sma200_120_ago=ma200_120_ago / current_factor,
            high_252=high_52w / current_factor, low_252=low_52w / current_factor,
            current_factor=current_factor,
            price_basis="历史价按数据源复权，统一换算为目标日人民币尺度；非历史未复权价均值",
        )
        values = (
            current > ma150 and current > ma200,
            ma150 > ma200,
            ma200 > ma200_120_ago,
            ma50 > ma150 and ma50 > ma200,
            current > ma50,
            current >= 1.30 * low_52w,
            current >= 0.75 * high_52w,
        )
        row["conditions"].update(zip(CONDITION_NAMES[:-1], map(bool, values)))

    scores = _rs_scores(returns)
    for row in records:
        code = row["code"]
        if code in scores:
            row["metrics"]["rs_proxy"] = scores[code]
            row["conditions"]["rs_proxy_at_least_70"] = scores[code] >= 70
        if row["status"] == "excluded" or row["data_gaps"]:
            continue
        if any(value is None for value in row["conditions"].values()):
            row["reason_codes"] = ["rs_unavailable"]
            row["data_gaps"] = ["rs_unavailable"]
            continue
        row["formula_complete"] = True
        row["matched"] = all(row["conditions"].values())
        row["status"] = "matched" if row["matched"] else "not_matched"
        row["failed_conditions"] = [
            key for key, passed in row["conditions"].items() if not passed
        ]

    matches = sorted(
        (row for row in records if row["matched"]),
        key=lambda row: (-row["metrics"]["return_252_sessions"], row["code"]),
    )
    start = 0
    while start < len(matches):
        end = start + 1
        score = matches[start]["metrics"]["return_252_sessions"]
        while end < len(matches) and matches[end]["metrics"]["return_252_sessions"] == score:
            end += 1
        for row in matches[start:end]:
            row.update(rank=start + 1, tie_group_size=end - start)
        start = end
    selected = matches[:top_n]
    if len(matches) > top_n:
        boundary = selected[-1]["metrics"]["return_252_sessions"]
        selected.extend(row for row in matches[top_n:]
                        if row["metrics"]["return_252_sessions"] == boundary)

    statuses = Counter(row["status"] for row in records)
    reasons = Counter(reason for row in records for reason in row["reason_codes"])
    stats = {
        "universe_count": len(records), "excluded_count": statuses["excluded"],
        "source_code_count": source_count,
        "duplicate_alias_count": source_count - len(records),
        "source_integrity_incomplete_count": sum(bool(row.get("source_integrity", {}).get("invalid_sessions"))
                                                   for row in records),
        "eligible_count": len(records) - statuses["excluded"],
        "formula_evaluated_count": statuses["matched"] + statuses["not_matched"],
        "history_incomplete_count": statuses["incomplete"],
        "rs_universe_count": len(returns), "matched_count": len(matches),
        "candidate_count": len(selected), "requested_top_n": top_n,
        "boundary_tie_extra_count": max(0, len(selected) - top_n),
        "status_counts": dict(statuses), "reason_counts": dict(reasons),
        "condition_pass_counts": {
            name: sum(row["conditions"][name] is True for row in records)
            for name in CONDITION_NAMES
        },
        "condition_evaluated_counts": {
            name: sum(row["conditions"][name] is not None for row in records)
            for name in CONDITION_NAMES
        },
    }
    return {
        "strategy_id": STRATEGY_ID, "session": session, "methodology": methodology(),
        "all_evaluated": records, "ranked_matches": matches,
        "candidates": selected, "stats": stats,
    }
