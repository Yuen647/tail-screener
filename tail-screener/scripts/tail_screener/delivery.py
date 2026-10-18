"""Portable run artifacts, built without business dependencies or account writes."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import zipfile

from .account_options import DEFAULTS


def _canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def build_bundle(output, *, account_dir=None, deadline=None):
    """Export the report's committed journal prefix, never a later live state.

    The recent snapshot retains adjustment anchors for the next invocation.
    HTTP caches, runtimes, unrelated accounts and observation databases are
    deliberately outside this export. No economic or completeness flag changes.
    """
    output=Path(output).resolve();destination=output/'run-bundle.zip'
    if destination.exists():raise ValueError('运行资料包已存在，未覆盖')
    report=json.loads((output/'report.json').read_text(encoding='utf-8'))
    entries={};mapping={};total=0
    def check_time():
        if deadline is not None and time.monotonic()>=deadline:
            raise TimeoutError('运行资料打包预算用尽；原报告和账户保留')
    def add(name,raw,origin=None):
        nonlocal total
        check_time()
        if name in entries:raise ValueError('交付文件名重复：'+name)
        total+=len(raw)
        if total>512*1024*1024:raise ValueError('运行资料超过512MiB交付上限；未截断账户')
        entries[name]=raw
        if origin is not None:mapping[str(origin)]=name
    def read(path):
        path=Path(path)
        if path.is_symlink() or not path.is_file():raise ValueError('交付源文件缺失或为链接：'+path.name)
        if path.stat().st_size>512*1024*1024:raise ValueError('交付源文件过大：'+path.name)
        with path.open('rb') as f:
            chunks=[]
            while True:
                check_time();chunk=f.read(1024*1024)
                if not chunk:break
                chunks.append(chunk)
        return b''.join(chunks)

    for name in ('index.html','report.json'):
        add(name,read(output/name),output/name)
    effective=report.get('effective_configuration',{})
    add('effective-configuration.json',json.dumps(effective,ensure_ascii=False,indent=2).encode())
    parameters=effective.get('parameters',{})
    config={'schema_version':1,'strategy':'ma5-20-account',
            **{key:parameters[key]['value'] for key in DEFAULTS if key in parameters}}
    add('account-config.json',json.dumps(config,ensure_ascii=False,indent=2).encode())
    for name in ('account-record.json','account-state.json','account-observations.json','observations.json','research-simulation.json','research-account-record.json','research-account-state.json','research-account-observations.json','execution.log'):
        if (output/name).is_file():add(name,read(output/name),output/name)
    def export_journal(forward,identity,prefix,folder_override=None):
        committed=forward.get('record')
        if not committed:return {'status':'not_available','reasons':['no_committed_account_in_report']}
        payload=committed['payload'];sequence=payload['sequence']
        if type(sequence) is not int or sequence<0:raise ValueError('账户序号无效')
        if payload['account']['rule_sha256']!=identity:raise ValueError('报告与账户身份不一致')
        folder=Path(folder_override) if folder_override else Path(forward['record_path']).parent
        if folder.is_symlink():raise ValueError('账户交付源目录为链接')
        previous=None;previous_time=None
        for index in range(sequence+1):
            path=folder/f'{index:08d}.json';raw=read(path);item=json.loads(raw);body=item['payload']
            at=datetime.fromisoformat(body['recorded_at'])
            if (body['sequence']!=index or body['previous_sha256']!=previous
                    or _digest(_canonical(body))!=item['sha256'] or at.tzinfo is None
                    or previous_time is not None and at<previous_time):
                raise ValueError('账户哈希链或记录时间无效')
            previous=item['sha256'];previous_time=at
            add(prefix+'/'+path.name,raw,path.resolve())
        if previous!=committed['sha256']:raise ValueError('交付账户终点与本次报告不匹配')
        return {'status':'ready','reasons':[],'account_identity':identity,
                'journal_records':sequence+1,'journal_tip_sha256':previous}

    continuation=export_journal(report.get('forward_account') or {},report.get('account_identity'),'account',account_dir)
    continuation['rule_sha256']=report.get('rule_sha256')
    research=report.get('research_simulation') or {}
    research_continuation=export_journal(research.get('forward_account') or {},research.get('account_identity'),'research-account')
    if continuation['status']=='ready' or research_continuation['status']=='ready':
        if report.get('account_config',{}).get('data_source','recent')=='recent':
            refresh=report.get('daily_refresh') or {};snapshot=refresh.get('snapshot_path')
            if snapshot:
                path=Path(snapshot);raw=read(path);envelope=json.loads(raw)
                digest=_digest(_canonical(envelope['data']))
                if digest!=envelope['sha256'] or digest!=refresh.get('snapshot_sha256'):
                    raise ValueError('近期快照哈希不匹配')
                expected=envelope['data']['sessions'][-1]+'-'+digest+'.json'
                if path.name!=expected:raise ValueError('近期快照文件名不匹配')
                add('data/recent/'+expected,raw,path.resolve())
                add('data/recent/latest.json',json.dumps({'snapshot':expected}).encode())
            else:
                for state in (continuation,research_continuation):
                    if state['status']=='ready':state.update(status='blocked',reasons=['recent_snapshot_missing'])
        else:
            for state in (continuation,research_continuation):
                if state['status']=='ready':state.update(status='requires_external_archive',reasons=['historical_archive_not_exported'])
    continuation['research']=research_continuation

    evidence=output/'disclosure-evidence'
    if evidence.exists():
        if evidence.is_symlink():raise ValueError('公告证据目录为链接')
        for path in sorted(evidence.glob('*.json')):
            raw=read(path)
            if path.stem!=_digest(raw):raise ValueError('公告证据文件哈希不匹配')
            add('disclosure-evidence/'+path.name,raw,path.resolve())
    instructions='''运行资料包

index.html：人类可读报告；report.json：原始结构化结果，均保留原字节。
effective-configuration.json：实际参数与来源；account-config.json：可直接读取的运行参数。
account/：严格账户哈希链；research-account/：独立研究账户哈希链；data/recent/：归一化锚点快照。
disclosure-evidence/：本次目录中保存的公告原响应。完整性状态以report.json为准。

先检查bundle-manifest.json中的continuation。ready表示账户及快照可供同一实现版本接续，
continuation.research单独表示研究账户的接续状态。两套成绩不混合。
不表示行情、策略收益或公告已经全面验证。blocked/not_available时不能承诺对应账户接续。
恢复时保留整个解压目录，使用与rule_sha256完全相同的Skill版本，在技能根目录运行：
python scripts/screen.py --config <解压目录>/account-config.json --account-dir <解压目录>/account --research-account-dir <解压目录>/research-account --data-cache <解压目录>/data/recent --output <新的空报告目录>
archive模式仍需另外提供原数据包的--data-cache路径。不要修改账户记录或倒填订单。
不同实现版本会拒绝接续，应保留原包及原版本，不重置账户冒充延续。

清单包含每个文件的SHA256与长度，可独立校验；这是本地一致性检查，不是外部时间公证。
原文中的旧绝对路径不改写，以保留哈希链；source_path_map给出本包已携带文件的位置。
未包含全市场HTTP响应缓存、Python环境和观察数据库；观察摘要保留在JSON中，
新环境的连续观察记录会重新建立。缺失的旧外部证据路径不能据此认定已完整归档。
'''
    add('USAGE.txt',instructions.encode('utf-8'))
    manifest={'schema_version':1,'kind':'tail_screener_run_bundle',
              'created_at':datetime.now(timezone.utc).isoformat(),'continuation':continuation,
              'run_status':report.get('run_status'), 'source_path_map':mapping,
              'files':{name:{'sha256':_digest(raw),'bytes':len(raw)} for name,raw in entries.items()}}
    entries['bundle-manifest.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode()
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(prefix='.run-bundle-',suffix='.tmp',dir=output,delete=False) as stream:
            temporary=Path(stream.name)
            with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
                for name,raw in entries.items():
                    check_time()
                    with archive.open(name,'w',force_zip64=True) as member:
                        for offset in range(0,len(raw),1024*1024):
                            check_time();member.write(raw[offset:offset+1024*1024])
        check_time();os.link(temporary,destination)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)
    return {'status':'created','path':str(destination),'files':len(entries),
            'bytes':destination.stat().st_size,'sha256':_digest(read(destination)),
            'continuation':continuation}
