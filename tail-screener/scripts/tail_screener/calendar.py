"""Offline exchange calendar checks with explicit dependency/coverage diagnostics.

XSHG holidays are precomputed by exchange_calendars.  Do not extend its known
range by assuming weekdays are trading days: that would invent A-share T+1
dates.  Version 4.12 added 2026 holidays; 4.13 also supports pandas 3 date units.
"""
from __future__ import annotations

from datetime import datetime
from importlib import import_module, metadata
import re
from zoneinfo import ZoneInfo


TZ = ZoneInfo("Asia/Shanghai")
MINIMUM_VERSION = "4.13"
REPAIR_ACTION = ("通过 scripts/screen.py 正常入口重新运行，技能会自动建立专用环境并修复依赖；"
                 "若仍失败，查看 --doctor 诊断中的日历版本和错误类型。")


def dependency_versions():
    versions = {}
    for name in ("exchange-calendars", "pandas"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _load_backend():
    # Keep imports lazy so --doctor can explain missing/broken dependencies.
    return import_module("exchange_calendars"), import_module("pandas")


def _exception_detail(exc):
    try:
        return str(exc)[:300]
    except Exception:
        # Some old exchange_calendars exceptions themselves break on pandas 3.
        return "异常信息无法格式化"


def _fail(result, code, message, action, exc=None):
    result.update(verified=False, is_session=False, error_code=code, error=message, action=action)
    if exc is not None:
        result.update(error_type=type(exc).__name__, error_detail=_exception_detail(exc))
    return result


def calendar_context(now):
    """Return the known session and its neighbours, or a specific failure.

    This function never accesses the network.  Both neighbours must be known
    before ``verified`` becomes true, including for holiday/non-session dates.
    """
    result = dict(verified=False, session=None, is_session=False,
                  previous_session=None, next_session=None,
                  source="exchange_calendars:XSHG", scope="沪深共用A股节假日日历",
                  dependency_versions=dependency_versions(),
                  required_exchange_calendars=">=4.13,<5.0",
                  coverage_start=None, coverage_end=None,
                  error_code=None, error=None, action=None)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return _fail(result, "invalid_datetime", "交易日历需要含时区的完整日期时间。",
                     "使用 Asia/Shanghai 时区或带 UTC 偏移的时间后重试。")
    day_string = now.astimezone(TZ).date().isoformat()
    result["session"] = day_string
    try:
        xcals, pd = _load_backend()
    except ModuleNotFoundError as exc:
        return _fail(result, "dependency_missing", "交易日历依赖未完整安装。", REPAIR_ACTION, exc)
    except Exception as exc:
        return _fail(result, "dependency_import_error", "交易日历依赖无法导入，可能存在版本或安装冲突。",
                     REPAIR_ACTION, exc)
    installed = result["dependency_versions"].get("exchange-calendars")
    release = re.match(r"^(\d+)\.(\d+)", installed or "")
    if release and not (4, 13) <= tuple(map(int, release.groups())) < (5, 0):
        return _fail(result, "unsupported_dependency_version",
                     f"exchange-calendars {installed} 不满足已验证的最低版本 {MINIMUM_VERSION}。",
                     REPAIR_ACTION)
    try:
        calendar = xcals.get_calendar("XSHG")
        start, end = calendar.first_session.date(), calendar.last_session.date()
        result.update(coverage_start=start.isoformat(), coverage_end=end.isoformat())
        day = pd.Timestamp(day_string)
    except Exception as exc:
        return _fail(result, "calendar_initialization_error", "交易日历初始化失败。", REPAIR_ACTION, exc)
    if not start.isoformat() <= day_string <= end.isoformat():
        return _fail(result, "date_out_of_range",
                     f"请求日期 {day_string} 超出已安装日历覆盖范围 {start} 至 {end}。",
                     "通过正常入口检查并自动修复依赖；如仍未覆盖，应等待对应交易所假日日历发布，不得按工作日推算。")
    try:
        is_session = bool(calendar.is_session(day))
    except Exception as exc:
        return _fail(result, "calendar_compatibility_error",
                     "请求日期在已知范围内，但交易日历查询失败；这不是日期范围不足。",
                     REPAIR_ACTION, exc)
    # Check actual session boundaries before date_to_session so a valid last
    # day does not get misreported as an out-of-range query for that day.
    if day_string in (start.isoformat(), end.isoformat()):
        return _fail(result, "adjacent_session_unavailable",
                     f"请求日期 {day_string} 已覆盖，但无法在已知范围内确认前一或下一交易日。",
                     "通过正常入口检查日历版本；下一交易日未经确认前，不生成 T+1 入场计划。")
    try:
        previous_session = calendar.date_to_session(day - pd.Timedelta(days=1), direction="previous")
        next_session = calendar.date_to_session(day + pd.Timedelta(days=1), direction="next")
        result.update(verified=True, is_session=is_session,
                      previous_session=previous_session.date().isoformat(),
                      next_session=next_session.date().isoformat())
    except Exception as exc:
        return _fail(result, "calendar_compatibility_error",
                     "交易日历邻接交易日查询失败。", REPAIR_ACTION, exc)
    return result
