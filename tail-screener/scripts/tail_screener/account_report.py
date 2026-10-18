"""Human-first presentation of recorded results; never selects or updates accounts."""
from datetime import datetime
from pathlib import Path
import re
from .trend_report import _e, _fmt, _financials, _notices, _link, _number
from .decision_review import decision_summary, research_preselection
from .kline_report import render as render_kline

_STATES={'complete':'完整','incomplete':'不完整','retrieved':'已取得','unresolved':'待核验',
         'distribution_model_supported':'模型支持','not_performed':'尚未复核',
         'mirror_bodies_collected':'已取得镜像正文','not_applicable':'本轮不适用',
         'unverified':'尚未核验','conditional_inputs_complete':'条件化输入完整'}
_OBS={'watching':'继续观察','unknown':'数据未知','invalid':'条件失效','expired':'观察到期'}
_CHANGES={'first_seen':'首次入选','entered_display':'进入展示榜','left_display':'移出展示榜，继续跟踪',
          'state_changed':'观察状态变化','same_session_revision':'同日输入修订，保留首次完整观察'}
_PHASES={'pre_open':'开盘前','continuous_trading':'连续交易中','lunch_break':'午间休市',
         'post_close':'收盘后','non_trading_day':'非交易日'}
_TOPICS={'delisting':'退市 / 风险警示','investigation':'调查 / 处罚','debt':'债务事项','pledge':'质押 / 冻结',
         'sale':'减持','earnings':'业绩事项','contract':'合同 / 订单','distribution':'分红送转','correction':'更正 / 终止'}


def _date(value, clock=False):
    if not value:return '未记录'
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).strftime('%Y-%m-%d %H:%M' if clock else '%Y-%m-%d')
    except ValueError:return str(value)


def _money(value):return '未核验' if _number(value) is None else _fmt(float(value)/100,suffix=' 元')


def _fold(title, content, identifier='', css='evidence'):
    return ('<details class="'+css+'"'+(' id="'+_e(identifier)+'"' if identifier else '')+'><summary>'+_e(title)
            +'</summary><div class="details-body">'+content+'</div></details>')


def _metric(label, value, note=''):
    return '<div><dt>'+_e(label)+'</dt><dd>'+_e(value)+'</dd>'+('<small>'+_e(note)+'</small>' if note else '')+'</div>'


def _anchor(code):return 'stock-'+re.sub(r'[^a-zA-Z0-9_-]','-',str(code))


def _body_review(value):
    if not value:return '<p>公告正文尚未采集。</p>'
    result='<p>近14日正文 '+_e(value.get('retrieved_count'))+' / '+_e(value.get('required_count'))+' 篇；采集状态：'+_e(_STATES.get(value.get('status'),'未核验'))+'。正文来自镜像，内容仍待阅读核对。</p>'
    for error in value.get('errors',[]):
        result+='<p class="attention">未完成：'+_e(error.get('article_id','公告目录'))+'；'+_e(error.get('reason','正文未取得'))+'。'+_e(error.get('category',''))+'</p>'
    for document in value.get('documents',[]):
        body='<p class="meta">披露于 '+_e(_date(document.get('published_at'),True))+' · 文档日期 '+_e(_date(document.get('document_date')))+'</p><p>'+_link(document.get('source_url'),'查看来源公告')+'</p>'
        for finding in document.get('findings',[]):
            for hit in finding.get('hits',[]):
                body+='<p><strong>待核对词项：'+_e(hit['term'])+'</strong></p><blockquote>'+_e(hit['excerpt'])+'</blockquote>'
        # HTML uses normalized line endings; the original bytes remain in JSON.
        content=_e(document.get('body')).replace('\r\n','\n').replace('\r','\n')
        body+=_fold('阅读全文','<pre>'+content+'</pre>')
        origin='<p>正文 SHA256：<code>'+_e(document.get('body_sha256'))+'</code></p>'
        for route in document.get('body_routes',[]):
            origin+='<p>接口：'+_e(route.get('request_url'))+'<br>采集时间：'+_e(_date(route.get('fetched_at'),True))+'。备用接口仍属同一数据商。</p>'
        for entry in document.get('evidence',[]):
            origin+='<p class="meta">证据文件：'+_e(entry.get('path'))+'<br>SHA256：'+_e(entry.get('sha256'))+'</p>'
        body+=_fold('来源与校验信息',origin)
        result+=_fold(document['title'],body,css='document')
    return result


