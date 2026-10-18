"""Load the fixed accuracy comparison without granting recommendation authority.

Hashes establish consistency with the local manifest, not independent data
authenticity or a live performance record. This loader never enables a model.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re


EVIDENCE_DIRECTORY = "references/accuracy-resolution-20261016"
INDEX_FILE = EVIDENCE_DIRECTORY + "/evidence-index.json"
RULE_IDS = ("alpha360-five-session-rolling", "trend-same-constraints")
PERIODS = ("2018-2020", "2021-2023", "2024-2026")
METRICS = ("net_return", "paired_excess", "stress_net_return")
COUNTS = ("recommendations", "resolved", "profitable", "nonprofitable",
          "entry_rejected", "unresolved", "censored", "mature_recommendations")


class _EvidenceError(ValueError):
    pass


def _result(verified, reasons, *, summary=None, strategies=None, files=None):
    return {
        "status": "improved_but_unqualified" if verified else "not_validated",
        "verified": verified, "evidence_verified": verified,
        "models_enabled": False, "eligible_for_recommendation": False,
        "reason": "；".join(reasons), "reasons": reasons,
        "summary": summary if verified else None,
        "strategies": strategies if verified else [],
        "verified_files": files if verified else [],
        "integrity_scope": "文件哈希仅证明与本地清单一致，不代表外部认证或实盘收益验证。",
    }


def _safe_file(root, relative):
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise _EvidenceError("证据路径无效")
    parts = relative.split("/")
    if (PurePosixPath(relative).is_absolute()
            or any(part in ("", ".", "..") or ":" in part or "\x00" in part for part in parts)
            or tuple(parts[:2]) != tuple(EVIDENCE_DIRECTORY.split("/"))):
        raise _EvidenceError("证据路径越界或格式无效")
    local = root
    for part in parts:
        local = local / part
        if local.is_symlink():
            raise _EvidenceError("证据路径包含符号链接，无法核验独立打包文件")
    local = local.resolve(strict=True)
    if not local.is_relative_to(root / EVIDENCE_DIRECTORY) or not local.is_file():
        raise _EvidenceError("证据路径不是包内文件")
    return local


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _EvidenceError("证据JSON包含重复字段")
        result[key] = value
    return result


def _invalid_constant(value):
    raise _EvidenceError("证据JSON包含非有限数值")


def _json(data):
    return json.loads(data, object_pairs_hook=_unique_object,
                      parse_constant=_invalid_constant)


def _number(value):
    return (value is None or isinstance(value, (int, float))
            and not isinstance(value, bool) and math.isfinite(value))


def _counts(row):
    if not isinstance(row, dict) or any(type(row.get(key)) is not int or row[key] < 0 for key in COUNTS):
        raise _EvidenceError("验证结果缺少有效的全量样本计数")
    if (sum(row[key] for key in ("resolved", "entry_rejected", "unresolved", "censored")) != row["recommendations"]
            or row["profitable"] + row["nonprofitable"] != row["resolved"]
            or row["mature_recommendations"] > row["recommendations"]):
        raise _EvidenceError("验证结果的样本分母不一致")
    for key in ("pooled_net_mean", "pooled_excess_mean", "pooled_stress_mean",
                "conditional_profitable_fraction", "confirmed_profitable_over_all"):
        if key not in row or not _number(row[key]):
            raise _EvidenceError("验证结果包含缺失或无效的统计字段")


def _extract(summary):
    if not isinstance(summary, dict) or not isinstance(summary.get("strategies"), dict):
        raise _EvidenceError("验证摘要结构不完整")
    if set(summary["strategies"]) != set(RULE_IDS):
        raise _EvidenceError("验证摘要的规则身份与本次登记不一致")
    rows = []
    for rule_id in RULE_IDS:
        source = summary["strategies"][rule_id]
        if not isinstance(source, dict):
            raise _EvidenceError("验证规则记录无效")
        _counts(source.get("all"))
        periods = source.get("periods")
        if not isinstance(periods, dict) or set(periods) != set(PERIODS):
            raise _EvidenceError("验证摘要未完整保留三个登记时期")
        for period in PERIODS:
            item = periods[period]
            if not isinstance(item, dict):
                raise _EvidenceError("分期验证记录无效")
            _counts(item.get("counts_and_pooled"))
            means = item.get("daily_equal_weight")
            if not isinstance(means, dict) or any(
                    key not in means or not _number(means[key])
                    for key in ("net_mean", "excess_mean", "double_cost_net_mean")):
                raise _EvidenceError("分期验证缺少有效的日期等权统计")
        if any(sum(periods[p]["counts_and_pooled"][key] for p in PERIODS) != source["all"][key]
               for key in COUNTS):
            raise _EvidenceError("分期验证计数与全量分母不一致")
        bootstrap = source.get("bootstrap")
        if not isinstance(bootstrap, dict) or not isinstance(bootstrap.get("metrics"), dict):
            raise _EvidenceError("验证摘要缺少区块置信区间")
        for field in METRICS:
            item = bootstrap["metrics"].get(field)
            if not isinstance(item, dict) or not _number(item.get("daily_equal_weight_mean")):
                raise _EvidenceError("区块验证统计无效")
            interval = item.get("percentile_interval95")
            if interval is not None and (not isinstance(interval, list) or len(interval) != 2
                    or any(value is None or not _number(value) for value in interval)
                    or interval[0] > interval[1]):
                raise _EvidenceError("区块置信区间无效")
        # Preserve the reported numbers, while refusing all promotion flags.
        rows.append({"id": rule_id, "status": "improved_but_unqualified",
                     "models_enabled": False, "eligible_for_recommendation": False,
                     "all": source["all"], "periods": periods, "bootstrap": bootstrap,
                     "signal_day_counts": source.get("signal_day_counts", {}),
                     "reported_economic_gates_passed": source.get("economic_gates_passed") is True})
    return rows


def load_accuracy_evidence(skill_root):
    """Verify the complete shipped evidence set and return display-only data.

    ``evidence_files`` maps skill-root-relative POSIX paths to SHA256 strings.
    ``summary_file`` must be one of those files. All evidence, including frozen
    source copies, stays in the dedicated references directory. On any missing,
    unsafe, changed or invalid input, no unverified summary or metrics escape.
    Neither an index flag nor a summary's claimed gate success can enable a
    model or turn this retrospective comparison into recommendation approval.
    """
    try:
        root = Path(skill_root).resolve(strict=True)
        index = _json(_safe_file(root, INDEX_FILE).read_bytes())
        if not isinstance(index, dict) or type(index.get("schema_version")) is not int or index["schema_version"] != 1:
            raise _EvidenceError("验证资料清单版本无效")
        manifest = index.get("evidence_files")
        summary_file = index.get("summary_file")
        if (not isinstance(manifest, dict) or not manifest
                or not isinstance(summary_file, str) or summary_file not in manifest):
            raise _EvidenceError("验证资料清单缺少摘要或文件哈希")
        summary_bytes = None
        for relative, expected in manifest.items():
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise _EvidenceError("验证资料SHA256格式无效")
            local = _safe_file(root, relative)
            digest = hashlib.sha256()
            chunks = [] if relative == summary_file else None
            with local.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    if chunks is not None:
                        chunks.append(chunk)
            if digest.hexdigest() != expected:
                raise _EvidenceError("验证资料哈希不一致，不能引用其中的准确性结果")
            if chunks is not None:
                summary_bytes = b"".join(chunks)
        summary = _json(summary_bytes)
        strategies = _extract(summary)
        return _result(True, ["本轮对照证据文件已核验，仍未取得成熟推荐资格，模型未启用",
                              "历史资料曾被重复研究，精确并列可能超过五支；条件统计不等于实盘或五股组合收益",
                              "历史ST、正式限价、完整公司行动和开盘成交分配仍未全部核实"],
                       summary=summary, strategies=strategies, files=sorted(manifest))
    except _EvidenceError as exc:
        return _result(False, [str(exc), "无法核验本轮准确性证据，模型保持停用"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return _result(False, ["验证资料缺失、损坏或无法安全读取", "无法核验本轮准确性证据，模型保持停用"])
