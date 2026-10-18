"""Query saved full candle catalog without rescanning or changing accounts."""
import argparse,json
from pathlib import Path
from tail_screener.kline_catalog import query,GROUPS
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);p.add_argument('--group',choices=list(GROUPS));p.add_argument('--code');p.add_argument('--limit',type=int,default=20)
    args=p.parse_args()
    try:result=query(json.loads(args.report.read_text(encoding='utf-8')),args.group,args.code,args.limit)
    except (ValueError,OSError,KeyError,TypeError) as e:p.error(str(e))
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    return 0 if result['status']=='complete' else 1
if __name__=='__main__':raise SystemExit(main())
