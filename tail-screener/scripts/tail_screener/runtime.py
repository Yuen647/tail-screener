"""Prepare an isolated runtime automatically, using only the Python standard library.

No global packages are changed. Proxy, certificate and package-index settings
are inherited unchanged; installer output is never echoed because it can
contain credentials embedded in URLs. Offline replay never installs packages.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from importlib import import_module, metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import sysconfig
import time


RUNTIME_MARKER = "TAIL_SCREENER_MANAGED_RUNTIME"
RUNTIME_FAILURE = 4
MIN_PYTHON = (3, 10)
INSTALL_TIMEOUT = 240
LOCK_TIMEOUT = 300
REPAIRABLE_CALENDAR_ERRORS = {
    "dependency_missing", "dependency_import_error", "unsupported_dependency_version",
    "calendar_initialization_error", "calendar_compatibility_error",
}


def _skill_root():
    return Path(__file__).resolve().parents[2]


def _version_tuple(value):
    # Installed prereleases are not accepted for a production trading workflow.
    match = re.fullmatch(r"(\d+(?:\.\d+)*)(?:\.post\d+)?(?:\+[a-zA-Z0-9.]+)?", value)
    return tuple(int(part) for part in match[1].split(".")) if match else None


def _satisfies(version, specifier):
    # Frozen environments may explicitly pin a post release or a transitive
    # package such as OpenTelemetry's beta-numbered semantic conventions.
    # Accept that exact reviewed identity; broad ranges still reject prereleases.
    pinned = re.fullmatch(r'\s*==\s*(\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?(?:\+[A-Za-z0-9.]+)?)\s*', specifier)
    if pinned and re.search(r'[A-Za-z+]', pinned[1]):
        return version == pinned[1]
    actual = _version_tuple(version)
    if actual is None:
        return False
    for part in specifier.split(","):
        match = re.fullmatch(r"\s*(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)\s*", part)
        if not match:
            return False
        required = tuple(map(int, match[2].split(".")))
        width = max(len(actual), len(required))
        left, right = actual + (0,) * (width - len(actual)), required + (0,) * (width - len(required))
        operations = {">=": left >= right, "<=": left <= right, "==": left == right,
                      "!=": left != right, ">": left > right, "<": left < right}
        if not operations[match[1]]:
            return False
    return True


def dependency_health(requirements=None):
    """Return safe diagnostics, including real import/calendar compatibility.

    Missing future exchange holidays are reported separately: reinstalling the
    same dependency cannot create a verified future exchange calendar.
    """
    path = Path(requirements) if requirements else _skill_root() / "requirements.txt"
    result = {"healthy": False, "python": sys.version.split()[0], "versions": {}, "issues": []}
    if sys.version_info[:2] < MIN_PYTHON:
        result["issues"].append({"code": "unsupported_python", "message": "宿主 Python 版本低于 3.10。"})
        return result
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        result["issues"].append({"code": "requirements_missing", "message": "技能包缺少依赖清单。"})
        return result
    for line in lines:
        line = line.partition("#")[0].strip()
        if not line:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)\s*((?:>=|<=|==|!=|>|<).+)", line)
        if not match:
            result["issues"].append({"code": "requirements_invalid", "message": "技能包依赖清单格式不支持。"})
            continue
        name, specification = match.groups()
        try:
            version = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = None
        result["versions"][name] = version
        if version is None or not _satisfies(version, specification):
            result["issues"].append({"code": "dependency_version", "package": name,
                                     "installed": version, "required": specification})
    if result["issues"]:
        return result
    try:
        for name in ("requests", "pandas", "exchange_calendars"):
            import_module(name)
        # Optional stock-model profile must pass real native-library imports,
        # not merely advertise an installed package version.
        if 'pyqlib' in result['versions']:
            for name in ('qlib', 'lightgbm', 'numpy', 'pyarrow'):
                import_module(name)
        from .calendar import calendar_context
        calendar = calendar_context(datetime.now(timezone.utc))
        result["calendar"] = calendar
        if calendar.get("error_code") in REPAIRABLE_CALENDAR_ERRORS:
            result["issues"].append({"code": calendar["error_code"],
                                     "message": calendar.get("error", "交易日历兼容检查失败。")})
    except Exception as exc:
        # Type is useful; raw import errors can contain environment paths/URLs.
        result["issues"].append({"code": "dependency_import_error", "error_type": type(exc).__name__})
    result["healthy"] = not result["issues"]
    return result


def current_runtime_info():
    return {"managed": bool(os.environ.get(RUNTIME_MARKER)), "python": sys.version.split()[0],
            "preparation": "automatic", "system_packages_modified": False}


def _runtime_home(skill_root, requirements_path=None):
    requirements = (Path(requirements_path) if requirements_path else skill_root / "requirements.txt").read_bytes()
    fingerprint = sha256(b'runtime-v3\n' + requirements).hexdigest()[:16]
    platform = re.sub(r"[^a-zA-Z0-9_.-]", "_", sysconfig.get_platform())
    label = f"{sys.implementation.cache_tag}-{platform}-{fingerprint}"
    parent = skill_root / ".runtime"
    try:
        parent.mkdir(parents=True, exist_ok=True)
        # Some hosts mount the uploaded skill as read-only. Keep this an
        # automatic platform choice, rather than asking users to move files.
        probe = parent / f".write-{os.getpid()}-{time.monotonic_ns()}"
        with probe.open("x", encoding="utf-8"):
            pass
        probe.unlink()
    except OSError:
        configured = os.environ.get("LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME")
        base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".cache"
        parent = base / "tail-screener" / "runtime"
        parent.mkdir(parents=True, exist_ok=True)
    return parent / label


def _python_path(runtime):
    return runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _status(message):
    print(f"tail-screener：{message}", file=sys.stderr, flush=True)


def _child_environment():
    environment = os.environ.copy()
    # Parent -X utf8 flags are not inherited by venv/pip/probe processes.
    environment['PYTHONUTF8'] = '1'
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PYTHONUNBUFFERED'] = '1'
    return environment


def _probe_runtime(python, skill_root, requirements_path=None):
    if not python.is_file():
        return {"healthy": False, "issues": [{"code": "runtime_missing"}]}
    probe = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
             "from tail_screener.runtime import dependency_health;"
             "print(json.dumps(dependency_health(sys.argv[2]),ensure_ascii=False))")
    try:
        proc = subprocess.run([str(python), "-c", probe, str(skill_root / "scripts"),
                               str(requirements_path or skill_root / "requirements.txt")],
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=_child_environment(), timeout=40, check=False)
        if proc.returncode != 0:
            return {"healthy": False, "issues": [{"code": "runtime_probe_failed"}]}
        result = json.loads(proc.stdout)
        if isinstance(result, dict) and isinstance(result.get("healthy"), bool):
            return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return {"healthy": False, "issues": [{"code": "runtime_probe_failed"}]}


@contextmanager
def _preparation_lock(parent):
    """Serialize preparation; reclaim only clearly dead/expired lock owners."""
    parent.mkdir(parents=True, exist_ok=True)
    lock = parent / ".prepare.lock"
    deadline = time.monotonic() + LOCK_TIMEOUT
    while True:
        try:
            lock.mkdir()
            (lock / "owner.json").write_text(json.dumps({"pid": os.getpid(), "at": time.time()}), encoding="utf-8")
            break
        except FileExistsError:
            try:
                owner = json.loads((lock / "owner.json").read_text(encoding="utf-8"))
                # A long lease avoids killing an installation whose process ID
                # is not inspectable on this host or through a container layer.
                stale = time.time() - float(owner["at"]) > 900
                if not _process_running(int(owner['pid'])):
                    stale = True
                if stale:
                    shutil.rmtree(lock)
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                # An interrupted mkdir before owner.json was written is safe
                # to reclaim after the same generous lease.
                try:
                    if time.time() - lock.stat().st_mtime > 900:
                        shutil.rmtree(lock)
                        continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise TimeoutError("runtime_lock_timeout")
            time.sleep(.25)
    try:
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _process_running(pid):
    if pid == os.getpid():return True
    if pid <= 0:return False
    if os.name == 'nt':
        # os.kill(pid, 0) is NOT a portable process-existence check on Windows.
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:return ctypes.get_last_error()!=87  # access denied means unknown/alive
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(pid,0)
    except ProcessLookupError:return False
    except (PermissionError,OSError):return True
    return True


def _failure_reason(output):
    value = (output or "").lower()
    if any(word in value for word in ("proxyerror", "connection", "timed out", "network", "ssl", "certificate")):
        return "依赖下载服务连接失败或网络证书校验未通过"
    if any(word in value for word in ("permission denied", "access is denied", "read-only")):
        return "运行环境目录不可写"
    if any(word in value for word in ("no matching distribution", "requires a different python", "resolutionimpossible")):
        return "当前宿主没有匹配的依赖发行版本"
    if any(word in value for word in ("ensurepip", "no module named venv", "no module named pip")):
        return "宿主缺少创建隔离运行环境的 Python 组件"
    return "隔离运行环境准备未成功"


def _installer_facts(output):
    """Extract allowlisted facts only; never persist URLs or raw installer logs."""
    value = (output or '').lower()
    signatures = {
        'proxy_error': ('proxyerror',),
        'certificate_error': ('certificate_verify_failed', 'certificate verify failed'),
        'network_error': ('connectionerror', 'connection refused', 'network is unreachable',
                          'temporary failure in name resolution', 'failed to establish a new connection'),
        'download_timeout': ('timed out', 'readtimeout'),
        'no_matching_distribution': ('no matching distribution', 'could not find a version'),
        'python_incompatible': ('requires a different python', 'require a different python'),
        'dependency_conflict': ('resolutionimpossible', 'conflicting dependencies'),
        'ensurepip_missing': ('ensurepip',),
        'venv_missing': ('no module named venv',),
        'pip_missing': ('no module named pip',),
        'externally_managed': ('externally-managed-environment',),
        'permission_denied': ('permission denied', 'access is denied', 'read-only'),
        'disk_full': ('no space left on device',),
    }
    return [code for code, needles in signatures.items() if any(word in value for word in needles)]


def _run_preparation(command, *, stage, runtime, timeout):
    """Persist a small, credential-free record even if pip fails or times out."""
    record = {'stage': stage, 'status': 'failed', 'python': sys.version.split()[0],
              'platform': sysconfig.get_platform(), 'system_packages_modified': False,
              'at': datetime.now(timezone.utc).isoformat()}
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', env=_child_environment(),
                                timeout=timeout, check=False)
        record.update(exit_code=result.returncode,
                      status='passed' if result.returncode == 0 else 'failed',
                      error_codes=_installer_facts((result.stdout or '') + (result.stderr or '')))
        return result
    except subprocess.TimeoutExpired:
        record.update(error_codes=['preparation_timeout'], timeout_seconds=timeout)
        raise
    except OSError as exc:
        record.update(error_codes=['process_start_failed'], error_type=type(exc).__name__)
        raise
    finally:
        # The runtime is private, never an account or input-data directory.
        diagnostic = runtime / 'preparation.jsonl'
        try:
            runtime.mkdir(parents=True, exist_ok=True)
            with diagnostic.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            diagnostic_note = str(diagnostic)
        except OSError:
            diagnostic_note = '诊断文件不可写，保留本条阶段信息'
        if record['status'] == 'failed':
            _status(f"环境阶段 {stage} 失败：{', '.join(record.get('error_codes') or ['unclassified_installer_error'])}；诊断：{diagnostic_note}")


def _build_runtime(runtime, skill_root, requirements_path=None):
    # A failed previous attempt is never trusted or allowed to poison a retry.
    runtime = Path(runtime).absolute()
    intended_parent = _runtime_home(skill_root, requirements_path).parent.resolve()
    if (runtime.parent.resolve() != intended_parent or runtime.is_symlink()
            or runtime.resolve() != intended_parent / runtime.name):
        raise RuntimeError('运行环境路径不在指定的专用环境目录内，未清理或覆盖')
    if runtime.exists():
        shutil.rmtree(runtime)
    _status("正在自动准备独立运行环境，首次运行可能需要几分钟。")
    # Reuse each available host package independently; requiring the entire
    # scientific stack first forces unnecessary downloads on minimal hosts.
    created = _run_preparation([sys.executable, "-m", "venv", "--system-site-packages", str(runtime)],
                               stage='create_venv', runtime=runtime, timeout=60)
    python = _python_path(runtime)
    installer = [str(python), '-m', 'pip']
    # Debian-derived/cloud hosts may provide venv without ensurepip. A recent
    # host pip can install into a pip-less venv without modifying global packages.
    creation_output = (created.stdout or '') + (created.stderr or '')
    if created.returncode and 'ensurepip' in creation_output.lower():
        try:
            host_pip = metadata.version('pip')
        except metadata.PackageNotFoundError:
            host_pip = '0'
        if _satisfies(host_pip, '>=22.3'):
            created = _run_preparation([sys.executable, '-m', 'venv', '--without-pip',
                                        '--system-site-packages', str(runtime)],
                                       stage='create_venv_without_pip', runtime=runtime, timeout=60)
            installer = [sys.executable, '-m', 'pip', '--python', str(python)]
    if created.returncode:
        raise RuntimeError(f"创建环境失败（退出码{created.returncode}）：" + _failure_reason((created.stdout or '') + (created.stderr or '')))
    installed = _run_preparation([*installer, "install", "--disable-pip-version-check",
                                "--no-input", "--no-user", "--progress-bar", "off", "--timeout", "20", "--retries", "2",
                                "-r", str(requirements_path or skill_root / "requirements.txt")],
                                stage='install_dependencies', runtime=runtime, timeout=INSTALL_TIMEOUT)
    if installed.returncode:
        raise RuntimeError(f"安装依赖失败（退出码{installed.returncode}）：" + _failure_reason((installed.stdout or '') + (installed.stderr or '')))
    health = (_probe_runtime(python, skill_root, requirements_path) if requirements_path
              else _probe_runtime(python, skill_root))
    if not health.get("healthy"):
        raise RuntimeError("依赖安装后的兼容性自检未通过：" + _health_summary(health))
    (runtime / "ready.json").write_text(json.dumps({"prepared_at": datetime.now(timezone.utc).isoformat(),
                                                   "versions": health.get("versions", {})}, ensure_ascii=False),
                                         encoding="utf-8")
    _status("独立运行环境已就绪，继续执行。")


def _invoke(python, skill_root, argv, runtime, *, entrypoint=None):
    environment = _child_environment()
    environment[RUNTIME_MARKER] = runtime.name
    # Keep HTTP(S)_PROXY, REQUESTS_CA_BUNDLE, SSL_CERT_FILE and PIP_* intact.
    try:
        script = Path(entrypoint).resolve() if entrypoint else skill_root / "scripts" / "screen.py"
        return subprocess.call([str(python), str(script), *argv], env=environment)
    except OSError:
        _status("独立运行环境启动失败，本次没有启动扫描。")
        return RUNTIME_FAILURE


def prepare_runtime(skill_root, argv, *, offline=False, entrypoint=None, requirements_path=None):
    """Return None when ready here, otherwise the managed child/failure exit code."""
    skill_root = Path(skill_root).resolve()
    requirements_path = Path(requirements_path).resolve() if requirements_path else None
    health = dependency_health(requirements_path or skill_root / "requirements.txt")
    if health["healthy"]:
        return None
    _status('当前解释器依赖未就绪：' + _health_summary(health))
    if sys.version_info[:2] < MIN_PYTHON:
        _status("当前宿主 Python 低于技能支持的 3.10，平台运行环境不支持本技能。")
        return RUNTIME_FAILURE
    if os.environ.get(RUNTIME_MARKER):
        _status("自动准备后的运行环境自检未通过，已停止重复安装；本次没有启动扫描。")
        return RUNTIME_FAILURE
    try:
        runtime = (_runtime_home(skill_root, requirements_path) if requirements_path else _runtime_home(skill_root))
        python = _python_path(runtime)
        ready = runtime / "ready.json"
        def probe():
            return (_probe_runtime(python, skill_root, requirements_path) if requirements_path
                    else _probe_runtime(python, skill_root))
        if ready.is_file() and probe().get("healthy"):
            return _invoke(python, skill_root, argv, runtime, entrypoint=entrypoint)
        if offline:
            _status("离线任务没有可复用的独立运行环境；保持离线，本次未联网准备依赖。")
            return RUNTIME_FAILURE
        with _preparation_lock(runtime.parent):
            if not (ready.is_file() and probe().get("healthy")):
                try:
                    if requirements_path:_build_runtime(runtime, skill_root, requirements_path)
                    else:_build_runtime(runtime, skill_root)
                except Exception:
                    ready.unlink(missing_ok=True)
                    raise
        return _invoke(python, skill_root, argv, runtime, entrypoint=entrypoint)
    except subprocess.TimeoutExpired:
        _status("自动准备运行环境超时，本次没有启动扫描。")
    except TimeoutError:
        _status("等待另一个实例准备运行环境超时，本次没有启动扫描。")
    except RuntimeError as exc:
        _status(f"{exc}；自动准备失败，本次没有启动扫描。")
    except (OSError, ValueError):
        _status("技能目录不可写或技能包不完整，自动准备失败，本次没有启动扫描。")
    return RUNTIME_FAILURE


def _health_summary(health):
    parts = []
    for issue in health.get('issues', []):
        if issue.get('code') == 'dependency_version':
            parts.append(f"{issue.get('package', '?')}（已安装 {issue.get('installed') or '无'}，需要 {issue.get('required', '?')}）")
        else:
            parts.append(issue.get('code', 'unknown_dependency_issue'))
    return '；'.join(parts) or 'unknown_dependency_issue'