def _distributions(value):
    if not value:return '<p>分红送转目录尚未取得。</p>'
    result='<p>所查分红送转目录：'+_e(_STATES.get(value.get('status'),'未核验'))+'；窗口 '+_e(value.get('start'))+' 至 '+_e(value.get('end'))+'。</p>'
    if not value.get('events'):result+='<p>所查窗口没有可展示的已实施分红送转记录；不等于不存在其他公司行动。</p>'
    for event in value.get('events',[]):
        result+='<p>登记日 '+_e(event.get('record_session'))+'；除权息日 '+_e(event.get('ex_session'))+'；每10股税前现金 '+_e(event.get('gross_cash_per_10'))+' 元；'+_e(_STATES.get(event.get('status'),'待核验'))+'。</p>'
        if event.get('action'):result+='<p>发放日 '+_e(event['action'].get('pay_session'))+'；按已支持税务模型计算，实际到账与扣税未核验。</p>'
    for error in value.get('errors',[]):result+='<p class="attention">待核验：'+_e(error.get('reason',error.get('category','事件证据缺项')))+'</p>'
    return result


def _configuration(report):
    names={'capital':'初始模拟资金（元）','max_positions':'最多持仓数','max_holding_sessions':'持有交易日上限',
           'max_entry_gap_pct':'相对信号收盘涨幅上限（%）','max_ma20_premium_pct':'MA20溢价上限（%）',
           'top':'严格候选展示名额','research_size':'研究跟踪名额','mode':'运行模式','data_source':'行情模式','daily_update_budget':'日线采集预算（秒）',
           'run_budget':'整次调用预算（秒）','daily_workers':'日线并发数'}
    sources={'default':'默认值','cli':'命令行','command_line':'命令行','config_file':'配置文件','config':'配置文件',
             'programmatic_or_default':'调用参数或默认值'}
    rows=''.join('<tr><td>'+_e(names.get(key,key))+'</td><td>'+_e(item.get('value'))+'</td><td>'+_e(sources.get(item.get('source'),item.get('source')))+'</td></tr>' for key,item in report.get('effective_configuration',{}).get('parameters',{}).items())
    return '<div class="table-scroll"><table><thead><tr><th>参数</th><th>实际值</th><th>来源</th></tr></thead><tbody>'+rows+'</tbody></table></div><p class="meta">命令行覆盖指定配置文件，最后采用默认值；未指定配置时不读取旧模式根配置。</p>'


def _review_brief(row):
    review=row.get('body_review') or {}
    if review.get('status')!='complete':return '公告未取全，需补核'
    topics=list(dict.fromkeys(_TOPICS.get(f.get('topic'),f.get('topic','其他事项')) for d in review.get('documents',[]) for f in d.get('findings',[])))
    if topics:return '待读：'+'、'.join(topics[:2])+('等' if len(topics)>2 else '')
    return '正文已取，待人工阅读' if review.get('required_count') else '所查窗口内0篇新公告'


def _premium(value):
    number=_number(value)
    return '未提供' if number is None else ('低于 ' if number<0 else '高于 ')+_fmt(abs(number),suffix='%')


def _trend_brief(row):
    evidence=row.get('price_diagnostics') or {}
    if evidence.get('status')!='computed':return '近期走势未核验'
    flags={'negative_5_session_return':'近5日下跌','close_below_ma5':'收盘低于MA5',
           'close_below_ma20':'收盘低于MA20','ma5_falling':'MA5下行'}
    labels=[flags[x] for x in evidence.get('risk_flags',[]) if x in flags]
    candle=row.get('kline_review') or {}
    if candle.get('status')=='computed':
        labels.append(candle['position'])
        labels.extend(candle.get('patterns',[])[:2])
    return '；'.join(labels) if labels else '近期走势未触发所定义的走弱条件'


def _decision_section(report):
    decision=decision_summary(report)
    reasons={'required_data_unverified':'本次必需数据尚未完整核验',
             'strategy_profitability_unvalidated':'默认均线与流动性规则尚未完成盈利有效性验证',
             'holding_horizon_unvalidated':'没有证据证明规则适合本次持有期限',
             'candidate_trade_theses_unvalidated':'逐股入场依据、失效条件与损失范围尚未验证',
             'disclosure_semantics_unverified':'公告内容尚未完成语义复核'}
    body='<section id="decision" class="panel warn"><h2>'+_e(decision['conclusion'])+'</h2>'
    body+='<p><strong>已验证买入建议：0 只。</strong>研究预选照常保留，用于比较机会和反对证据；模拟计划用于检验规则。尚未验证盈利不等于没有候选，但候选不能直接当作下单建议。</p>'
    body+=_fold('验证状态明细','<ul>'+''.join('<li>'+_e(reasons[x])+'</li>' for x in decision['reason_codes'])+'</ul>')
    body+='<p>展示 '+str(decision['display_count'])+' 只候选；同时持仓上限 '+_e(decision['max_positions'])+' 仓。展示数量不要求买满，也不代表分成相同数量的仓位。</p>'
    body+='<p>名义每仓可买性预算 '+_money(decision['nominal_slot_budget_cents'])+'，由初始资金预留5%后除以持仓上限计算；这是模拟模型的等额预算假设，不是用户指定的配仓比例。</p>'
    holding_sessions=decision.get('holding_sessions')
    timing=('按本次配置，第1个交易日买入，持有满'+str(holding_sessions)+'个交易日后，安排第'+str(holding_sessions+1)+'个交易日开盘尝试退出；均线退出可能更早，受阻则延后。'
            if type(holding_sessions) is int and holding_sessions>0 else '本次持有交易日上限未记录，不能推定固定退出日。')
    body+='<p><strong>实际退出时点：</strong>'+_e(timing)+'持有上限与买入价格上限均不限制最大亏损。</p>'
    shortlist=research_preselection(report)
    if shortlist['candidates']:
        body+='<h3>'+('逐股研究重点' if shortlist['status']=='complete' else '部分观察的待核查事项')+'</h3><ul>'
        for row in shortlist['candidates']:
            concerns='；'.join(row['observed_concerns']) or '上述走弱标记未触发，仍需验证具体机会'
            body+='<li><strong>'+_e(row['name'])+'</strong>：'+_e(row['research_focus'])+'。'+_e(concerns)+'。</li>'
        body+='</ul><p>研究重点按已发生的走势描述，不新增收益评分，也不改变原流动性顺序。</p>'
    return body+'</section>'


