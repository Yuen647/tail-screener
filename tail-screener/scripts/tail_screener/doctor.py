"""Small, standalone diagnostics; importing this module needs only Python's stdlib."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib
from importlib import metadata
import json
from math import isfinite
from pathlib import Path
import re
import sys
from time import monotonic
from urllib.parse import urlsplit, urlunsplit


TZ = timezone(timedelta(hours=8))
REQUIREMENTS = Path(__file__).resolve().parents[2] / 'requirements.txt'
REPAIR_ACTION = '重新运行技能入口，由自动环境准备流程安装或修复依赖；若仍失败，保留诊断报告。'
PROBE_TIMEOUTS = ((3, 4), (5, 8))
PROBE_BUDGET_SECONDS = 24
MODULES = {'requests': 'requests', 'exchange-calendars': 'exchange_calendars',
           'pandas': 'pandas', 'tzdata': 'tzdata'}
PROBES = (
    dict(id='sina_directory', name='新浪沪深股票目录',
         url='https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount',
         params={'node': 'hs_a'}, referer='https://finance.sina.com.cn/'),
    dict(id='eastmoney_directory', name='东方财富备用股票目录',
         url='https://datacenter.eastmoney.com/securities/api/data/v1/get',
         params={'reportName': 'RPT_F10_BASIC_ORGINFO', 'columns': 'SECUCODE,EM2016',
                 'pageSize': 10, 'pageNumber': 1, 'source': 'F10', 'client': 'PC'},
         referer='https://data.eastmoney.com/'),
    dict(id='tencent_quote', name='腾讯带日期行情',
         url='https://qt.gtimg.cn/q=sh600000', params=None,
         referer='https://gu.qq.com/'),
    dict(id='sina_quote', name='新浪交叉核对行情',
         url='https://hq.sinajs.cn/list=sh600000', params=None,
         referer='https://finance.sina.com.cn/'),
)


def _error(exc):
    """Do not expose exception text: request errors can contain proxy credentials."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    if 'ProxyError' in names:
        category = 'proxy_error'
    elif 'SSLError' in names:
        category = 'tls_error'
    elif names & {'Timeout', 'ConnectTimeout', 'ReadTimeout', 'TimeoutError'}:
        category = 'timeout'
    elif 'HTTPError' in names:
        category = 'http_error'
    elif names & {'ConnectionError', 'ConnectionRefusedError', 'gaierror', 'URLError'}:
        category = 'connection_error'
    elif names & {'ModuleNotFoundError', 'ImportError', 'PackageNotFoundError'}:
        category = 'missing_dependency'
    elif names & {'JSONDecodeError', 'ValueError', 'UnicodeDecodeError', 'KeyError', 'IndexError', 'TypeError'}:
        category = 'invalid_response'
    else:
        category = 'unexpected_error'
    result = {'error_category': category, 'error_type': type(exc).__name__}
    status = getattr(getattr(exc, 'response', None), 'status_code', None)
    if isinstance(status, int) and 100 <= status <= 599:
        result['http_status'] = status
    return result


def _version_satisfies(version, requirement):
    """Handle the numeric release bounds used by this package without packaging."""
    if not re.fullmatch(r'\d+(?:\.\d+)*', version):
        return None
    installed = tuple(int(x) for x in version.split('.'))
    constraints = re.sub(r'^[A-Za-z0-9_.-]+', '', requirement)
    for bound in filter(None, constraints.split(',')):
        match = re.fullmatch(r'(>=|<=|==|!=|>|<)(\d+(?:\.\d+)*)', bound.strip())
        if not match:
            return None
        expected = tuple(int(x) for x in match[2].split('.'))
        width = max(len(installed), len(expected))
        left = installed + (0,) * (width - len(installed))
        right = expected + (0,) * (width - len(expected))
        if not {'>=': left >= right, '<=': left <= right, '==': left == right,
                '!=': left != right, '>': left > right, '<': left < right}[match[1]]:
            return False
    return True


def _dependency_checks(importer, version_reader):
    checks, loaded = [], {}
    try:
        requirements = REQUIREMENTS.read_text(encoding='utf-8-sig').splitlines()
    except (OSError, UnicodeError):
        return [dict(id='requirements', status='failed', error_category='missing_package_file',
                     action='重新解压完整技能包，确认 requirements.txt 与 scripts 目录一起存在。')], loaded
    for line in requirements:
        requirement = line.split('#', 1)[0].strip()
        if not requirement:
            continue
        match = re.match(r'^([A-Za-z0-9_.-]+)', requirement)
        if not match:
            checks.append(dict(id='requirements', status='failed',
                               error_category='invalid_requirements_file'))
            continue
        name = match[1]
        module_name = MODULES.get(name, name.replace('-', '_'))
        check = dict(id=name, requirement=requirement)
        try:
            version = version_reader(name)
            check['installed_version'] = version
            module = importer(module_name)
            loaded[name] = module
            acceptable = _version_satisfies(version, requirement)
            check.update(import_ok=True, version_satisfies_requirement=acceptable,
                         status='passed' if acceptable is True else 'failed' if acceptable is False else 'warning')
            if acceptable is False:
                check['error_category'] = 'unsupported_dependency_version'
            if acceptable is not True:
                check['action'] = REPAIR_ACTION
        except Exception as exc:
            check.update(status='failed', import_ok=False, **_error(exc), action=REPAIR_ACTION)
        checks.append(check)
    return checks, loaded


