"""Dependency-free supervision of a single default screening invocation."""
import argparse
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

from tail_screener.account_options import parse_account_args
from tail_screener.paths import data_root
from tail_screener.delivery import build_bundle

MARKER = 'TAIL_SCREENER_SUPERVISED'
DEADLINE = 'TAIL_SCREENER_DEADLINE'


def terminate_tree(process):
    """Terminate only the process group/tree created for this invocation."""
    if os.name == 'nt':
        result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        if result.returncode and process.poll() is None:
            raise RuntimeError('无法确认本次子进程树已终止')
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=5)


def wait_for_child(command, environment, timeout, log=None):
    options = {'env': dict(environment, PYTHONUTF8='1', PYTHONUNBUFFERED='1'),
               'stdout':subprocess.PIPE,'stderr':subprocess.STDOUT,
               'text':True,'encoding':'utf-8','errors':'replace'}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options['start_new_session'] = True
    process = subprocess.Popen(command, **options)
    def relay():
        try:
            for line in process.stdout:
                if log is not None:
                    log.write(line);log.flush()
                print(line,end='',flush=True)
        finally:
            process.stdout.close()
    reader=threading.Thread(target=relay,daemon=True)
    reader.start()
    try:
        return process.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        terminate_tree(process)
        return 2, True
    except BaseException:
        terminate_tree(process)
        raise
    finally:
        reader.join(timeout=2)


def failure_report(output, reason, message, elapsed, configuration):
    # An interrupted publication is evidence, not a finished report. Preserve
    # its bytes before writing the supervisor's explicitly failed artifact.
    for name in ('report.json', 'index.html'):
        existing = output/name
        if existing.exists():
            existing.rename(output/('interrupted-'+name))
    progress = None
    try:
        progress = json.loads((output/'daily-inputs/progress.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass
    report = {'version':'3.2.0', 'generated_at':datetime.now(timezone.utc).isoformat(),
              'run_status':{'execution_status':'failed','data_status':'failed','reasons':[reason]},
              'requested_session':(progress or {}).get('requested_session'), 'data_session':None, 'candidates':[], 'stats':{},
              'warnings':[message], 'data_issues':[{'code':reason,'message':message}],
              'recommendation_eligible':False, 'effective_configuration':configuration,
              'elapsed_seconds':round(elapsed,3),'last_progress':progress,
              'research_simulation':{'status':'not_verified_after_interruption',
                  'target_size':configuration.get('parameters',{}).get('research_size',{}).get('value',6),
                  'candidates':[],'reason':'run_interrupted'},
              'forward_account':{'status':'not_verified_after_interruption'}}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    content = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>筛选未完成</title><style>body{font:18px/1.8 sans-serif;max-width:850px;margin:40px auto;padding:20px}h1{color:#a32a22}pre{white-space:pre-wrap}</style><h1>本次筛选未完成</h1><p>'+html.escape(message)+'</p><p>未形成完整观察名单，不能据此冻结新模拟计划。已采集输入保留在本次目录和缓存中；账户是否已写入须核验原记录，不据中断状态推断。</p>'
    if progress:
        content += '<h2>最后保存的日线输入进度</h2><p>已核验 '+str(progress.get('validated_inputs',0))+' 只；这不是入选数量。</p>'
    content += '<p>模拟资金与运行配置见 <a href="report.json">结构化结果</a>。下次在同一可写存储中运行可复用仍有效的缓存。</p></html>'
    (output/'index.html').write_text(content,encoding='utf-8')


def supervise(root, argv):
    """Return None for auxiliary/legacy commands, otherwise an exit status."""
    if os.environ.get(MARKER) == '1':
        return None
    if any(a.split('=')[0] in ('--help','-h','--doctor','--doctor-offline','--restore-only','--replay') for a in argv):
        return None
    routing = argparse.ArgumentParser(add_help=False)
    routing.add_argument('--strategy', default='ma5-20-account')
    selected, _ = routing.parse_known_args(argv)
    if selected.strategy != 'ma5-20-account':
        return None
    args = parse_account_args(argv)  # Reject invalid settings before any mutation.
    began = time.monotonic()
    output = (Path(args.output) if args.output else data_root(root)/'reports'/datetime.now().strftime('account_%Y%m%d_%H%M%S_%f')).resolve()
    output.mkdir(parents=True,exist_ok=True)
    if any(output.iterdir()):
        raise ValueError('输出目录须为空，历史报告不覆盖')
    environment = os.environ.copy()
    environment[MARKER] = '1'
    # Leave a small publication reserve inside the requested wall-time budget.
    environment[DEADLINE] = str(began+max(1,args.run_budget-5))
    command = [sys.executable,str(root/'scripts/screen.py'),*argv,'--output',str(output)]
    print(f'本次调用总预算 {args.run_budget} 秒（含环境准备）；日线并发 {args.daily_workers}。首次无缓存需逐股采集，超时将交付未完成报告。',flush=True)
    code = 2; timed_out = False
    log_path = None
    try:
        # Keep the output directory empty until the worker validates it. The
        # live execution transcript lives beside it, then moves into the result.
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='.screen-',suffix='.log',dir=output.parent,delete=False) as log:
            log_path=Path(log.name)
            code,timed_out = wait_for_child(command,environment,max(1,args.run_budget-3),log)
        if timed_out:
            failure_report(output,'run_budget_exhausted','本次调用达到总时限，采集或处理未全部完成；未取得完整全市场筛选结果。',time.monotonic()-began,args.effective_configuration)
        elif not (output/'report.json').is_file() or not (output/'index.html').is_file():
            if code == 0:code = 2
            failure_report(output,'child_exit_without_report',f'执行进程退出（退出码 {code}），尚未生成完整报告。请查看同次执行的环境诊断和采集输出。',time.monotonic()-began,args.effective_configuration)
    except (OSError,RuntimeError,subprocess.SubprocessError) as exc:
        failure_report(output,'supervision_failed',f'运行控制失败（{type(exc).__name__}）；本次数据和账户状态尚未核验。',time.monotonic()-began,args.effective_configuration)
        code = 2
    finally:
        if log_path is not None and log_path.exists():
            log_path.replace(output/'execution.log')
    control = {'budget_seconds':args.run_budget,'elapsed_seconds':round(time.monotonic()-began,3),
               'timed_out':timed_out,'exit_code':code,'includes_environment_preparation':True}
    try:
        control['delivery']=build_bundle(output,account_dir=args.account_dir,deadline=began+args.run_budget)
    except (OSError,ValueError,KeyError,TypeError,TimeoutError) as exc:
        control['delivery']={'status':'failed','reason':str(exc)}
        if code==0:code=1
    control.update(exit_code=code,elapsed_seconds=round(time.monotonic()-began,3))
    (output/'run-control.json').write_text(json.dumps(control,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'html':str(output/'index.html'),'report':str(output/'report.json'),'run_control':control},ensure_ascii=False),flush=True)
    return code
