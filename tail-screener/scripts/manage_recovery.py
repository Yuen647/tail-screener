"""Inspect/enqueue recovery tasks or import a saved recent-data report offline."""
import argparse
import json
from pathlib import Path
from tail_screener.recovery_queue import RecoveryQueue


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--queue',type=Path,required=True)
    g=p.add_mutually_exclusive_group()
    g.add_argument('--import-report',type=Path)
    g.add_argument('--enqueue',type=Path,help='JSON任务列表：code/start/end/kind/priority')
    p.add_argument('--list',action='store_true')
    args=p.parse_args();q=RecoveryQueue(args.queue)
    if args.import_report:
        report=json.loads(args.import_report.read_text(encoding='utf-8'))
        q.ingest_recent(report['daily_refresh'],args.import_report.parent/'daily-inputs')
    if args.enqueue:
        for spec in json.loads(args.enqueue.read_text(encoding='utf-8')):q.enqueue(**spec)
    print(json.dumps({'summary':q.summary(),**({'tasks':q.tasks()} if args.list else {})},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
