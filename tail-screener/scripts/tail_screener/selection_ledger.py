"""Immutable candidate observations, without invented transactions or returns.

The horizons describe signal-date-to-future-date adjusted closing-price changes.
They are not investable returns, fills, portfolio performance, or a win rate.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import uuid
from zoneinfo import ZoneInfo

from .daily_store import canonical_code


TZ = ZoneInfo("Asia/Shanghai")
HORIZONS = (1, 5, 10, 20)
LABEL = "信号后价格变化，不是可交易收益/胜率"
MAX_RECORD_BYTES = 2 * 1024 * 1024
RAW_PRICE_TOLERANCE = 0.011


def _now():
    return datetime.now(TZ)


def _day(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("交易日格式无效")
    return value


def _timestamp(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(TZ)


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("价格必须是有限正数")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("价格必须是有限正数")
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _archive_hash(report):
    source = report.get("data_source") or {}
    value = (source.get("manifest") or {}).get("archive_sha256")
    if value is None:
        value = source.get("archive_sha256")
    if not isinstance(value, str):
        raise ValueError("缺少来源数据包 SHA-256，不能冻结不可定位的数据")
    value = value.removeprefix("sha256:")
    if not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("来源数据包 SHA-256 无效")
    return value


def _timing(signal_session, written_at, next_session):
    close = datetime.combine(date.fromisoformat(signal_session), time(15), TZ)
    if written_at < close:
        return {"classification": "invalid_signal_time", "prospective": False,
                "reason": "冻结时信号交易日尚未收盘或仍在未来"}
    if next_session is None:
        return {"classification": "calendar_unverified", "prospective": False,
                "reason": "未提供已核验的信号日下一交易日，不能认定事前记录"}
    opening = datetime.combine(date.fromisoformat(next_session), time(9, 30), TZ)
    closing = datetime.combine(date.fromisoformat(next_session), time(15), TZ)
    if written_at < opening:
        return {"classification": "prospective", "prospective": True,
                "reason": "实际冻结时间在信号日收盘后、已核验下一交易日09:30之前"}
    if written_at < closing:
        return {"classification": "partially_seen", "prospective": False,
                "reason": "下一交易日已开盘，已经看到部分信号后行情"}
    return {"classification": "retrospective", "prospective": False,
            "reason": "冻结时下一交易日已收盘或更晚，不能称独立事前记录"}


def freeze_selection(report, ledger_dir, created_at=None, next_session=None):
    """Append one immutable selection using the actual clock at the write.

    ``created_at`` is retained only as caller-supplied audit metadata; it can
    never override the actual write timestamp. ``next_session`` must be the
    first exchange session AFTER the signal date, verified by the caller's
    exchange calendar. A missing calendar never produces prospective status.
    """
    signal_session = _day(report.get("data_session") or report.get("session"))
    generated_at = _timestamp(report["generated_at"]).isoformat()
    next_session = _day(next_session) if next_session is not None else None
    if next_session is not None and next_session <= signal_session:
        raise ValueError("已核验的下一交易日必须晚于信号交易日")
    requested_creation = _timestamp(created_at).isoformat() if created_at is not None else None
    strategy_id = report.get("strategy_id")
    methodology = report.get("methodology")
    if not isinstance(strategy_id, str) or not strategy_id or not isinstance(methodology, dict) or not methodology:
        raise ValueError("冻结需要明确策略身份和规则定义")
    rule_identity = {
        "strategy_id": strategy_id, "methodology": methodology,
        "selection": report.get("selection") or {},
        "source_manifest": (report.get("strategy") or {}).get("source_manifest") or {},
    }
    codes, candidates = set(), []
    for row in report.get("candidates", []):
        code = canonical_code(row["code"])
        if code in codes:
            raise ValueError("冻结名单含重复股票")
        codes.add(code)
        rank = row.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 1:
            raise ValueError("冻结名单缺少有效策略排名")
        candidates.append({"code": code, "rank": rank,
                           "raw_close": _positive(row["metrics"]["price"])})
    archive_sha256 = _archive_hash(report)
    directory = Path(ledger_dir)
    if directory.is_symlink():
        raise ValueError("记录目录不能是符号链接")
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    destination = directory / (identifier + ".json")
    temporary = directory / ("." + identifier + ".tmp")
    # Capture now here, after input validation and immediately before writing.
    written_at = _timestamp(_now())
    payload = {
        "schema_version": 1, "record_id": identifier, "strategy_id": strategy_id,
        "signal_session": signal_session, "report_generated_at": generated_at,
        "created_at": written_at.isoformat(),
        "caller_supplied_created_at": requested_creation,
        "next_session": next_session,
        "next_session_verification": "caller_verified_exchange_calendar" if next_session else None,
        "timing": _timing(signal_session, written_at, next_session),
        "source_archive_sha256": archive_sha256,
        "rule_identity_sha256": _sha(rule_identity), "rule_identity": rule_identity,
        "candidates": candidates, "observation_label": LABEL,
        "creation_time_note": "actual local wall clock at freeze; report time and caller time cannot backdate creation",
        "integrity_limit": "local hash detects accidental changes; it is not independent timestamp notarization",
    }
    envelope = {"payload": payload, "sha256": _sha(payload)}
    serialized = _canonical(envelope)
    if len(serialized) > MAX_RECORD_BYTES:
        raise ValueError("冻结记录过大")
    try:
        with temporary.open("xb") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        # The complete inode is published atomically. link() refuses an existing
        # destination, unlike replace(); existing history is never overwritten.
        os.link(temporary, destination)
        try:
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Directory fsync is unsupported on some operating systems.
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return {"record_id": identifier, "path": str(destination.resolve()),
            "sha256": envelope["sha256"], "created_at": payload["created_at"],
            "signal_session": signal_session, "timing": payload["timing"],
            "candidate_count": len(candidates), "observation_label": LABEL}


def _read_record(path):
    if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
        raise ValueError("记录文件路径无效")
    if not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError("记录文件大小无效")
    envelope = json.loads(path.read_text(encoding="utf-8"))
    payload, digest = envelope["payload"], envelope["sha256"]
    if not isinstance(digest, str) or not hmac.compare_digest(digest, _sha(payload)):
        raise ValueError("冻结记录哈希不符")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["record_id"] != path.stem:
        raise ValueError("冻结记录身份无效")
    _day(payload["signal_session"])
    created_at = _timestamp(payload["created_at"])
    _timestamp(payload["report_generated_at"])
    next_session = payload["next_session"]
    if next_session is not None and _day(next_session) <= payload["signal_session"]:
        raise ValueError("冻结记录交易日顺序无效")
    if payload["timing"] != _timing(payload["signal_session"], created_at, next_session):
        raise ValueError("冻结记录时点分类与实际冻结时间不符")
    if not re.fullmatch("[0-9a-f]{64}", payload["source_archive_sha256"]):
        raise ValueError("冻结记录缺少有效来源哈希")
    if not hmac.compare_digest(payload["rule_identity_sha256"], _sha(payload["rule_identity"])):
        raise ValueError("冻结记录规则哈希不符")
    seen = set()
    if not isinstance(payload["candidates"], list):
        raise ValueError("冻结名单格式无效")
    for row in payload["candidates"]:
        code = canonical_code(row["code"])
        if code != row["code"] or code in seen:
            raise ValueError("冻结股票代码无效或重复")
        seen.add(code)
        _positive(row["raw_close"])
        if isinstance(row["rank"], bool) or not isinstance(row["rank"], int) or row["rank"] < 1:
            raise ValueError("冻结股票排名无效")
    return payload, digest


def _value(store, code, field, position):
    values = store.values(code, field, position, position)
    if len(values) != 1:
        raise ValueError("观测日字段长度无效")
    return _positive(float(values[0]))


def evaluate_ledger(ledger_dir, store):
    """Observe frozen lists at 1/5/10/20 actual completed exchange sessions.

    Both adjusted closes are read from the SAME current data version. Raw
    baseline close is checked against the frozen close; revisions over ¥0.011
    invalidate the observation instead of silently changing its baseline.
    Missing or zero-volume endpoints are invalid, never filled or set to zero.
    """
    asof = _timestamp(_now())
    sessions = list(store.sessions)
    if sessions != sorted(set(sessions)):
        raise ValueError("实际交易日历无效")
    for day in sessions:
        _day(day)
    # Do not use a dataset's future calendar or today's unfinished daily bar.
    completed = [day for day in sessions if day < asof.date().isoformat()
                 or (day == asof.date().isoformat() and asof.time() >= time(15))]
    positions = {day: index for index, day in enumerate(sessions)}
    completed_set = set(completed)
    counts = {str(horizon): Counter() for horizon in HORIZONS}
    timing_counts, observations, invalid_records = Counter(), [], []
    directory = Path(ledger_dir)
    if directory.is_symlink():
        raise ValueError("记录目录不能是符号链接")
    paths = sorted(directory.glob("*.json")) if directory.exists() else []
    valid_records = []
    for path in paths:
        try:
            valid_records.append(_read_record(path))
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            invalid_records.append({"filename": path.name, "reason": str(exc)[:160]})
    valid_records.sort(key=lambda item: (item[0]["created_at"], item[0]["record_id"]))
    for payload, digest in valid_records:
        session = payload["signal_session"]
        timing = dict(payload["timing"])
        base_position = positions.get(session)
        calendar_conflict = (
            base_position is not None and base_position + 1 < len(completed)
            and payload["next_session"] is not None
            and sessions[base_position + 1] != payload["next_session"]
        )
        if calendar_conflict:
            timing = {"classification": "calendar_conflict", "prospective": False,
                      "reason": "冻结时声明的下一交易日与当前实际交易日历不符"}
        timing_counts[timing["classification"]] += 1
        record = {"record_id": payload["record_id"], "record_sha256": digest,
                  "signal_session": session, "created_at": payload["created_at"],
                  "timing": timing, "source_archive_sha256": payload["source_archive_sha256"],
                  "rule_identity_sha256": payload["rule_identity_sha256"], "stocks": []}
        for candidate in payload["candidates"]:
            code = candidate["code"]
            stock = {"code": code, "rank": candidate["rank"],
                     "frozen_raw_close": candidate["raw_close"], "horizons": {}}
            baseline, baseline_issue = None, None
            try:
                if base_position is None or session not in completed_set:
                    raise ValueError("signal_session_not_in_completed_data")
                if timing["classification"] == "invalid_signal_time":
                    raise ValueError("signal_was_not_complete_at_freeze")
                baseline = _value(store, code, "close", base_position)
                factor = _value(store, code, "factor", base_position)
                _value(store, code, "volume", base_position)
                raw_close = baseline / factor
                stock["current_dataset_baseline_raw_close"] = raw_close
                stock["baseline_raw_difference"] = raw_close - candidate["raw_close"]
                if abs(raw_close - candidate["raw_close"]) > RAW_PRICE_TOLERANCE:
                    raise ValueError("baseline_raw_price_revision")
            except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
                baseline_issue = str(exc)[:160]
            baseline_pending = (baseline_issue == "signal_session_not_in_completed_data"
                                and timing["classification"] != "invalid_signal_time"
                                and (not completed or session > completed[-1]))
            stock["baseline_status"] = ("pending" if baseline_pending else
                                         "invalid" if baseline_issue else "verified")
            if baseline_issue:
                stock["baseline_issue"] = baseline_issue
            for horizon in HORIZONS:
                target = base_position + horizon if base_position is not None else None
                actual_target = sessions[target] if target is not None and target < len(sessions) else None
                if baseline_pending:
                    outcome = {"status": "pending", "reason": "baseline_dataset_not_yet_available"}
                elif baseline_issue:
                    outcome = {"status": "invalid", "reason": baseline_issue}
                elif actual_target is None or actual_target not in completed_set:
                    outcome = {"status": "pending", "reason": "future_completed_session_not_available"}
                else:
                    try:
                        future_close = _value(store, code, "close", target)
                        _value(store, code, "volume", target)
                        outcome = {"status": "resolved", "observed_session": actual_target,
                                   "adjusted_price_change": future_close / baseline - 1}
                    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
                        outcome = {"status": "invalid", "observed_session": actual_target,
                                   "reason": "endpoint_missing_or_invalid: " + str(exc)[:120]}
                stock["horizons"][str(horizon)] = outcome
                counts[str(horizon)][outcome["status"]] += 1
            record["stocks"].append(stock)
        observations.append(record)
    return {
        "observation_label": LABEL, "evaluated_at": asof.isoformat(),
        "latest_completed_data_session": completed[-1] if completed else None,
        "horizons_in_exchange_sessions": list(HORIZONS),
        "methodology": {
            "price_change": "adjusted_close(signal+h)/adjusted_close(signal)-1 from one current dataset",
            "raw_baseline_tolerance_yuan": RAW_PRICE_TOLERANCE,
            "missing_values": "invalid or pending, never zero or forward-filled",
            "execution_assumptions": "none: no orders, fills, allocations, fees or executable-return claims",
            "aggregation": "observation-status counts only; no portfolio return or win rate",
            "duplicate_runs": "each frozen invocation remains separate; repeated lists are not independent statistical samples",
            "timing_proof": "local recorded wall clock, not independent timestamp notarization",
        },
        "stats": {"valid_records": len(valid_records), "invalid_records": len(invalid_records),
                  "stock_observations": sum(len(record["stocks"]) for record in observations),
                  "timing_counts": dict(timing_counts),
                  "horizon_status_counts": {
                      key: {status: value[status] for status in ("resolved", "pending", "invalid")}
                      for key, value in counts.items()
                  }},
        "records": observations, "invalid_records": invalid_records,
    }