def _candidate_table(rows):
    body=''
    for row in rows:
        body+=('<tr><td class="rank">'+_e(row.get('rank'))+'</td><td><a class="stock-link" href="#'+_anchor(row['code'])+'">'+_e(row.get('name',row['code']))+'</a><small>'+_e(row['code'])+'</small></td>'
               '<td class="numeric">'+_fmt(row.get('price'))+'</td><td class="numeric">'+_fmt((row.get('price_diagnostics') or {}).get('return_5_sessions_pct'),suffix='%')+'</td><td>'+_e(_trend_brief(row))+'</td><td class="numeric ceiling">'+_fmt(row.get('entry_price_ceiling'),3)+'</td><td>'+_e(_premium(row.get('ma20_premium_pct')))+'</td><td class="review-note">'+_e(_review_brief(row))+'</td></tr>')
    return '<p class="mobile-hint">左右滑动表格，查看近期走势、价格上限与公告事项。</p><div class="table-scroll"><table class="candidate-table"><thead><tr><th>流动性顺序<br>非买入排名</th><th>股票</th><th>信号收盘 / 元</th><th>近5交易日涨跌</th><th>近期走势证据</th><th>模型价格上限 / 元</th><th>收盘相对MA20</th><th>公告待核对事项</th></tr></thead><tbody>'+body+'</tbody></table></div>'


def _kline_catalog_section(report):
    catalog=report.get('kline_catalog') or {}
    if not catalog:return ''
    run=report.get('run_status') or {}
    complete=(bool(report.get('data_session')) and run.get('execution_status')=='completed' and run.get('data_status')=='complete'
              and report.get('data_session')==report.get('requested_session')==catalog.get('session')
              and not report.get('data_issues'))
    out='<section class="panel" id="kline-catalog"><h2>'+('全候选K线检索' if complete else '部分候选K线检索')+'</h2><p>本次已计算 '+str(catalog.get('count',0))+' 只严格合格候选，不限于上方展示名单。各组按流动性排列，每组显示至多20只，可继续按形态或代码查看。目录尚未逐股完成公告复核，不增加模拟持仓。</p>'
    for group,label in catalog.get('groups',{}).items():
        records=[r for r in catalog.get('rows',[]) if group in r.get('groups',[])]
        if not records:continue
        body=''
        for row in records[:20]:
            k=row.get('kline_review') or {}
            body+='<tr><td>'+_e(row.get('name',row['code']))+'<small>'+_e(row['code'])+'</small></td><td>'+_fmt(row.get('price'))+'</td><td>'+_fmt(k.get('amount_vs_previous5'),suffix='倍')+'</td><td>'+_fmt(k.get('prior5_low'))+'—'+_fmt(k.get('prior5_high'))+'</td></tr>'
        out+=_fold(label+' · '+str(len(records))+' 只','<div class="table-scroll"><table><thead><tr><th>股票</th><th>信号收盘 / 元</th><th>成交额 / 前5日均额</th><th>此前5日价格区间 / 元</th></tr></thead><tbody>'+body+'</tbody></table></div>')
    return out+'</section>'


