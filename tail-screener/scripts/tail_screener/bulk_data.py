"""Anonymous, checksum-verified daily A-share data archives.

The archive is data, never executable code. A valid archive/date does not by
itself prove per-stock coverage, tradability, or strategy performance.
"""
from __future__ import annotations

from datetime import date
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import time

import requests

REPOSITORY='chenditc/investment_data'
API=f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
MAX_ARCHIVE_BYTES=2_000_000_000
MAX_EXPANDED_BYTES=4_000_000_000
MAX_MEMBERS=200_000
DOWNLOAD_CHUNK_BYTES=8*1024*1024


class BulkDataError(RuntimeError):
    pass


@contextmanager
def download_lock(directory):
    directory.mkdir(parents=True,exist_ok=True)
    lock=directory/'download.lock'
    for attempt in range(2):
        try:
            with lock.open('x',encoding='utf-8') as handle:handle.write(str(os.getpid()))
            break
        except FileExistsError as exc:
            from .runtime import _process_running
            try:dead=not _process_running(int(lock.read_text(encoding='utf-8')))
            except (ValueError,OSError):dead=False
            if attempt==0 and dead:
                lock.unlink();continue
            raise BulkDataError('该固定版本已有下载进程或无法核验的锁；不并发写入下载文件') from exc
    try:yield
    finally:lock.unlink(missing_ok=True)


def _day(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):
        raise BulkDataError('数据日期格式无效')
    try:date.fromisoformat(value)
    except ValueError as exc:raise BulkDataError('数据日期无效') from exc
    return value


def validate_manifest(value, tag):
    if not isinstance(value,dict) or value.get('release_tag')!=tag:
        raise BulkDataError('数据包清单与固定发布版本不一致')
    _day(tag);_day(value.get('target_trade_date'))
    size=value.get('archive_size_bytes')
    if isinstance(size,bool) or not isinstance(size,int) or not 1<=size<=MAX_ARCHIVE_BYTES:
        raise BulkDataError('数据包声明大小超出支持范围')
    digest=value.get('archive_sha256')
    if not isinstance(digest,str) or not re.fullmatch(r'sha256:[0-9a-f]{64}',digest):
        raise BulkDataError('数据包缺少有效SHA256')
    for key in ('investment_data_commit','qlib_commit','dolt_commit'):
        if not isinstance(value.get(key),str) or not value[key]:
            raise BulkDataError('数据包缺少来源版本')
    return dict(value)


def _hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda:handle.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def verify_extract(archive, destination, manifest, progress=None):
    """Verify complete bytes before extracting ordinary data files only."""
    archive,destination=Path(archive),Path(destination)
    if archive.stat().st_size!=manifest['archive_size_bytes']:
        raise BulkDataError('数据包下载大小与清单不一致')
    if _hash(archive)!=manifest['archive_sha256'].split(':',1)[1]:
        raise BulkDataError('数据包SHA256校验失败')
    destination.mkdir(parents=True,exist_ok=False)
    seen=set();expanded=0;count=0
    try:
        with tarfile.open(archive,'r:gz') as source:
            for member in source:
                path=PurePosixPath(member.name)
                if (path.is_absolute() or '..' in path.parts or '\\' in member.name or ':' in member.name
                    or not path.parts or path.parts[0]!='qlib_bin'
                    or not (member.isdir() or member.isfile()) or str(path) in seen):
                    raise BulkDataError('数据包包含不安全路径、重复文件或非普通文件')
                seen.add(str(path));count+=1;expanded+=member.size
                if count>MAX_MEMBERS or expanded>MAX_EXPANDED_BYTES:
                    raise BulkDataError('数据包解压大小超出支持范围')
                target=destination.joinpath(*path.parts)
                if member.isdir():target.mkdir(parents=True,exist_ok=True);continue
                if member.size<0:raise BulkDataError('无效文件大小')
                target.parent.mkdir(parents=True,exist_ok=True)
                reader=source.extractfile(member)
                if reader is None:raise BulkDataError('无法读取数据成员')
                with reader, target.open('xb') as writer:
                    shutil.copyfileobj(reader,writer,1024*1024)
                if target.stat().st_size!=member.size:raise BulkDataError('数据成员不完整')
        dataset=destination/'qlib_bin'
        calendar=(dataset/'calendars/day.txt').read_text().splitlines()
        if not calendar or calendar!=sorted(set(calendar)):
            raise BulkDataError('实际交易日历为空、重复或乱序')
        if any(_day(day)!=day for day in calendar):raise BulkDataError('无效交易日')
        if calendar[-1]!=manifest['target_trade_date']:
            raise BulkDataError('实际行情日历末日与清单不一致')
        if not (dataset/'instruments/all.txt').is_file() or not (dataset/'features').is_dir():
            raise BulkDataError('数据包缺少股票范围或历史字段')
        result={'members':count,'expanded_bytes':expanded,'calendar_last':calendar[-1],
                'calendar_sha256':_hash(dataset/'calendars/day.txt')}
        if progress:progress('批量数据哈希、路径与真实交易日期核验通过')
        return result
    except Exception:
        shutil.rmtree(destination,ignore_errors=True)
        raise


