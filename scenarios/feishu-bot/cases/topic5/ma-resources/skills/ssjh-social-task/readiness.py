"""Credential-free connection checks, separate from offline doctor and billing."""
import socket
import ssl
from urllib.parse import urlsplit
from transport import validate_endpoints
from errors import describe


def connections(api):
    """Probe configured hosts only. No HTTP payload, credentials or supplier task."""
    validate_endpoints(api)
    rows=[]
    for key, purpose in [('base_url', 'login_create_status'), ('count_base_url', 'volume_count_download')]:
        url=urlsplit(api['api'][key]);port=url.port or (443 if url.scheme=='https' else 80)
        row=dict(endpoint=key,purpose=purpose,scheme=url.scheme,host=url.hostname,port=port)
        try:
            with socket.create_connection((url.hostname,port),timeout=8) as raw:
                if url.scheme=='https':
                    with ssl.create_default_context().wrap_socket(raw,server_hostname=url.hostname):
                        row.update(ok=True,code='TLS_VERIFIED')
                else:
                    row.update(ok=True,code='TCP_CONNECTED_HTTP_INTERNAL' if url.hostname=='api.dc.datastory.com.cn' else 'TCP_CONNECTED_LOOPBACK')
        except ssl.SSLCertVerificationError:
            row.update(ok=False,code='TLS_VERIFICATION_FAILED')
        except (ssl.SSLError,OSError,ValueError):
            row.update(ok=False,code='CONNECTION_FAILED')
        rows.append(row)
    return dict(ok=all(r['ok'] for r in rows),endpoints=rows,http_requests=0,
                authentication='NOT_TESTED',data_contract='NOT_TESTED',
                warning='HTTP仅按已授权内测配置使用；TCP连接成功不证明接口或凭证有效。')


def preflight(config,api_config,run_dir=None,registry=None):
    from diagnostics import doctor
    import ssjh_social_task as core
    report=doctor(config,api_config,run_dir,registry)
    try:
        network=connections(core.load_yaml(api_config))
    except Exception as exc:
        network=dict(ok=False,error=describe(exc,'connection_preflight'))
    return dict(ok=report['ready_for_local_checks'] and network['ok'],local=report,connection=network,
                task_created=False,billing_calls=0,live_collection='NOT_TESTED')