def _calendar_reader(now):
    try:
        module = importlib.import_module('.calendar', __package__)
    except ModuleNotFoundError as exc:
        if exc.name != f'{__package__}.calendar':
            raise
        module = importlib.import_module('.providers', __package__)
    return module.calendar_context(now)


def _calendar_check(now, reader):
    try:
        context = reader(now)
        # Keep the known calendar fields, never propagate raw exception messages.
        allowed = ('verified', 'session', 'is_session', 'previous_session', 'next_session',
                   'source', 'scope', 'coverage_start', 'coverage_end', 'first_session',
                   'last_session', 'calendar_version', 'error_code', 'error_type')
        result = {key: context[key] for key in allowed if key in context}
        result.update(id='trading_calendar', status='passed' if context.get('verified') is True else 'failed')
        if result['status'] == 'failed':
            result['action'] = '由自动环境准备流程更新依赖后核对日历覆盖范围；仍未覆盖时报告日历不可用。'
        return result
    except Exception as exc:
        return dict(id='trading_calendar', status='failed', **_error(exc),
                    action=REPAIR_ACTION)


def _probe_payload(probe_id, raw):
    text = raw.decode('gbk' if probe_id in ('tencent_quote', 'sina_quote') else 'utf-8')
    if probe_id == 'sina_directory':
        total = int(json.loads(text))
        if not 1000 < total < 12000:
            raise ValueError('Unexpected universe count')
        return {'reported_total': total}
    if probe_id == 'eastmoney_directory':
        payload = json.loads(text)
        rows = payload['result']['data']
        if payload.get('success') is not True or not isinstance(rows, list) or not rows:
            raise ValueError('Missing directory page')
        if any(not isinstance(row, dict) or not re.fullmatch(r'\d{6}\.(?:SH|SZ|BJ)', str(row.get('SECUCODE', ''))) for row in rows):
            raise ValueError('Invalid directory identities')
        return {'sample_rows': len(rows)}
    expression = r'v_sh600000="([^"]+)"' if probe_id == 'tencent_quote' else r'hq_str_sh600000="([^"]+)"'
    match = re.search(expression, text)
    if not match:
        raise ValueError('Missing sample quote')
    fields = match[1].split('~' if probe_id == 'tencent_quote' else ',')
    if probe_id == 'tencent_quote':
        if len(fields) < 49 or fields[2] != '600000':
            raise ValueError('Invalid sample quote')
        at = datetime.strptime(fields[30], '%Y%m%d%H%M%S').replace(tzinfo=TZ)
    else:
        if len(fields) < 32:
            raise ValueError('Invalid sample quote')
        at = datetime.strptime(fields[30] + ' ' + fields[31], '%Y-%m-%d %H:%M:%S').replace(tzinfo=TZ)
    price = float(fields[3])
    if not isfinite(price) or price <= 0:
        raise ValueError('Invalid sample price')
    return {'sample_code': '600000.SH', 'quote_at': at.isoformat(),
            'freshness_checked': False}


def _public_url(url):
    """Diagnostics identify endpoints without query values or URL credentials."""
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    if parsed.port:
        host += f':{parsed.port}'
    return urlunsplit((parsed.scheme, host, parsed.path, '', ''))


def _network_action(result):
    category = result.get('error_category')
    if category == 'timeout':
        return '本次样例请求在有界重试后仍超时；保存各次耗时，由采集流程尝试备用源并核对实际响应。'
    if category == 'tls_error':
        return '本次证书校验失败；核对执行环境的受信任证书和现有代理配置，保留证书校验。'
    if category == 'proxy_error':
        return '本次代理连接失败；核对执行环境已有的代理连通性后重新检查。'
    if result.get('http_status') == 429:
        return '接口本次返回限流；降低请求频率，稍后重试。'
    if category == 'http_error':
        return '接口本次返回 HTTP 错误；按状态码核对访问条件，由采集流程尝试备用源。'
    if category == 'invalid_response':
        return '样例响应未通过格式校验；检查接口结构是否变化，并尝试经过校验的备用源。'
    return '本次样例访问未成功；保存诊断，由采集流程尝试备用源并核对实际响应。'


