"""Analyze supplied completed OHLC bars without network access or account writes."""
import argparse,json,math
from datetime import date
from pathlib import Path
from tail_screener.kline import review
from tail_screener.kline_report import render

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    try:
        data=json.loads(args.input.read_text(encoding='utf-8'))
        basis=data['price_basis'];unit=data['amount_unit'];fields=data['fields']
        if basis not in ('adjusted','raw') or unit not in ('yuan','thousand_yuan'):raise ValueError('Explicit price_basis adjusted/raw and amount_unit yuan/thousand_yuan required')
        n=len(fields['close'])
        if n<20 or any(len(fields[k])!=n for k in ('open','high','low','close','amount','factor')):raise ValueError('Aligned arrays of at least20bars required')
        fields={k:[float(x) for x in fields[k]] for k in ('open','high','low','close','amount','factor')}
        if basis=='raw':
            for k in ('open','high','low','close'):fields[k]=[x*f for x,f in zip(fields[k],fields['factor'])]
        if unit=='yuan':fields['amount']=[x/1000 for x in fields['amount']]
        sessions=data.get('sessions')
        if sessions is not None:
            if not isinstance(sessions,list) or len(sessions)!=n or any(date.fromisoformat(x).isoformat()!=x for x in sessions) or any(a>=b for a,b in zip(sessions,sessions[1:])):raise ValueError('Sessions must be aligned unique ascending ISOdates')
        result=review(fields);result['input_price_basis']=basis;result['input_amount_unit']=unit
        if sessions is not None:result['sessions']=sessions[-20:]
    except (KeyError,ValueError,TypeError,OSError) as exc:p.error(str(exc))
    args.output.mkdir(parents=True,exist_ok=True)
    if any(args.output.iterdir()):p.error('Output directory must be empty')
    (args.output/'kline.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    (args.output/'index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>K线结构复核</title><body><main style="max-width:900px;margin:30px auto;font-family:sans-serif"><h1>K线结构复核</h1>'+render(result)+'</main></body></html>',encoding='utf-8')
    print(json.dumps({'status':result['status'],'output':str(args.output)},ensure_ascii=False))
    return 0 if result['status']=='computed' else 1
if __name__=='__main__':raise SystemExit(main())
