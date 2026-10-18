"""Point-observation integrity and explicitly documented security identity.

These checks reject inconsistent records; they never guess a unit correction.
Passing does not establish point-in-time listing eligibility or tradability.
The identity map contains only the independently documented SH601313 to
SH601360 code change, not inferred similarities between companies or names.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import numpy as np

from .daily_store import canonical_code


BAR_FIELDS = ("open", "high", "low", "close", "volume", "factor", "vwap", "amount")
FLOAT32_EPS = float(np.finfo(np.float32).eps)
FLOAT32_SUBNORMAL = float(np.nextafter(np.float32(0), np.float32(1)))
CODE_CHANGE_SESSION = "2018-02-28"
CODE_CHANGE_SOURCE = (
    "https://static.sse.com.cn/disclosure/listedinfo/announcement/c/"
    "2018-02-23/601313_20180223_18.pdf"
)


def bar_integrity_mask(arrays: Mapping[str, np.ndarray]) -> dict:
    """Check broadcastable normalized OHLCV/factor/VWAP/amount observations.

    Return ``{'valid': bool ndarray, 'reason_masks': {reason: bool ndarray}}``.
    True in a reason mask means that check failed. Missing fields, nonnumeric
    arrays and incompatible broadcast shapes raise ValueError. Scalar inputs
    produce zero-dimensional arrays. Inputs are neither modified nor filled.

    Every field must be finite and strictly positive. OHLC geometry is exact.
    The VWAP/bar tolerance is ``factor * .01 + 8 * eps32 * max(|L|,|H|,|VWAP|)``:
    one raw-price cent in normalized units plus float32 arithmetic allowance.

    Independently, the verified source units imply normalized VWAP equals
    ``amount / volume * 10`` (amount in thousands of RMB; volume in adjusted
    lots of 100 shares). Its allowance is ``8 * eps32 * max(|implied|,|VWAP|)``
    plus eight float32 subnormal quanta. This conservatively exceeds the
    combined float32 storage error of three independently rounded fields and
    float64 quotient/multiplication. It adds no market-price cent tolerance:
    the two quantities describe the same amount/volume calculation. This is
    a fixed representation bound, not a fitted discrepancy threshold.

    Geometry/consistency reasons are assessed only when their prerequisite
    inputs are finite and positive, keeping missing-data reasons separate.
    """
    if not isinstance(arrays, Mapping):
        raise ValueError("bar fields must be supplied as a mapping")
    fields = []
    for field in BAR_FIELDS:
        if field not in arrays:
            raise ValueError(f"missing bar field: {field}")
        value = np.asarray(arrays[field])
        if value.dtype.kind not in "fiu":
            raise ValueError(f"bar field must be numeric: {field}")
        fields.append(value.astype(np.float64, copy=False))
    try:
        values = dict(zip(BAR_FIELDS, np.broadcast_arrays(*fields)))
    except ValueError as exc:
        raise ValueError("bar field shapes cannot be broadcast together") from exc
    positive = {field: np.isfinite(value) & (value > 0) for field, value in values.items()}
    reasons = {f"invalid_{field}": np.asarray(~positive[field], dtype=bool)
               for field in BAR_FIELDS}
    open_, high, low, close = (values[field] for field in ("open", "high", "low", "close"))
    vwap, factor, amount, volume = (values[field] for field in ("vwap", "factor", "amount", "volume"))
    ohlc_ok = positive["open"] & positive["high"] & positive["low"] & positive["close"]
    reasons["ohlc_geometry"] = np.asarray(
        ohlc_ok & ((high < np.maximum.reduce([open_, close, low]))
                   | (low > np.minimum.reduce([open_, close, high]))), dtype=bool)
    vwap_inputs_ok = positive["vwap"] & positive["low"] & positive["high"] & positive["factor"]
    amount_inputs_ok = positive["vwap"] & positive["volume"] & positive["amount"]
    with np.errstate(divide="ignore", invalid="ignore", over="ignore", under="ignore"):
        price_tolerance = factor * .01 + 8 * FLOAT32_EPS * np.maximum.reduce(
            [np.abs(low), np.abs(high), np.abs(vwap)])
        reasons["vwap_outside_bar"] = np.asarray(
            vwap_inputs_ok & ((vwap < low - price_tolerance) | (vwap > high + price_tolerance)),
            dtype=bool)
        implied_vwap = amount / volume * 10.0
        amount_tolerance = (8 * FLOAT32_EPS * np.maximum(np.abs(implied_vwap), np.abs(vwap))
                            + 8 * FLOAT32_SUBNORMAL)
        reasons["amount_volume_vwap_mismatch"] = np.asarray(
            amount_inputs_ok & (~np.isfinite(implied_vwap)
                                | (np.abs(implied_vwap - vwap) > amount_tolerance)), dtype=bool)
    valid = np.ones_like(open_, dtype=bool)
    for failed in reasons.values():
        valid &= ~failed
    return {"valid": valid, "reason_masks": reasons}


def source_identity(code: str, session: str) -> dict[str, str]:
    """Resolve the one evidenced code change without relabeling history.

    The 2018-02-23 SSE announcement makes the display-code change effective
    2018-02-28. Both aliases identify the same security. The source archive's
    SH601360 series contains usable pre-change history; SH601313's conflicting
    units are not repaired or mixed into it. ``data_code`` directs consumers
    to that separately checked series, which must still pass bar integrity.

    This is identity resolution only: it does not certify availability,
    listing/ST status, corporate actions, or contemporaneous eligibility.
    Every unlisted-in-this-map A-share code keeps its own identity; current
    names never determine historical exclusions.
    """
    code = canonical_code(code)
    if not isinstance(session, str):
        raise ValueError("session must be an ISO calendar date")
    try:
        if date.fromisoformat(session).isoformat() != session:
            raise ValueError("session must be an ISO calendar date")
    except ValueError as exc:
        raise ValueError("session must be an ISO calendar date") from exc
    if code in {"SH601313", "SH601360"}:
        return {"stable_id": "SH601360",
                "display_code": "SH601313" if session < CODE_CHANGE_SESSION else "SH601360",
                "data_code": "SH601360"}
    return {"stable_id": code, "display_code": code, "data_code": code}


def deduplicate_source_codes(codes: Sequence[str], session: str) -> list[dict[str, str]]:
    """Resolve identities once each, preserving the first stable-ID order.

    Deduplicate before computing cross-sectional ranks or top-k membership so
    the two source aliases cannot receive two slots or distort the universe.
    The function supplies identity records, not an eligibility decision.
    """
    seen, result = set(), []
    for code in codes:
        identity = source_identity(code, session)
        if identity["stable_id"] not in seen:
            seen.add(identity["stable_id"])
            result.append(identity)
    return result