def _probe(probe, get):
    start = monotonic()
    deadline = start + PROBE_BUDGET_SECONDS
    result = {key: probe[key] for key in ('id', 'name')}
    result.update(url=_public_url(probe['url']), attempts=[], budget_seconds=PROBE_BUDGET_SECONDS)
    final = {}
    for number, configured_timeout in enumerate(PROBE_TIMEOUTS, 1):
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        # requests bounds each connect/read operation separately. Keep both under
        # the remaining diagnostic budget and also check elapsed time per chunk.
        timeout = tuple(min(value, remaining / 2) for value in configured_timeout)
        attempt_start = monotonic()
        attempt = dict(number=number, timeout_seconds=dict(connect=timeout[0], read=timeout[1]))
        response = None
        try:
            response = get(probe['url'], params=probe['params'], timeout=timeout, stream=True,
                           headers={'User-Agent': 'Mozilla/5.0', 'Referer': probe['referer']})
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                if monotonic() >= deadline:
                    raise TimeoutError('Diagnostic time budget exhausted')
                size += len(chunk)
                if size > 131072:
                    raise ValueError('Probe response exceeded diagnostic size bound')
                chunks.append(chunk)
            final = dict(_probe_payload(probe['id'], b''.join(chunks)), status='passed',
                         http_status=response.status_code)
            attempt.update(status='passed', http_status=response.status_code, retryable=False)
        except Exception as exc:
            final = dict(status='failed', **_error(exc))
            retryable = final['error_category'] in ('timeout', 'connection_error', 'proxy_error') or (
                final['error_category'] == 'http_error' and final.get('http_status') in (429, 500, 502, 503, 504))
            attempt.update(final, retryable=retryable)
        finally:
            if response is not None:
                response.close()
        attempt['elapsed_seconds'] = round(monotonic() - attempt_start, 3)
        result['attempts'].append(attempt)
        if final['status'] == 'passed' or not attempt['retryable']:
            break
    if not final:
        final = dict(status='failed', error_category='timeout', error_type='DiagnosticBudgetExceeded')
    result.update(final, attempt_count=len(result['attempts']),
                  recovered_after_retry=final['status'] == 'passed' and len(result['attempts']) > 1,
                  elapsed_seconds=round(monotonic() - start, 3))
    if final['status'] == 'failed':
        result['action'] = _network_action(final)
    return result


def run_doctor(network=True, *, now=None, importer=None, version_reader=None,
               http_get=None, calendar_reader=None):
    """Return a JSON-safe diagnosis. Inject dependencies to test without network."""
    start = monotonic()
    now = now or datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    importer = importer or importlib.import_module
    version_reader = version_reader or metadata.version
    dependencies, loaded = _dependency_checks(importer, version_reader)
    python_check = dict(id='python', version='.'.join(str(x) for x in sys.version_info[:3]),
                        executable=sys.executable, minimum='3.10',
                        status='passed' if sys.version_info >= (3, 10) else 'failed')
    calendar = _calendar_check(now.astimezone(TZ), calendar_reader or _calendar_reader)
    network_checks = []
    if network:
        get = http_get or (getattr(loaded.get('requests'), 'get', None))
        if get is None:
            network_checks = [dict(id=p['id'], name=p['name'], url=_public_url(p['url']), status='skipped',
                                   error_category='missing_dependency', action=REPAIR_ACTION) for p in PROBES]
        else:
            with ThreadPoolExecutor(max_workers=len(PROBES)) as pool:
                network_checks = list(pool.map(lambda probe: _probe(probe, get), PROBES))
    failed_base = python_check['status'] == 'failed' or calendar['status'] == 'failed' or any(c['status'] == 'failed' for c in dependencies)
    failed_network = sum(c['status'] != 'passed' for c in network_checks)
    if failed_base:
        status, summary = 'blocked', '当前解释器或交易日历未就绪；本诊断只检查现状，不能据此认定自动安装失败。'
    elif network_checks and failed_network == len(network_checks):
        status, summary = 'network_unverified', '本地依赖和交易日历通过，但本次所有样例接口检查均未成功；需结合实际采集确认访问情况。'
    elif failed_network or any(c['status'] == 'warning' for c in dependencies):
        status, summary = 'degraded', '部分检查未通过；备用目录可减少单点故障，但交叉行情等必要数据仍可能阻断候选。'
    elif network:
        status, summary = 'passed', '依赖、交易日历和样例接口检查通过；仍需实际运行验证全市场覆盖和数据时效。'
    else:
        status, summary = 'offline_passed', '本地依赖和交易日历检查通过；尚未检查网络接口。'
    if python_check['status'] == 'failed' or any(c['status'] != 'passed' for c in dependencies):
        repair_action = REPAIR_ACTION
    elif calendar['status'] == 'failed':
        repair_action = calendar.get('action', '核对交易日历覆盖范围。')
    elif failed_network:
        repair_action = '；'.join(dict.fromkeys(c.get('action', '') for c in network_checks if c['status'] != 'passed'))
    else:
        repair_action = None
    return dict(version=2, mode='doctor', generated_at=now.astimezone(TZ).isoformat(),
                diagnostic_scope='current_interpreter', installation_result='not_measured',
                status=status, summary=summary, python=python_check, dependencies=dependencies,
                calendar=calendar, network_requested=bool(network), network=network_checks,
                elapsed_seconds=round(monotonic() - start, 3),
                repair_action=repair_action,
                note='本文件不记录安装过程；缺少依赖不证明自动安装失败。安装失败须结合正常入口的退出码及preparation.jsonl或执行日志判断。联网诊断只反映少量样例请求，通过也不代表全市场扫描完成、行情新鲜或产生买入信号。')
