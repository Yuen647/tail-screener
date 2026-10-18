"""Read verified Qlib data without mistaking normalized prices for RMB.

This module does not turn a public archive into point-in-time ST/announcement
data. Prices retain their source adjustment convention; raw prices are only
recovered with the same day's factor.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re

import numpy as np
import pandas as pd

ASHARE = re.compile(r'^(SH(?:60\d{4}|688\d{3})|SZ(?:00\d{4}|30\d{4}))$')
FIELDS = ('open', 'high', 'low', 'close', 'volume', 'factor', 'vwap', 'amount')


def canonical_code(code):
    """Return SH600000/SZ000001; exclude funds, Beijing and foreign stocks."""
    value = str(code).upper()
    if re.fullmatch(r'\d{6}\.(SH|SZ)', value):
        value = value[-2:] + value[:6]
    if not ASHARE.fullmatch(value):
        raise ValueError('仅支持沪深A股代码')
    return value


class DailyStore:
    def __init__(self, directory):
        self.root = Path(directory)
        self.sessions = (self.root / 'calendars/day.txt').read_text().splitlines()
        if not self.sessions or self.sessions != sorted(set(self.sessions)):
            raise ValueError('交易日历无效')
        for day in self.sessions:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError('交易日格式无效')
        self.session_index = {day: index for index, day in enumerate(self.sessions)}
        self.intervals = {}
        for line in (self.root / 'instruments/all.txt').read_text().splitlines():
            items = line.split()
            if len(items) != 3:
                raise ValueError('股票区间记录无效')
            code, first, last = items
            if not ASHARE.fullmatch(code):
                continue
            date.fromisoformat(first); date.fromisoformat(last)
            if first > last:
                raise ValueError('股票区间倒置')
            self.intervals.setdefault(code, []).append((first, last))

    def codes(self, session=None):
        if session is None:
            return sorted(self.intervals)
        if session not in self.session_index:
            raise ValueError('所需日期没有实际交易日线')
        return sorted(code for code, intervals in self.intervals.items()
                      if any(first <= session <= last for first, last in intervals))

    def values(self, code, field, first, last):
        code = canonical_code(code)
        if field not in FIELDS or not 0 <= first <= last < len(self.sessions):
            raise ValueError('历史字段或范围无效')
        path = self.root / 'features' / code.lower() / (field + '.day.bin')
        result = np.full(last - first + 1, np.nan, dtype='float64')
        if not path.is_file():
            return result
        if path.stat().st_size < 4 or path.stat().st_size % 4:
            raise ValueError('历史字段文件损坏')
        with path.open('rb') as handle:
            start = float(np.frombuffer(handle.read(4), dtype='<f4')[0])
            if not np.isfinite(start) or start != int(start) or not 0 <= start < len(self.sessions):
                raise ValueError('历史字段起点损坏')
            start = int(start)
            count = path.stat().st_size // 4 - 1
            if start + count > len(self.sessions):
                raise ValueError('历史字段超出实际交易日历')
            left, right = max(first, start), min(last + 1, start + count)
            if left < right:
                handle.seek(4 * (1 + left - start))
                result[left - first:right - first] = np.frombuffer(
                    handle.read(4 * (right - left)), dtype='<f4')
        result[~np.isfinite(result)] = np.nan
        return result

    def history(self, code, session=None, count=300):
        session = session or self.sessions[-1]
        if session not in self.session_index or not isinstance(count, int) or count < 1:
            raise ValueError('历史日期或长度无效')
        last = self.session_index[session]
        first = max(0, last - count + 1)
        frame = pd.DataFrame({field: self.values(code, field, first, last) for field in FIELDS},
                             index=pd.Index(self.sessions[first:last + 1], name='date'))
        return self._with_units(frame)

    @staticmethod
    def _with_units(frame):
        valid_factor = frame['factor'].where(frame['factor'] > 0)
        for field in ('open', 'high', 'low', 'close', 'vwap'):
            frame['raw_' + field] = frame[field] / valid_factor
        frame['raw_volume_shares'] = frame['volume'] * valid_factor * 100
        frame['raw_amount_yuan'] = frame['amount'] * 1000
        frame['valid_ohlcv'] = (
            (frame[['open', 'high', 'low', 'close', 'volume', 'factor', 'vwap']] > 0).all(axis=1)
            & (frame['high'] >= frame[['open', 'close', 'low']].max(axis=1))
            & (frame['low'] <= frame[['open', 'close', 'high']].min(axis=1))
        )
        return frame

    def cross_section(self, session=None):
        session = session or self.sessions[-1]
        if session not in self.session_index:
            raise ValueError('所需日期没有实际交易日线')
        position = self.session_index[session]
        rows, errors = [], {}
        for code in self.codes(session):
            try:
                row = {field: self.values(code, field, position, position)[0] for field in FIELDS}
                row['code'] = code
                rows.append(row)
            except (ValueError, OSError) as exc:
                errors[code] = type(exc).__name__
        if not rows:
            return pd.DataFrame(index=pd.Index([], name='code')), errors
        return self._with_units(pd.DataFrame(rows).set_index('code')), errors
