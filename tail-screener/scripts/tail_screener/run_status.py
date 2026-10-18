"""Describe acquisition failures separately from an empty strategy result."""
from datetime import time

from .engine import fraction, stamp


def describe_run(snapshot, config, now, conditional_count, replay=False, evaluated=None, watch_count=None):
    context = snapshot.get('context', {})
    calendar = context.get('calendar', {})
    research_mode = snapshot.get('analysis_mode') == 'post_close'
    if watch_count is None:
        watch_count = sum(bool(item.get('watch_eligible')) for item in evaluated or [])
    in_window = time.fromisoformat(config['start_time']) <= now.time().replace(tzinfo=None) <= time.fromisoformat(config['last_entry_time'])
    timing = ('replay' if replay else 'post_close' if research_mode else 'calendar_unknown' if not calendar.get('verified')
              else 'non_trading_day' if not calendar.get('is_session')
              else 'entry_window' if in_window else 'outside_window')
    reasons = []
    stats = snapshot.get('stats', {})
    count = stats.get('universe', 0)
    collection_errors = snapshot.get('errors', {})
    history_failures = len(collection_errors.get('history') or {})
    deep_failures = len(collection_errors.get('deep') or {})
    history_success = stats.get('history_checked', 0)
    catalog_complete = snapshot.get('universe_metadata', {}).get('catalog_complete')
    market_incomplete = catalog_complete is False
    if not count:
        reasons.append('全市场有效个股报价为0，筛选未完成')
    for key, threshold, label in [
        ('universe_coverage', config['min_universe_coverage'], '股票目录覆盖不足'),
        ('valid_quote_coverage', config['min_universe_coverage'], '有效个股报价覆盖不足'),
        ('industry_coverage', config['min_industry_coverage'], '行业映射覆盖不足'),
    ]:
        value = fraction(context.get(key))
        if value is None or value < threshold:
            reasons.append(label)
            if key in ('universe_coverage', 'valid_quote_coverage'):
                market_incomplete = True
    if not calendar.get('verified'):
        reasons.append('交易日历无法核验：' + str(calendar.get('error', calendar.get('error_code', '原因未知'))))
    indices = context.get('indices', [])
    if {x.get('code') for x in indices if isinstance(x, dict)} != {'000001.SH', '399001.SZ'}:
        reasons.append('上证与深证指数数据不完整')
    incomplete = 0
    if research_mode:
        research = context.get('research', {})
        if not research.get('verified'):
            reasons.extend(research.get('gaps') or ['最新完整交易日及收盘报价覆盖未核验'])
        data_session = research.get('data_session')
        for index in indices:
            at = stamp(index.get('quote_at'))
            if at is None or at.date().isoformat() != data_session or at.time().replace(tzinfo=None) < time(15) or at > now:
                reasons.append('指数收盘数据与观察交易日不一致')
                break
        incomplete = sum(item.get('research_status') == 'incomplete' for item in evaluated or [])
        if incomplete:
            reasons.append(f'{incomplete}/{len(evaluated)}只深入核查标的存在收盘数据缺项或来源冲突，未通过核验')
    # Stale closing quotes are expected during an after-hours interface check.
    # They are still blocked by the engine and never become intraday evidence.
    if timing == 'entry_window':
        value = fraction(context.get('dated_quote_coverage'))
        if value is None or value < config['min_universe_coverage']:
            reasons.append('盘中新鲜且带日期的报价覆盖不足')
        for key in ('snapshot_fetched_at', 'market_quote_at'):
            at = stamp(context.get(key))
            if at is None or not 0 <= (now-at).total_seconds() <= config['snapshot_max_age_seconds']:
                reasons.append('全市场横截面时间缺失或过期')
                break
        if any(not stamp(x.get('quote_at')) or not 0 <= (now-stamp(x['quote_at'])).total_seconds() <= 120 for x in indices):
            reasons.append('指数报价缺少完整日期或已经过期')
    for stage, errors in collection_errors.items():
        if errors:
            if stage == 'history':
                reasons.append(f'历史量价核查未完成（history）：成功 {history_success} 只，失败 {history_failures} 只；失败标的未进入后续深入筛选')
            else:
                reasons.append(f'{stage} 阶段存在 {len(errors)} 项采集失败')
    if catalog_complete is False:
        reasons.append('股票目录未通过完整性核验')
    reasons = list(dict.fromkeys(reasons))
    data_status = 'failed' if not count else 'incomplete' if reasons else 'complete'
    if data_status == 'failed':
        code, label = 'scan_failed', '扫描失败'
        summary = '全市场有效报价为0，本次筛选未完成；不能据此认定市场没有合格股票。'
    elif data_status == 'incomplete':
        code, label = 'data_incomplete', '筛选未完成，数据核验不完整'
        summary = '采集或核验尚未完成。已取得的结果仅覆盖本次成功采集的范围，不能据此认定全市场没有合格股票。'
        if market_incomplete:
            label = '全市场扫描未完成，无法形成可信的筛选名单'
            summary = (f'仅取得 {count} 只有效报价，股票目录或报价覆盖未达要求。'
                       '行业与涨跌统计仅代表已采集样本；核查记录仅供定位缺项，不是推荐名单。'
                       '不能据此认定全市场没有合格股票。')
        elif history_failures or deep_failures:
            stages = []
            if history_failures:
                stages.append(f'历史量价核查未完成：成功 {history_success} 只，失败 {history_failures} 只；失败标的未进入后续深入筛选')
            if deep_failures:
                stages.append(f'深入采集仍有 {deep_failures} 只失败')
            label = ('分层筛选未完成，历史量价和深入采集存在失败' if history_failures and deep_failures else
                     '分层筛选未完成，历史量价核查存在失败' if history_failures else
                     '分层筛选未完成，深入采集存在失败')
            summary = f'已取得 {count} 只有效报价。' + '。'.join(stages) + '。'
            if evaluated is not None:
                summary += f'本次仅形成 {len(evaluated)} 只深入核查记录'
                summary += (f'，其中 {incomplete} 只还存在数据缺项或来源冲突。' if incomplete
                            else '；这些记录不能覆盖前序采集失败的标的。')
            summary += '采集范围不完整，无法形成可信的筛选名单，也不能据此认定全市场没有合格股票。'
        elif research_mode and evaluated and incomplete:
            all_incomplete = incomplete == len(evaluated)
            label = ('盘后核查未完成，全部深入核查标的缺少有效证据' if all_incomplete
                     else '盘后筛选未完成，部分深入核查数据待补齐')
            summary = (f'已取得 {count} 只有效报价并深入核查 {len(evaluated)} 只，'
                       f'其中 {incomplete} 只存在数据缺项或来源冲突。缺项记录不具备研究规则通过资格，'
                       '不能据此认定全市场没有合格股票。')
    elif timing == 'replay':
        code, label = 'replay', '离线研究回放'
        summary = '使用冻结快照和记录参数回放，未重新采集市场数据，不产生当前入场资格。'
        if watch_count:
            summary += f' 按当前版本规则处理原快照，展示 {watch_count} 只个股条件满足、待市场或行业环境确认的观察标的。'
    elif timing == 'post_close':
        code, label = 'post_close_complete', '盘后观察筛选完成'
        summary = f"已按 {context['research'].get('data_session')} 的完整收盘数据完成分层观察筛选；财报与消息截至实际生成时间，仅供下一交易日复核，不是历史14:30还原。"
        ready_count = sum(item.get('research_status') == 'ready' for item in evaluated or [])
        if watch_count:
            label = '盘后筛选完成，已区分规则通过与待确认观察'
            summary += (f' 全部研究规则通过 {ready_count} 只；展示待环境确认观察 {watch_count} 只。'
                        '观察标的的个股条件已满足，市场或行业条件仍未通过；需按所列条件重新扫描确认。')
        elif evaluated is not None and not ready_count:
            label = '盘后筛选完成，零研究候选'
            summary += ' 本次分层筛选范围内没有满足全部研究规则的候选；不代表逐只深查了全市场。'
    elif timing == 'outside_window':
        code, label = 'outside_window', '非尾盘窗口观察'
        summary = '本次仅检查接口和观察当前数据；没有当时冻结快照，不能还原14:30的筛选结果。'
    elif timing == 'non_trading_day':
        code, label = 'non_trading_day', '非交易日观察'
        summary = '已核验当前不是交易日，结果仅供观察。'
    elif conditional_count:
        code, label = 'completed', '筛选完成'
        summary = f'本次分层筛选得到 {conditional_count} 只条件候选，仍需公告正文及人工成交复核。'
        if watch_count:
            summary += f' 另展示 {watch_count} 只待环境确认观察标的，尚不具备入场条件。'
    elif watch_count:
        code, label = 'completed_no_candidates', '筛选完成，个股条件满足但环境待确认'
        summary = (f'全部入场规则通过 0 只；展示待环境确认观察 {watch_count} 只。'
                   '观察标的的个股条件已满足，市场或行业条件仍未通过；需按所列条件重新扫描确认，暂不生成买卖计划。')
    else:
        code, label = 'completed_no_candidates', '筛选完成，零条件候选'
        summary = '本次分层筛选范围内没有满足全部入场规则的候选；不代表逐只深查了全市场。'
    return {'code': code, 'label': label, 'summary': summary, 'data_status': data_status,
            'timing_status': timing, 'reasons': reasons, 'market_scope_complete': not market_incomplete and bool(count),
            'scope': '全市场横截面初筛；历史量价和深入核查按配置截取，未逐只深查全市场'}
