"""Dependency-free configuration for the default account CLI."""
import argparse
import json
from pathlib import Path

from .account_config import account_config

DEFAULTS = {'capital': '100000', 'max_positions': 6, 'max_holding_sessions': 5,
            'max_entry_gap_pct': '3', 'max_ma20_premium_pct': '8', 'top': 6, 'research_size': 6,
            'mode': 'auto', 'data_source': 'recent', 'daily_update_budget': 600,
            'run_budget': 600, 'daily_workers': 12}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('配置字段重复：' + key)
        result[key] = value
    return result


def parse_account_args(argv=None):
    parser = argparse.ArgumentParser(description='沪深A股短线研究与条件化模拟账户')
    parser.add_argument('--strategy', choices=['ma5-20-account'], default='ma5-20-account')
    parser.add_argument('--config', type=Path, help='默认账户JSON配置；命令行覆盖文件，未知字段报错')
    parser.add_argument('--output')
    parser.add_argument('--data-cache', type=Path)
    parser.add_argument('--source-cache', type=Path, help='独立公共响应缓存；空目录可用于首次运行验收')
    parser.add_argument('--account-dir', type=Path)
    parser.add_argument('--research-account-dir', type=Path, help='独立研究模拟账户；不与严格策略账户混用')
    for name in DEFAULTS:
        kwargs = {'default': argparse.SUPPRESS}
        if name in ('max_positions', 'max_holding_sessions', 'top', 'research_size', 'daily_update_budget', 'run_budget', 'daily_workers'):
            kwargs['type'] = int
        if name == 'mode': kwargs['choices'] = ['auto', 'live', 'post-close']
        if name == 'data_source': kwargs['choices'] = ['recent', 'archive']
        parser.add_argument('--' + name.replace('_', '-'), **kwargs)
    args = parser.parse_args(argv)
    values = dict(DEFAULTS)
    origins = {key: 'default' for key in values}
    try:
        if args.config:
            document = json.loads(args.config.read_text(encoding='utf-8-sig'),
                                  object_pairs_hook=_unique_object,
                                  parse_constant=lambda x: (_ for _ in ()).throw(ValueError('非有限数值：'+x)))
            if not isinstance(document, dict): raise ValueError('配置必须是JSON对象')
            if type(document.get('schema_version')) is not int or document['schema_version'] != 1:
                raise ValueError('配置schema_version必须为整数1')
            if document.get('strategy', args.strategy) != args.strategy:
                raise ValueError('配置strategy与当前策略不一致')
            unknown = set(document) - set(DEFAULTS) - {'schema_version', 'strategy'}
            if unknown: raise ValueError('当前账户不支持配置字段：' + ', '.join(sorted(unknown)))
            for key in DEFAULTS:
                if key in document:
                    values[key] = document[key]
                    origins[key] = 'config:' + str(args.config.resolve())
        for key in DEFAULTS:
            if hasattr(args, key):
                values[key] = getattr(args, key)
                origins[key] = 'command_line'
        for key, lo, hi in [('top', 1, 50), ('daily_update_budget', 30, 1800),
                            ('research_size', 1, 50),
                            ('run_budget', 30, 1800), ('daily_workers', 1, 16),
                            ('max_positions', 1, 50), ('max_holding_sessions', 1, 60)]:
            if type(values[key]) is not int or not lo <= values[key] <= hi:
                raise ValueError(f'{key}须为{lo}至{hi}的整数')
        if values['mode'] not in ('auto', 'live', 'post-close'): raise ValueError('mode无效')
        if values['data_source'] not in ('recent', 'archive'): raise ValueError('data_source无效')
        account_config(values['capital'], values['max_positions'], values['max_holding_sessions'],
                       max_entry_gap_pct=values['max_entry_gap_pct'],
                       max_ma20_premium_pct=values['max_ma20_premium_pct'])
    except (ValueError, TypeError, OSError, ArithmeticError) as exc:
        parser.error(str(exc))
    for key, value in values.items(): setattr(args, key, value)
    args.effective_configuration = {
        'schema_version': 1, 'strategy': args.strategy,
        'parameters': {key: {'value': value, 'source': origins[key]} for key, value in values.items()},
        'paths': {key: str(getattr(args, key)) if getattr(args, key) is not None else None
                  for key in ('output', 'data_cache', 'source_cache', 'account_dir', 'research_account_dir')},
        'precedence': ['command_line', 'config', 'default']}
    return args
