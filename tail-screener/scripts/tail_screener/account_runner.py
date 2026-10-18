"""Automatic data/environment entry for the full stock-rule paper workflow."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
import os
import sqlite3
from pathlib import Path
import time
from .account_strategy import STRATEGY_ID, scan_account, rule_identity
from .account_journal import TZ, update_forward_account
from .account_report import render_account_report
from .decision_review import apply_decision_review
from .bulk_data import ensure_bulk_dataset
from .daily_store import DailyStore, canonical_code
from .paths import data_root
from .trend_runner import calendar_context, latest_complete_session, PublicSources, _enrich
from .candidate_risk import instrument_eligibility
from .daily_refresh import refresh_daily_store
from .account_config import account_config, config_identity
from .recent_daily import collect_recent
from .short_term import trading_context,refresh_candidate_quotes
from .disclosures import review_bodies
from .corporate_actions import collect_actions
from .account_options import parse_account_args, DEFAULTS
from .research_status import evidence_dimensions
from .identities import identity_manifest
from .recovery_queue import RecoveryQueue
from .observations import update_observations
from .research_simulation import select_basket,simulation_config,advance_research

VERSION='3.5.0-hgb33-experimental'


def data_issues(report, calendar, catalog):
    """Explain every incomplete acquisition state in machine-readable form."""
    issues=[]
    def add(code,message):issues.append({'code':code,'message':message})
    requested=report.get('requested_session');session=report.get('data_session')
    if not calendar.get('verified') or not requested:
        add('calendar_unverified','交易日历或目标完整交易日未核验。')
    if requested and session!=requested:
        add('daily_session_mismatch',f'需要 {requested} 日线，实际为 {session}；未取得并核验目标日期完整日线。')
    stats=report.get('stats',{})
    if not stats.get('universe'):
        add('empty_universe','没有可核验的历史股票范围，不能判定为零信号。')
    if stats.get('incomplete',0):
        add('history_incomplete',f"{stats['incomplete']} 只股票历史数据不完整。")
    if stats.get('quote_data_excluded',0):
        add('quotes_incomplete',f"{stats['quote_data_excluded']} 只股票因报价数据问题被排除。")
    if not catalog.get('catalog_complete'):
        add('catalog_incomplete','股票目录完整性未核验。')
    refresh=report.get('daily_refresh',{})
    if refresh.get('status') in ('blocked','incomplete'):
        add('daily_refresh_incomplete',f"当前筛选范围的日线仍有缺项：{len(refresh.get('required_failed',refresh.get('failed',{})))}只；原因见daily_refresh。")
    quote_refresh=report.get('candidate_quote_refresh',{})
    if quote_refresh.get('status')=='incomplete':
        add('candidate_quote_refresh_incomplete',f"{len(quote_refresh['failed'])}只候选在扫描结束前未取得合格的新报价；不回退使用扫描开始时的旧报价。")
    review=report.get('research_checks',{})
    if review.get('body_incomplete'):
        add('notice_bodies_incomplete',f"{len(review['body_incomplete'])}只候选的近14日公告正文或目录覆盖不完整。")
    if review.get('distribution_incomplete'):
        add('distribution_evidence_incomplete',f"{len(review['distribution_incomplete'])}只候选的分红送转目录或近期事件正文未完成核验。")
    if report.get('recovery_tasks',{}).get('status')=='incomplete':
        add('recovery_evidence_index_incomplete','恢复证据索引保存失败，本次不冻结新入场计划。')
    return issues


def save(path,value):
    with Path(path).open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)


def load_validation(root):
    path=root/'references/account-workflow/validation.json'
    if not path.is_file():return {'conclusion':'账户历史检验尚未完成；不授予推荐资格。','results':{}}
    try:
        record=json.loads(path.read_text(encoding='utf-8'))
        if record['rule_sha256']!=rule_identity():raise ValueError('rule changed')
        for relative,expected in record['files'].items():
            p=(root/relative).resolve()
            if not p.is_relative_to(root) or hashlib.sha256(p.read_bytes()).hexdigest()!=expected:raise ValueError('evidence changed')
        record['recommendation_eligible']=False
        return record
    except (ValueError,KeyError,OSError,TypeError):return {'conclusion':'验证文件缺失或与当前规则不符，不能引用原成绩。','results':{}}


def run(args):
    began=time.monotonic();root=Path(__file__).resolve().parents[2];storage=data_root(root)
    total_budget=getattr(args,'run_budget',600)
    deadline=min(began+total_budget-5,float(os.environ.get('TAIL_SCREENER_DEADLINE',began+total_budget-5)))
    source_cache=getattr(args,'source_cache',None) or storage/'.cache/account-public'
    config=account_config(getattr(args,'capital',100000),getattr(args,'max_positions',6),getattr(args,'max_holding_sessions',5),
                          max_entry_gap_pct=getattr(args,'max_entry_gap_pct',3),max_ma20_premium_pct=getattr(args,'max_ma20_premium_pct',8))
    mode=getattr(args,'data_source','recent')
    config['data_source']=mode
    now=datetime.now(TZ);calendar=calendar_context(now);requested=latest_complete_session(now,calendar)
    output=Path(args.output) if args.output else storage/'reports'/now.strftime('account_%Y%m%d_%H%M%S_%f')
    output.mkdir(parents=True,exist_ok=True)
    if any(output.iterdir()):raise ValueError('输出目录须为空，历史报告不覆盖')
    report={'version':VERSION,'strategy_id':STRATEGY_ID,'generated_at':now.isoformat(),
            'data_session':None,'requested_session':requested,'rule_sha256':rule_identity(),
            'candidates':[],'stats':{},'warnings':[],'recommendation_eligible':False,'account_config':config}
    report['effective_configuration']=getattr(args,'effective_configuration',{
        'schema_version':1,'strategy':'ma5-20-account','parameters':{
            key:{'value':getattr(args,key,default),'source':'programmatic_or_default'}
            for key,default in DEFAULTS.items()}})
    report['account_identity']=config_identity(report['rule_sha256'],config)
    research_size=getattr(args,'research_size',6)
    report['research_simulation']={'status':'not_started','target_size':research_size,'candidates':[],
                                   'vacancies':research_size,'reason':'inputs_not_available'}
    report['runtime']={'run_budget_seconds':total_budget,'daily_workers':getattr(args,'daily_workers',12),
                       'stages':{},'source_cache':str(Path(source_cache).resolve())}
    def stage(name):
        report['runtime']['stages'][name]=round(time.monotonic()-began,3)
        print(f'阶段：{name}；扫描已用 {time.monotonic()-began:.1f} 秒，总剩余 {max(0,deadline-time.monotonic()):.1f} 秒。',flush=True)
    try:
        stage('universe')
        if mode=='archive':
            data=ensure_bulk_dataset(args.data_cache or storage/'.cache/bulk',target_session=requested,deadline=min(deadline,time.monotonic()+600),progress=print)
            store=DailyStore(data['directory']);session=store.sessions[-1]
            report['data_session']=session;report['data_source']=data['metadata']
        refresh_budget=getattr(args,'daily_update_budget',600)
        source=PublicSources(source_cache,budget=max(0,deadline-time.monotonic()),workers=4)
        try:
            quotes,catalog=source.universe(allow_preopen=True,include_inactive=True)
            quote_checked_at=datetime.now(TZ)
            quote_calendar=calendar_context(quote_checked_at)
            by_code={}
            for quote in quotes:
                try:by_code[canonical_code(quote['code'])]=quote
                except (ValueError,KeyError):continue
            if mode=='recent':
                if not requested:raise ValueError('目标完整交易日未核验')
                print(f'采集截至{requested}的近期日线、复权序列和上市日期，逐股核验。',flush=True)
                stage('daily_inputs')
                # Preserve a share of the total budget for disclosures, account
                # verification and report publication, including on cold starts.
                daily_budget=max(0,min(refresh_budget,deadline-time.monotonic()-min(120,total_budget*.25)))
                store,metadata=collect_recent(source,list(by_code.values()),requested,
                    args.data_cache or storage/'.cache/recent-daily',output/'daily-inputs',
                    budget=daily_budget,workers=getattr(args,'daily_workers',12),progress=lambda message:print(message,flush=True))
                report['data_source']=metadata;report['daily_refresh']=metadata
                report['data_session']=metadata['data_session'];session=store.sessions[-1]
                if metadata['updated']==0:raise ValueError('目标交易日没有任何通过核验的日线；不能生成观察名单')
                try:
                    report['recovery_tasks']={'status':'complete',**RecoveryQueue(storage/'.cache/recovery').ingest_recent(metadata,output/'daily-inputs')}
                except (ValueError,OSError,sqlite3.Error) as exc:
                    report['recovery_tasks']={'status':'incomplete','error':str(exc)}
            elif requested and session<requested:
                print(f'批量日线截至{session}，正在补齐至{requested}并重算完整均线窗口。',flush=True)
                store,refresh=refresh_daily_store(store,source,list(by_code.values()),requested,
                                                 output/'daily-update',budget=refresh_budget,progress=lambda message:print(message,flush=True))
                report['daily_refresh']=refresh
                session=store.sessions[-1];report['data_session']=session
            excluded={}
            expected=quote_calendar.get('session') if quote_calendar.get('is_session') and (quote_checked_at.hour,quote_checked_at.minute)>=(9,30) else quote_calendar.get('previous_session')
            data_excluded=0
            for code in store.codes(session):
                check=instrument_eligibility(by_code.get(code),asof=quote_checked_at,expected_session=expected)
                blocking=[x for x in check['issues'] if not (quote_calendar.get('is_session') and (quote_checked_at.hour,quote_checked_at.minute)<(9,30) and x['code']=='no_quote_trading')]
                explanation=report.get('daily_refresh',{}).get('explained_exclusions',{}).get(code)
                if explanation:
                    if expected in explanation['covered_sessions']:
                        blocking=[x for x in blocking if x['code']!='no_quote_trading']
                    blocking.append({'code':'reported_suspension_window_excluded','category':'scope',
                                     'message':'数据源停牌记录解释了所需窗口缺失；不满足完整连续日线门槛'})
                if blocking:excluded[code]=blocking
                if any(x['category']=='data' for x in blocking):data_excluded+=1
            stage('screening')
            scan=scan_account(store,session,excluded,top=max(config['max_positions'],args.top),config=config);report['stats']=scan['stats']
            report['kline_catalog']=scan['kline_catalog']
            for item in report['kline_catalog']['rows']:
                item['name']=(by_code.get(item['code']) or {}).get('name',item['code'])
            basket=select_basket(scan,config,research_size)
            for row in basket['candidates']:
                row['name']=by_code.get(row['code'],{}).get('name',row['code'])
                row['quote']=by_code.get(row['code'])
            report['research_simulation']={**basket,'status':'selected_not_recorded'}
            report['stats']['quote_data_excluded']=data_excluded
            report['quote_checked_at']=quote_checked_at.isoformat()
            def enrich_candidate(row):
                detail=_enrich(source,row['code']);checked=datetime.now(TZ)
                detail['review_gaps']=[g for g in detail['review_gaps'] if g!='公告正文与机构资金身份未核验']
                detail['body_review']=review_bodies(source,detail.get('notices'),row['code'][2:]+'.'+row['code'][:2],
                                                   checked,output/'disclosure-evidence')
                # This horizon is a source collection window, not a promised
                # liquidation date; suspensions can extend any holding period.
                end=(checked.date()+timedelta(days=3*config['max_holding_sessions']+14)).isoformat()
                detail['distributions']=collect_actions(source,row['code'],session,end,checked,output/'disclosure-evidence')
                if detail['body_review']['status']=='incomplete':detail['review_gaps'].append('近14日公告正文采集不完整')
                if detail['distributions']['status']=='incomplete':detail['review_gaps'].append('分红送转证据仍有缺项')
                detail['review_gaps'].append('正文为公告镜像；词项定位不等于财务、合同真实性或投资价值核验')
                return detail
            stage('candidate_evidence')
            with ThreadPoolExecutor(max_workers=4) as pool:details=list(pool.map(enrich_candidate,scan['candidates']))
            for row,detail in zip(scan['candidates'],details):
                row.update(detail);row['name']=by_code.get(row['code'],{}).get('name',row['code']);row['quote']=by_code.get(row['code'])
            by_detail={row['code']:row for row in scan['candidates']}
            for row in basket['candidates']:
                detail=by_detail.get(row['code'])
                row['review_status']={'notice_bodies':detail['body_review']['status'],
                                      'distributions':detail['distributions']['status']} if detail else {
                                      'notice_bodies':'not_collected_for_control','distributions':'checked_on_execution'}
            report['research_checks']={
                'body_incomplete':[r['code'] for r in scan['candidates'] if r['body_review']['status']!='complete'],
                'distribution_incomplete':[r['code'] for r in scan['candidates'] if r['distributions']['status']!='complete'],
                'checked_codes':[r['code'] for r in scan['candidates']]}
            quote_refresh_at=datetime.now(TZ)
            report['candidate_quote_refresh']=refresh_candidate_quotes(source,scan['candidates'],quote_refresh_at,
                calendar_context(quote_refresh_at),clock=lambda:datetime.now(TZ))
            report['candidates']=[r for r in scan['candidates'] if r['rank']<=args.top]
            report['diagnostics']={'catalog':catalog,'source_requests':source.diagnostics,'exclusions':excluded}
        finally:
            report.setdefault('diagnostics',{})['source_requests']=source.diagnostics
            source.close()
        issues=data_issues(report,calendar,catalog)
        completed_at=datetime.now(TZ)
        latest_at_finish=latest_complete_session(completed_at,calendar_context(completed_at))
        report['latest_complete_session_at_finish']=latest_at_finish
        if latest_at_finish!=requested:
            issues.append({'code':'target_session_changed_during_run',
                'message':f'运行期间目标完整交易日由{requested}变为{latest_at_finish}；请重新运行获取新目标日线，本次不冻结入场计划。'})
        context=calendar_context(datetime.fromisoformat(session+'T16:00:00+08:00'))
        next_session=context.get('next_session') if context.get('verified') else None
        if not next_session:
            issues.append({'code':'next_session_unverified','message':'下一交易日未核验，不能冻结新入场计划。'})
        report['data_issues']=issues
        if time.monotonic()>=deadline:
            issues.append({'code':'run_budget_exhausted','message':'本次调用总预算已用尽，证据或账户核验未全部完成；不冻结新入场计划。'})
        report['warnings'].extend(item['message'] for item in issues)
        optional_gaps=report.get('daily_refresh',{}).get('scope_excluded_gaps',{})
        if optional_gaps:report['warnings'].append(f'{len(optional_gaps)}只证券因风险简称等资格规则已排除，其历史缺项仍保留，未参与排名。')
        if report.get('daily_refresh',{}).get('source_counts',{}).get('tencent_raw_qfq'):
            report['warnings'].append('部分日线与报价来自腾讯同一来源；一致性通过不等于独立来源复核通过。')
        report['review_status']={'financials':'display_only',
            'notice_bodies':('incomplete' if report['research_checks']['body_incomplete'] else 'mirror_bodies_collected') if scan['candidates'] else 'not_applicable',
            'semantic_review':'not_performed','corporate_actions':'distribution_scope_only',
            'real_withholding_verified':False}
        for row in report['candidates']:
            for gap in row.get('review_gaps',[]):
                report['warnings'].append(f"{row['code']}：{gap}")
        entry_scan={**scan,'candidates':[r for r in scan['candidates'] if r['rank']<=config['max_positions']]}
        report['identity_manifest']=identity_manifest(report['rule_sha256'],config,report['account_identity'],
            report.get('daily_refresh',{}).get('snapshot_sha256'))
        evidence={'data_source':report['data_source'],'daily_refresh':report.get('daily_refresh'),
                  'identity_manifest':report['identity_manifest'],
                  'quotes':[r.get('quote') for r in entry_scan['candidates']],
                  'raw_source_scope':'selected20bars embedded; universe inputs identified by source manifest and hashes','fetched_at':datetime.now(TZ).isoformat()}
        ledger=Path(args.account_dir) if args.account_dir else storage/'.cache/paper-accounts'/report['account_identity']
        if 'conditions' in scan:
            try:
                report['observations']=update_observations(ledger/'observations',series=report['account_identity'],
                    session=session,sessions=list(store.sessions),candidates=scan['candidates'],conditions=scan['conditions'],
                    complete=not issues,snapshot_sha256=report.get('daily_refresh',{}).get('snapshot_sha256'))
                save(output/'observations.json',report['observations'])
            except (ValueError,OSError,sqlite3.Error) as exc:
                report['observations']={'status':'incomplete','error':str(exc)}
                issues.append({'code':'observation_save_failed','message':'观察记录保存失败，本次不冻结新入场计划。'})
        try:
            if rule_identity()!=report['rule_sha256']:raise ValueError('运行期间规则代码发生变化；保留数据报告，不接续账户')
            stage('account_verification')
            action_source=PublicSources(source_cache,budget=max(0,min(180,deadline-time.monotonic())),workers=2)
            try:
                forward=update_forward_account(store,entry_scan,evidence,ledger,next_session=next_session,
                    entry_block_reasons=[item['code'] for item in issues],config=config,
                    action_loader=lambda code,start,end:collect_actions(action_source,code,start,end,datetime.now(TZ),output/'disclosure-evidence'))
            finally:
                report['diagnostics']['account_action_requests']=action_source.diagnostics
                action_source.close()
            save(output/'account-record.json',forward['record']);save(output/'account-state.json',forward['account'])
            save(output/'account-observations.json',{'snapshots':forward['history_snapshots'],'metrics':forward['metrics']})
            report['forward_account']={k:v for k,v in forward.items() if k!='history_snapshots'}
            if any(p.get('errors') for p in forward.get('record',{}).get('payload',{}).get('corporate_action_evidence',{}).values()):
                issues.append({'code':'account_distribution_evidence_incomplete',
                               'message':'账户逐日处理所需分红送转证据不完整；已保留原输入，不能将现金账户或跳过入场视为事件核验成功。'})
            if forward.get('account',{}).get('nav_cents') is None:
                issues.append({'code':'account_nav_unverified','message':'当前账户净值未完整核验，不能报告完整估值。'})
            if forward.get('metrics',{}).get('valuation_complete') is not True:
                issues.append({'code':'account_history_unverified','message':'账户净值序列未完整核验，不能报告全期收益或最大回撤。'})
        except (ValueError,OSError,KeyError,TypeError) as exc:
            report['forward_account']={'error':str(exc),'status':'blocked'}
            issues.append({'code':'account_update_blocked','message':'账户更新未完成；未重置旧账户或填造收益。'})
        try:
            stage('research_simulation')
            if rule_identity()!=report['rule_sha256']:raise ValueError('运行期间规则代码发生变化')
            research_identity=config_identity(report['rule_sha256'],simulation_config(config,research_size))
            research_dir=getattr(args,'research_account_dir',None) or ledger/'research'/research_identity
            if Path(research_dir).resolve()==ledger.resolve():raise ValueError('研究与严格账户目录必须分开')
            research_source=PublicSources(source_cache,budget=max(0,min(180,deadline-time.monotonic())),workers=2)
            try:
                report['research_simulation'],research_forward=advance_research(store,basket,report,research_dir,
                    next_session=next_session,extra_blockers=['run_budget_exhausted'] if time.monotonic()>=deadline else [],
                    action_loader=lambda code,start,end:collect_actions(research_source,code,start,end,datetime.now(TZ),output/'disclosure-evidence'))
            finally:
                report['diagnostics']['research_action_requests']=research_source.diagnostics;research_source.close()
            save(output/'research-account-record.json',research_forward['record'])
            save(output/'research-account-state.json',research_forward['account'])
            save(output/'research-account-observations.json',{'snapshots':research_forward['history_snapshots'],'metrics':research_forward['metrics']})
            if (research_forward['account'].get('nav_cents') is None
                    or research_forward['metrics'].get('valuation_complete') is not True):
                issues.append({'code':'research_nav_unverified','message':'独立研究账户净值或历史估值未完整核验；保留待核算，不填零或合并到严格策略成绩。'})
        except (ValueError,OSError,KeyError,TypeError) as exc:
            report['research_simulation']={**basket,'status':'blocked','error':str(exc)}
            issues.append({'code':'research_simulation_failed','message':'独立研究模拟未完成；名单与原因已保留，不重置账户。'})
        report['warnings']=list(dict.fromkeys([*report['warnings'],*[item['message'] for item in issues]]))
        report['run_status']={'execution_status':'completed','data_status':'incomplete' if issues else 'complete',
                              'reasons':[item['code'] for item in issues]}
    except (OSError,ValueError,RuntimeError,KeyError) as exc:
        report['run_status']={'execution_status':'failed','data_status':'failed','reasons':['data_acquisition_or_processing_failed']}
        report['data_issues']=[{'code':'data_acquisition_or_processing_failed','message':str(exc)}]
        report['warnings'].append(str(exc))
    stage('report_publication')
    report['account_validation']=load_validation(root);report['elapsed_seconds']=round(time.monotonic()-began,3)
    finished=datetime.now(TZ);report['generated_at']=finished.isoformat()
    final_calendar=calendar_context(finished)
    latest_at_publication=latest_complete_session(finished,final_calendar)
    report['latest_complete_session_at_finish']=latest_at_publication
    if report['run_status']['execution_status']!='failed' and latest_at_publication!=requested:
        if not any(i['code']=='target_session_changed_during_run' for i in report['data_issues']):
            issue={'code':'target_session_changed_during_run',
                'message':f'报告发布时完整交易日已变为{latest_at_publication}，本次目标为{requested}；需要重新运行。'}
            report['data_issues'].append(issue);report['warnings'].append(issue['message'])
        report['run_status']['data_status']='incomplete'
        report['run_status']['reasons']=list(dict.fromkeys([*report['run_status']['reasons'],'target_session_changed_during_run']))
    report['ranking_scope']='verified_current_universe' if report['run_status']['data_status']=='complete' else 'partial_verified_subset'
    report['short_term']=trading_context(report,finished,final_calendar,mode=getattr(args,'mode','auto'))
    report['evidence_dimensions']=evidence_dimensions(report)
    apply_decision_review(report)
    save(output/'research-simulation.json',report['research_simulation'])
    save(output/'report.json',report)
    with (output/'index.html').open('x',encoding='utf-8') as f:f.write(render_account_report(report))
    print(json.dumps({'html':str((output/'index.html').resolve()),'report':str((output/'report.json').resolve()),
                      'stats':report['stats'],'status':report['run_status']},ensure_ascii=False))
    return report


def main(argv=None):
    return run(parse_account_args(argv))
