"""Rebuild old research candidate scenarios in independent, non-live artifacts."""
import argparse
from datetime import datetime
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshots',type=Path,nargs='+',required=True);p.add_argument('--forecasts',type=Path,nargs='+',required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--action-cache',type=Path,required=True)
    p.add_argument('--seed-actions',type=Path);p.add_argument('--budget',type=int,default=300)
    p.add_argument('--costs',type=int,nargs='+',default=[1,2],choices=(1,2));p.add_argument('--offline',action='store_true')
    args=p.parse_args()
    if args.output.exists() and any(args.output.iterdir()):p.error('输出目录须为空')
    if not 1<=args.budget<=1800:p.error('预算须在1至1800秒之间')
    from tail_screener.legacy_reconstruction import read,file_sha,merge_history,reconstruct,write_demand
    from tail_screener.corporate_actions import collect_actions,parse_implementation
    from tail_screener.providers import PublicSources
    from tail_screener.calendar import TZ
    from tail_screener.account_strategy import rule_identity
    snapshot=merge_history(args.snapshots);identity=rule_identity()
    args.output.mkdir(parents=True,exist_ok=True);args.action_cache.mkdir(parents=True,exist_ok=True)
    sources={str(f.resolve()):file_sha(f) for f in args.forecasts+args.snapshots}
    registration={'rule_sha256':identity,'generated_at':datetime.now(TZ).isoformat(),'sources':sources,
                  'costs':args.costs,'data_class':'retrospective_development','independent_holdout':False}
    (args.output/'registration.json').write_text(json.dumps(registration,indent=2),encoding='utf-8')
    (args.output/'snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    source=PublicSources(args.action_cache/'http',budget=args.budget,workers=1)
    packages={};evidence={};results={};input_complete={}
    def loader(code,start,end):
        key=f'{code}-{start}-{end}';path=args.action_cache/(key+'.json')
        if key not in packages:
            if path.exists():package=read(path)
            elif args.seed_actions and (args.seed_actions/(code+'.json')).exists():
                package=read(args.seed_actions/(code+'.json'))
                if package['start']>start or package['end']<end:raise ValueError('预置事件范围不足')
            elif args.offline:raise ValueError('离线公司行动证据缺失：'+key)
            else:package=collect_actions(source,code,start,end,datetime.now(TZ),args.action_cache/'evidence'/code)
            # Reparse saved full bodies with this build; an earlier parser's
            # supported flag cannot establish present compatibility.
            actions=[];errors=[e for e in package.get('errors',[]) if 'ex_session' not in e]
            for event in package.get('events',[]):
                if 'document' not in event:
                    errors.append({'ex_session':event['ex_session'],'reason':'body_missing'});event['status']='unresolved';continue
                try:
                    action=parse_implementation(event,event['document']);event.update(action=action,status='distribution_model_supported');actions.append(action)
                except ValueError as exc:
                    event.update(status='unresolved',error={'reason':str(exc)});errors.append({'ex_session':event['ex_session'],'reason':str(exc)})
            package.update(actions=actions,errors=errors,status='incomplete' if errors else 'complete',parser_rule_sha256=identity)
            packages[key]=package
            target=args.output/'action-evidence'/(key+'.json');target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(package,ensure_ascii=False,indent=2),encoding='utf-8');evidence[key]=file_sha(target)
            if not path.exists():path.write_text(json.dumps(package,ensure_ascii=False),encoding='utf-8')
            print('actions',key,len(actions),'unresolved',len(errors),flush=True)
        return packages[key]
    try:
        for path in args.forecasts:
            forecasts=read(path)
            for cost in args.costs:
                result=reconstruct(forecasts,snapshot,loader,cost_multiplier=cost)
                name=path.stem.removesuffix('-forecasts')+f'-cost{cost}'
                result.update(source_forecast_sha256=sources[str(path.resolve())],action_evidence_files=evidence.copy())
                (args.output/(name+'.json')).write_text(json.dumps(result,ensure_ascii=False,allow_nan=False),encoding='utf-8')
                write_demand(result,args.output/(name+'-missing.csv'));results[name]=result['metrics'];input_complete[name]=result['input_complete']
                print(name,json.dumps(result['metrics'],ensure_ascii=False),flush=True)
    finally:source.close()
    if any(file_sha(Path(path))!=sha for path,sha in sources.items()) or identity!=rule_identity():raise ValueError('运行期间输入或代码发生变化')
    summary={'rule_sha256':identity,'results':results,'input_complete':input_complete,'recommendation_eligible':False,'current_strategy_validated':False}
    (args.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if results and all(input_complete.values()) and all(r['valuation_complete'] for r in results.values()) else 1


if __name__=='__main__':raise SystemExit(main())
