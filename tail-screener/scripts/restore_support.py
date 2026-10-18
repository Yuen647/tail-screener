"""Prepare embedded rules and optionally restore archived research evidence."""
from __future__ import annotations
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import runpy
import shutil
import tarfile
import tempfile
import time


PORTABLE_FILES = {'SKILL.md': 'utf8-newlines-v1',
                  'config.json': 'json-layout-v1',
                  'configs/account-config.json': 'json-layout-v1'}
HOST_METADATA = frozenset(('SKILL.md', 'agents/openai.yaml'))
RUNTIME_REFERENCES = ('references/account-workflow/protocol.json',
                      'references/account-workflow/pre-evaluation-affordability-clarification.json')


def host_metadata_files(manifest):
    """Only an explicit versioned release policy permits non-runtime metadata."""
    policy = manifest.get('runtime_policy')
    if policy is None: return frozenset()  # Older releases keep their own contract.
    if (not isinstance(policy, dict) or policy.get('schema_version') != 1
            or type(policy.get('schema_version')) is not int
            or set(policy) != {'schema_version', 'host_metadata'}
            or not isinstance(policy['host_metadata'], list)
            or len(policy['host_metadata']) != len(HOST_METADATA)
            or any(not isinstance(name, str) for name in policy['host_metadata'])
            or set(policy['host_metadata']) != HOST_METADATA
            or not HOST_METADATA.issubset(manifest['files'])):
        raise ValueError('运行校验边界无效：只能将技能说明和可选界面元数据标记为宿主管理')
    return HOST_METADATA


def portable_bytes(raw, mode):
    """Normalize declared presentation changes only; never trading parameters."""
    text = raw.decode('utf-8-sig')
    if mode == 'utf8-newlines-v1':
        return text.replace('\r\n', '\n').replace('\r', '\n').encode('utf-8')
    if mode == 'json-layout-v1':
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError('JSON字段重复：' + key)
                result[key] = value
            return result
        def invalid(value): raise ValueError('JSON非有限数值：' + value)
        value = json.loads(text, object_pairs_hook=unique, parse_constant=invalid)
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    raise ValueError('未知的格式兼容校验方式')


def portability_entry(relative, raw):
    mode = PORTABLE_FILES[relative]
    return {'normalization': mode, 'sha256': hashlib.sha256(portable_bytes(raw, mode)).hexdigest()}


def _checked_bytes(root, relative, expected, portable):
    raw = safe_target(root, relative).read_bytes()
    if hashlib.sha256(raw).hexdigest() == expected: return raw
    entry = portable.get(relative)
    if entry and PORTABLE_FILES.get(relative) == entry.get('normalization'):
        normalized = portable_bytes(raw, entry['normalization'])
        if hashlib.sha256(normalized).hexdigest() == entry.get('sha256'):
            return raw
    raise ValueError('文件内容与发布清单不符：' + relative)


def _compare_release(root, *, strict_release=False):
    """Compare installed resources with a release; only publication is gated."""
    root = Path(root).resolve()
    manifest = json.loads((root/'PACKAGE-MANIFEST.json').read_text(encoding='utf-8-sig'))
    files = manifest.get('files')
    if not isinstance(files, dict) or not files: raise ValueError('安装清单缺少文件表')
    portable = manifest.get('portable_files', {})
    if (not isinstance(portable, dict) or set(portable)-set(files)
            or set(portable)-set(PORTABLE_FILES)):
        raise ValueError('安装清单格式兼容范围无效')
    host_files = host_metadata_files(manifest)
    issues, adjusted, metadata_changes = [], [], []
    for relative, expected in files.items():
        try:
            raw = _checked_bytes(root, relative, expected, {} if strict_release else portable)
            if hashlib.sha256(raw).hexdigest() != expected: adjusted.append(relative)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            item = {'path': relative, 'reason': str(exc), 'error_type': type(exc).__name__}
            if not strict_release and relative in host_files:
                item['status'] = 'missing' if isinstance(exc, FileNotFoundError) else 'different'
                metadata_changes.append(item)
            else: issues.append(item)
    if issues and strict_release:
        raise ValueError('安装包校验失败：' + json.dumps(issues, ensure_ascii=False)
                         + '；开发发布包必须与源文件一致。')
    return {'status': 'warning' if issues or metadata_changes else 'passed',
            'scope': 'release' if strict_release else 'release_comparison',
            'blocks_execution': bool(strict_release and issues),
            'verified_files': len(files)-len(issues)-len(metadata_changes)-len(adjusted),
            'release_bytes_match': not issues and not adjusted and not metadata_changes,
            'issues': issues, 'format_adjusted_files': adjusted,
            'host_metadata_changes': metadata_changes}


