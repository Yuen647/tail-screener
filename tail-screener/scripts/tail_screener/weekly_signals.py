"""Fixed weekly A-share ranking signals; no fills or outcome calculations.

The source study uses decile portfolios and, where applicable, overlapping
holdings. These local long-only top-five signals are explicitly an adaptation.
No return or accuracy claims from the paper are inherited by this module.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping
from datetime import date, timedelta
import math
from types import MappingProxyType

import numpy as np

from .daily_store import canonical_code


SOURCE_URL = "https://arxiv.org/html/1910.13115"
RULES = MappingProxyType({
    "weekly-winner-j2-k1": MappingProxyType({"J": 2, "K": 1, "direction": "winners"}),
    "weekly-loser-j4-k4": MappingProxyType({"J": 4, "K": 4, "direction": "losers"}),
})


def _date(value):
    if not isinstance(value, str):
        raise ValueError("交易日必须是 YYYY-MM-DD 字符串")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("交易日格式无效")
    return parsed


def _monday(value):
    day = _date(value) if isinstance(value, str) else value
    return day - timedelta(days=day.weekday())


def _week_id(monday):
    year, week, _ = monday.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _rule(rule_id):
    if rule_id not in RULES:
        raise ValueError("仅支持预先固定的两个周度规则")
    return RULES[rule_id]


def weekly_schedule(actual_sessions):
    """Keep all calendar ISO weeks, including exchange-holiday empty weeks."""
    sessions = list(actual_sessions)
    if not sessions or sessions != sorted(set(sessions)):
        raise ValueError("实际交易日历必须非空、递增且无重复")
    grouped = {}
    for session in sessions:
        monday = _monday(_date(session))
        grouped.setdefault(monday, []).append(session)
    current, last = _monday(sessions[0]), _monday(sessions[-1])
    weeks = []
    while current <= last:
        days = grouped.get(current, [])
        weeks.append({
            "week_id": _week_id(current), "monday": current.isoformat(),
            "sunday": (current + timedelta(days=6)).isoformat(),
            "sessions": days, "session_count": len(days),
            "first_session": days[0] if days else None,
            "last_session": days[-1] if days else None,
        })
        current += timedelta(days=7)
    return weeks


def methodology(rule_id):
    rule = _rule(rule_id)
    return {
        "strategy_id": rule_id, "source_url": SOURCE_URL, "source_section": "2.2",
        "formation_weeks_J": rule["J"], "holding_weeks_K": rule["K"],
        "direction": rule["direction"], "scope": "Shanghai and Shenzhen A shares only",
        "time_index": "entry week t; signal produced after the last actual session of calendar ISO week t-1",
        "formation": "calendar ISO weeks t-J-1 through t-2 inclusive; skip t-1",
        "formation_return": "adjusted close at end of t-2 / adjusted close at end of t-J-2 - 1",
        "week_calendar": "actual exchange-session dates grouped into ISO calendar weeks, retaining empty weeks",
        "short_week_adaptation": (
            "local conservative rule: skip the signal if any baseline, formation or skip week has <=2 actual sessions; "
            "never compress missing/short weeks; next entry week must be the immediately following ISO week"
        ),
        "first_listing_month_adaptation": (
            "earliest source interval date proxies listing date (not official IPO date); exclude if formation or skip "
            "sessions fall in that calendar month; the baseline close alone may be in that month"
        ),
        "history_quality": "every actual session from baseline close through signal close must have valid positive OHLCV and recorded interval coverage; signal-day factor must be finite and positive for RMB price display",
        "ranking": ("descending" if rule["direction"] == "winners" else "ascending") + " unrounded formation return",
        "top_n_adaptation": "local long-only top-five display, exact boundary ties all retained; not the original equal-weight decile portfolio",
        "relative_rank": "a winner can have negative formation return; rank is not expected return or probability",
        "entry": "signal only after signal-week close; earliest possible entry is next ISO week's first actual session open, subject to separate tradability checks",
        "holding": f"source K={rule['K']} calendar weeks; execution, overlapping positions and exits require a separate implementation",
        "source_performance": "paper performance does not validate this long-only top-five adaptation",
        "current_security_names": "current ST/name flags are not backfilled into historical signal generation",
    }


def weekly_signal_window(actual_sessions, signal_session, rule_id, next_session=None):
    """Define one complete weekly signal without reading any future prices.

    For the latest stored week, ``next_session`` is the caller-verified next
    exchange date. Historical weeks use the next date in the actual calendar.
    A date within an unfinished week never substitutes for that week's close.
    """
    rule = _rule(rule_id)
    sessions = list(actual_sessions)
    weeks = weekly_schedule(sessions)
    signal_day = _date(signal_session)
    if signal_session not in sessions:
        raise ValueError("信号日不在实际交易日历中")
    index = {week["monday"]: week for week in weeks}
    signal_monday = _monday(signal_day)
    signal_week = index[signal_monday.isoformat()]
    entry_monday = signal_monday + timedelta(days=7)
    first_formation_monday = signal_monday - timedelta(weeks=rule["J"])
    baseline_monday = first_formation_monday - timedelta(days=7)
    formation_mondays = [first_formation_monday + timedelta(weeks=n) for n in range(rule["J"])]
    baseline_week = index.get(baseline_monday.isoformat())
    formation_weeks = [index.get(monday.isoformat()) for monday in formation_mondays]
    reasons = []
    if signal_week["last_session"] != signal_session:
        reasons.append("signal_is_not_last_session_of_week")
    position = bisect_right(sessions, signal_session)
    calendar_next = sessions[position] if position < len(sessions) else None
    if next_session is not None:
        _date(next_session)
        if next_session <= signal_session:
            raise ValueError("下一实际交易日必须晚于信号日")
        if calendar_next is not None and calendar_next != next_session:
            raise ValueError("调用方下一交易日与已记录实际交易日历冲突")
    verified_next = calendar_next or next_session
    if verified_next is None:
        reasons.append("week_completion_unverified")
    elif _monday(verified_next) == signal_monday:
        reasons.append("signal_week_not_complete")
    elif _monday(verified_next) != entry_monday:
        reasons.append("entry_week_empty_or_nonadjacent")
    needed = [("baseline", baseline_week),
              *[("formation", week) for week in formation_weeks], ("skip", signal_week)]
    short_weeks = []
    for kind, week in needed:
        if week is None:
            reasons.append("insufficient_calendar_history")
        elif week["session_count"] <= 2:
            reasons.append("short_or_empty_calendar_week")
            short_weeks.append({"role": kind, "week_id": week["week_id"],
                                "session_count": week["session_count"]})
    first_formation = formation_weeks[0] if formation_weeks else None
    final_formation = formation_weeks[-1] if formation_weeks else None
    return {
        "rule_id": rule_id, "status": "skipped" if reasons else "ready",
        "reason_codes": list(dict.fromkeys(reasons)), "short_weeks": short_weeks,
        "signal_session": signal_session, "signal_week": signal_week,
        "entry_week_id": _week_id(entry_monday), "entry_week_monday": entry_monday.isoformat(),
        "next_entry_session": verified_next if verified_next and _monday(verified_next) == entry_monday else None,
        "baseline_week": baseline_week, "formation_weeks": formation_weeks,
        "skip_week": signal_week,
        "baseline_session": baseline_week["last_session"] if baseline_week else None,
        "formation_first_session": first_formation["first_session"] if first_formation else None,
        "formation_end_session": final_formation["last_session"] if final_formation else None,
        "holding_weeks_K": rule["K"],
    }


def rank_weekly_metrics(formation_returns, rule_id, top_n=5):
    """Pure fixed ranking shared by the production reader and batch validation.

    Input is a mapping of already history-qualified code -> formation return.
    Nonfinite/undefined metrics are omitted explicitly, never ranked as zero.
    """
    rule = _rule(rule_id)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError("top_n 必须是正整数")
    if not isinstance(formation_returns, Mapping):
        raise ValueError("形成期指标必须是股票代码到收益变化的映射")
    records, invalid, seen = [], [], set()
    for supplied, value in formation_returns.items():
        code = canonical_code(supplied)
        if code in seen:
            raise ValueError("指标映射含重复规范股票代码")
        seen.add(code)
        if isinstance(value, bool) or value is None:
            invalid.append({"code": code, "reason": "invalid_formation_return"})
            continue
        try:
            value = float(value)
        except (ValueError, TypeError):
            invalid.append({"code": code, "reason": "invalid_formation_return"})
            continue
        if not math.isfinite(value) or value <= -1:
            invalid.append({"code": code, "reason": "invalid_formation_return"})
            continue
        records.append({"code": code, "formation_return": value})
    multiplier = -1 if rule["direction"] == "winners" else 1
    records.sort(key=lambda row: (multiplier * row["formation_return"], row["code"]))
    start = 0
    while start < len(records):
        end = start + 1
        while end < len(records) and records[end]["formation_return"] == records[start]["formation_return"]:
            end += 1
        for row in records[start:end]:
            row.update(rank=start + 1, tie_group_size=end - start)
        start = end
    chosen = records[:top_n]
    if len(records) > top_n:
        boundary = chosen[-1]["formation_return"]
        chosen.extend(row for row in records[top_n:] if row["formation_return"] == boundary)
    return {"ranked_matches": records, "candidates": chosen, "invalid_metrics": invalid,
            "stats": {"ranked_count": len(records), "candidate_count": len(chosen),
                      "requested_top_n": top_n, "boundary_tie_extra_count": max(0, len(chosen) - top_n),
                      "invalid_metric_count": len(invalid)}}


def _exclusions(excluded_codes):
    if excluded_codes is None:
        return {}
    items = excluded_codes.items() if isinstance(excluded_codes, Mapping) else (
        (code, "caller_exclusion") for code in excluded_codes
    )
    return {canonical_code(code): value for code, value in items}


def scan_weekly(store, signal_session, rule_id, top_n=5, excluded_codes=None, next_session=None):
    """Read only the declared historical window and calculate a weekly signal."""
    window = weekly_signal_window(store.sessions, signal_session, rule_id, next_session)
    # Validate the display argument even when a calendar window is skipped.
    rank_weekly_metrics({}, rule_id, top_n)
    exclusions = _exclusions(excluded_codes)
    codes = []
    for value in store.codes(signal_session):
        try:
            codes.append(canonical_code(value))
        except ValueError:
            continue
    records, returns = [], {}
    for code in codes:
        row = {"code": code, "session": signal_session, "status": "incomplete",
               "metrics": {}, "reason_codes": [], "data_gaps": [], "selected": False}
        records.append(row)
        if code in exclusions:
            row.update(status="excluded", reason_codes=["caller_exclusion"],
                       exclusion_reasons=exclusions[code])
            continue
        if window["status"] != "ready":
            row.update(status="not_evaluated", reason_codes=window["reason_codes"].copy())
            continue
        first = store.session_index[window["baseline_session"]]
        last = store.session_index[signal_session]
        formation_end = store.session_index[window["formation_end_session"]] - first
        dates = store.sessions[first:last + 1]
        intervals = getattr(store, "intervals", {}).get(code, [])
        if not intervals:
            row["reason_codes"] = row["data_gaps"] = ["recorded_interval_unavailable"]
            continue
        first_source_date = min(left for left, _ in intervals)
        first_month = first_source_date[:7]
        formation_start = window["formation_first_session"]
        if any(day >= formation_start and day[:7] == first_month for day in dates):
            row.update(status="excluded", reason_codes=["first_recorded_calendar_month"],
                       first_recorded_date=first_source_date,
                       listing_proxy_note="来源区间起点代理，非官方上市日期")
            continue
        if any(not any(left <= day <= right for left, right in intervals) for day in dates):
            row["reason_codes"] = row["data_gaps"] = ["historical_recorded_interval_gap"]
            continue
        try:
            fields = {key: store.values(code, key, first, last)
                      for key in ("open", "high", "low", "close", "volume")}
            if any(np.shape(values) != (len(dates),) for values in fields.values()):
                raise ValueError("历史字段长度与实际交易日不符")
            factors = store.values(code, "factor", last, last)
            if np.shape(factors) != (1,):
                raise ValueError("目标日复权因子长度无效")
            factor = float(factors[0])
        except (OSError, ValueError) as exc:
            row["reason_codes"] = row["data_gaps"] = ["history_read_error"]
            row["error_type"] = type(exc).__name__
            continue
        prices = np.stack([fields[key] for key in ("open", "high", "low", "close")])
        reasons = []
        if not np.isfinite(prices).all():
            reasons.append("historical_ohlc_gap")
        if (np.isfinite(prices) & (prices <= 0)).any():
            reasons.append("nonpositive_historical_price")
        if ((fields["high"] < prices.max(axis=0)) | (fields["low"] > prices.min(axis=0))).any():
            reasons.append("inconsistent_historical_ohlc")
        if not np.isfinite(fields["volume"]).all():
            reasons.append("historical_volume_gap")
        if (fields["volume"] == 0).any():
            reasons.append("zero_volume_sessions")
        if (fields["volume"] < 0).any():
            reasons.append("negative_historical_volume")
        if not np.isfinite(factor) or factor <= 0:
            reasons.append("invalid_current_factor")
        if reasons:
            row["reason_codes"], row["data_gaps"] = reasons, reasons.copy()
            continue
        close = fields["close"]
        formation_return = float(close[formation_end] / close[0] - 1)
        returns[code] = formation_return
        row.update(status="eligible", first_recorded_date=first_source_date)
        row["metrics"] = {
            "formation_return": formation_return, "baseline_adjusted_close": float(close[0]),
            "formation_end_adjusted_close": float(close[formation_end]),
            "signal_adjusted_close": float(close[-1]), "price": float(close[-1] / factor),
            "window_sessions": len(dates), "current_factor": factor,
            "price_basis": "形成期使用同一来源复权价；当前价格为信号日人民币尺度",
        }
    ranking = rank_weekly_metrics(returns, rule_id, top_n)
    by_code = {row["code"]: row for row in records}
    for ranked in ranking["ranked_matches"]:
        by_code[ranked["code"]].update(rank=ranked["rank"], tie_group_size=ranked["tie_group_size"])
    ranked_matches = [by_code[row["code"]] for row in ranking["ranked_matches"]]
    candidates = [by_code[row["code"]] for row in ranking["candidates"]]
    for row in candidates:
        row["selected"] = True
    statuses = Counter(row["status"] for row in records)
    stats = dict(ranking["stats"], universe_count=len(records), eligible_count=statuses["eligible"],
                 history_incomplete_count=statuses["incomplete"], excluded_count=statuses["excluded"],
                 not_evaluated_count=statuses["not_evaluated"], status_counts=dict(statuses),
                 reason_counts=dict(Counter(reason for row in records for reason in row["reason_codes"])))
    return {"strategy_id": rule_id, "session": signal_session, "window": window,
            "methodology": methodology(rule_id), "all_evaluated": records,
            "ranked_matches": ranked_matches, "candidates": candidates, "stats": stats}
