"""Plan, resume and export explicitly scoped free historical daily data."""
import argparse
import json
from pathlib import Path
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope',type=Path,required=True,help='schema_version=1，securities包含code和listing_date')
    parser.add_argument('--start',required=True);parser.add_argument('--end',required=True)
    parser.add_argument('--queue',type=Path);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--budget',type=int,default=300);parser.add_argument('--max-tasks',type=int,default=200)
    parser.add_argument('--chunk-sessions',type=int,default=40);parser.add_argument('--overlap',type=int,default=3)
    parser.add_argument('--adjustment-basis',choices=['tencent_hfq','sina_hfq_factor'],default='tencent_hfq')
    parser.add_argument('--plan-only',action='store_true');parser.add_argument('--refresh',action='store_true')
    parser.add_argument('--retry-incomplete',action='store_true')
    args=parser.parse_args()
    if not 1<=args.budget<=1800 or not 1<=args.max_tasks<=1000:parser.error('预算须为1至1800秒，任务数1至1000')
    if args.output.exists() and any(args.output.iterdir()):parser.error('输出目录须为空')
    from tail_screener.historical_recovery import create_plan,HistoricalFetcher,export_history,verified_sessions
    from tail_screener.recovery_queue import RecoveryQueue
    from tail_screener.providers import PublicSources
    from tail_screener.paths import data_root
    from tail_screener.account_strategy import rule_identity
    storage=data_root(Path(__file__).resolve().parents[1]);began=time.monotonic()
    build=rule_identity()
    try:
        scope=json.loads(args.scope.read_text(encoding='utf-8-sig'))
        calendar=verified_sessions(args.start,args.end)
        queue=RecoveryQueue(args.queue or storage/'.cache/history-recovery')
        plan=create_plan(scope,args.start,args.end,queue,chunk_sessions=args.chunk_sessions,overlap=args.overlap,calendar=calendar,adjustment_basis=args.adjustment_basis)
    except (OSError,ValueError,KeyError,TypeError) as exc:parser.error(str(exc))
    ids=[task['id'] for task in plan['data']['tasks']]
    if args.refresh or args.retry_incomplete:queue.requeue(ids,include_completed=args.refresh)
    source=PublicSources(queue.root/'http-cache',budget=args.budget,workers=1)
    execution={'processed':0};error=None
    try:
        if not args.plan_only:
            execution=queue.run_pending(HistoricalFetcher(source,plan['data']['sessions'],refresh=args.refresh,adjustment_basis=args.adjustment_basis),
                budget_seconds=args.budget,max_tasks=args.max_tasks,kind='historical_daily',task_ids=ids)
    except (ValueError,OSError) as exc:error=str(exc)
    try:
        if build!=rule_identity():raise ValueError('恢复期间源代码发生变化，保留任务证据，请重新导出')
        result=export_history(plan,queue,args.output,source=None if args.plan_only else source)
    finally:diagnostics=source.diagnostics;source.close()
    details={'plan_path':plan['path'],'plan_sha256':plan['sha256'],'execution':execution,
             'implementation_sha256':build,'operation':'plan_only' if args.plan_only else 'recover_and_export',
             'requests':diagnostics,'error':error,'elapsed_seconds':round(time.monotonic()-began,3)}
    with (args.output/'acquisition.json').open('x',encoding='utf-8') as handle:json.dump(details,handle,ensure_ascii=False,indent=2)
    print(json.dumps({'output':str(args.output.resolve()),'plan':plan['path'],'tasks':len(ids),
                     'price_data_complete':result['price_data_complete'],'required_bars':result['required_bars'],
                     'acquisition_complete':result['acquisition_complete'],'reported_suspension_bars':result['reported_suspension_bars'],
                     'valid_bars':result['valid_bars'],'missing_field_cells':result['missing_or_conflicting_field_cells'],
                     'elapsed_seconds':details['elapsed_seconds'],'historical_eligibility_verified':False,
                     'corporate_actions_audited':False,'error':error},ensure_ascii=False))
    return 0 if args.plan_only or result['acquisition_complete'] and error is None else 1


if __name__=='__main__':raise SystemExit(main())