def verify_package(root, *, strict_release=False):
    """A missing/rewritten release manifest is advisory during normal execution.

    Python imports, dependency checks and actual input/evidence validation decide
    whether the request can run. They never borrow validity from this comparison.
    """
    if strict_release:return _compare_release(root,strict_release=True)
    try:return _compare_release(root)
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        return {'status':'warning','scope':'release_comparison','blocks_execution':False,
                'verified_files':0,'release_bytes_match':False,'format_adjusted_files':[],
                'host_metadata_changes':[], 'issues':[{'path':'PACKAGE-MANIFEST.json',
                    'reason':str(exc),'error_type':type(exc).__name__}]}


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def safe_target(root,relative):
    p=PurePosixPath(relative)
    if not isinstance(relative,str) or not p.parts or p.is_absolute() or '..' in p.parts or '\\' in relative:
        raise ValueError('资料清单包含非法路径')
    target=root.joinpath(*p.parts)
    for parent in [target,*target.parents]:
        if parent==root:break
        if parent.is_symlink():raise ValueError('资料路径包含符号链接，未写入')
    if not target.resolve().is_relative_to(root.resolve()):raise ValueError('资料路径超出技能目录')
    return target


@contextmanager
def writer_lock(root):
    lock=root/'.support-restore.lock';deadline=time.monotonic()+60
    while True:
        try:
            descriptor=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'w') as f:f.write(str(os.getpid()))
            break
        except FileExistsError:
            # Supervised timeouts can interrupt restoration. Reclaim only a
            # verifiably dead owner; unknown/active owners remain protected.
            from tail_screener.runtime import _process_running
            try:
                owner=lock.read_text(encoding='utf-8')
                if not _process_running(int(owner)) and lock.read_text(encoding='utf-8')==owner:
                    lock.unlink();continue
            except (OSError,ValueError):
                pass
            if time.monotonic()>=deadline:raise RuntimeError('资料还原锁仍被占用；原文件未覆盖，请检查是否有其他调用正在还原')
            time.sleep(.1)
    try:yield
    finally:lock.unlink()


def _shell_snapshot(root):
    """Inventory actual installed files; a publisher manifest is not required."""
    directories=('scripts','assets','configs')
    ordinary={'SKILL.md','agents/openai.yaml','config.json','requirements.txt',
        'requirements-qlib.txt','screen.cmd','install_skill.py','package_skill.py',
        'skill-install/SKILL.md.template','PACKAGE-MANIFEST.json',
        'support-files.json','support-files.tar.xz','docs/environment-setup.md',
        'docs/usage.md','docs/research-tools.md','references/stock-trend-template-source.json',
        'examples/history-scope.json',
        *RUNTIME_REFERENCES}
    files={}
    def record(path):
        if path.is_symlink() or not path.is_file():return
        relative=path.relative_to(root).as_posix()
        try:files[relative]=sha(safe_target(root,relative))
        except ValueError:
            if relative not in HOST_METADATA:raise
    for relative in sorted(ordinary):record(root/relative)
    for folder in directories:
        for directory,children,names in os.walk(root/folder,followlinks=False):
            children[:]=sorted(name for name in children if name!='__pycache__'
                               and not (Path(directory)/name).is_symlink())
            for name in sorted(names):
                if not name.endswith(('.pyc','.pyo')):record(Path(directory)/name)
    return files


def _copy_shell(source,destination,files=None):
    """Copy a consistent installed image, without demanding original release bytes."""
    files=_shell_snapshot(source) if files is None else files
    for relative,expected in files.items():
        if relative in HOST_METADATA:continue
        target=safe_target(destination,relative)
        if target.exists() and sha(target)!=expected:
            raise ValueError('可写运行副本与当前安装文件不同，未覆盖：'+relative)
    for relative,expected in files.items():
        if relative in HOST_METADATA:
            try:path=safe_target(source,relative);target=safe_target(destination,relative)
            except ValueError:continue
            if target.exists():continue
        else:path=safe_target(source,relative);target=safe_target(destination,relative)
        if target.exists():continue
        target.parent.mkdir(parents=True,exist_ok=True)
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
                temporary=Path(handle.name)
                with path.open('rb') as origin:shutil.copyfileobj(origin,handle)
            if sha(temporary)!=expected:raise ValueError('复制期间安装文件发生变化：'+relative)
            try:os.link(temporary,target)
            except FileExistsError:
                if relative not in HOST_METADATA and sha(target)!=expected:
                    raise ValueError('运行副本并发写入冲突：'+relative)
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)


