"""Register or execute a reproducible default-account development experiment."""
import argparse
import json
from pathlib import Path


def main():
    p=argparse.ArgumentParser(description=__doc__)
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--register',type=Path,help='实验定义JSON；只登记，不计算结果')
    g.add_argument('--registration',type=Path,help='已登记文件')
    p.add_argument('--registry',type=Path)
    p.add_argument('--snapshot',type=Path)
    p.add_argument('--coverage',type=Path)
    p.add_argument('--output',type=Path)
    args=p.parse_args()
    if args.register and not args.registry:p.error('--register需要--registry')
    if args.registration and any(getattr(args,key) is None for key in ('snapshot','coverage','output')):
        p.error('--registration需要--snapshot、--coverage和--output')
    from tail_screener.account_validation import register_experiment,validate_account
    read=lambda path:json.loads(path.read_text(encoding='utf-8'))
    if args.register:
        print(register_experiment(read(args.register),args.registry));return
    result=validate_account(read(args.registration),read(args.snapshot),read(args.coverage))
    args.output.mkdir(parents=True,exist_ok=True)
    target=args.output/'validation.json'
    with target.open('x',encoding='utf-8') as handle:json.dump(result,handle,ensure_ascii=False,indent=2,allow_nan=False)
    print(json.dumps({'result':str(target.resolve()),'metrics':result['metrics'],'recommendation_eligible':False},ensure_ascii=False))


if __name__=='__main__':main()