def _candidate_detail(row, report):
    observation=report.get('observations',{}).get('cycles',{}).get(row['code'],{})
    quote=row.get('quote') or {};check=row.get('short_term_check') or {}
    check_text={'passed':'对应报价约束通过，仍不代表可成交','quote_checks_passed_research_only':'对应报价约束通过，仍不代表可成交',
                'blocked':'当前不满足报价或时点约束','unverified':'报价或时点尚未核验'}.get(check.get('status'),'报价或时点尚未核验')
    reasons={'not_continuous_trading':'运行时不在连续交易时段','data_incomplete':'本次所需数据不完整',
             'quote_stale':'报价过期','entry_price_ceiling_exceeded':'超过模型价格上限',
             'future_quote':'报价时间在核验时点之后','quote_not_current':'报价日期或时效不合格',
             'quote_unverified':'报价尚未核验','above_frozen_price_ceiling':'超过冻结的模型价格上限',
             'universe_or_account_incomplete':'筛选范围或账户核验不完整'}
    body='<p class="meta">'+_e(_OBS.get(observation.get('state'),'观察状态未核验'))+' · 首次观察 '+_e(_date(observation.get('first_signal_session')))+' · 已确认 '+_e(observation.get('confirmed_sessions','未记录'))+' 日 · 连续确认 '+_e(observation.get('consecutive_confirmed_sessions','未记录'))+' 日</p>'
    amount=_number(row.get('mean_amount20'))
    body+='<dl class="metrics three">'+_metric('MA5 / MA20',_fmt(row.get('ma5'),4)+' / '+_fmt(row.get('ma20'),4),'入选均线条件为 MA5 > MA20')+_metric('20日日均成交额',_fmt(amount/1e8,suffix=' 亿元') if amount is not None else '未提供','用于成交容量排序')+_metric('模型价格上限',_fmt(row.get('entry_price_ceiling'),3,suffix=' 元'),'超过上限不追价、不自动换股')+'</dl>'
    diagnostics=row.get('price_diagnostics') or {}
    body+='<p><strong>近期走势：</strong>'+_e(_trend_brief(row))+'。近5交易日涨跌 '+_fmt(diagnostics.get('return_5_sessions_pct'),suffix='%')+'；MA5较上一交易日变化 '+_fmt(diagnostics.get('ma5_change_pct'),suffix='%')+'；相对近20日最高收盘回撤 '+_fmt(diagnostics.get('drawdown_from_20_session_close_high_pct'),suffix='%')+'。</p>'
    body+='<p>走势指标使用同一复权收盘口径，只描述已发生的变化。MA5大于MA20并不等于新发生金叉，也不证明当前止跌。上述回撤不是未来损失上限。</p><p><strong>买入论证：</strong>尚未验证入场优势、触发条件、判断失效条件及损失范围，维持观察；模型上限不是目标价或止损价。</p>'
    body+='<p>'+_e(check_text)+'；报价时间 '+_e(_date(quote.get('quote_at'),True))+'。</p>'
    if check.get('reasons'):body+='<p class="meta">'+_e('；'.join(reasons.get(k,k) for k in check['reasons']))+'</p>'
    body+='<p><strong>退出规则：</strong>'+_e(row.get('holding_rule','未记录'))+'。</p>'
    body+=render_kline(row.get('kline_review',{}))
    body+=_fold('公告正文与待核对段落',_body_review(row.get('body_review')))
    body+=_fold('财务字段与公告目录','<h4>财务披露</h4>'+_financials(row.get('financials'))+'<h4>公告目录</h4>'+_notices(row.get('notices')))
    body+=_fold('分红送转记录',_distributions(row.get('distributions')))
    if row.get('review_gaps'):body+=_fold('其他待核验项','<ul>'+''.join('<li>'+_e(item)+'</li>' for item in row['review_gaps'])+'</ul>')
    return _fold(str(row.get('rank',''))+'. '+str(row.get('name',row['code']))+' · '+str(row['code']),body,_anchor(row['code']),'stock-detail')


def _plan_text(report):
    timing=report.get('forward_account',{}).get('record',{}).get('payload',{}).get('plan',{}).get('timing',{})
    if timing.get('prospective'):return '已保存 '+_date(timing.get('entry_session'))+' 的开盘前模拟计划；尚未发生真实成交。'
    return {'entry_window_missed_no_backfill':'已错过该信号对应的开盘时点。本次只保留观察结果，不补造买入。',
            'entry_day_already_frozen_first_plan_retained':'该目标交易日已有冻结计划，沿用首次记录。',
            'data_incomplete_no_entry_plan':'数据或账户估值不完整，本次没有冻结新的模拟入场计划。',
            'next_session_unverified':'下一交易日尚未核验，本次没有冻结新的模拟入场计划。'}.get(timing.get('reason'),'本次未确认新的模拟入场计划，详见账户记录。')


