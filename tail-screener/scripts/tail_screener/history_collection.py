"""Bounded historical acquisition with persistent workers and circuit recovery.

The provider owns HTTP retries, caches, access restrictions and route selection.
This scheduler preserves successful inputs and never interprets an unsubmitted
or circuit-rejected symbol as a failed strategy condition.
"""
from __future__ import annotations

from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
import math
import time

from .providers import BudgetExpired, safe_source_error, source_diagnostic


MAX_RECOVERY_PROBES = 3
MAX_CALLS_PER_CODE = 4
TRANSIENT = {'timeout', 'connection_error', 'proxy_error', 'request_error', 'circuit_open'}
RETRYABLE_HTTP = {429, 500, 501, 502, 503, 504}


def _failure(exc):
    detail = source_diagnostic(exc, 'history')
    category = detail['category']
    cause = getattr(exc, 'cause', None) or {}
    access_blocked = category in ('http_denied', 'tls_error') or cause.get('category') in ('http_denied', 'tls_error')
    retryable = not access_blocked and (category in TRANSIENT or
                  category == 'http_error' and detail.get('status_code') in RETRYABLE_HTTP)
    request_failed = getattr(exc, 'request_failed', category not in ('circuit_open', 'budget_expired'))
    detail['request_failed'] = bool(request_failed)
    route_attempts = getattr(exc, 'route_attempts', None)
    if route_attempts:
        detail['route_attempts'] = route_attempts
    message = safe_source_error(exc)
    if request_failed and category in ('circuit_open', 'budget_expired'):
        message += '；本次前序历史路由已发生真实请求失败'
    return {'category': category, 'retryable': retryable, 'access_blocked': access_blocked,
            'request_failed': bool(request_failed), 'detail': detail, 'message': message}


def _health(source):
    getter = getattr(source, 'history_health', None)
    if not callable(getter):
        return {}
    try:
        result = getter()
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _cooldown(source, deadline):
    """Wait only as requested by provider state; never wait past the budget."""
    health = _health(source)
    if health.get('all_routes_blocked'):
        return False, 0., health, 'access_blocked'
    raw_delay = health.get('retry_after_seconds', 1.)
    try:
        delay = float(raw_delay)
    except (TypeError, ValueError, OverflowError):
        delay = 1.
    if not math.isfinite(delay) or delay < 0:
        delay = 1.
    # A brief pause also damps request bursts before the circuit threshold.
    delay = max(.05, min(delay, 3.))
    if deadline - time.monotonic() <= delay + .01:
        return False, 0., health, 'budget_exhausted'
    waited = 0.
    until = time.monotonic() + delay
    while True:
        remaining = min(until, deadline) - time.monotonic()
        if remaining <= 0:
            break
        part = min(.25, remaining)
        time.sleep(part)
        waited += part
    return time.monotonic() < deadline, waited, health, None


