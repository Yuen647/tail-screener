"""Repair saved paper-account valuations into a separate research artifact."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    parser=argparse.ArgumentParser(description='用补充的免费公司行动证据重放原事前模拟计划；不修改原账户')
    parser.add_argument('--account-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--budget',type=int,default=600)
    args=parser.parse_args(argv)
    if not 30<=args.budget<=1800:parser.error('--budget须为30至1800秒')
    if args.output.exists() and any(args.output.iterdir()):parser.error('--output须为新目录或空目录')
    from tail_screener.runtime import prepare_runtime
    prepared=prepare_runtime(Path(__file__).resolve().parents[1],argv,entrypoint=Path(__file__).resolve())
    if prepared is not None:return prepared
    from tail_screener.account_journal import TZ
    from tail_screener.account_replay import replay_journal
    from tail_screener.corporate_actions import collect_actions
    from tail_screener.providers import PublicSources
    args.output.mkdir(parents=True,exist_ok=True)
    source=PublicSources(args.output/'source-cache',budget=args.budget,workers=2)
    try:
        # Replay artifacts go in a child directory because source evidence is
        # retained even if input validation or later processing fails.
        result=replay_journal(args.account_dir,args.output/'reconstruction',
            lambda code,start,end:collect_actions(source,code,start,end,datetime.now(TZ),args.output/'evidence'))
        print(json.dumps({'output':str(args.output.resolve()),'metrics':result['metrics']},ensure_ascii=False))
        return 0 if result['metrics']['valuation_complete'] else 1
    finally:source.close()

if __name__=='__main__':raise SystemExit(main())