def _request(session,url,deadline,*,stream=False,headers=None):
    last=None
    for attempt in range(3):
        remaining=deadline-time.monotonic()
        if remaining<=0:raise BulkDataError('批量数据下载预算已用尽')
        try:
            response=session.get(url,timeout=(min(10,remaining/2),min(30,remaining/2)),stream=stream,
                                 **({'headers':headers} if headers else {}))
            if response.status_code in (401,403):
                response.close();raise BulkDataError('公开数据源访问被拒绝')
            if response.status_code==429 or response.status_code>=500:
                response.close();raise requests.ConnectionError('temporary source response')
            response.raise_for_status()
            return response
        except BulkDataError:raise
        except requests.exceptions.SSLError as exc:
            raise BulkDataError('公开数据源TLS核验失败') from exc
        except requests.RequestException as exc:
            last=type(exc).__name__
            if attempt<2 and deadline-time.monotonic()>1:time.sleep(.3*(attempt+1))
    raise BulkDataError('公开数据源连接失败：'+str(last))


def _download_archive(session,url,path,manifest,deadline,progress):
    """Retry interrupted bodies; resume only when the fixed asset confirms Range."""
    expected=manifest['archive_size_bytes']
    next_notice=64*1024*1024
    failures=0
    while True:
        offset=path.stat().st_size if path.exists() else 0
        if offset==expected:return
        if offset>expected:raise BulkDataError('缓存下载超出声明大小')
        end=min(expected-1,offset+DOWNLOAD_CHUNK_BYTES-1)
        headers={'Range':f'bytes={offset}-{end}'}
        try:
            with _request(session,url,deadline,stream=True,headers=headers) as response:
                if response.status_code==206:
                    value=response.headers.get('Content-Range','')
                    match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',value)
                    if not match or int(match[1])!=offset or int(match[3])!=expected or int(match[2])!=end:
                        raise BulkDataError('数据下载续传范围与固定文件不一致')
                    mode='ab'
                elif response.status_code==200:
                    offset=0;mode='wb'
                else:
                    raise BulkDataError('数据下载返回意外的分段响应')
                total=offset
                with path.open(mode) as handle:
                    for block in response.iter_content(64*1024):
                        if time.monotonic()>=deadline:raise BulkDataError('批量数据下载预算已用尽')
                        if not block:continue
                        total+=len(block)
                        if total>(end+1 if response.status_code==206 else expected):raise BulkDataError('下载超出声明范围')
                        handle.write(block)
                        if progress and total>=next_notice:
                            progress(f'历史数据已下载{total//(1024*1024)}MiB')
                            next_notice=(total//(64*1024*1024)+1)*64*1024*1024
                if total==expected:return
                if response.status_code==206 and total==end+1:
                    failures=0;continue
                raise requests.ConnectionError('incomplete body')
        except requests.exceptions.SSLError as exc:
            raise BulkDataError('公开数据源TLS核验失败') from exc
        except requests.RequestException as exc:
            failures+=1
            if failures>=3:raise BulkDataError('历史数据传输中断，有限重试后仍不完整') from exc
            if progress:progress('历史数据传输中断，正在对同一固定版本有限重试')


def _result(folder,marker,target_session,*,cached,error=None):
    dataset=folder/'data/qlib_bin'
    if (not dataset.is_dir() or not (dataset/'features').is_dir()
        or _hash(dataset/'calendars/day.txt')!=marker['verification']['calendar_sha256']):
        raise BulkDataError('已缓存的数据包结构或交易日历发生变化')
    day=marker['manifest']['target_trade_date']
    stale=day!=target_session if target_session else bool(error)
    gaps=[]
    if stale:gaps.append('批量行情日期未达到本次要求，不能冒充当前交易日')
    if error:gaps.append(error)
    return {'directory':str(dataset),'metadata':{
        'repository':REPOSITORY,'release':marker['manifest']['release_tag'],
        'data_session':day,'requested_session':target_session,'cached':cached,'stale':stale,
        'verified':True,'verification_scope':'下载字节、包结构、实际交易日历；个股覆盖须另行核验',
        'manifest':marker['manifest'],'verification':marker['verification'],'gaps':gaps}}


def ensure_bulk_dataset(cache_root,target_session=None,deadline=None,progress=None,*,session=None,release_tag=None):
    """Automatically download or reuse a fixed daily archive, with no account.

    A caller must check ``metadata.stale`` and per-stock data before screening.
    A failed refresh never changes the cached source's original trade date.
    """
    if target_session is not None:_day(target_session)
    if release_tag is not None:_day(release_tag)
    root=Path(cache_root);root.mkdir(parents=True,exist_ok=True)
    deadline=time.monotonic()+600 if deadline is None else deadline
    own=session is None;session=session or requests.Session()
    pointer=root/'latest-verified.json'
    try:
        if release_tag is None:
            with _request(session,API,deadline) as response:release=response.json()
            tag=release.get('tag_name');_day(tag)
        else:tag=release_tag
        base=f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
        with _request(session,base+'qlib_bin.manifest.json',deadline) as response:
            manifest=validate_manifest(response.json(),tag)
        identity=tag+'-'+manifest['archive_sha256'].split(':')[1][:16]
        folder=root/identity;marker_path=folder/'verified.json'
        if marker_path.is_file():
            marker=json.loads(marker_path.read_text())
            if marker['manifest']!=manifest:raise BulkDataError('缓存清单身份冲突')
            result=_result(folder,marker,target_session,cached=True)
            temp_pointer=root/('.latest-'+str(os.getpid())+'.json')
            temp_pointer.write_text(json.dumps({'folder':identity}))
            os.replace(temp_pointer,pointer)
            return result
        if progress:progress(f'首次准备免费历史数据：约{manifest["archive_size_bytes"]/1_000_000:.0f}MB')
        download=root/'.downloads'/identity
        with download_lock(download):
            archive=download/'archive.part'
            # An interruption preserves bytes for the next invocation. Never
            # reuse a partial file for a different manifest identity.
            if not (archive.exists() and archive.stat().st_size==manifest['archive_size_bytes']
                    and _hash(archive)==manifest['archive_sha256'].split(':')[1]):
                if archive.exists() and archive.stat().st_size>=manifest['archive_size_bytes']:
                    os.replace(archive,download/('rejected-'+str(time.time_ns())+'.part'))
                _download_archive(session,base+'qlib_bin.tar.gz',archive,manifest,deadline,progress)
            with tempfile.TemporaryDirectory(prefix='.bulk-stage-',dir=root) as temporary:
                stage=Path(temporary)
                verification=verify_extract(archive,stage/'data',manifest,progress)
                marker={'manifest':manifest,'verification':verification}
                (stage/'verified.json').write_text(json.dumps(marker,ensure_ascii=False,indent=2),encoding='utf-8')
                if folder.exists():raise BulkDataError('目标缓存目录已存在但未通过核验')
                os.replace(archive,stage/'archive.tar.gz')
                os.replace(stage,folder)
        temp_pointer=root/('.latest-'+str(os.getpid())+'.json')
        temp_pointer.write_text(json.dumps({'folder':identity}))
        os.replace(temp_pointer,pointer)
        return _result(folder,marker,target_session,cached=False)
    except (BulkDataError,requests.RequestException,OSError,ValueError,TypeError,AttributeError,KeyError,tarfile.TarError) as exc:
        message=str(exc) if isinstance(exc,BulkDataError) else '批量数据准备失败：'+type(exc).__name__
        if pointer.is_file():
            try:
                identity=json.loads(pointer.read_text())['folder']
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}-[0-9a-f]{16}',identity):raise ValueError('invalid cache identity')
                folder=root/identity;marker=json.loads((folder/'verified.json').read_text())
                return _result(folder,marker,target_session,cached=True,error=message)
            except (OSError,ValueError,KeyError,BulkDataError):pass
        raise BulkDataError(message) from exc
    finally:
        if own:session.close()
