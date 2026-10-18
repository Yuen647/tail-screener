"""Small stdlib bootstrap: diagnostics work even before dependencies are installed."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from .paths import data_root
from .strategy_status import DEFAULT_STRATEGY


def main(argv=None, *, startup_exit_code=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    skill_root = Path(__file__).resolve().parents[2]
    if '--help' in argv or '-h' in argv:
        parser = argparse.ArgumentParser(description='沪深A股作者策略研究；自动准备环境，按策略确认时点与动态持有规则运行')
        parser.add_argument('--strategy', choices=('ma5-20-account','minervini-trend','connors-rsi2','legacy-tail'), default=DEFAULT_STRATEGY,
                            help='默认ma5-20-account：六席位严格账户及独立六股研究模拟；其他保留显式研究')
        parser.add_argument('--config', help='所选策略的JSON配置；默认账户使用schema_version=1，命令行优先，未知字段报错')
        parser.add_argument('--top', type=int, help='严格候选展示名额，默认6；排名边界精确并列时全部展示')
        parser.add_argument('--research-size', type=int, help='独立研究篮子名额，默认6；不足时补入标注的规则对照')
        parser.add_argument('--research-account-dir', help='独立研究模拟账户目录，与严格账户分开')
        parser.add_argument('--output', help='新输出目录')
        parser.add_argument('--data-cache', help='所选数据模式的缓存位置；默认自动管理')
        parser.add_argument('--data-source', choices=('recent','archive'), help='默认recent近期日线；archive使用完整历史包')
        parser.add_argument('--account-dir', help='仅ma5-20-account：独立模拟账户目录；默认自动维护')
        parser.add_argument('--daily-update-budget', type=int, help='仅ma5-20-account：日线补齐秒数预算，默认600')
        parser.add_argument('--run-budget', type=int, help='默认账户：整次调用总预算秒数，默认600，含环境准备；超时交付失败报告')
        parser.add_argument('--daily-workers', type=int, help='默认账户：日线滚动并发，默认12，范围1至16')
        parser.add_argument('--source-cache', help='默认账户：公共响应缓存目录；独立空目录用于无缓存验收')
        parser.add_argument('--capital', help='初始模拟资金，默认100000元')
        parser.add_argument('--max-positions', type=int, help='最多同时持仓数，默认6')
        parser.add_argument('--max-holding-sessions', type=int, help='含买入日的持有交易日上限，默认5；受阻退出延后')
        parser.add_argument('--max-entry-gap-pct', help='相对信号收盘的入场涨幅上限%%，默认3')
        parser.add_argument('--max-ma20-premium-pct', help='相对MA20的溢价上限%%，默认8')
        parser.add_argument('--mode', choices=('auto','live','post-close'), help='默认auto：盘后自动采集最新完整交易日并做观察筛选，无需旧快照')
        parser.add_argument('--replay', help='冻结快照离线回放，不联网、不产生实时入场资格')
        parser.add_argument('--rerank', action='store_true', help='仅配合作者策略--replay，对原冻结输入做当前排序对照')
        parser.add_argument('--doctor', action='store_true', help='自动准备环境并诊断样例接口')
        parser.add_argument('--doctor-offline', action='store_true', help='诊断现有环境，不安装、不联网')
        parser.print_help()
        return 0
    # Validate the default account before installing dependencies or contacting sources.
    if not any(arg in argv for arg in ('--doctor', '--doctor-offline')):
        routing = argparse.ArgumentParser(add_help=False)
        routing.add_argument('--strategy', default=DEFAULT_STRATEGY)
        routing.add_argument('--replay')
        selected, _ = routing.parse_known_args(argv)
        if selected.strategy == 'ma5-20-account' and not selected.replay:
            from .account_options import parse_account_args
            parse_account_args(argv)
    if '--doctor-offline' not in argv:
        from .runtime import prepare_runtime
        prepared = prepare_runtime(skill_root, argv, offline=any(arg == '--replay' or arg.startswith('--replay=') for arg in argv))
        if prepared is not None:
            if prepared == 4:
                main(['--doctor-offline'], startup_exit_code=prepared)
            return prepared
    if '--doctor' in argv or '--doctor-offline' in argv:
        parser = argparse.ArgumentParser(description='检查运行环境、交易日历与数据源，不做选股')
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--doctor', action='store_true', help='检查依赖、日历和少量真实接口')
        group.add_argument('--doctor-offline', action='store_true', help='仅检查依赖和日历，不访问网络')
        parser.add_argument('--output', type=Path, help='诊断输出目录；拒绝覆盖已有 diagnostics.json')
        args = parser.parse_args(argv)
        from .doctor import run_doctor
        report = run_doctor(network=not args.doctor_offline)
        # A standalone offline inspection never proves that installation ran.
        # When called after a real startup failure, retain that distinct fact.
        report['startup_exit_code'] = startup_exit_code
        directory = args.output or data_root(skill_root)/'reports'/datetime.now(timezone.utc).strftime('diagnostics_%Y%m%d_%H%M%S_%f')
        directory.mkdir(parents=True, exist_ok=True)
        target = directory/'diagnostics.json'
        with target.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(json.dumps({'diagnostics': str(target.resolve()), **report}, ensure_ascii=False, indent=2))
        return 0 if report.get('status') in ('ok', 'passed', 'offline_passed') else 1
    try:
        routing = argparse.ArgumentParser(add_help=False)
        routing.add_argument('--strategy', default=DEFAULT_STRATEGY)
        routing.add_argument('--replay')
        selected, _ = routing.parse_known_args(argv)
        if selected.strategy == 'ma5-20-account' and not selected.replay:
            from .account_runner import main as screen
        elif selected.strategy == 'minervini-trend' and not selected.replay:
            from .trend_runner import main as screen
        else:
            from .runner import main as screen
    except (ImportError, ModuleNotFoundError) as exc:
        print(f'自动准备后仍无法加载筛选依赖（{type(exc).__name__}），本次没有启动扫描。技能将保留诊断结果。', file=sys.stderr)
        main(['--doctor-offline'], startup_exit_code=4)
        return 4
    report = screen(argv)
    status = report.get('run_status', {})
    if status.get('execution_status') == 'failed':
        return 2
    return {'complete': 0, 'incomplete': 1, 'failed': 2}.get(status.get('data_status'), 1)