def _account_section(report):
    forward=report.get('forward_account',{});account=forward.get('account',{});config=report.get('account_config',{});holdings=account.get('positions',{})
    result='<section id="account" class="panel"><div class="section-heading"><h2>严格策略模拟账户与下一步</h2><span>不执行实盘交易</span></div>'
    result+='<dl class="metrics three">'+_metric('模拟现金余额',_money(account.get('cash_cents')),'含预留税款，不等于全部可用于新买入')+_metric('条件化模拟净值',_money(account.get('nav_cents')),'输入可计算，不等于真实账户已审计')+_metric('当前模拟持仓',str(len(holdings))+' / '+str(config.get('max_positions','未记录')),'最多同时持仓数量')+'</dl>'
    result+='<p class="plan">'+_e(_plan_text(report))+'</p>'
    if forward.get('error'):result+='<p class="attention">账户更新未完成：'+_e(forward['error'])+'</p>'
    if holdings:
        body=''.join('<tr><td>'+_e(code)+'</td><td>'+_e(p.get('quantity'))+'</td><td>'+_e(p.get('entry_session'))+'</td><td>'+_e(p.get('holding_sessions'))+'</td><td>'+_e(p.get('quarantine') or ('等待退出' if p.get('pending_exit') else '持有'))+'</td></tr>' for code,p in holdings.items())
        result+='<div class="table-scroll"><table><thead><tr><th>股票</th><th>股数</th><th>入场日</th><th>持有交易日</th><th>状态</th></tr></thead><tbody>'+body+'</tbody></table></div>'
    else:result+='<p class="muted">暂无模拟持仓。观察名单与已持仓股票分别记录。</p>'
    limit=config.get('max_holding_sessions')
    result+='<div class="rule-line"><p><strong>入场：</strong>仅对开盘前实际冻结的名单，下一开盘作条件化模拟；已有持仓不加仓，无法成交不补位。</p><p><strong>退出：</strong>收盘MA5低于MA20，或达到持有上限'+('（含买入日 '+str(limit)+' 个交易日）' if limit else '（未记录）')+'后，下一开盘尝试退出；停牌等受阻情况会延后。</p></div>'
    explanation='<p>初始模拟资金 '+_e(_money(config.get('initial_capital_cents')))+'。共享现金、不借钱、不加仓、不每日重平衡；保留5%预算余量。卖出资金可用于同日新买入，股票执行T+1。</p>'
    explanation+='<p>预留模拟股息税 '+_e(_money(account.get('dividend_tax_reserve_cents')))+'，不用于新买入。自然人现金分红按持有期模型计算；实际到账与扣税尚未核验。</p><p>复权兼容不能证明没有公司行动。公司行动、税务或可售日期缺证时，保留缺项。日线、跳空和成交量只能支持条件化模拟，不能证明实际开盘成交或排队结果。</p>'
    explanation+='<p>本系列已保存 '+_e(forward.get('records','未记录'))+' 份账户记录，事前计划 '+_e(forward.get('prospective_entry_plans','未记录'))+' 份；本地哈希链不是外部时间公证。</p>'
    result+=_fold('账户计算口径与税费说明',explanation)
    changes=report.get('observations',{}).get('changes',[])
    if changes:
        content='<ul>'+''.join('<li>'+_e(str(c.get('code',''))+'：'+_CHANGES.get(c.get('type'),'观察变化'))+'</li>' for c in changes)+'</ul><p class="meta">跌出展示榜不等于失效。漏跑或数据未知不计连续确认；同日修订单独保留，不改写首次冻结计划。</p>'
        result+=_fold('本次观察记录变化（'+str(len(changes))+'项）',content)
    return result+'</section>'