def _commit_staged(source, target):
    """Commit verified staging bytes without requiring the same disk as TEMP."""
    try:
        os.link(source,target)
        return
    except OSError as exc:
        if exc.errno!=errno.EXDEV:raise
    # Use a short temporary basename beside the destination, then link only
    # complete bytes. A failed copy never leaves a partially written final file.
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent,delete=False) as handle:
            temporary=Path(handle.name)
            with source.open('rb') as origin:shutil.copyfileobj(origin,handle)
        os.link(temporary,target)
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def _restore(root):
    index=json.loads((root/'support-files.json').read_text(encoding='utf-8'))
    files=index.get('files',{})
    if index.get('schema_version')!=1 or not isinstance(files,dict) or not 1<=len(files)<=4096:
        raise ValueError('资料清单版本或数量无效')
    total=0
    for relative,entry in files.items():
        safe_target(root,relative)
        if PurePosixPath(relative).parts[0] not in ('references','examples'):raise ValueError('资料范围无效')
        if type(entry.get('bytes')) is not int or entry['bytes']<0:raise ValueError('资料长度无效')
        total+=entry['bytes']
    if total!=index['expanded_bytes'] or total>1024**3:raise ValueError('资料总长度无效')
    missing=[]
    for relative,entry in files.items():
        p=safe_target(root,relative)
        if not p.exists():missing.append(relative)
        elif p.stat().st_size!=entry['bytes'] or sha(p)!=entry['sha256']:
            raise ValueError('已有资料与原版不一致，未覆盖：'+relative)
    if not missing:return {'restored':False,'verified_files':len(files),'root':str(root)}
    payload=root/'support-files.tar.xz'
    if payload.stat().st_size!=index['payload_bytes'] or sha(payload)!=index['payload_sha256']:
        raise ValueError('无损资料包校验失败，未运行筛选')
    print('首次自动还原完整验证资料与历史附件（离线、无额外依赖）……',flush=True)
    # Staging under a deep installation adds another component to every member
    # and can exceed Windows' path limit even when all final paths are valid.
    with tempfile.TemporaryDirectory(prefix='tail-support-') as name:
        stage=Path(name);seen=set()
        with tarfile.open(payload,'r:xz') as tar:
            for member in tar:
                relative=member.name
                if relative in seen or relative not in files or not member.isfile():raise ValueError('归档包含重复、额外或非普通文件')
                entry=files[relative]
                if member.size!=entry['bytes']:raise ValueError('归档文件长度不符')
                target=safe_target(stage,relative);target.parent.mkdir(parents=True,exist_ok=True)
                source=tar.extractfile(member)
                if source is None:raise ValueError('无法读取资料文件')
                with source,target.open('xb') as f:shutil.copyfileobj(source,f)
                if sha(target)!=entry['sha256']:raise ValueError('还原文件哈希不符：'+relative)
                seen.add(relative)
        if seen!=set(files):raise ValueError('无损资料包内容缺失')
        for relative in missing:
            target=safe_target(root,relative);target.parent.mkdir(parents=True,exist_ok=True)
            try:_commit_staged(stage/relative,target)
            except FileExistsError:
                if sha(target)!=files[relative]['sha256']:raise ValueError('并发还原内容冲突')
    return {'restored':True,'verified_files':len(files),'root':str(root)}


def _ensure_runtime_support(root, *, restore_all=False):
    """Daily execution needs small rule references, not historical audit archives."""
    has_archive=(root/'support-files.tar.xz').is_file() and (root/'support-files.json').is_file()
    missing=[relative for relative in RUNTIME_REFERENCES if not (root/relative).is_file()]
    if has_archive and (missing or restore_all):return _restore(root)
    if missing:raise FileNotFoundError('缺少实际运行所需的规则资料：'+', '.join(missing))
    for relative in RUNTIME_REFERENCES:
        value=json.loads((root/relative).read_text(encoding='utf-8-sig'))
        if not isinstance(value,dict):raise ValueError('必要规则资料须为JSON对象：'+relative)
    return {'restored':False,'root':str(root),'support_mode':'embedded_runtime',
            'historical_materials':'available_on_request' if has_archive else 'not_included',
            'runtime_references':list(RUNTIME_REFERENCES)}


def ensure_ready(source, *, restore_all=False):
    source=Path(source).resolve()
    # Release auditing is an explicit developer/diagnostic operation. Do not
    # emit a release mismatch during normal startup or require a manifest.
    verification={'status':'not_requested','scope':'release_comparison',
                  'blocks_execution':False}
    # Separate private executable copies by actual installed content. Publisher
    # manifests can be rewritten or absent and are never a startup prerequisite.
    choose_root=runpy.run_path(str(source/'scripts/tail_screener/paths.py'))['data_root']
    root=choose_root(source)
    files=None
    if root!=source:
        files=_shell_snapshot(source)
        identity=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:20]
        root=root/'releases'/identity
        root.mkdir(parents=True,exist_ok=True)
    with writer_lock(root):
        if root!=source:_copy_shell(source,root,files)
        result=_ensure_runtime_support(root,restore_all=restore_all)
    result['package_verification']=verification
    return root,result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--verify-package',action='store_true',help='提示与发布包的差异，不阻断正常运行、不联网')
    group.add_argument('--verify-release',action='store_true',help='开发发布验收：所有发布文件原始字节必须相符')
    args=parser.parse_args()
    try:
        source=Path(__file__).resolve().parents[1]
        result=(verify_package(source,strict_release=args.verify_release)
                if args.verify_package or args.verify_release else ensure_ready(source,restore_all=True)[1])
        print(json.dumps(result,ensure_ascii=False))
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__,'reason':str(exc)},ensure_ascii=False))
        raise SystemExit(4)
