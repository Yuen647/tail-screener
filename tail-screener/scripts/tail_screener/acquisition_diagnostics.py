"""Explain this scan's observed failures without running another network sample."""
from collections import Counter

from .engine import stamp
from .run_status import describe_run


def summarize_collection(snapshot, config):
    rows=snapshot.get('source_diagnostics',[])
    counts=Counter()
    for row in rows:
        counts[row.get('category','unknown')]+=row.get('attempts',0)
    errors=snapshot.get('errors',{})
    meta=snapshot.get('universe_metadata',{})
    validation=describe_run(snapshot,config,stamp(snapshot['as_of']),0)
    complete=validation['data_status']=='complete'
    reasons=list(validation['reasons'])
    if counts['timeout']:
        reasons.append(f"本次记录 {counts['timeout']} 次请求超时、{counts['ok']} 次成功响应；超时不等于接口永久不可用。")
    if counts['budget_expired']:
        reasons.append('部分请求因采集总预算用尽而未执行。')
    if meta.get('catalog_complete') is not True:
        reasons.append('股票目录完整性未通过；目录内报价比例不能代替全市场覆盖率。')
    for stage, failures in errors.items():
        if failures:
            reasons.append(f'{stage} 阶段仍有 {len(failures)} 项采集缺项。')
    return {'version':2,'mode':'actual_collection','generated_at':snapshot['as_of'],
            'status':'complete' if complete else 'incomplete',
            'summary':'基于本次实际请求与横截面核验的诊断；个股证据仍由筛选规则另行核验，样例探测不能替代实际采集。',
            'request_categories':dict(counts),'sources':rows,
            'budget_seconds':config['budget_seconds'],
            'elapsed_seconds':snapshot.get('stats',{}).get('elapsed_seconds'),
            'stage_seconds':snapshot.get('stage_seconds',{}),
            'remaining_failures':{key:len(value) for key,value in errors.items()},
            'catalog_complete':meta.get('catalog_complete'),
            'reasons':reasons,
            'action':('采集完成；按研究规则核验结果。' if complete else
                      '保留本次诊断、快照和报告；根据具体接口及错误类别检查恢复记录。数据缺项不产生买入资格。')}
