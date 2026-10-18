"""Choose writable storage automatically for copied or read-only skill bundles."""
from hashlib import sha256
import os
from pathlib import Path
import time


def _writable_directory(path):
    """Use an actual exclusive write, not permission bits or root-sensitive access()."""
    probe, created = None, False
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f'.tail-screener-write-{os.getpid()}-{time.monotonic_ns()}'
        with probe.open('x', encoding='utf-8'):
            created = True
        return True
    except OSError:
        return False
    finally:
        if created:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def data_root(skill_root):
    """Keep normal reports beside the skill; use host user storage if read-only.

    Separate source directories get separate namespaces in the fallback so two
    copied skill packages do not share reports or live data caches by accident.
    No proxy, credentials, or system-package settings are inspected or changed.
    """
    skill_root = Path(skill_root).resolve()
    if _writable_directory(skill_root):
        return skill_root
    configured = os.environ.get('LOCALAPPDATA' if os.name == 'nt' else 'XDG_CACHE_HOME')
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / '.cache'
    namespace = sha256(str(skill_root).encode('utf-8')).hexdigest()[:16]
    fallback = base / 'tail-screener' / 'data' / namespace
    if _writable_directory(fallback):
        return fallback
    raise OSError('无法创建技能专用数据目录；当前宿主没有可用的用户写入空间。')
