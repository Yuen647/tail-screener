#!/usr/bin/env python3
"""Install or safely upgrade a recognized tail-screener using the standard library."""
from pathlib import Path
import argparse
from datetime import datetime, timezone
import errno
import json
import os
import re
import shutil
import tempfile


class InstallError(RuntimeError):
    pass


def _present(path):
    return path.exists() or path.is_symlink()


def _is_tail_screener(directory):
    """Recognize an exact root frontmatter name without adding YAML dependencies."""
    try:
        if not directory.is_dir():
            return False
        with (directory / 'SKILL.md').open(encoding='utf-8-sig') as handle:
            lines = handle.read(65536).splitlines()
    except (OSError, UnicodeError):
        return False
    if not lines or lines[0].strip() != '---':
        return False
    end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == '---'), None)
    if end is None:
        return False
    names = [line for line in lines[1:end] if re.match(r'^name\s*:', line)]
    return len(names) == 1 and bool(re.fullmatch(
        r'''name\s*:\s*(?:tail-screener|'tail-screener'|"tail-screener")\s*(?:#.*)?''', names[0]))


def _stage(source, destination):
    try:
        destination.symlink_to(source, target_is_directory=True)
        return 'symlink'
    except NotImplementedError:
        pass
    except OSError as exc:
        supported_errors = {errno.EACCES, errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP}
        if exc.errno not in supported_errors and getattr(exc, 'winerror', None) != 1314:
            raise
    # Runtime is prepared on execution; another machine's venv is not portable.
    ordinary_ignores=shutil.ignore_patterns('.runtime', '.baselines', '.cache', '.venv', 'venv', '.test-deps',
        '*-permission-backup', '__pycache__', '.pytest_cache', '.git', '*.pyc', '*.pyo')
    def ignore(directory,names):
        excluded=set(ordinary_ignores(directory,names))
        # Generated reports can contain thousands of response files, private
        # paper accounts and long paths. Installation copies the reusable skill;
        # original reports remain at source, alongside the preserved archive.
        if Path(directory).resolve()==source.resolve():excluded.add('reports')
        return excluded
    shutil.copytree(source, destination, ignore=ignore)
    return 'copy'


def _activate(staged, target, mode):
    """Exclusively reserve an absent name, then atomically replace our placeholder."""
    if os.name == 'nt':
        # Windows rename already refuses every existing destination. Unlike
        # POSIX, replacing an empty directory placeholder is not portable.
        os.rename(staged, target)
        return
    if mode == 'symlink':
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
    else:
        target.mkdir(mode=0o700)
    identity = target.lstat()
    try:
        os.replace(staged, target)
    except BaseException:
        try:
            current = target.lstat()
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                if mode == 'symlink' and current.st_size == 0:
                    target.unlink()
                elif mode == 'copy':
                    target.rmdir()  # refuse removal if someone populated it
        except OSError:
            pass
        raise


def register_skill(source, skills_dir):
    """Back up only an identified previous tail-screener, outside active discovery.

    Symlink backups preserve the registration verbatim. Metadata records its
    resolved source and original link text; the old source is never moved or
    deleted. Relative links recover when moved back to the original path.
    """
    source = Path(source).expanduser().resolve(strict=True)
    skills_dir = Path(skills_dir).expanduser().resolve()
    target = skills_dir / 'tail-screener'
    if not _is_tail_screener(source):
        raise InstallError(f'安装源不是可识别的 tail-screener：{source}')
    if _present(target) and target.resolve() == source:
        return dict(status='already_registered', mode='symlink' if target.is_symlink() else 'in_place',
                    source=str(source), target=str(target), backup=None)
    exists = _present(target)
    if exists and not _is_tail_screener(target):
        raise InstallError(f'同名路径不是可识别的 tail-screener，未覆盖：{target}')
    if exists and not target.is_symlink() and source.is_relative_to(target.resolve()):
        raise InstallError('安装源位于旧技能目录内，移动旧目录会使源失效；请从独立解压目录安装。')
    previous_identity = target.lstat() if exists else None
    previous_source = str(target.resolve()) if exists else None
    previous_link = os.readlink(target) if exists and target.is_symlink() else None
    skills_dir.mkdir(parents=True, exist_ok=True)
    backups_dir = skills_dir.parent / 'skill-backups'
    if backups_dir.resolve().is_relative_to(skills_dir):
        raise InstallError('备份目录指向技能发现目录内部，不能安全升级。')
    if backups_dir.resolve().is_relative_to(source):
        raise InstallError('备份目录位于安装源内部，无法安全准备复制安装；请从独立解压目录安装。')
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    transaction = Path(tempfile.mkdtemp(prefix=f'tail-screener-{stamp}-', dir=backups_dir))
    staged, backup = transaction / 'new-registration', transaction / 'tail-screener'
    backed_up, registered = False, False
    try:
        mode = _stage(source, staged)
        if exists:
            current = target.lstat()
            if (current.st_dev, current.st_ino) != (previous_identity.st_dev, previous_identity.st_ino):
                raise InstallError('安装期间目标已发生变化，未替换；请重新运行安装。')
            (transaction / 'registration.json').write_text(json.dumps({
                'original_target': str(target), 'resolved_source': previous_source,
                'original_symlink': previous_link, 'backup_kind': 'symlink' if previous_link is not None else 'directory',
                'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'restore': 'Move the saved tail-screener registration back to original_target; do not follow/delete a symlink source.',
            }, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(target, backup)
            backed_up = True
        _activate(staged, target, mode)
        registered = True
    except BaseException as exc:
        if backed_up:
            if _present(target):
                raise InstallError(f'注册失败且目标出现其他内容，未覆盖该内容。旧技能备份保留于 {backup}；原因：{exc}') from exc
            try:
                os.replace(backup, target)
                backed_up = False
            except OSError as rollback_error:
                raise InstallError(f'注册失败，自动恢复受阻。旧技能完整备份保留于 {backup}；恢复错误：{rollback_error}') from exc
            if isinstance(exc, Exception):
                raise InstallError(f'注册失败，已自动恢复旧版本：{target}；原因：{exc}') from exc
        raise
    finally:
        if not backed_up:
            shutil.rmtree(transaction, ignore_errors=True)
        elif not registered and _present(staged):
            if staged.is_symlink():
                staged.unlink()
            else:
                shutil.rmtree(staged, ignore_errors=True)
    return dict(status='upgraded' if exists else 'installed', mode=mode,
                source=str(source), target=str(target), backup=str(backup) if backed_up else None,
                previous_source=previous_source)


def main(argv=None):
    parser = argparse.ArgumentParser(description='自动注册或升级 tail-screener；旧版本保存在技能目录外，无需配置环境。')
    parser.add_argument('--skills-dir', type=Path, default=Path.home() / '.agents' / 'skills')
    args = parser.parse_args(argv)
    try:
        result = register_skill(Path(__file__).resolve().parents[1], args.skills_dir)
    except (InstallError, OSError) as exc:
        raise SystemExit(f'安装未完成：{exc}') from exc
    label = {'already_registered': '已注册，无需重复安装', 'installed': '安装完成', 'upgraded': '升级完成'}[result['status']]
    print(f'{label}：{result["target"]}')
    print(f'注册方式：{result["mode"]}；源目录：{result["source"]}')
    if result['backup']:
        print(f'旧注册备份：{result["backup"]}')
        print(f'旧版本源路径：{result["previous_source"]}')
    print('新建/重启支持技能发现的会话后可使用 $tail-screener；运行环境会在首次执行时自动准备。')
    return result


if __name__ == '__main__':
    main()
