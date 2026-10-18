"""Qlib Alpha158 inference adapter; no implicit approval or trading action.

Official features and the exact trained model are identified by hashes. This
adapter is not an entrypoint: release eligibility is decided separately after
the full-market evaluation, including the requested five-position comparison.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from .daily_store import DailyStore


def model_identity(model_directory):
    directory = Path(model_directory)
    metadata = json.loads((directory / 'model-metadata.json').read_text())
    for name in ('model.txt', 'feature_schema.json'):
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != metadata['sha256'].get(name):
            raise ValueError('模型或特征定义与已验证版本不一致')
    schema = json.loads((directory / 'feature_schema.json').read_text())
    if len(schema['names']) != 158 or len(schema['expressions']) != 158:
        raise ValueError('Alpha158特征定义不完整')
    return metadata, schema


def calculate_signals(dataset_directory, model_directory, session, *, cache_directory=None):
    """Return the complete ranked cross-section for a completed source session.

    Current stock names, news and financial filings are NOT backfilled into
    these features or historical tests. Scores are relative model outputs, not
    probabilities of success or forecasts expressed as return percentages.
    """
    import lightgbm as lgb
    import qlib
    from qlib.data import D
    from qlib.contrib.data.loader import Alpha158DL

    identity, schema = model_identity(model_directory)
    fields, names = Alpha158DL.get_feature_config()
    if fields != schema['expressions'] or names != schema['names']:
        raise ValueError('运行库Alpha158定义与训练定义不一致')
    store = DailyStore(dataset_directory)
    codes = store.codes(session)
    if not codes:
        raise ValueError('该交易日没有沪深A股范围记录')
    model = lgb.Booster(model_file=str(Path(model_directory) / 'model.txt'))
    if model.feature_name() != names:
        raise ValueError('模型特征顺序不一致')
    os.environ.setdefault('MLFLOW_DISABLE_AGENT_HINT', '1')
    # Inference does not create a recorder or rely on a writable MLflow store.
    qlib.init(provider_uri=str(Path(dataset_directory).resolve()), region='cn', kernels=2,
              expression_cache=None, dataset_cache=None)
    frame = D.features(codes, fields, start_time=session, end_time=session, freq='day', disk_cache=0)
    frame.columns = names
    frame = frame.astype('float32').replace([np.inf, -np.inf], np.nan)
    if frame.index.get_level_values('datetime').unique().tolist() != [pd.Timestamp(session)]:
        raise ValueError('模型特征的实际日期不一致')
    quotes, errors = store.cross_section(session)
    observed_codes = quotes.index[quotes['valid_ohlcv']]
    frame = frame.loc[frame.index.get_level_values('instrument').isin(observed_codes)]
    if frame.empty:
        raise ValueError('该日没有量价有效的模型输入')
    scores = model.predict(frame[names].to_numpy(dtype='float32'))
    if not np.isfinite(scores).all():
        raise ValueError('模型输出包含无效数值')
    output = frame.copy()
    output['score'] = scores
    output = output.reset_index().sort_values(['score', 'instrument'], ascending=[False, True], kind='stable')
    output['rank'] = np.arange(1, len(output) + 1)
    output['feature_count'] = output[names].notna().sum(axis=1)
    output = output.set_index('instrument').join(quotes.add_prefix('daily_')).reset_index()
    if cache_directory:
        cache = Path(cache_directory)
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / ('signals-' + session + '-' + identity['sha256']['model.txt'][:16] + '.parquet')
        # The caller owns per-run output. Never overwrite a frozen signal input.
        if target.exists():
            raise FileExistsError('模型信号冻结文件已存在')
        output.to_parquet(target, compression='zstd')
    return output, {'session': session, 'scope_codes': len(codes),
                    'valid_daily_codes': len(observed_codes), 'scored_codes': len(output),
                    'file_errors': errors, 'model': identity,
                    'score_interpretation': '相对模型评分；不是上涨概率、胜率或预期收益百分比',
                    'ordering': 'score descending, code ascending only for exact ties',
                    'official_strategy': 'TopkDropoutStrategy',
                    'execution': '已完成交易日t产生信号，原基准在t+1收盘交易；不能回填t收盘成交'}
