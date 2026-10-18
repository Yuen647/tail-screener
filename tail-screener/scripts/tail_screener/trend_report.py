"""Standalone, source-preserving Chinese report for the stock trend template.

The renderer does not calculate signals, invent missing observations, or turn
the display limit into a portfolio allocation. All remote text is escaped and
only ordinary HTTP(S) evidence links are made clickable.
"""
from __future__ import annotations

from html import escape
import json
import math
from typing import Any
from urllib.parse import urlsplit


def _text(value: Any, default: str = "未提供") -> str:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _e(value: Any, default: str = "未提供") -> str:
    return escape(_text(value, default), quote=True)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _first(mapping: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value: Any, places: int = 2, suffix: str = "") -> str:
    number = _number(value)
    return "未提供" if number is None else f"{number:,.{places}f}{suffix}"


def _money(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "未提供"
    scale, unit = (1e8, " 亿元") if abs(number) >= 1e8 else (1e4, " 万元")
    return _fmt(number / scale, suffix=unit)


def _issue_text(value: Any) -> str:
    if isinstance(value, dict):
        return _text(_first(value, "message", "reason", "description", "title", "detail", "note", "code", default=value))
    return _text(value)


def _count(value: Any) -> str:
    number = _number(value)
    return "未提供" if number is None or number < 0 else f"{int(number):,}"


def _link(url: Any, label: Any = "查看来源") -> str:
    raw = str(url or "")
    try:
        parsed = urlsplit(raw)
        valid = (parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                 and not parsed.username and not any(ord(char) < 32 for char in raw))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        return '<span class="muted">' + _e(label) + '（链接未提供或无效）</span>'
    return '<a href="' + _e(raw) + '" target="_blank" rel="noopener noreferrer">' + _e(label) + ' ↗</a>'


def _paragraphs(value: Any, empty: str = "未提供") -> str:
    rows = _items(value)
    return "".join('<p>' + _e(item) + '</p>' for item in rows) if rows else '<p class="muted">' + _e(empty) + '</p>'


# Aliases allow callers to retain their recorded condition identifiers. The
# original label always wins. Unknown identifiers are retained, not relabeled.
_CONDITIONS = [
    ("价格高于 150 日与 200 日均线", ("price_above_150_200", "price_above_sma150_sma200", "close_above_150_200", "close_above_ma150_and_ma200")),
    ("150 日均线高于 200 日均线", ("sma150_above_200", "sma150_above_sma200", "ma150_above_200", "ma150_above_ma200")),
    ("200 日均线向上", ("sma200_rising", "sma200_rising_20", "ma200_rising")),
    ("50 日均线高于 150 日与 200 日均线", ("sma50_above_150_200", "sma50_above_sma150_sma200", "ma50_above_150_200", "ma50_above_ma150_and_ma200")),
    ("价格高于 50 日均线", ("price_above_50", "price_above_sma50", "close_above_50", "close_above_ma50")),
    ("价格较 52 周低点至少高 30%", ("above_low_30pct", "above_52w_low_30pct", "price_above_low_30pct", "above_low", "close_at_least_130pct_52w_low")),
    ("价格距 52 周高点不超过 25%", ("within_high_25pct", "within_52w_high_25pct", "price_near_high", "near_high", "close_at_least_75pct_52w_high")),
    ("相对强弱代理达到规定分位", ("rs_above70", "rs_proxy_above_70", "rs_proxy_ge_70", "relative_strength", "rs", "rs_proxy_at_least_70")),
]
_CONDITION_LABELS = {alias: label for label, aliases in _CONDITIONS for alias in aliases}
_CONDITION_LABELS.update({"ma200_above_120_sessions_ago": "200 日均线高于 120 个交易日前", "rs_proxy_at_least_70": "RS 代理值至少为 70"})


def _condition_observation(identifier: str, metrics: dict) -> str | None:
    """Describe recorded observations; never recalculate the signal."""
    price, ma50, ma150, ma200 = (_fmt(metrics.get(key)) for key in ("price", "sma50", "sma150", "sma200"))
    observations = {
        "close_above_ma150_and_ma200": f"收盘 {price}；150 日 {ma150}；200 日 {ma200}",
        "ma150_above_ma200": f"150 日 {ma150}；200 日 {ma200}",
        "ma200_above_120_sessions_ago": f"当前 {ma200}；120 个交易日前 {_fmt(metrics.get('sma200_120_ago'))}",
        "ma50_above_ma150_and_ma200": f"50 日 {ma50}；150 日 {ma150}；200 日 {ma200}",
        "close_above_ma50": f"收盘 {price}；50 日 {ma50}",
        "close_at_least_130pct_52w_low": "较低点高 " + _fmt(metrics.get("above_52w_low_pct"), suffix="%"),
        "close_at_least_75pct_52w_high": "距高点 " + _fmt(metrics.get("below_52w_high_pct"), suffix="%"),
        "rs_proxy_at_least_70": "本次代理值 " + _fmt(metrics.get("rs_proxy")),
    }
    return observations.get(identifier)


def _conditions(value: Any, metrics: dict | None = None) -> str:
    rows = []
    if isinstance(value, dict):
        entries = [(key, item) for key, item in value.items()]
    else:
        entries = [(str(index), item) for index, item in enumerate(_items(value))]
    for key, raw in entries:
        item = _dict(raw)
        identifier = str(_first(item, "id", "key", default=key))
        label = _first(item, "label", "description", "rule", default=_CONDITION_LABELS.get(identifier, identifier))
        passed = _first(item, "passed", "pass", "met", default=raw if isinstance(raw, bool) else None)
        state, css = ("通过", "pass") if passed is True else ("未通过", "fail") if passed is False else ("未核验", "unknown")
        detail = _first(item, "detail", "evidence", "observation")
        if detail is None and "observed" in item:
            detail = _text(item["observed"])
            if "threshold" in item:
                detail += " " + _text(item.get("operator"), "对照") + " " + _text(item["threshold"])
        if detail is None and metrics:
            detail = _condition_observation(identifier, metrics)
        rows.append('<li><span class="condition-state ' + css + '">' + state + '</span><div>'
                    + _e(label) + ('<small>' + _e(detail) + '</small>' if detail is not None else '') + '</div></li>')
    if not rows:
        rows = ['<li><span class="condition-state unknown">未核验</span><div>' + _e(label) + '</div></li>'
                for label, _ in _CONDITIONS]
    return '<ul class="conditions">' + ''.join(rows) + '</ul>'


def _financials(value: Any) -> str:
    data = _dict(value)
    metrics = _dict(data.get("metrics")) or data
    fields = [
        (("revenue_yoy", "revenue_yoy_pct", "revenue_growth_pct"), "营收同比", "%"),
        (("profit_yoy", "profit_yoy_pct", "net_profit_yoy", "net_profit_growth_pct"), "归母净利同比", "%"),
        (("roe", "roe_pct"), "ROE", "%"),
        (("eps", "basic_eps"), "每股收益", " 元"),
        (("pe_ttm", "pe"), "市盈率", " 倍"),
        (("pb",), "市净率", " 倍"),
    ]
    rows = []
    for key, label in [("revenue", "营业收入"), ("net_profit", "归母净利润"), ("core_profit", "扣非归母净利润")]:
        if metrics.get(key) is not None:
            amount = _number(metrics[key])
            loss = amount is not None and amount < 0 and key in {"net_profit", "core_profit"}
            rows.append('<div><dt>' + label + '</dt><dd' + (' class="loss-value"' if loss else '') + '>'
                        + _e(_money(metrics[key])) + ('<small class="loss-value">本期亏损</small>' if loss else '') + '</dd></div>')
    for aliases, label, suffix in fields:
        result = _first(metrics, *aliases)
        if result is not None:
            rows.append('<div><dt>' + label + '</dt><dd>' + _e(_fmt(result, suffix=suffix)) + '</dd></div>')
    body = '<dl class="compact-metrics">' + ''.join(rows) + '</dl>' if rows else '<p class="muted">财务数值未提供，不能据此认定基本面通过。</p>'
    report_date = _first(data, "report_date", "period", "report_period")
    announced = _first(data, "announced_at", "published_at", "announcement_date")
    fetched = _first(data, "fetched_at", "retrieved_at")
    if report_date is not None:
        body += '<p class="meta">财务报告期：' + _e(report_date) + '</p>'
    if announced is not None:
        body += '<p class="meta">披露时间：' + _e(announced) + '</p>'
    elif rows:
        body += '<p class="meta">披露时间未提供，无法据此核验当时可知性。</p>'
    if fetched is not None:
        body += '<p class="meta">采集时间：' + _e(fetched) + '</p>'
    if data.get("source_url") or data.get("url"):
        body += '<p class="meta">' + _link(_first(data, "source_url", "url"), _first(data, "source", "source_name", default="财务来源")) + '</p>'
    if data.get("message"):
        body += '<p class="meta">' + _e(data["message"]) + '</p>'
    return body


def _notices(value: Any) -> str:
    data = _dict(value)
    entries = _items(_first(data, "recent_items", "items", "notices", "headlines", default=value if isinstance(value, list) else []))
    rows = []
    for raw in entries[:5]:
        item = _dict(raw)
        title = _first(item, "title", "headline", "name", default=raw if isinstance(raw, str) else "未提供标题")
        url = _first(item, "url", "source_url", "link")
        headline = _link(url, title) if url else _e(title)
        timestamp = _first(item, "published_at", "announced_at", "announcement_date", "date")
        rows.append('<li>' + headline + '<small>披露时间：' + _e(timestamp) + '</small></li>')
    if rows:
        body = '<ul class="notices">' + ''.join(rows) + '</ul><p class="meta">公告标题用于进一步核查；标题清单不等于正文已完成审阅。</p>'
        if len(entries) > 5:
            body += '<p class="meta">本卡展示最近 5 条，完整 ' + str(len(entries)) + ' 条见 report.json。</p>'
    elif data.get("status") in {"ok", "complete", "success"} or data.get("verified") is True:
        body = '<p class="muted">本次来源返回 0 条公告；不能据此推断没有未覆盖事项。</p>'
    else:
        body = '<p class="muted">公告未完成核验，尚无可展示的来源记录。</p>'
    period = _first(data, "window", "lookback", "date_range")
    if period is not None:
        body += '<p class="meta">查询范围：' + _e(period) + '</p>'
    if data.get("scope"):
        body += '<p class="meta">来源筛查口径：' + _e(data["scope"]) + '。列表按来源返回顺序展示，具体日期见各条披露时间。</p>'
    fetched = _first(data, "fetched_at", "retrieved_at")
    if fetched:
        body += '<p class="meta">采集时间：' + _e(fetched) + '</p>'
    if data.get("message"):
        body += '<p class="meta">' + _e(data["message"]) + '</p>'
    return body


def _candidate(row: dict, index: int) -> str:
    metrics = _dict(row.get("metrics"))
    risk = _dict(row.get("risk"))
    reason = row.get("selection_reason") or "本次记录未提供排序理由；请核对下方规则证据。"
    price = _first(metrics, "price", "raw_close")
    rs = _first(metrics, "rs_proxy", "rs_proxy_pct", "rs_percentile", "rs_proxy_percentile")
    # Raw Qlib ma50/high_52w fields are normalized source values, not RMB.
    # Only renderer-facing aliases may be displayed alongside the RMB price.
    low = metrics.get("low_252")
    high = metrics.get("high_252")
    amount = _number(_first(metrics, "amount_yuan", "turnover_amount"))
    amount_text = _fmt(amount / 1e8, suffix=" 亿元") if amount is not None else "未提供"
    metric_fields = [
        ("收盘价（元）", _fmt(price)), ("RS 代理（1–99）" if "rs_proxy" in metrics else "RS 代理分位", _fmt(rs, suffix="" if "rs_proxy" in metrics else "%")),
        ("50 日均线", _fmt(metrics.get("sma50"))),
        ("150 日均线", _fmt(metrics.get("sma150"))),
        ("200 日均线", _fmt(metrics.get("sma200"))),
        ("52 周低 / 高", _fmt(low) + " / " + _fmt(high)), ("信号日成交额", amount_text),
    ]
    metrics_html = ''.join('<div><dt>' + label + '</dt><dd>' + _e(value) + '</dd></div>' for label, value in metric_fields)
    issues = _items(risk.get("issues")) + _items(row.get("review_gaps"))
    issue_texts = list(dict.fromkeys(_issue_text(item) for item in issues))
    review = '<ul>' + ''.join('<li>' + _e(item) + '</li>' for item in issue_texts) + '</ul>' if issue_texts else '<p class="muted">未提供额外待核查事项；这不代表全部风险已核验。</p>'
    known_issues = list(dict.fromkeys(_issue_text(item) for item in (_items(row.get("review_flags")) + _items(risk.get("issues")))))
    known_html = ('<div class="known-issues"><strong>已知事项与核验状态</strong><ul>'
                  + ''.join('<li>' + _e(item) + '</li>' for item in known_issues) + '</ul></div>') if known_issues else ''
    financial_metrics = _dict(_dict(row.get("financials")).get("metrics")) or _dict(row.get("financials"))
    losses = []
    for key, label in [("net_profit", "归母净利润"), ("core_profit", "扣非归母净利润")]:
        amount = _number(financial_metrics.get(key))
        if amount is not None and amount < 0:
            losses.append(label + "亏损 " + _money(abs(amount)))
    loss_html = '<p class="loss-fact">财务事实：' + _e('；'.join(losses)) + '（本期报告值）</p>' if losses else ''
    label = "趋势模板候选"
    if risk.get("eligible") is False:
        label = "数据核验未完成" if risk.get("data_verified") is False else "风险核查未通过"
    elif risk.get("eligible") is None:
        label = "趋势候选 · 风险待核查"
    basis = _first(metrics, "price_basis", default=row.get("price_basis"))
    supplied_rank = _number(row.get("rank"))
    rank = int(supplied_rank) if supplied_rank is not None and supplied_rank > 0 else index
    tie_size = _number(row.get("tie_group_size"))
    tie = ' · 同分并列 ' + str(int(tie_size)) + ' 只' if tie_size is not None and tie_size > 1 else ''
    quote = _dict(row.get('quote'))
    quote_html = ('<p class="meta">采集时报价：' + _e(_fmt(quote.get('price'))) + ' 元 · '
                  + _e(quote.get('quote_at')) + '；与信号日收盘价分开记录，不是成交承诺。</p>') if quote else ''
    return ('<article class="candidate" id="candidate-' + str(index) + '"><div class="candidate-heading">'
            '<div class="identity"><span class="rank">' + str(rank).zfill(2) + '</span><div><h3>'
            + _e(row.get("name") or row.get("code")) + '</h3><span class="code">' + _e(row.get("code"))
            + tie + '</span></div></div><span class="tag">' + label + '</span></div>'
            '<p class="reason">' + _e(reason) + '</p>' + loss_html + known_html + '<dl class="metrics">' + metrics_html + '</dl>'
            + ('<p class="meta">价格口径：' + _e(basis) + '</p>' if basis else '')
            + quote_html
            + '<h4>趋势模板逐项核验</h4>' + _conditions(row.get("conditions"), metrics)
            + '<div class="review-grid"><section><h4>基本面摘要</h4>' + _financials(row.get("financials"))
            + '</section><section><h4>近期公告</h4>' + _notices(row.get("notices")) + '</section></div>'
            '<details class="review"><summary>需要进一步核查的事项</summary>' + review + '</details></article>')


def _holding(strategy: dict) -> str:
    holding = strategy.get("holding_period")
    if isinstance(holding, dict):
        holding = _first(holding, "label", "description", "summary")
    return holding or "趋势模板没有规定统一持有天数或完整退出规则；持有时长需由后续交易计划定义。"


def _method(report: dict) -> str:
    strategy = _dict(report.get("strategy"))
    selection = _dict(report.get("selection"))
    validation = strategy.get("validation")
    validation = _first(validation, "label", "summary", "message", "decision", "profitability", default="未提供 A 股策略表现验证结论。") if isinstance(validation, dict) else validation
    rules = strategy.get("rule_descriptions")
    if isinstance(rules, dict):
        rules = list(rules.values())
    rules_html = '<ol>' + ''.join('<li>' + _e(item) + '</li>' for item in _items(rules)) + '</ol>' if rules else '<p class="muted">具体规则阈值未提供，请核对 report.json。</p>'
    rule = _first(selection, "description", "rule", "ranking_rule", "label", default="本次排序说明未提供。")
    observation = _dict(strategy.get('historical_observation'))
    observation_html = ''
    if observation:
        horizons = _dict(_dict(observation.get('all_period')).get('horizons'))
        rows = []
        for horizon in ('1', '5', '10', '20'):
            facts = _dict(horizons.get(horizon))
            values = [_number(facts.get(key)) for key in ('selected_observation_mean', 'selected_observation_median', 'paired_daily_difference_mean')]
            rows.append('<tr><th>' + horizon + '个交易日</th>' + ''.join('<td>' + _e(_fmt(value * 100, places=3) if value is not None else None) + '</td>' for value in values) + '</tr>')
        observation_html = ('<h3>固定规则历史观察：2018—2020年</h3><p>730个信号日。以下是信号后的复权价格变化，未假设成交；不计费用，不能视为交易收益或胜率。</p>'
                            '<table class="source-table"><tr><th>观察期</th><th>均值（%）</th><th>中位数（%）</th><th>相对比较总体（百分点）</th></tr>'
                            + ''.join(rows) + '</table><p>次日均值略落后比较总体；较长期正向均值主要来自2020年，2018年各观察期均落后。未支持稳定短线优势。</p>'
                            '<p class="meta">比较总体为同日历史完整的沪深股票。缺少完整历史ST与可成交性数据；重复选股和观察区间重叠，不是独立样本。完整月度拆分与原始数据随技能包提供。</p>')
    return ('<details class="panel" id="method"><summary>策略来源、排序与持有安排</summary>'
            '<div class="method-grid"><section><h3>采用什么方法</h3><p>' + _e(strategy.get("name") or "Minervini 趋势模板")
            + '</p><p>' + _link(strategy.get("source_url"), "查看公开规则来源") + '</p>' + rules_html
            + '</section><section><h3>如何得到展示顺序</h3><p>' + _e(rule) + '</p>'
            '<p>RS 代理分位来自本次实现使用的价格强弱排序，不是 IBD / MarketSmith 专有 RS Rating，不能作为收益率或胜率。</p>'
            '<p>默认展示 5 只表示报告展示名额，不规定持仓数量、资金分配或必须买入 5 只。</p>'
            '<h3>持有时长与退出</h3><p>' + _e(_holding(strategy)) + '</p>'
            '<p>趋势模板用于筛出符合趋势条件的股票，不等于完整 SEPA 买入策略；本页没有将模板筛选结果换算成买入价或收益承诺。</p>'
            '</section></div><h3>本次适配与验证状态</h3>' + _paragraphs(strategy.get("adaptations"), "未提供适配说明。")
            + '<p>' + _e(validation, "未提供 A 股策略表现验证结论。") + '</p>' + observation_html + '</details>')


_ADMISSION_LABELS = {
    "not_validated": "推荐资格未验证", "screening_only": "仅可筛选候选",
    "rejected": "未通过推荐准入", "conditional": "附条件准入",
    "admitted": "报告记录已通过推荐准入",
}
_VALIDATION_COUNTS = [
    ("recommendation_total", "全部推荐"), ("resolved_trades", "模型成交且到期"),
    ("net_profitable", "模型净盈利"), ("net_nonprofitable", "模型净亏损或持平"),
    ("execution_rejected", "模型拒绝成交"), ("pending", "未到期"), ("unverifiable", "无法核验"),
]


def _integer_count(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 and number.is_integer() else None


def _admission(value: Any, issues: list[str] | None = None) -> str:
    data = _dict(value)
    level = data.get("level", "not_validated")
    label = _ADMISSION_LABELS.get(level, "推荐资格未验证")
    if issues and level in {"admitted", "conditional"}:
        label = "准入结论待复核：验证证据不完整"
    css = "pass" if level == "admitted" and not issues else "fail" if level == "rejected" else "unknown"
    return '<span class="validation-admission ' + css + '">' + label + '</span>'


def _validation_strategy(row: dict, position: int) -> tuple[str, list[str]]:
    """Keep all recommendation outcomes visible and verify denominators.

    Counts must describe mutually exclusive final states. Return means are
    supplied in percent/percentage-point units, never inferred from win rates.
    The renderer does not decide profitability admission or fill missing data.
    """
    raw = _dict(row.get("counts"))
    counts = {key: _integer_count(raw.get(key)) for key, _ in _VALIDATION_COUNTS}
    issues = []
    missing = [label for key, label in _VALIDATION_COUNTS if counts[key] is None]
    if missing:
        issues.append("计数缺失或无效：" + "、".join(missing) + "；不能核验推荐分母。")
    else:
        total, resolved = counts["recommendation_total"], counts["resolved_trades"]
        if resolved + counts["execution_rejected"] + counts["pending"] + counts["unverifiable"] != total:
            issues.append("分母不闭合：模型成交且到期、模型拒绝成交、未到期、无法核验之和不等于全部推荐。")
        if counts["net_profitable"] + counts["net_nonprofitable"] != resolved:
            issues.append("盈利分类不闭合：模型净盈利与模型净亏损或持平之和不等于模型成交且到期。")
        if total == 0:
            issues.append("全部推荐计数为 0；没有可用于推荐有效性判断的样本。")
    counts_valid = not issues
    total, resolved, profitable = (counts[key] for key in ("recommendation_total", "resolved_trades", "net_profitable"))
    all_ratio = _fmt(profitable / total * 100, suffix="%") if counts_valid and total else "未验证"
    resolved_ratio = _fmt(profitable / resolved * 100, suffix="%") if counts_valid and resolved else "未验证"
    denominator_text = lambda numerator, denominator: _count(numerator) + " / " + _count(denominator)
    metrics = _dict(row.get("metrics"))
    metric_html = []
    negative = []
    for key, field, label, unit in [("net_return", "mean_pct", "模型成交到期净均值", "%"),
                                    ("excess_return", "mean_pp", "模型相对指数差均值", " 个百分点"),
                                    ("double_cost_net_return", "mean_pct", "双倍费用下模型净均值", "%")]:
        metric = _dict(metrics.get(key))
        mean = _number(metric.get(field))
        samples = _integer_count(metric.get("sample_count"))
        sample_valid = samples is not None and resolved is not None and 0 < samples <= resolved
        if mean is None or not sample_valid:
            issues.append(label + "的数值或样本计数缺失、无效，尚不能核验。")
        value = _fmt(mean, suffix=unit) if mean is not None else "未验证"
        if mean is not None and mean < 0:
            negative.append(label + "为负（" + value + "）")
        metric_html.append('<div><dt>' + label + '</dt><dd' + (' class="loss-value"' if mean is not None and mean < 0 else '')
                           + '>' + _e(value) + '</dd><small>有效样本 / 模型成交到期：'
                           + denominator_text(samples, resolved) + (' · 数值待核验' if mean is not None and not sample_valid else '')
                           + '</small>' + ('<small>比较指数：' + _e(metric.get("benchmark")) + '</small>' if key == "excess_return" else '') + '</div>')
    admission = _dict(row.get("admission"))
    counts_html = ''.join('<div><dt>' + label + '</dt><dd>'
                          + (_count(counts[key]) if counts[key] is not None else _e(raw.get(key), "未提供") + '（未核验）')
                          + '</dd></div>' for key, label in _VALIDATION_COUNTS)
    coverage = _fmt(resolved / total * 100, suffix="%") if counts_valid and total else "未验证"
    evidence_issues = ('<div class="known-issues"><strong>证据核验未完成</strong><ul>'
                       + ''.join('<li>' + _e(issue) + '</li>' for issue in issues) + '</ul></div>') if issues else ''
    negatives = '<p class="loss-fact">' + _e('；'.join(negative)) + '。</p>' if negative else ''
    unresolved = (counts.get("pending") or 0) + (counts.get("unverifiable") or 0)
    unresolved_note = ('<p class="validation-note">仍有未到期或无法核验记录；模型净盈利占比不是最终全部推荐成功率。</p>'
                       if unresolved or not counts_valid else '')
    return ('<article class="validation-strategy"><div class="validation-heading"><h3>'
            + _e(row.get("name") or row.get("id") or "固定策略 " + str(position)) + '</h3>' + _admission(admission, issues)
            + '</div><p>' + _e(admission.get("reason"), "未提供推荐准入判断依据。") + '</p><p class="meta">持有安排：'
            + _e(row.get("holding_period")) + '</p>' + negatives + evidence_issues
            + '<dl class="validation-metrics"><div><dt>模型净盈利 / 全部推荐</dt><dd>' + all_ratio
            + '</dd><small>分子 / 分母：' + denominator_text(profitable, total) + '</small></div>'
            '<div><dt>模型成交到期净盈利比例</dt><dd>' + resolved_ratio + '</dd><small>条件分母：'
            + denominator_text(profitable, resolved) + '；不能称为全部推荐成功率</small></div>' + ''.join(metric_html) + '</dl>'
            '<dl class="validation-counts">' + counts_html + '</dl><p class="validation-note">模型成交到期评估覆盖：' + coverage
            + '。模型净盈利 + 模型净亏损或持平 = 模型成交且到期。全部推荐 = 模型成交且到期 + 模型拒绝成交 + 未到期 + 无法核验；模型拒绝成交和未决记录保留在全部推荐分母中。</p>'
            + unresolved_note + '<p class="meta">模型净均值与相对指数差仅对应各自已列样本，不把未成交或无法核验记录按零收益填入。</p>'
            + (('<h4>适用限制</h4><ul>' + ''.join('<li>' + _e(_issue_text(item)) + '</li>' for item in _items(row.get("limitations"))) + '</ul>')
               if row.get("limitations") else '') + '</article>', issues)


def _recommendation_validation(report: dict) -> str:
    data = _dict(report.get("recommendation_validation"))
    entries = [row for row in _items(data.get("strategies")) if isinstance(row, dict)]
    cards, evidence_issues = [], []
    for position, row in enumerate(entries, 1):
        card, issues = _validation_strategy(row, position)
        cards.append(card)
        evidence_issues.extend(issues)
    if not entries:
        evidence_issues.append("未提供固定策略的推荐结果及完整分母。")
    admission = _dict(data.get("admission"))
    body = ''.join(cards) if cards else '<p class="validation-note">未验证：尚无固定策略的模型净盈利比例、模型净均值、指数对照及完整评估覆盖记录。</p>'
    evidence_note = '<p class="validation-note"><strong>证据性质：</strong>' + _e(data.get("evidence_note"), "证据性质未提供；不据此声称有新的独立留出验证。") + '</p>'
    gate = data.get("gate_summary")
    if isinstance(gate, dict):
        gate_items = [str(key) + "：" + _issue_text(value) for key, value in gate.items()]
    else:
        gate_items = [_issue_text(item) for item in _items(gate)]
    gate_html = ('<h3>推荐准入核验</h3><ul>' + ''.join('<li>' + _e(item) + '</li>' for item in gate_items) + '</ul>') if gate_items else '<p class="meta">推荐准入检验项未提供。</p>'
    methodology = _items(data.get("methodology"))
    methods = ('<h3>费用、模型成交与评估口径</h3><ul>' + ''.join('<li>' + _e(_issue_text(item)) + '</li>' for item in methodology) + '</ul>') if methodology else '<p class="meta">费用、模型成交和评估口径未提供，净值口径尚不能完整复核。</p>'
    return ('<section class="panel recommendation-validation" id="recommendation-validation"><div class="validation-heading">'
            '<h2>推荐有效性检验</h2>' + _admission(admission, evidence_issues) + '</div>'
            '<p>候选筛选与推荐准入分开判断：符合选股公式不会自动获得推荐资格。</p>'
            '<p class="validation-note">以下为给定成交与费用假设下的条件化历史模拟，没有实盘成交记录；“全部推荐”指模型生成的历史推荐样本。</p><p>'
            + _e(admission.get("reason"), "当前未提供推荐准入结论。") + '</p><p class="meta">评估时间：'
            + _e(data.get("evaluated_at")) + ' · 评估范围：' + _e(data.get("scope")) + '</p>'
            + evidence_note + gate_html + body + methods + '</section>')


def _accuracy_evidence(report: dict) -> str:
    data = _dict(report.get('accuracy_evidence'))
    if not data:
        return ''
    opening = '<section class="panel" id="accuracy-evidence"><h2>最新排序验证：尚未取得推荐资格</h2>'
    if data.get('verified') is not True:
        return opening + '<p>验证资料缺失或校验失败，本次无法使用历史比较证明推荐有效。</p></section>'
    summary = _dict(data.get('summary'))
    strategies = _dict(summary.get('strategies'))
    for key in ('trend-same-constraints', 'alpha360-five-session-rolling'):
        entry = _dict(strategies.get(key))
        totals = _dict(entry.get('all'))
        metrics = _dict(_dict(entry.get('bootstrap')).get('metrics'))
        counts_required = ('recommendations', 'resolved', 'profitable', 'entry_rejected', 'unresolved', 'censored')
        if (any(_integer_count(totals.get(field)) is None for field in counts_required)
                or _number(totals.get('conditional_profitable_fraction')) is None
                or any(_number(_dict(metrics.get(field)).get('daily_equal_weight_mean')) is None
                       for field in ('net_return', 'stress_net_return'))):
            return opening + '<p>历史比较缺少必要指标或完整分母，无法展示有效的推荐准确性结论。</p></section>'
    def pct(value):
        number = _number(value)
        return _fmt(number * 100, places=3, suffix='%') if number is not None else '未提供'
    rows, denominators = [], []
    for key, label in [('trend-same-constraints', '趋势排序对照'),
                       ('alpha360-five-session-rolling', 'Alpha360 五日目标排序')]:
        row = _dict(strategies.get(key))
        all_rows = _dict(row.get('all'))
        metrics = _dict(_dict(row.get('bootstrap')).get('metrics'))
        rows.append('<tr><th>' + label + '</th><td>' + pct(_dict(metrics.get('net_return')).get('daily_equal_weight_mean'))
                    + '</td><td>' + pct(_dict(metrics.get('stress_net_return')).get('daily_equal_weight_mean'))
                    + '</td><td>' + pct(all_rows.get('conditional_profitable_fraction')) + '</td></tr>')
        denominators.append('<li>' + label + '：全部名单 ' + _count(all_rows.get('recommendations'))
                            + '；可结算 ' + _count(all_rows.get('resolved')) + '；其中盈利 ' + _count(all_rows.get('profitable'))
                            + '；入场被拒 ' + _count(all_rows.get('entry_rejected')) + '；结果未解决 '
                            + _count(all_rows.get('unresolved')) + '；未到期 ' + _count(all_rows.get('censored')) + '。</li>')
    model = _dict(strategies.get('alpha360-five-session-rolling'))
    intervals = _dict(_dict(_dict(model.get('bootstrap')).get('metrics')).get('paired_excess')).get('percentile_interval95')
    intervals = [_number(value) for value in intervals] if isinstance(intervals, list) and len(intervals) == 2 else []
    valid_interval = len(intervals) == 2 and all(value is not None for value in intervals) and intervals[0] <= intervals[1]
    interval_text = '至'.join(_fmt(value * 100, places=3, suffix='个百分点') for value in intervals) if valid_interval else '未提供'
    if valid_interval:
        interval_note = ('区间包含零，不能据此确认正向优势。' if intervals[0] <= 0 <= intervals[1]
                         else '这是条件化历史区间，不能替代完整交易数据和事前记录验证。')
    else:
        interval_note = '区间缺失，无法判断优势的统计稳定性。'
    counts = _dict(model.get('signal_day_counts'))
    return (opening + '<p>固定参数，逐年只用此前数据训练；2018—2026年10月15日，计划次日开盘入场、持有5个交易日，受阻退出顺延。'
            '以下模型尚未获得推荐资格，Alpha360排序未启用为默认推荐。</p>'
            '<table class="source-table"><tr><th>排序</th><th>扣费均值</th><th>双倍成本均值</th><th>可结算样本盈利比例</th></tr>'
            + ''.join(rows) + '</table><p class="meta">收益先在信号日内取可结算样本均值，再对日期等权；不是每日收益率、年化收益或组合回报。'
            '盈利比例按可结算票据计算，不是全部名单的成功率，也不是本次候选的上涨概率。</p><ul>' + ''.join(denominators) + '</ul>'
            '<p>新排序相对同端点沪深300价格变化的均值差，95%区间为 ' + interval_text + '；' + interval_note +
            '指数未扣成本、未计分红；个股模型扣费。</p><p>新排序共 ' + _count(counts.get('with_selection')) + ' 个信号日，其中 '
            + _count(counts.get('exactly_five')) + ' 天恰好5只，' + _count(counts.get('more_than_five')) + ' 天有边界并列，最多 '
            + _count(counts.get('maximum_candidates')) + ' 只。并列组成绩不能作为任意截取5只的成绩。</p>'
            '<p class="meta">历史数据此前已用于其他研究；这不是新的独立留出或实盘记录。历史ST、官方涨跌停、完整公司行动与开盘竞价分配仍未完全核验。'
            '趋势对照采用相同数据与成交约束，其历史资格口径与当前名称过滤不同；原始记录及逐年结果随技能包提供。</p></section>')


def _evidence(report: dict) -> str:
    original = _dict(report.get("data_source"))
    source = dict(_dict(original.get('manifest')), **original)
    source.setdefault('provider', original.get('repository'))
    source.setdefault('release_tag', original.get('release'))
    source.setdefault('cache_hit', original.get('cached'))
    labels = [("provider", "数据提供方"), ("source", "数据来源"), ("release_tag", "冻结版本"),
              ("target_trade_date", "来源交易日"), ("last_session", "最新实际交易日"),
              ("sha256", "文件 SHA-256"), ("archive_sha256", "文件 SHA-256"),
              ("verified_at", "校验时间"), ("stale", "是否使用旧数据"),
              ("cross_check", "交叉核对"), ("cache_hit", "本次是否命中缓存")]
    rows = ''.join('<tr><th>' + label + '</th><td>' + _e(source[key]) + '</td></tr>'
                   for key, label in labels if key in source)
    links = ''.join('<p>' + _link(source[key], label) + '</p>' for key, label in
                    [("url", "数据链接"), ("source_url", "数据来源链接"), ("manifest_url", "来源清单")]
                    if source.get(key))
    warnings = _items(report.get("warnings"))
    warning_html = '<h3>本次数据备注</h3><ul>' + ''.join('<li>' + _e(item) + '</li>' for item in warnings) + '</ul>' if warnings else ''
    monitoring = _dict(report.get('monitoring'))
    observation_html = ''
    if monitoring.get('record_file') == 'selection-record.json' and monitoring.get('observation_file') == 'observations.json':
        frozen = _dict(monitoring.get('record'))
        timing = _dict(frozen.get('timing'))
        observation_html = ('<h3>名单留存与后续检验</h3><p>' + _e(monitoring.get('observation_label'))
                            + '。后续调用按实际交易日跟踪1、5、10、20日价格变化。</p><p>'
                            + _e(timing.get('reason')) + '</p><div class="file-links">'
                            '<a href="selection-record.json">本次不可覆盖的名单记录</a>'
                            '<a href="observations.json">已有记录的后续观测</a></div>')
    return ('<details class="panel" id="evidence"><summary>数据来源与可复核文件</summary>'
            + ('<table class="source-table">' + rows + '</table>' if rows else '<p class="muted">来源摘要未提供，完整输入见随附 JSON。</p>')
            + links + warning_html + observation_html + '<div class="file-links"><a href="report.json">报告原始数据 · report.json</a>'
            '<a href="snapshot.json">本次冻结输入 · snapshot.json</a></div>'
            '<p class="meta">报告生成时间与行情交易日分开记录；旧行情不因重新生成报告而变成实时行情。</p></details>')


_STYLE = """
:root{color-scheme:light;--ink:#17283f;--muted:#65738a;--line:#dfe5ec;--blue:#173b65;--teal:#08675f;--paper:#fff;--bg:#f3f5f8}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}a{color:#1c5383;text-underline-offset:3px;overflow-wrap:anywhere}main{max-width:1160px;margin:auto;padding:32px 30px 64px}.topline{display:flex;justify-content:space-between;gap:16px;font-size:12px;letter-spacing:.08em;color:var(--muted)}nav{display:flex;gap:18px;letter-spacing:0}.hero{padding:38px 0 26px}.eyebrow{font-size:13px;font-weight:700;color:var(--blue);letter-spacing:.12em}h1{font-size:38px;line-height:1.25;margin:10px 0 16px;letter-spacing:-.035em}h2{font-size:23px;margin:0}h3{font-size:19px;margin:0 0 7px}h4{font-size:14px;margin:20px 0 10px}p{margin:8px 0}.lead{font-size:17px;max-width:850px}.muted,.meta{color:var(--muted)}.meta{font-size:12px;overflow-wrap:anywhere}.dates{display:flex;flex-wrap:wrap;gap:8px 28px;margin-top:18px;font-size:13px}.dates strong{display:block;font-weight:600;color:var(--ink)}.status{border-left:3px solid var(--teal);padding:10px 17px;background:#eaf3f0;border-radius:0 9px 9px 0;margin:6px 0 20px}.status.partial{background:#fff3e1;border-color:#b68124}.status strong{font-size:14px}.status p{margin:2px 0;font-size:13px}.funnel{display:grid;grid-template-columns:repeat(5,1fr);background:var(--paper);border:1px solid var(--line);border-radius:12px;overflow:hidden;margin:20px 0 10px}.funnel div{padding:20px;border-right:1px solid var(--line)}.funnel div:last-child{border:0}.funnel dt{font-size:12px;color:var(--muted)}.funnel dd{margin:4px 0 0;font-size:27px;font-weight:650;font-variant-numeric:tabular-nums}.section-heading{display:flex;align-items:baseline;justify-content:space-between;gap:15px;margin:32px 0 14px}.section-heading p{font-size:13px;color:var(--muted)}.candidate,.panel,.empty{border:1px solid var(--line);background:var(--paper);border-radius:14px;padding:25px 28px;margin:0 0 20px;box-shadow:0 2px 7px #18334e04}.candidate-heading,.identity{display:flex;align-items:center;gap:16px}.candidate-heading{justify-content:space-between}.candidate h3{margin:0;font-size:24px}.rank{font-size:28px;color:#7891ae;font-weight:600;line-height:1}.code{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}.tag{background:#edf3f8;color:var(--blue);padding:5px 10px;border-radius:5px;font-size:12px;white-space:nowrap}.reason{margin:18px 0;color:#284765}.metrics{display:grid;grid-template-columns:repeat(7,1fr);gap:14px;margin:18px 0;padding:16px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.metrics dt,.compact-metrics dt{font-size:11px;color:var(--muted)}.metrics dd{margin:5px 0 0;font-size:17px;font-weight:600;font-variant-numeric:tabular-nums;white-space:nowrap}.conditions{list-style:none;padding:0;margin:0;display:grid;grid-template-columns:1fr 1fr;gap:8px 20px}.conditions li{display:flex;align-items:flex-start;gap:9px;padding:6px 0;font-size:13px}.condition-state{font-size:11px;border-radius:4px;padding:1px 6px;white-space:nowrap;margin-top:1px}.pass{color:#126b57;background:#eaf5ef}.fail{color:#a13f31;background:#fceeea}.unknown{color:#7a6440;background:#f8f1e5}small{display:block;font-size:11px;color:var(--muted);overflow-wrap:anywhere}.review-grid,.method-grid{display:grid;grid-template-columns:1fr 1fr;gap:28px}.review-grid{border-top:1px solid var(--line);margin-top:18px}.review-grid h4{margin-top:17px}.review-grid p,.review-grid li{font-size:13px}.compact-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 12px}.compact-metrics dd{margin:2px 0;font-size:15px}.notices{padding-left:18px;margin:0}.notices li{padding:0 0 7px}.review{border-top:1px solid var(--line);margin-top:16px;padding-top:12px;font-size:13px}.review ul{padding-left:20px}summary{cursor:pointer;font-weight:600;color:var(--blue)}.panel>summary{font-size:17px}.panel[open]>summary{margin-bottom:20px}.panel p,.panel li,.source-table{font-size:13px}.panel h3{font-size:15px;margin-top:16px}.panel ol,.panel ul{padding-left:21px}.source-table{border-collapse:collapse;width:100%}.source-table th,.source-table td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}.source-table th{width:150px;color:var(--muted);font-weight:500}.file-links{display:flex;gap:20px;flex-wrap:wrap;margin-top:18px;font-size:13px}footer{color:var(--muted);font-size:12px;text-align:center;padding:16px}.empty h3{font-size:18px}.empty p{max-width:780px;font-size:14px}
@media(max-width:900px){.metrics{grid-template-columns:repeat(4,1fr)}.funnel div{padding:16px 12px}.funnel dd{font-size:23px}.metrics dd{font-size:16px}}
@media(max-width:640px){main{padding:22px 16px 40px}.topline{flex-direction:column;gap:8px}h1{font-size:29px}.hero{padding-top:26px}.lead{font-size:15px}.funnel{grid-template-columns:repeat(3,1fr)}.funnel div{border-bottom:1px solid var(--line)}.funnel div:last-child{border-bottom:1px solid var(--line)}.funnel dd{font-size:21px}.candidate,.panel,.empty{padding:20px 17px}.candidate h3{font-size:21px}.candidate-heading{align-items:flex-start;gap:10px}.tag{font-size:10px;padding:3px 6px}.identity{gap:10px}.rank{font-size:23px}.metrics{grid-template-columns:repeat(3,1fr);gap:12px 10px}.metrics dd{font-size:15px}.conditions,.review-grid,.method-grid{grid-template-columns:1fr}.review-grid{gap:0}.section-heading{display:block}.source-table th{width:95px;padding-left:0}}
@media print{body{background:white}main{max-width:none;padding:0}nav,.file-links{display:none}.candidate,.panel{box-shadow:none;break-inside:avoid}.panel{border:0}a{color:inherit}details:not([open])>summary{display:block}}
.known-issues{background:#fff6e8;border-left:3px solid #be8d3a;padding:10px 14px;margin:12px 0;font-size:12px;border-radius:0 5px 5px 0}.known-issues strong{font-size:12px;color:#715220}.known-issues ul{margin:4px 0 0;padding-left:18px}.loss-fact{font-size:13px;font-weight:600;color:#a13f31;margin:10px 0}.loss-value{color:#a13f31}
.recommendation-validation{margin-top:24px}.validation-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}.validation-heading h3{margin:0}.validation-admission{font-size:12px;padding:4px 8px;border-radius:5px;overflow-wrap:anywhere}.validation-strategy{border-top:1px solid var(--line);margin-top:22px;padding-top:22px}.validation-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin:18px 0}.validation-metrics dt{font-size:12px;color:var(--muted)}.validation-metrics dd{font-size:21px;font-weight:600;margin:5px 0;overflow-wrap:anywhere}.validation-metrics small{font-size:11px}.validation-counts{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:10px;background:#f4f6f9;padding:12px;border-radius:8px}.validation-counts dt{font-size:11px;color:var(--muted)}.validation-counts dd{font-size:17px;margin:3px 0 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}.validation-note{font-size:13px;color:#465c73}.validation-strategy ul{font-size:12px}.validation-strategy .loss-fact{font-size:14px}
@media(max-width:900px){.validation-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.validation-counts{grid-template-columns:repeat(4,minmax(0,1fr))}}@media(max-width:640px){.validation-heading{flex-direction:column}.validation-counts{grid-template-columns:repeat(2,minmax(0,1fr))}.validation-metrics dd{font-size:19px}}
"""


def render_report(report: dict) -> str:
    """Render the supplied report without mutating evidence or re-ranking rows."""
    stats = _dict(report.get("stats"))
    status = _dict(report.get("run_status"))
    strategy = _dict(report.get("strategy"))
    candidates = [row for row in _items(report.get("candidates")) if isinstance(row, dict)]
    complete = status.get("data_status") == "complete"
    universe = _first(stats, "universe", "universe_count")
    checked = _first(stats, "history_complete", "formula_evaluated_count", "evaluated", "history_checked")
    matched = _first(stats, "matched", "matched_count", "raw_matches", "confirmed_matches")
    incomplete = _first(stats, "incomplete", "history_incomplete_count", "unassessed")
    values = [("日线沪深范围", universe), ("历史核验完成", checked), ("符合模板", matched),
              ("本次展示", len(candidates)), ("历史不足 / 断档", incomplete)]
    funnel = '<dl class="funnel">' + ''.join('<div><dt>' + label + '</dt><dd>' + _count(value) + '</dd></div>' for label, value in values) + '</dl>'
    mode = {"closed_daily": "日线收盘确认", "post_close": "日线收盘确认", "live": "盘中预备观察", "intraday": "盘中预备观察"}.get(report.get("analysis_mode"), "分析模式未提供")
    status_title = "本次数据扫描完成" if complete else "本次数据覆盖尚未完成"
    if not complete and status.get('execution_status') == 'completed' and candidates:
        status_title = '筛选完成 · 部分股票未纳入计算'
    status_message = _first(status, "message", "summary", default="具体扫描范围见下方计数。")
    status_html = '<section class="status' + ('' if complete else ' partial') + '"><strong>' + status_title + '</strong><p>' + _e(status_message) + '</p>'
    if not complete:
        status_html += '<p>名单仅覆盖已核验范围；未评估股票不记为规则失败，不能据此认定全市场没有机会。</p>'
    status_html += '</section>'
    selection = _dict(report.get("selection"))
    requested = _first(selection, "requested_count", "top", "top_n", "display_limit", default=stats.get("requested_top_n", 5))
    header = ('<div class="section-heading" id="candidates"><h2>本次趋势候选 <span class="muted">' + str(len(candidates))
              + ' 只</span></h2><p>请求展示 ' + _e(requested) + ' 只 · 按记录顺序展示</p></div>'
              '<p class="meta">持有安排：' + _e(_holding(strategy)) + '</p>')
    if candidates:
        cards = ''.join(_candidate(row, index) for index, row in enumerate(candidates, 1))
    else:
        body = ('已完成核验的范围内，本次没有可展示的模板候选。规则条件与未完成范围见计数和随附数据。'
                if complete else '当前证据不足以生成可展示名单。补齐历史与规则核验后才能判断尚未覆盖的股票。')
        cards = '<section class="empty"><h3>本次展示 0 只</h3><p>' + body + '</p><p class="muted">不以未通过或未核验的股票补足展示名额。</p></section>'
    coverage = '历史核验完成数与符合模板数为不同阶段；缺失数据不按不符合条件处理。'
    excluded = _first(stats, "excluded", "excluded_count")
    if excluded is not None:
        coverage += ' 范围或资格排除 ' + _count(excluded) + ' 只。'
    if stats.get('live_catalog_not_in_daily'):
        coverage += ' 当前报价目录另有 ' + _count(stats['live_catalog_not_in_daily']) + ' 只未在本次日线范围内，未参与排名。'
    if _number(stats.get("boundary_tie_extra_count")):
        coverage += ' 边界完全并列额外保留 ' + _count(stats["boundary_tie_extra_count"]) + ' 只，展示数量因此可能超过请求名额。'
    title = '沪深 A 股趋势选股 · ' + _text(report.get("data_session"), "行情日期未提供")
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="color-scheme" content="light"><title>' + _e(title) + '</title><style>' + _STYLE + '</style></head><body><main>'
            '<div class="topline"><span>TAIL SCREENER · ' + _e(report.get("version")) + '</span><nav aria-label="报告导航">'
            '<a href="#candidates">候选名单</a><a href="#method">策略说明</a><a href="#evidence">数据证据</a></nav></div>'
            '<header class="hero"><div class="eyebrow">沪深 A 股 / ' + mode + '</div><h1>从趋势出发，逐项核查</h1>'
            '<p class="lead">' + _e(strategy.get("name") or "Minervini 趋势模板") + ' · 展示满足记录规则的股票及其筛选依据。</p>'
            '<div class="dates"><div><span class="muted">行情交易日</span><strong>' + _e(report.get("data_session"))
            + '</strong></div><div><span class="muted">请求分析日期</span><strong>' + _e(report.get("requested_session"))
            + '</strong></div><div><span class="muted">报告生成时间（含时区）</span><strong>' + _e(report.get("generated_at"))
            + '</strong></div></div></header>' + status_html + funnel + '<p class="meta">' + coverage + '</p>'
            + _accuracy_evidence(report) + _recommendation_validation(report) + header + cards
            + _method(report) + _evidence(report) + '<footer>TAIL SCREENER · 每项结论对应输入证据 · 数据快照见随附文件</footer>'
            '</main></body></html>')
