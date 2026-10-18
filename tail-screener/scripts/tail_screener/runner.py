from __future__ import annotations

from collections import Counter
from datetime import datetime
from pathlib import Path
import argparse
import json
import time
import hashlib
import csv

from .engine import TZ, coarse_filter, coarse_rank, evaluate, sector_context, technical, validate_config, stamp
from .providers import PublicSources, parallel, calendar_context, safe_source_error
from .report import write_report
from .signals import select_diverse
from .run_status import describe_run
from .runtime import current_runtime_info
from .paths import data_root
from .research import resolve_analysis, research_context, research_technical, evaluate_research, closed_quote_matches
from .acquisition_diagnostics import summarize_collection
from .opportunities import annotate_opportunities

SKILL_ROOT=Path(__file__).resolve().parents[2]


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode('utf-8')).hexdigest()


def code_version():
    digest=hashlib.sha256()
    for path in sorted((SKILL_ROOT/'scripts').rglob('*.py')):
        digest.update(str(path.relative_to(SKILL_ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_replay(path):
    """Restore the recorded configuration; never silently use today's defaults."""
    snapshot=json.loads(path.read_text(encoding='utf-8'))
    report_path=path.parent/'report.json'
    original=json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
    if original:
        if original.get('run_id') != canonical_hash(snapshot):
            raise ValueError('回放快照与原报告运行哈希不一致')
        config=original.get('config')
        if not isinstance(config,dict) or original.get('config_hash') != canonical_hash(config):
            raise ValueError('原报告配置缺失或哈希不一致')
        embedded=snapshot.get('screening_config')
        if embedded is not None and embedded != config:
            raise ValueError('快照配置与原报告配置不一致')
        source_code=original.get('code_version')
    else:
        config=snapshot.get('screening_config')
        if not isinstance(config,dict):
            raise ValueError('旧快照缺少原参数；请同时提供原 report.json，不能按当前默认参数冒充原配置回放')
        source_code=snapshot.get('screening_code_version')
    return snapshot,dict(config),{'source_run_id':canonical_hash(snapshot),
                                'source_config_hash':canonical_hash(config),'source_code_version':source_code}


def finish(snapshot, c, output, replay=False, replay_origin=None):
    now=stamp(snapshot['as_of']) if replay else datetime.now(TZ)
    if now is None:
        raise ValueError('快照缺少带日期的 as_of')
    current_code=code_version()
    if not replay:
        snapshot=dict(snapshot,screening_config=dict(c),screening_code_version=current_code)
    context=snapshot['context']
    analysis_mode=snapshot.get('analysis_mode','live')
    evaluated=[]
    for bundle in snapshot['bundles']:
        result=(evaluate_research(bundle,context,now,c) if analysis_mode=='post_close'
                else evaluate(bundle,context,now,c,replay=replay))
        evaluated.append(result)
    initial_status=describe_run(snapshot,c,now,0,replay,evaluated=evaluated)
    evaluated=annotate_opportunities(evaluated,context,c,analysis_mode,initial_status)
    evaluated.sort(key=lambda r:(r['status']=='conditional',r.get('research_status')=='ready',
                                 r.get('watch_eligible',False),r['score']),reverse=True)
    selection=select_diverse(evaluated,snapshot['bundles'],c['top_n'],c['max_per_industry'],c.get('max_return_correlation',.85))
    picks=selection['selected']
    stats=dict(snapshot['stats'],conditional_count=sum(r['status']=='conditional' for r in picks),deep_checked=len(evaluated),
               research_ready_count=sum(r.get('research_status')=='ready' for r in picks),
               research_incomplete_count=sum(r.get('research_status')=='incomplete' for r in evaluated),
               watchlist_count=sum(r.get('watch_eligible',False) for r in picks))
    rule_passed=sum(r['status']=='conditional' or r.get('research_status')=='ready' for r in evaluated)
    environment_only=sum(r.get('watch_eligible',False) for r in evaluated)
    data_incomplete=(sum(r.get('research_status')=='incomplete' for r in evaluated)
                     if analysis_mode=='post_close' else None)
    opportunity_summary={'evaluated_count':len(evaluated),'rule_passed_count':rule_passed,
                         'environment_only_count':environment_only,'data_incomplete_count':data_incomplete,
                         'other_blocked_count':len(evaluated)-rule_passed-environment_only-(data_incomplete or 0),
                         'data_classification':'separate' if analysis_mode=='post_close' else 'combined_with_other',
                         'scope':'仅统计本次深入核查标的；单个交易日不能证明长期触发频率或收益。'}
    config_hash=canonical_hash(c)
    run_id=canonical_hash(snapshot)
    for r in evaluated:
        r['signal_id']=hashlib.sha256(f"{run_id}:{r['code']}:{config_hash}".encode()).hexdigest()
    replay_details=None
    replay_warnings=[]
    if replay:
        origin=replay_origin or {'source_config_hash':canonical_hash(snapshot['screening_config']) if snapshot.get('screening_config') else None,
                                 'source_code_version':snapshot.get('screening_code_version')}
        replay_details=dict(origin,execution_code_version=current_code,
                            original_config_used=origin.get('source_config_hash')==config_hash,
                            same_code_version=origin.get('source_code_version')==current_code)
        if not replay_details['original_config_used']:
            replay_warnings.append('回放参数已变更或原参数未知，仅为参数对照，不能视作原运行重现。')
        if not replay_details['same_code_version']:
            replay_warnings.append('回放代码与原运行不同或原代码版本未知；代码哈希不是源码归档，不能视作原实现重现。')
    report={'version':2,'strategy_version':'2.1.0','run_id':run_id,'config_hash':config_hash,'code_version':current_code,
            'as_of':now.isoformat(),'mode':'离线回放' if replay else '盘后研究' if analysis_mode=='post_close' else '实时采集',
            'generated_at':datetime.now(TZ).isoformat(),
            'analysis_mode':analysis_mode,'data_session':snapshot.get('data_session'),
            'replay_provenance':replay_details,
            'runtime':snapshot.get('runtime'),
            'universe_metadata':snapshot.get('universe_metadata',{}),
            'source_diagnostics':snapshot.get('source_diagnostics',[]),
            'source_fallbacks':snapshot.get('source_fallbacks',[]),
            'diagnostics':snapshot.get('diagnostics'),
            'stage_seconds':snapshot.get('stage_seconds',{}),
            'run_status':describe_run(snapshot,c,now,stats['conditional_count'],replay,evaluated=evaluated,
                                      watch_count=stats['watchlist_count']),
            'config':c,'context':context,'stats':stats,'picks':picks,'all_candidates':evaluated,
            'watchlist':[r for r in picks if r.get('watch_eligible')],
            'opportunity_summary':opportunity_summary,
            'selection_excluded':selection['excluded'],
            'rejection_counts':dict(Counter(b for r in evaluated for b in r['blockers']
                                           if b != '盘后研究仅供观察，须下一交易日按实时规则重新筛选')),
            'warnings':snapshot.get('warnings',[])+selection['warnings']+replay_warnings+(['回放数据不产生实时入场资格。'] if replay else [])}
    output=output or data_root(SKILL_ROOT)/'reports'/now.strftime('%Y%m%d_%H%M%S_%f')
    output.mkdir(parents=True,exist_ok=True)
    if (output/'report.json').exists() or (output/'snapshot.json').exists():
        raise ValueError('报告目录已有冻结记录；请使用新目录，不能覆盖历史信号')
    (output/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    if snapshot.get('diagnostics'):
        (output/'diagnostics.json').write_text(json.dumps(snapshot['diagnostics'],ensure_ascii=False,indent=2),encoding='utf-8')
    with (output/'outcomes.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=['signal_id','status','recorded_at','entry_at','entry_price','exit_at','exit_price','shares','kind','reason'])
        writer.writeheader()
        writer.writerows({'signal_id':r['signal_id']} for r in picks if r['status']=='conditional')
    target=write_report(report,output)
    return report,target


def collect(c, progress=print, mode='auto'):
    sources=[]
    try:
        return _collect(c,progress,mode,sources)
    finally:
        for source in sources:
            close=getattr(source,'close',None)
            if close:
                close()


def _collect(c,progress,mode,sources):
    started=time.monotonic()
    stage_started=started
    stage_seconds={}
    def checkpoint(name):
        nonlocal stage_started
        completed=time.monotonic()
        stage_seconds[name]=round(completed-stage_started,3)
        stage_started=completed
    started_at=datetime.now(TZ)
    analysis=resolve_analysis(started_at,mode.replace('-','_'),calendar=calendar_context(started_at))
    source=PublicSources(data_root(SKILL_ROOT)/'.cache',c['budget_seconds'],c['workers'])
    sources.append(source)
    warnings=[]
    progress(f"TAIL SCREENER v2.1.0 · {'最新完整交易日盘后研究' if analysis['mode']=='post_close' else '当前行情扫描'} · 正在实际采集",flush=True)
    progress('1/4 获取沪深股票横截面和行业映射…',flush=True)
    try:
        universe,meta=source.universe()
    except Exception as exc:
        universe,meta=[],{'coverage':0,'errors':{'universe':safe_source_error(exc)},'fetched_at':datetime.now(TZ).isoformat()}
        warnings.append('全市场接口不可用，本次不输出入场候选。')
    checkpoint('universe')
    if meta.get('errors'):
        warnings.append(f"全市场分页或接口异常 {len(meta['errors'])} 项；详见输入快照。")
    if meta.get('fallback_used'):
        warnings.append('主股票目录未能完整获取，已切换东方财富备用目录；'
                        + ('备用目录完整性核验通过。' if meta.get('catalog_complete')
                           else '备用目录仍未通过完整性核验，本次扫描范围不完整。'))
    if meta.get('quote_batch_recovery'):
        warnings.append('部分行情批次失败后已拆分为小批次恢复；实际有效报价覆盖单独核验。')
    if analysis['mode']=='live' and meta.get('dated_quote_coverage',0)<c['min_universe_coverage']:
        warnings.append('全市场新鲜且带日期的报价覆盖不足，板块统计仅供观察。')
    try:
        industry_data=source.industries() if universe else {'mapping':{}}
    except Exception as exc:
        industry_data={'mapping':{},'error':type(exc).__name__}
        warnings.append('行业映射不可用，方向和候选均降为观察。')
    checkpoint('industries')
    mapping=industry_data['mapping']
    market_universe=([r for r in universe if closed_quote_matches(r,datetime.now(TZ),analysis.get('data_session'))]
                     if analysis['mode']=='post_close' else universe)
    sectors,coverage,breadth=sector_context(market_universe,mapping)
    filtered=sorted([r for r in market_universe if coarse_filter(r,c)],key=coarse_rank,reverse=True)
    shortlisted=filtered[:c['history_candidates']]
    progress(f"2/4 沪深有效报价 {len(universe)}，初筛 {len(filtered)}，并行计算 {len(shortlisted)} 只历史量价…",flush=True)
    histories,history_errors=parallel([r['code'] for r in shortlisted],source.history,c['workers'])
    checkpoint('history')
    reference_now=datetime.now(TZ)
    technical_rows=[]
    for r in shortlisted:
        if r['code'] not in histories:
            continue
        result=(research_technical(r,histories[r['code']],reference_now,c,analysis.get('data_session'))
                if analysis['mode']=='post_close' else technical(r,histories[r['code']],reference_now,c))
        technical_rows.append((not result['gaps'],result['score'],coarse_rank(r),r))
    technical_rows.sort(key=lambda x:x[:3],reverse=True)
    deep_rows=[x[3] for x in technical_rows[:c['deep_candidates']]]
    progress(f"3/4 对 {len(deep_rows)} 只候选核查财报和公告…",flush=True)
    codes=[r['code'] for r in deep_rows]
    enriched,errors=parallel(codes,source.enrich,c['workers'])
    checkpoint('enrichment')
    progress('4/4 刷新分钟成交、盘口和指数；按报告发布时点复核时效…',flush=True)
    refreshed,refresh_errors=parallel(codes,source.refresh,c['workers'])
    checkpoint('refresh')
    indices,index_errors=parallel(['000001.SH','399001.SZ'],source.quote,2)
    checkpoint('indices')
    now=datetime.now(TZ)
    context={'calendar':calendar_context(now),'industries':mapping,'sectors':sectors,
             'industry_coverage':coverage,'market_breadth':breadth,'universe_coverage':meta['coverage'],
             'valid_quote_coverage':meta.get('valid_quote_coverage',0),'dated_quote_coverage':meta.get('dated_quote_coverage',0),
             'market_quote_at':meta.get('market_quote_at'),
             'snapshot_fetched_at':meta['fetched_at'],'indices':list(indices.values())}
    context['coverage_counts']={'quote_denominator':meta.get('eligible_symbols'),
                                'valid_quotes':len(universe),'market_quotes':len(market_universe),
                                'industry_mapped':sum(bool(mapping.get(r['code'])) for r in market_universe),
                                'advancing_quotes':sum(r.get('change_pct',0)>0 for r in market_universe)}
    if analysis['mode']=='post_close':
        context['research']=research_context(universe,now,c,analysis)
    bundles=[]
    enrichment_errors={}
    recovered_errors={}
    for row in deep_rows:
        code=row['code']
        original=enriched.get(code,{'errors':['深入采集未完成']})
        remaining=list(original.get('errors',[]))
        details=dict(original.get('error_details',{}))
        bundle={'snapshot':row,'history':histories[code],**original}
        if code in refreshed:
            update=refreshed[code]
            bundle.update({key:value for key,value in update.items() if key not in ('errors','error_details')})
            if update.get('quote'):
                resolved=[e for e in remaining if e.startswith('quote采集失败')]
                if resolved:
                    recovered_errors[code]=resolved
                remaining=[e for e in remaining if not e.startswith('quote采集失败')]
                details.pop('quote',None)
            if remaining:
                enrichment_errors[code]=list(remaining)
            refresh_gaps=list(update.get('errors',[]))
            if refresh_gaps:
                refresh_errors[code]=refresh_gaps
            remaining.extend(refresh_gaps)
            details.update(update.get('error_details',{}))
        else:
            if remaining:
                enrichment_errors[code]=list(remaining)
            remaining.append('发布前分时/报价刷新失败')
        bundle['errors']=list(dict.fromkeys(remaining))
        bundle['error_details']=details
        bundle['source_urls']=[row['source_url']]
        if not any(item.get('source_url') for item in bundle.get('history',[])):
            bundle['source_urls'].append(f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code[-2:].lower()+code[:6]},day,,,100,qfq')
        if bundle.get('quote'):
            bundle['source_urls'].append(bundle['quote']['source_url'])
        if bundle.get('cross_quote'):
            bundle['source_urls'].append(bundle['cross_quote']['source_url'])
        for item in bundle.get('history',[])+bundle.get('minutes',[]):
            if item.get('source_url'):
                bundle['source_urls'].append(item['source_url'])
        bundle['source_urls']=list(dict.fromkeys(bundle['source_urls']))
        bundles.append(bundle)
    if history_errors:
        warnings.append(f'{len(history_errors)}只日线未能获取，未将缺失数据计为利好。')
    if refresh_errors or index_errors:
        warnings.append('部分最终报价或分时刷新失败，相关候选不得入场。')
    if enrichment_errors:
        warnings.append(f'{len(enrichment_errors)}只候选存在报价、财报或公告采集缺项，相关候选仅观察。')
    if not context['calendar'].get('verified'):
        cal=context['calendar']
        warnings.append(f"交易日历核验失败 [{cal.get('error_code','unknown')}]：{cal.get('error','原因未知')}。{cal.get('action','请运行 --doctor 检查依赖和日历范围。')}")
    if analysis['mode']=='post_close':
        warnings.append(f"本次按 {analysis.get('data_session') or '待核验'} 完整交易日的收盘数据做下一交易日观察筛选；财报与消息按实际采集时点核验，不是历史14:30结果。")
    elif not c['start_time'] <= now.strftime('%H:%M:%S') <= c['last_entry_time']+':00':
        warnings.append('当前为非尾盘窗口运行，只用于检查接口和观察研究。')
    probe=None
    if not universe or not context['calendar'].get('verified'):
        from .doctor import run_doctor
        progress('自动诊断运行环境与样例接口，结果随报告保存…',flush=True)
        probe=run_doctor(network=True)
    fallback_events=getattr(source,'fallback_events',[])
    if not isinstance(fallback_events,list):
        fallback_events=[]
    if fallback_events:
        warnings.append(f'{len(fallback_events)}项日线/分时采集尝试了备用访问入口，原始错误与实际来源已保存；同一供应商的入口不计为独立交叉来源。')
    snapshot={'as_of':now.isoformat(),'context':context,'bundles':bundles,'universe':universe,
            'analysis_mode':analysis['mode'],'data_session':analysis.get('data_session'),
            'runtime':current_runtime_info(),
            'source_diagnostics':getattr(source,'diagnostics',[]),
            'source_fallbacks':fallback_events,
            'stage_seconds':stage_seconds,'recovered_errors':recovered_errors,
            'universe_metadata':meta,'industry_metadata':{k:v for k,v in industry_data.items() if k!='mapping'},
            'errors':{'history':history_errors,'deep':errors,'enrichment':enrichment_errors,'refresh':refresh_errors,'indices':index_errors},
            'stats':{'universe':len(universe),'liquid_candidates':len(filtered),'history_checked':len(histories),
                     'market_quotes_used':len(market_universe),
                     'elapsed_seconds':round(time.monotonic()-started,1)},'warnings':warnings}
    snapshot['diagnostics']=summarize_collection(snapshot,c)
    if probe is not None:
        snapshot['diagnostics']['sample_probe']=probe
    return snapshot


def main(argv=None):
    from .strategy_status import DEFAULT_STRATEGY, DEFAULT_STATUS_MESSAGE
    parser=argparse.ArgumentParser(description='沪深A股筛选维护入口；ETF默认策略已撤下，旧规则仅显式研究/回放',allow_abbrev=False)
    parser.add_argument('--strategy',choices=('connors-rsi2','legacy-tail'),default=None,
                        help='没有默认策略；显式选择保留的历史研究规则')
    parser.add_argument('--config',type=Path,help='JSON覆盖配置文件')
    parser.add_argument('--top',type=int,help='目标展示名额，默认5；排名边界精确并列时全部展示')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--mode',choices=('auto','live','post-close'),default=None,
                        help='默认auto：盘后自动用最新完整交易日做观察筛选；live当前行情；post-close盘后研究')
    parser.add_argument('--replay',type=Path,help='冻结快照离线回放；完全禁用网络且不产生实时买入资格')
    parser.add_argument('--rerank',action='store_true',help='仅配合作者策略--replay：对原冻结输入做当前排序对照，保留原快照')
    args=parser.parse_args(argv)
    if args.rerank and not args.replay:
        parser.error('--rerank 仅用于作者策略冻结快照的 --replay 离线排序对照')
    replay_origin=None
    selected_strategy=args.strategy or DEFAULT_STRATEGY
    if selected_strategy not in ('connors-rsi2', 'legacy-tail') and not args.replay:
        parser.error(DEFAULT_STATUS_MESSAGE)
    if args.replay:
        if args.mode is not None:
            parser.error('回放使用快照记录的分析模式，不能同时指定 --mode')
        try:
            with args.replay.open(encoding='utf-8') as handle:
                frozen_header=json.load(handle)
        except (ValueError,OSError) as exc:
            parser.error(str(exc))
        if not isinstance(frozen_header,dict):
            parser.error('冻结快照必须是JSON对象')
        recorded_id=frozen_header.get('strategy_id')
        # A full-market snapshot may be large. Release the routing read before
        # the selected replay engine loads and validates its original input.
        del frozen_header
        if recorded_id not in (None,'legacy_tail_v2_1','connors_rsi2_a_share_research'):
            parser.error('快照策略身份未知，不能按另一套规则静默回放')
        recorded_strategy='connors-rsi2' if recorded_id=='connors_rsi2_a_share_research' else 'legacy-tail'
        if args.strategy and args.strategy!=recorded_strategy:
            parser.error('回放不能切换策略；旧快照只重现其原策略的输入范围')
        selected_strategy=recorded_strategy
        if selected_strategy=='connors-rsi2':
            if args.config or args.top is not None:
                parser.error('作者策略回放使用原参数；本入口不支持回放时覆盖 --config 或 --top')
            from .author_runner import run_author_scan
            try:
                report,target=run_author_scan({},args.output,replay_path=args.replay,rerank=args.rerank)
            except (ValueError,OSError) as exc:
                parser.error(str(exc))
            print(json.dumps({'report':str(target.resolve()),'strategy_id':report['strategy_id'],
                              'run_status':report['run_status'],'stats':report['stats']},ensure_ascii=False,indent=2))
            return report
        if args.rerank:
            parser.error('--rerank 不适用于旧版尾盘策略快照，不能切换冻结输入的策略身份')
        try:
            snapshot,c,replay_origin=load_replay(args.replay)
        except (ValueError,OSError) as exc:
            parser.error(str(exc))
    else:
        c=json.loads((SKILL_ROOT/'config.json').read_text())
    if args.config:
        overrides=json.loads(args.config.read_text())
        if not isinstance(overrides,dict) or set(overrides)-set(c):
            parser.error('配置不是对象或包含未知字段')
        author_operations={'top_n','workers','budget_seconds','quote_max_age_seconds',
                           'snapshot_max_age_seconds','min_universe_coverage'}
        if selected_strategy=='connors-rsi2' and set(overrides)-author_operations:
            parser.error('作者策略只接受展示、采集预算与数据质量参数；不套用旧版评分、涨幅、量比或固定退出阈值')
        c.update(overrides)
    if args.top is not None:
        c['top_n']=args.top
        c['max_per_industry']=min(c['max_per_industry'],args.top)
    try:
        validate_config(c)
    except (ValueError,TypeError) as exc:
        parser.error(str(exc))
    if selected_strategy=='connors-rsi2':
        from .author_runner import run_author_scan
        try:
            report,target=run_author_scan(c,args.output,mode=args.mode or 'auto')
        except (ValueError,OSError) as exc:
            parser.error(str(exc))
        print(json.dumps({'report':str(target.resolve()),'strategy_id':report['strategy_id'],
                          'run_status':report['run_status'],'stats':report['stats']},ensure_ascii=False,indent=2))
        return report
    if not args.replay:
        snapshot=collect(c,mode=args.mode or 'auto')
    report,target=finish(snapshot,c,args.output,replay=bool(args.replay),replay_origin=replay_origin)
    print(json.dumps({'report':str(target.resolve()),'analysis_mode':report['analysis_mode'],'data_session':report['data_session'],
                      'run_status':report['run_status'],'conditional_candidates':report['stats']['conditional_count'],
                      'environment_watchlist':report['stats']['watchlist_count'],
                      'observations':(report['stats']['research_ready_count']+report['stats']['watchlist_count']
                                      if report['run_status']['data_status']=='complete' else 0),
                      'displayed_records':len(report['picks']),'stats':report['stats']},ensure_ascii=False,indent=2))
    return report