def _research_section(report):
    research=report.get('research_simulation')
    if not research:return ''
    rows=research.get('candidates',[]);forward=research.get('forward_account',{});account=forward.get('account',{})
    metrics=forward.get('metrics',{});target=research.get('target_size',6)
    text='<section id="research" class="panel"><div class="section-heading"><h2>'+_e(str(target))+'股研究模拟</h2><span>独立对照账户 · 不计入严格策略成绩</span></div>'
    text+='<p>优先纳入通过规则的股票，不足时补入行情、资格、流动性和名义可买性合格的对照标的。未通过的均线或MA20溢价条件逐只标注；研究入场只检查冻结的收盘涨幅上限，退出及资金约束仍执行。</p>'
    text+='<dl class="metrics three">'+_metric('跟踪名单 / 目标',str(len(rows))+' / '+str(target),'空位 '+str(research.get('vacancies',target)))+_metric('研究账户净值',_money(account.get('nav_cents')),'缺失必要输入时保持未核验')+_metric('研究账户持仓',str(len(account.get('positions',{})))+' / '+str(research.get('account_config',report.get('account_config',{})).get('max_positions',6)),'跟踪名单不等于已经买入')+'</dl>'
    reasons={'ma_condition':'均线条件未通过','ma20_premium':'MA20溢价门槛未通过'}
    if rows:
        body=''
        for row in rows:
            label='策略条件通过' if row.get('screen_passed') else '规则未通过的对照'
            failures='；'.join(reasons.get(x,x) for x in row.get('screen_rejections',[]))
            if row.get('review_status',{}).get('notice_bodies')=='not_collected_for_control':
                failures+='；研究对照正文未采集'
            elif row.get('review_status',{}).get('notice_bodies')=='incomplete':failures+='；公告资料待补核'
            timing=forward.get('record',{}).get('payload',{}).get('plan',{}).get('timing',{})
            position=account.get('positions',{}).get(row['code'])
            rejected=next((e for e in reversed(forward.get('new_events',[])) if e.get('code')==row['code'] and e.get('type') in ('entry_rejected','entry_skipped')),None)
            if position:
                state='待核算（价格或公司行动缺项）' if position.get('quarantine') or position.get('data_gap') else '待退出' if position.get('pending_exit') else '持有'
            elif rejected:
                event_reasons={'cash_or_minimum_lot':'资金或最低股数不足','entry_price_ceiling_exceeded':'超过冻结价格上限',
                    'bar_unverified':'价格未核验','halted':'停牌','one_price_day':'单一价格日',
                    'previous_nav_unverified':'前期净值未核验','slot_limit_or_ambiguous_tie':'席位不足或并列不明确',
                    'assumed_4_5pct_open_gap':'开盘跳空约束','adjustment_evidence_missing':'复权依据缺失'}
                state='入场未成交：'+event_reasons.get(rejected.get('reason'),str(rejected.get('reason')))
            elif timing.get('prospective'):state='待入场：'+str(timing.get('entry_session'))
            elif timing.get('reason')=='entry_day_already_frozen_first_plan_retained':state='沿用首次计划，本次不改订单'
            else:
                closed=next((t for t in reversed(account.get('closed_trades',[])) if t.get('code')==row['code']),None)
                state='最近已退出：'+str(closed.get('exit_session')) if closed else '已跟踪，暂未冻结入场'
            body+='<tr><td>'+_e(row.get('rank'))+'</td><td>'+_e(row.get('name',row['code']))+'<small>'+_e(row['code'])+'</small></td><td>'+_e(label)+'<small>'+_e(failures or '按容量排序')+'</small></td><td>'+_fmt(row.get('price'))+'</td><td>'+_fmt(row.get('entry_price_ceiling'),3)+'</td><td>'+_e(state)+'</td></tr>'
        text+='<div class="table-scroll"><table><thead><tr><th>顺序</th><th>股票</th><th>入组依据</th><th>信号收盘 / 元</th><th>研究模型上限 / 元</th><th>跟踪与模拟状态</th></tr></thead><tbody>'+body+'</tbody></table></div>'
    else:text+='<p class="attention">没有足够的已核验、可计算输入，保留空位；不编造股票、价格或成交。</p>'
    text+='<p class="plan">'+_e(_plan_text({'forward_account':forward}))+'</p>'
    if research.get('status')!='recorded':text+='<p class="attention">研究记录尚未完成：'+_e(research.get('error') or research.get('reason','见数据缺项'))+'</p>'
    if research.get('selection_scope')=='partial_verified_subset':text+='<p class="attention">本组来自部分已核验范围，不能解释为完整全市场前列。</p>'
    if research.get('review_issues'):text+='<p>资料审查或严格账户存在缺项，已留在研究证据中；研究计划不表示审查通过。持仓缺价或公司行动未解决时，保留待核算状态。</p>'
    holdings=account.get('positions',{})
    if holdings:
        text+='<h3>当前研究持仓</h3><ul>'+''.join('<li>'+_e(code)+'：'+_e(p.get('quantity'))+'股；'+_e('待核算' if p.get('quarantine') or p.get('data_gap') else '等待退出' if p.get('pending_exit') else '持有')+'</li>' for code,p in holdings.items())+'</ul>'
    def percentage(value):return '未核验' if value is None else _fmt(value*100,suffix='%')
    text+='<p>研究账户已平仓 '+_e(metrics.get('closed_trades',0))+' 笔；累计条件化收益 '+_e(percentage(metrics.get('total_return')))+'，最大回撤 '+_e(percentage(metrics.get('max_drawdown')))+'。未收齐估值输入时不填零。</p>'
    return text+'</section>'