def collect_histories(source, codes, *, count=300, workers=8, deadline=None):
    """Return acquired inputs, real final failures and explicit unfinished work.

    ``attempted_codes`` contains history-call submissions and pure-cache hits.
    ``logical_calls`` counts source.history calls, including provider cache hits
    and circuit refusals; it is deliberately not called an HTTP request count.
    ``errors`` includes only unresolved codes with a genuine request/data error;
    a circuit refusal or an unsubmitted symbol alone cannot create an error.
    """
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 16:
        raise ValueError('workers须为1—16的整数')
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 1000:
        raise ValueError('count须为1—1000的整数')
    codes = list(codes)
    if any(not isinstance(code, str) or not code for code in codes) or len(set(codes)) != len(codes):
        raise ValueError('历史采集代码须为非空且不重复的字符串')
    boundary = getattr(source, 'deadline', float('inf'))
    deadline = min(boundary, float(deadline) if deadline is not None else boundary)
    if not math.isfinite(deadline):
        raise ValueError('历史采集必须有有限的单调时钟截止时间')
    histories, errors, failure_details, request_failure_details = {}, {}, {}, {}
    attempted, had_failure, recovered = set(), set(), set()
    categories, calls_by_code = Counter(), Counter()
    cache_counts = {'hits': 0, 'misses': 0, 'errors': 0, 'unchecked': len(codes)}
    recovery_events, probe_count = [], 0
    concurrency = workers
    healthy_completions = 0
    terminal_reason = None
    first_started = time.monotonic()
    waves = []
    peak_in_flight = 0

    def submit_history(pool, code):
        attempted.add(code)
        calls_by_code[code] += 1

        def guarded_history():
            if time.monotonic() >= deadline:
                raise BudgetExpired('历史采集预算已用尽')
            return source.history(code, count=count)

        return pool.submit(guarded_history)

    def record(code, value=None, exc=None):
        if exc is None:
            histories[code] = value
            errors.pop(code, None)
            failure_details.pop(code, None)
            request_failure_details.pop(code, None)
            if code in had_failure:
                recovered.add(code)
            return None
        failure = _failure(exc)
        categories[failure['category']] += 1
        had_failure.add(code)
        failure_details[code] = failure['detail']
        if failure['request_failed']:
            errors[code] = failure['message']
            request_failure_details[code] = failure['detail']
        return failure

    def execute(pool, wave):
        nonlocal terminal_reason
        pending = {}
        for code in wave:
            if time.monotonic() >= deadline:
                terminal_reason = 'budget_exhausted'
                break
            pending[submit_history(pool, code)] = code
        failures = []
        for future in as_completed(pending):
            code = pending[future]
            try:
                record(code, value=future.result())
            except Exception as exc:
                failure = record(code, exc=exc)
                failures.append((code, failure))
        return failures

    def execute_window(pool, queue):
        """Keep a bounded window busy, then drain it before route recovery."""
        nonlocal terminal_reason, concurrency, healthy_completions, peak_in_flight
        pending, failures, fault_window = {}, [], set()
        draining = False
        segment = {'workers': concurrency, 'submitted': 0, 'failed': 0}

        def replenish():
            nonlocal terminal_reason, peak_in_flight
            while queue and len(pending) < concurrency:
                if time.monotonic() >= deadline:
                    terminal_reason = 'budget_exhausted'
                    return
                code = queue.popleft()
                pending[submit_history(pool, code)] = code
                segment['submitted'] += 1
                peak_in_flight = max(peak_in_flight, len(pending))

        replenish()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            completed_codes = [pending[future] for future in done]
            successes = 0
            batch_failed = False
            # Process every already-completed result before replenishing: a
            # failed result in the same completion group must pause dispatch.
            for future in done:
                code = pending.pop(future)
                try:
                    record(code, value=future.result())
                    successes += 1
                except Exception as exc:
                    failure = record(code, exc=exc)
                    batch_failed = True
                    failures.append((code, failure))
                    segment['failed'] += 1
                    if (failure['retryable'] or failure['access_blocked'] or
                            failure['category'] == 'budget_expired'):
                        if not draining:
                            fault_window.update(completed_codes)
                            fault_window.update(pending.values())
                        draining = True
            if draining or terminal_reason is not None:
                continue
            if batch_failed:
                healthy_completions = 0
            else:
                healthy_completions += successes
                if healthy_completions >= 2 * concurrency and concurrency < workers:
                    waves.append(segment)
                    concurrency = min(workers, concurrency * 2)
                    healthy_completions = 0
                    segment = {'workers': concurrency, 'submitted': 0, 'failed': 0}
            replenish()
        if segment['submitted'] or segment['failed']:
            waves.append(segment)
        return failures, fault_window

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='author-history') as pool:
        # Pure-cache preflight means a global transport outage cannot suppress
        # valid existing inputs behind thousands of pointless circuit calls.
        cached = getattr(source, 'cached_history', None)
        if callable(cached):
            for offset in range(0, len(codes), workers * 4):
                if time.monotonic() >= deadline:
                    terminal_reason = 'budget_exhausted'
                    break
                wave = codes[offset:offset + workers * 4]
                pending = {pool.submit(cached, code, count=count): code for code in wave}
                for future in as_completed(pending):
                    code = pending[future]
                    cache_counts['unchecked'] -= 1
                    try:
                        value = future.result()
                    except Exception:
                        cache_counts['errors'] += 1
                        continue
                    if value is not None:
                        histories[code] = value
                        attempted.add(code)
                        cache_counts['hits'] += 1
                    else:
                        cache_counts['misses'] += 1
        queue = deque(code for code in codes if code not in histories)
        while queue and terminal_reason is None:
            if time.monotonic() >= deadline:
                terminal_reason = 'budget_exhausted'
                break
            failures, wave = execute_window(pool, queue)
            if terminal_reason:
                break
            if any(failure['access_blocked'] for _, failure in failures):
                terminal_reason = 'access_blocked'
                break
            if any(failure['category'] == 'budget_expired' for _, failure in failures):
                terminal_reason = 'budget_exhausted'
                break
            retry = [code for code, failure in failures
                     if failure['retryable'] and calls_by_code[code] < MAX_CALLS_PER_CODE]
            if not retry:
                continue
            healthy_completions = 0
            probe_ok = False
            for probe_index in range(MAX_RECOVERY_PROBES):
                can_probe, waited, health, reason = _cooldown(source, deadline)
                if not can_probe:
                    terminal_reason = reason or 'budget_exhausted'
                    break
                available = [code for code in retry if calls_by_code[code] < MAX_CALLS_PER_CODE]
                if not available and not queue:
                    break
                # A single stock can have a persistent bad response while its
                # host is healthy. A different symbol tests route recovery
                # without declaring the entire market down from one ticker.
                probe = queue.popleft() if probe_index > 0 and queue else available[0]
                probe_count += 1
                failed = execute(pool, [probe])
                event = {'probe_code': probe, 'wait_seconds': round(waited, 4),
                         'workers_before': concurrency, 'host_health': health,
                         'outcome': 'recovered' if probe in histories else 'failed'}
                recovery_events.append(event)
                if probe in histories:
                    probe_ok = True
                    break
                if terminal_reason:
                    break
                if failed and failed[0][1]['access_blocked']:
                    terminal_reason = 'access_blocked'
                    break
                if failed and failed[0][1]['category'] == 'budget_expired':
                    terminal_reason = 'budget_exhausted'
                    break
                if failed and not failed[0][1]['retryable']:
                    # A symbol-specific invalid response must not prevent
                    # other symbols from being requested after recovery.
                    if probe in retry:
                        retry.remove(probe)
                    if not retry:
                        probe_ok = True
                        break
                elif failed and probe not in retry:
                    retry.append(probe)
            if not probe_ok:
                # Several bad symbols do not establish a route-wide outage.
                # Provider state distinguishes local data/request failures
                # from a circuit that actually suppresses every route.
                routes = _health(source).get('routes', [])
                has_ready_route = any(isinstance(route, dict) and route.get('state') == 'ready'
                                      for route in routes)
                if terminal_reason is not None or (not has_ready_route and
                                                   not any(code in histories for code in wave)):
                    terminal_reason = terminal_reason or 'recovery_exhausted'
                    break
                # A ready route or a success in this wave provides evidence
                # that remaining symbols can still be collected. Per-symbol
                # call limits and the deadline keep this path bounded.
            concurrency = min(2, workers)
            retry = [code for code in retry
                     if code not in histories and calls_by_code[code] < MAX_CALLS_PER_CODE]
            if probe_ok:
                # Once recovery is confirmed, fill the earlier gaps first.
                queue.extendleft(reversed(retry))
            else:
                # A nominally ready route has not yet supplied a good probe.
                # Try other symbols before spending the same ticker's entire
                # retry allowance on a transient route-wide failure.
                queue.extend(retry)

    if terminal_reason is None:
        terminal_reason = 'completed' if len(histories) == len(codes) else 'completed_with_errors'
    unfinished = {}
    for code in codes:
        if code in histories:
            continue
        if code in errors:
            unfinished[code] = 'access_blocked' if failure_details.get(code, {}).get('category') in ('http_denied', 'tls_error') else 'request_failed'
        elif terminal_reason == 'budget_exhausted':
            unfinished[code] = 'budget_exhausted'
        elif terminal_reason == 'access_blocked':
            unfinished[code] = 'access_blocked'
        elif failure_details.get(code, {}).get('category') == 'circuit_open' or terminal_reason == 'recovery_exhausted':
            unfinished[code] = 'circuit_deferred'
        else:
            unfinished[code] = 'not_attempted'
    not_attempted = [code for code in codes if code not in attempted and code not in histories]
    diagnostics = {
        'requested_count': len(codes), 'attempted_count': len(attempted),
        'succeeded_count': len(histories), 'failed_count': len(errors),
        'deferred_count': len(unfinished) - len(errors), 'not_attempted_count': len(not_attempted),
        'logical_calls': sum(calls_by_code.values()), 'retried_calls': sum(max(0, value - 1) for value in calls_by_code.values()),
        'category_counts': dict(categories), 'unfinished_counts': dict(Counter(unfinished.values())),
        'failed_code_details': {code: request_failure_details[code] for code in errors},
        'recovered_count': len(recovered), 'cache_recovered_count': cache_counts['hits'],
        'cache_statuses': cache_counts, 'host_health': _health(source),
        'recovery': {'probe_count': probe_count, 'events': recovery_events, 'terminal_reason': terminal_reason},
        'dispatch_mode': 'bounded_sliding_window', 'peak_in_flight': peak_in_flight,
        'waves': waves, 'elapsed_seconds': round(time.monotonic() - first_started, 3),
        'count_note': 'attempted含纯缓存命中；logical_calls为history调用次数，不是HTTP请求数。熔断拒绝与未派发代码不单独计为实际请求失败。',
    }
    return {'histories': histories, 'errors': errors,
            'attempted_codes': [code for code in codes if code in attempted],
            'not_attempted_codes': not_attempted, 'unfinished_labels': unfinished,
            'diagnostics': diagnostics}
