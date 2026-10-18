"""Unprocessed Qlib v0.9.7 Alpha360 features, using past observations only.

This module reproduces ``Alpha360DL.get_feature_config``. It does not fit a
model, normalize across stocks, fill gaps, create labels, or select securities.
The public reference is Qlib's MIT-licensed ``qlib/contrib/data/loader.py``:
https://github.com/microsoft/qlib/blob/v0.9.7/qlib/contrib/data/loader.py

Arrays retain the archive's normalized units. Converting to raw RMB would
change the historical adjustment convention and would not reproduce Qlib.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from numbers import Integral

import numpy as np

from .daily_store import canonical_code


LOOKBACK = 60
FIELDS = ("close", "open", "high", "low", "vwap", "volume")
SOURCE_VERSION = "Qlib v0.9.7 Alpha360DL.get_feature_config"


def feature_names() -> tuple[str, ...]:
    """Official order: CLOSE59..CLOSE0, OPEN59.., ..., VOLUME59..0."""
    return tuple(f"{field.upper()}{lag}" for field in FIELDS
                 for lag in range(LOOKBACK - 1, -1, -1))


def feature_expressions() -> tuple[str, ...]:
    """Return the exact public Qlib expressions, for reproducibility checks."""
    result = []
    for field in FIELDS:
        denominator = "($volume+1e-12)" if field == "volume" else "$close"
        for lag in range(LOOKBACK - 1, -1, -1):
            numerator = f"Ref(${field}, {lag})" if lag else f"${field}"
            result.append(f"{numerator}/{denominator}")
    return tuple(result)


def _indices(indices: Sequence[int]) -> np.ndarray:
    result = np.asarray(indices)
    if result.ndim != 1:
        raise ValueError("indices must be a one-dimensional sequence")
    if result.size == 0:
        return np.empty(0, dtype=np.int64)
    if result.dtype.kind not in "iu" or result.dtype.kind == "b":
        raise ValueError("indices must contain integer calendar positions")
    if np.any(result < 0) or np.any(result > np.iinfo(np.int64).max):
        raise ValueError("indices must be nonnegative int64 calendar positions")
    return result.astype(np.int64, copy=False)


def _arrays_and_indices(arrays: Mapping[str, np.ndarray], indices: Sequence[int],
                        array_start: int):
    if isinstance(array_start, bool) or not isinstance(array_start, Integral) or array_start < 0:
        raise ValueError("array_start must be a nonnegative integer calendar position")
    positions = _indices(indices)
    values = {}
    length = None
    for field in FIELDS:
        if field not in arrays:
            raise ValueError(f"missing Alpha360 field: {field}")
        value = np.asarray(arrays[field], dtype=np.float32)
        if value.ndim != 1 or (length is not None and len(value) != length):
            raise ValueError("all six arrays must be one dimensional with equal length")
        values[field] = value
        length = len(value)
    if positions.size and (np.any(positions < array_start)
                           or np.any(positions - array_start >= length)):
        raise ValueError("requested calendar position is outside the supplied arrays")
    return values, positions - array_start


def features_from_arrays(arrays: Mapping[str, np.ndarray], indices: Sequence[int],
                         *, array_start: int = 0) -> np.ndarray:
    """Compute float32 ``[len(indices), 360]`` without accessing later rows.

    ``indices`` and ``array_start`` refer to the complete source calendar, not
    compressed per-stock observations. Missing past positions produce NaN;
    NaN/Inf and division-by-zero semantics are left as in Qlib expressions.
    Gap handling and any model processors are explicitly the caller's job.
    Inputs are cast to float32 before arithmetic, matching Qlib bin storage.
    An empty request returns shape ``(0, 360)``. Input arrays are never mutated.
    """
    values, local_indices = _arrays_and_indices(arrays, indices, array_start)
    output = np.full((len(local_indices), len(FIELDS) * LOOKBACK), np.nan, dtype=np.float32)
    if not len(local_indices):
        return output
    positions = local_indices[:, None] - np.arange(LOOKBACK - 1, -1, -1, dtype=np.int64)
    available = positions >= 0
    # Only past or current positions are gathered. Clipping permits safe
    # vectorization; unavailable positions are restored to NaN afterwards.
    safe_positions = np.maximum(positions, 0)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        for block, field in enumerate(FIELDS):
            denominator = (values["volume"][local_indices] + np.float32(1e-12)
                           if field == "volume" else values["close"][local_indices])
            values_block = values[field][safe_positions] / denominator[:, None]
            values_block[~available] = np.nan
            output[:, block * LOOKBACK:(block + 1) * LOOKBACK] = values_block
    return output


def window_complete_mask(arrays: Mapping[str, np.ndarray], indices: Sequence[int],
                         *, array_start: int = 0) -> np.ndarray:
    """True only for 60 observed, finite sessions in each of the six fields.

    This is a data-completeness mask, not a tradability/positivity filter. A
    zero-volume observation can be complete; it does not imply a stock can be
    bought. The feature matrix remains independent of this mask.
    """
    values, local_indices = _arrays_and_indices(arrays, indices, array_start)
    if not len(local_indices):
        return np.empty(0, dtype=bool)
    positions = local_indices[:, None] - np.arange(LOOKBACK - 1, -1, -1, dtype=np.int64)
    mask = np.all(positions >= 0, axis=1)
    safe_positions = np.maximum(positions, 0)
    for field in FIELDS:
        mask &= np.isfinite(values[field][safe_positions]).all(axis=1)
    return mask


def read_feature_arrays(store, code: str, first: int, last: int) -> dict[str, np.ndarray]:
    """Read only the inclusive requested calendar range from six Qlib bins.

    The header is an absolute float32 calendar offset. Missing files/positions
    are represented by NaN, never shifted forward or filled with future data.
    Corrupt headers, partial floats, short reads and out-of-calendar files
    raise ValueError. Code validation prevents non-A-share and path inputs.
    """
    code = canonical_code(code)
    count_sessions = len(store.sessions)
    if (isinstance(first, bool) or isinstance(last, bool)
            or not isinstance(first, Integral) or not isinstance(last, Integral)
            or not 0 <= first <= last < count_sessions):
        raise ValueError("invalid feature calendar range")
    result = {}
    for field in FIELDS:
        values = np.full(last - first + 1, np.nan, dtype=np.float32)
        path = store.root / "features" / code.lower() / f"{field}.day.bin"
        if not path.is_file():
            result[field] = values
            continue
        with path.open("rb") as handle:
            size = path.stat().st_size
            if size < 4 or size % 4:
                raise ValueError(f"corrupt feature file: {field}")
            header = handle.read(4)
            if len(header) != 4:
                raise ValueError(f"short feature header: {field}")
            start = float(np.frombuffer(header, dtype="<f4")[0])
            if not np.isfinite(start) or start != int(start) or not 0 <= start < count_sessions:
                raise ValueError(f"corrupt feature offset: {field}")
            start = int(start)
            count = size // 4 - 1
            if start + count > count_sessions:
                raise ValueError(f"feature data exceeds calendar: {field}")
            left, right = max(first, start), min(last + 1, start + count)
            if left < right:
                handle.seek(4 * (1 + left - start))
                block = handle.read(4 * (right - left))
                if len(block) != 4 * (right - left):
                    raise ValueError(f"short feature read: {field}")
                values[left - first:right - first] = np.frombuffer(block, dtype="<f4")
        result[field] = values
    return result


def features_at(store, code: str, indices: Sequence[int]) -> np.ndarray:
    """Compute Alpha360 for absolute calendar indices using bounded reads."""
    code = canonical_code(code)
    positions = _indices(indices)
    if not positions.size:
        return np.empty((0, len(FIELDS) * LOOKBACK), dtype=np.float32)
    if np.any(positions >= len(store.sessions)):
        raise ValueError("requested calendar position is outside the store")
    first = max(0, int(positions.min()) - LOOKBACK + 1)
    last = int(positions.max())
    arrays = read_feature_arrays(store, code, first, last)
    return features_from_arrays(arrays, positions, array_start=first)