def _evidence_section(report):
    stats=report.get('stats',{});refresh=report.get('daily_refresh',{});states=report.get('evidence_dimensions',{})
    result='<section id="evidence" class="panel"><div class="section-heading"><h2>数据与核验说明</h2><span>需要追溯时展开</span></div>'
    result+='<div class="evidence-grid"><p><strong>本次必需输入</strong><br>'+_e(_STATES.get(report.get('run_status',{}).get('data_status'),'未确认'))+'</p><p><strong>公告正文采集</strong><br>'+_e(_STATES.get(states.get('notice_bodies'),'未确认'))+'</p><p><strong>公告内容复核</strong><br>'+_e(_STATES.get(states.get('disclosure_semantics'),'尚未复核'))+'</p></div>'
    coverage='<p>目录证券 '+_e(stats.get('universe'))+'；完成公式计算 '+_e(stats.get('evaluated'))+'；排除 '+_e(stats.get('excluded'))+'；历史窗口缺项 '+_e(stats.get('incomplete'))+'；报价数据缺项 '+_e(stats.get('quote_data_excluded'))+'。</p>'
    coverage+='<p>流动性排除 '+_e(stats.get('liquidity_excluded'))+'；名义可买性排除 '+_e(stats.get('nominal_lot_excluded'))+'；追价门槛排除 '+_e(stats.get('chase_excluded'))+'；通过全部门槛 '+_e(stats.get('bullish'))+'。</p>'
    coverage+='<p>日线核验通过 '+_e(refresh.get('updated'))+' 只；当前筛选范围 '+_e(_STATES.get(refresh.get('status'),'未核验'))+'；原始采集 '+_e(_STATES.get(refresh.get('acquisition_status'),'未核验'))+'。资格规则排除证券的缺项、停牌窗口的解释仍保留，不将范围完整称为所有证券历史完整。</p>'
    coverage+='<p>入选还要求当前证券资格、至少252个交易日存续、20根完整日线、20日日均成交额至少1,000万元，以及资金与追价约束。通过数量不是仅满足MA5>MA20的股票数。精确并列保留，名额不足不补。</p>'
    result+=_fold('筛选范围与排除明细',coverage)
    warnings=list(dict.fromkeys(report.get('warnings',[])))
    if warnings:result+=_fold('完整提示与数据限制（'+str(len(warnings))+'条）','<ul>'+''.join('<li>'+_e(w)+'</li>' for w in warnings)+'</ul>')
    result+=_fold('实际生效参数',_configuration(report))
    validation=report.get('account_validation',{});history='<p>'+_e(validation.get('conclusion','历史账户验证尚未完成。'))+'</p>'
    for name,item in validation.get('results',{}).items():
        metrics=item.get('metrics',{})
        history+='<p>'+_e(name)+'：处理 '+_e(metrics.get('sessions'))+' 个交易日，未核验净值 '+_e(metrics.get('unverified_nav_sessions'))+' 天；'+('全期收益未计算。' if metrics.get('total_return') is None else '条件化累计收益 '+_fmt(metrics['total_return']*100,suffix='%')+'。')+'</p>'
    history+='<p>旧历史资料只作研究与淘汰证据。盈利有效性、真实成交及全部公司行动尚未验证；本次完整性不会使策略自动获得买入推荐资格。</p>'
    result+=_fold('历史验证与适用边界',history)
    origin='<p>规则来自AKQuant单只股票双均线教学示例。全市场流动性排序、资金分配、持有上限和追价约束为本地扩展，没有可继承的业绩保证。</p><p>'+_link('https://github.com/akfamily/akquant/blob/e4da08fdaae822eb8c652726f1baa7779fa3d31a/examples/strategies/01_stock_dual_moving_average.py','固定版本策略出处')+'</p><p>原报告规则及账户身份：<code>'+_e(report.get('rule_sha256'))+'</code></p><p><a href="report.json" download>下载 report.json 机器明细</a></p>'
    return result+_fold('策略出处与机器明细',origin)+'</section>'


