"""Bounded retries for observational calls only; mutations/cursors run once."""
import datetime as dt
import email.utils
import random
import time
import math
import ipaddress
from urllib.parse import urlsplit

from errors import SkillError

SAFE = {'status', 'volume', 'count'}


def validate_endpoints(api):
    """Reject unauthorized remote cleartext before any request/budget callback.

    Literal loopback HTTP supports offline acceptance and a host-local gateway.
    Missing keys are handled by configuration validation/the concrete caller.
    This is not DNS/SSRF protection or proof of a gateway's upstream transport.
    """
    for key, value in api.get('api', {}).items():
        try:
            if not isinstance(value, str) or any(c.isspace() for c in value):
                raise ValueError()
            url = urlsplit(value)
            if not url.hostname or url.username is not None or url.password is not None or url.query or url.fragment:
                raise ValueError()
            port = url.port  # Validate malformed/out-of-range ports without printing URL.
            if url.scheme == 'https':
                continue
            # Explicit internal-test policy, never a fallback from failed TLS.
            # The login/create host and arbitrary HTTP gateways remain excluded.
            if (key == 'count_base_url' and
                    api.get('runtime', {}).get('transport_profile') == 'official-http-internal' and
                    value.rstrip('/') == 'http://api.dc.datastory.com.cn/api'):
                continue
            if url.scheme == 'http' and ipaddress.ip_address(url.hostname).is_loopback:
                continue
        except (ValueError, TypeError):
            pass
        raise SkillError('INSECURE_OR_INVALID_ENDPOINT', 'security',
            next_action='核对管理员配置的HTTPS端点或已授权的官方HTTP内测配置；禁止关闭证书校验或自动降级。')


def retry_after(value):
    try:
        number=float(value)
        return max(0., number) if math.isfinite(number) else None
    except (TypeError, ValueError):
        try:
            date=email.utils.parsedate_to_datetime(value)
            return max(0., date.timestamp()-time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def failure(operation, status=None, provider=None, delay=None):
    value=status if status and status>=300 else provider
    category = ('authentication' if value in (401,100000) else 'permission' if value in (403,100001) else
                'credentials' if value==100110 else
                'rate_limit' if value==429 else 'provider' if value and 500<=value<=599 else 'response')
    retryable=operation in SAFE and (value==429 or bool(value and 500<=value<=599))
    action = {'authentication':'核对凭证；固定 token 需由维护方更新。',
              'permission':'核对账户和接口权限后继续原请求。',
              'credentials':'账号或密码错误；停止重试，由维护方更新凭证。',
              'rate_limit':'等待 Retry-After 指定时间，再恢复原请求。'}.get(category,'保留运行目录，核对供应商结果后恢复原请求。')
    if operation in {'create','download'}:
        action='请求结果可能已生效；创建用 recover/reconcile 核对，下载用原 job 的分页恢复，禁止盲目重放。'
    return SkillError('REMOTE_'+category.upper(),category,operation=operation,retryable=retryable,
        http_status=status,provider_code=provider,retry_after_seconds=delay,
        outcome_uncertain=operation in {'create','download'},next_action=action)


def call_json(operation, call, api, emit=None, refresh=None, budget=None):
    validate_endpoints(api)
    rt=api.get('runtime',{})
    attempts=rt.get('retry_attempts',3); seconds=rt.get('retry_budget_seconds',45)
    if type(attempts) is not int or not 1<=attempts<=5 or type(seconds) not in (int,float) or not 0<seconds<=60:
        raise SkillError('INVALID_RETRY_POLICY')
    attempts=attempts if operation in SAFE else 1
    seconds=min(seconds,budget) if budget is not None else seconds
    deadline=time.monotonic()+seconds; refreshed=False
    for attempt in range(1,attempts+1):
        remaining=deadline-time.monotonic()
        if remaining<=0:
            raise SkillError('RETRY_BUDGET_EXHAUSTED','timeout',operation=operation)
        if emit: emit(dict(operation=operation,event='request_attempt',attempt=attempt,max_attempts=attempts))
        response=None
        try:
            # Connect/read phases share a conservative allowance. Requests timeouts
            # are inactivity bounds, not a hard wall-clock interrupt (see SKILL).
            response=call(min(30.,remaining/2))
            status=getattr(response,'status_code',200)
            status=status if type(status) is int else 200
            delay=retry_after(getattr(response,'headers',{}).get('Retry-After'))
            if status>=300: raise failure(operation,status,delay=delay)
            try: payload=response.json()
            except (ValueError,TypeError):
                raise SkillError('REMOTE_JSON_INVALID','schema',operation=operation,
                    outcome_uncertain=operation in {'create','download'}) from None
            if not isinstance(payload,dict):
                raise SkillError('REMOTE_SCHEMA_INVALID','schema',operation=operation,
                    outcome_uncertain=operation in {'create','download'})
            code=payload.get('code'); code=code if type(code) is int else None
            if payload.get('success') is not True or (code is not None and code>=400):
                raise failure(operation,status,code,delay)
            return payload
        except SkillError as exc:
            error=exc
        except Exception as exc:
            # Import is optional so diagnostics remain usable without requests.
            import requests
            if isinstance(exc,requests.exceptions.SSLError):
                raise SkillError('TLS_VERIFICATION_FAILED','security',operation=operation,
                    next_action='核对端点与证书；不要禁用 TLS 校验。') from None
            if not isinstance(exc,(requests.Timeout,requests.ConnectionError)):
                raise
            error=SkillError('NETWORK_TIMEOUT' if isinstance(exc,requests.Timeout) else 'NETWORK_CONNECTION',
                'network',operation=operation,retryable=operation in SAFE,
                outcome_uncertain=operation in {'create','download'},
                next_action='保留运行目录；状态/声量可稍后重查，创建和游标结果未知时按原请求恢复。')
        finally:
            if response is not None and callable(getattr(response,'close',None)): response.close()
        error.detail.update(attempt=attempt,max_attempts=attempts)
        remaining=deadline-time.monotonic()
        can_refresh=(operation=='status' and error.detail['category']=='authentication' and refresh and not refreshed)
        if attempt>=attempts or remaining<=0: raise error from None
        if can_refresh:
            refreshed=True
            if emit: emit(dict(operation=operation,event='refresh_credentials',attempt=attempt))
            refresh(remaining)
            continue
        if not error.detail['retryable']: raise error from None
        delay=max(error.detail['retry_after_seconds'] or 0, min(8.,2**(attempt-1))+random.uniform(0,.25))
        if delay>=remaining: raise error from None
        if emit: emit(dict(operation=operation,event='retry_wait',attempt=attempt,delay_seconds=delay,code=error.detail['code']))
        time.sleep(delay)
    raise AssertionError('UNREACHABLE')