def render_account_report(report, *, preview=False):
    rows=report.get('candidates',[]);stats=report.get('stats',{});config=report.get('account_config',{})
    run=report.get('run_status',{});status=run.get('data_status');failed=run.get('execution_status')=='failed' or status=='failed'
    mismatch=bool(report.get('requested_session') and report.get('data_session')!=report['requested_session'])
    complete=status=='complete' and not failed and not mismatch
    tone='good' if complete else 'bad' if failed else 'warn'
    state='筛选所需数据完整' if complete else '运行未完成' if failed else '数据日期不一致' if mismatch else '所需数据不完整' if status=='incomplete' else '完整性尚未确认'
    headline=('本次找到 '+str(len(rows))+' 只观察候选' if rows else '本次没有满足全部门槛的候选') if complete else ('运行失败，请先处理下方问题' if failed else '本次结果仅供部分观察')
    summary='按20日日均成交额展示研究预选，并逐股解释研究重点与反对证据；买入依据尚未验证。' if complete else '缺项尚未解除，不能将名单视为完整筛选结果，也不冻结新的模拟入场计划。'
    blockers=list(dict.fromkeys(i.get('message') or i.get('code') for i in report.get('data_issues',[]) if i.get('message') or i.get('code')))
    if mismatch and not any('日线' in str(b) for b in blockers):blockers.insert(0,'目标交易日与实际日线日期不同，需要重新核验。')
    blocker_html=''
    if not complete:
        if not blockers:blockers=['未取得完整的数据与账户核验结果，请查看完整提示。']
        blocker_html='<div class="blockers"><strong>本次需要处理</strong><ul>'+''.join('<li>'+_e(b)+'</li>' for b in blockers[:4])+'</ul>'
        if len(blockers)>4:blocker_html+=_fold('其余 '+str(len(blockers)-4)+' 项阻点','<ul>'+''.join('<li>'+_e(b)+'</li>' for b in blockers[4:])+'</ul>')
        blocker_html+='</div>'
    reviews=[r.get('body_review') or {} for r in rows]
    known=bool(rows) and all(isinstance(b.get('required_count'),int) and isinstance(b.get('retrieved_count'),int) for b in reviews)
    body_count=str(sum(b['retrieved_count'] for b in reviews))+' / '+str(sum(b['required_count'] for b in reviews)) if known else '本轮不适用' if complete and not rows else '未核验'
    phase=_PHASES.get(report.get('short_term',{}).get('phase'),'时点未核验')
    limits='模拟资金 '+_money(config.get('initial_capital_cents'))+' · 最多 '+str(config.get('max_positions','未记录'))+' 仓 · 持有上限 '+str(config.get('max_holding_sessions','未记录'))+' 个交易日（含买入日）'
    hero='<header class="hero"><div class="brand">TAIL SCREENER <span>短线研究</span></div><div class="headline-row"><h1>短线观察日报</h1><span class="status '+tone+'">'+_e(state)+'</span></div><p class="dateline">信号日 '+_e(_date(report.get('data_session')))+' · 目标日 '+_e(_date(report.get('requested_session')))+' · '+('原始报告生成于 ' if preview else '生成于 ')+_e(_date(report.get('generated_at'),True))+'</p>'
    if preview:hero+='<p class="preview">已有结果的版式预览 · 未重新采集、筛选或更新账户</p>'
    hero+='</header><nav aria-label="报告导航"><a href="#overview">结果概览</a><a href="#decision">买入依据检查</a><a href="#candidates">候选名单</a><a href="#account">模拟账户</a><a href="#evidence">数据核验</a></nav>'
    if report.get('research_simulation'):hero=hero.replace('<a href="#account">模拟账户</a>','<a href="#research">研究模拟</a><a href="#account">严格账户</a>')
    overview='<section id="overview" class="panel overview '+tone+'"><p class="eyebrow">本次结果</p><h2>'+_e(headline)+'</h2><p>'+_e(summary)+'</p>'+blocker_html
    overview+='<dl class="metrics three">'+_metric('通过全部筛选门槛',str(stats.get('bullish','未记录'))+' 只','展示 '+str(len(rows))+' 只，展示名额不等于持仓席位')+_metric('已计算证券',str(stats.get('evaluated','未记录'))+' 只','完整性只针对本次所需范围')+_metric('所列候选正文',body_count,'采集篇数 / 所需篇数，内容仍待复核')+'</dl>'
    overview+='<p class="context"><strong>运行时点：'+_e(phase)+'</strong>。盘中信号也使用上一完整交易日；价格上限是模型约束，不能当作买入指令。</p><p class="scope-note">仅供短线观察。名次表示成交容量优先，不表示上涨概率；策略盈利与真实成交尚未验证。</p></section>'
    candidates='<section id="candidates" class="panel"><div class="section-heading"><h2>观察名单</h2><span>'+_e(str(len(rows))+' 只展示候选')+'</span></div><p class="meta">'+_e(limits)+'</p>'
    if rows:candidates+=_candidate_table(rows)+'<p class="meta">价格上限按信号形成时冻结，超过不追价、不自动递补。公告事项是待阅读的词项，不代表风险已发生；当前名单尚无买入推荐资格。</p><h3>个股详情 <small>点击展开</small></h3>'+''.join(_candidate_detail(row,report) for row in rows)
    else:candidates+='<p class="empty">'+('本次没有满足全部门槛的候选，不强行凑满名额。' if complete else '本次没有可展示的完整名单；请先处理上方缺项，不能解释为市场没有信号。')+'</p>'
    candidates+='</section>'
    try:
        style=(Path(__file__).resolve().parents[2]/'assets/report.css').read_text(encoding='utf-8')
        style_notice=''
    except OSError:
        # Presentation damage must not discard an otherwise saved data report.
        style='body{font-family:sans-serif;max-width:1100px;margin:24px auto;padding:16px;line-height:1.8}table{width:100%;border-collapse:collapse}th,td{padding:10px;border:1px solid #ddd}pre,code{overflow-wrap:anywhere;white-space:pre-wrap}.table-scroll{overflow-x:auto}details{margin:14px 0}'
        style_notice='<p>版式样式未加载，已使用基础布局；请检查技能资源文件。</p>'
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>短线观察日报 · '+_e(_date(report.get('data_session')))+'</title><style>'+style+'</style></head><body><main>'+style_notice+hero+overview+_decision_section(report)+candidates+_kline_catalog_section(report)+_research_section(report)+_account_section(report)+_evidence_section(report)+'<footer>报告版本 '+_e(report.get('version'))+' · 仅保存本次结果，不自动持续监控或下单。</footer></main></body></html>')
