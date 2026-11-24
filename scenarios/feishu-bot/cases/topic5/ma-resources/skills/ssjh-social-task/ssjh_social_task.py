"""SSJH provider functions; the CLI uses guarded workflow and version.py."""

import os
import re
import sys
import json
import time
import datetime
import pathlib
import argparse
try:
    import requests
except ImportError:
    requests = None
from errors import SkillError
from transport import call_json
import urllib.parse

try:
    import yaml
except ImportError:
    yaml = None


# ─────────────────────────────────────────────
# 配置加载
# ─────────────────────────────────────────────
def load_yaml(path: str) -> dict:
    if yaml is None: raise ImportError("PyYAML")
    with open(path, "r", encoding="utf-8-sig") as f:
        try: value = yaml.safe_load(f)
        except yaml.YAMLError: raise SkillError("YAML_CONFIG_INVALID") from None
    if not isinstance(value, dict): raise SkillError("CONFIG_MAPPING_REQUIRED")
    return value


def resolve_env(value: str) -> str:
    """
    解析 ${ENV_VAR} 占位符，替换为环境变量值。
    若环境变量不存在，保留原始字符串（调用方负责校验）。
    """
    if not isinstance(value, str):
        return value
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        value
    )


def load_configs(config_path: str, api_config_path: str) -> tuple:
    """加载任务配置和 API 配置，返回 (cfg, api_cfg)"""
    cfg = load_yaml(config_path)
    api_cfg = load_yaml(api_config_path)

    # 解析凭证环境变量
    creds = api_cfg.get("credentials", {})
    creds["username"]    = resolve_env(creds.get("username", ""))
    creds["password"]    = resolve_env(creds.get("password", ""))
    creds["fixed_token"] = resolve_env(creds.get("fixed_token", ""))

    return cfg, api_cfg


# ─────────────────────────────────────────────
# 字段模板加载
# ─────────────────────────────────────────────
def load_template(template_path: str) -> list:
    """
    加载输出模板，返回 [(field_name, display_name), ...] 有序列表。
    template_path 为空则返回 None（输出全量字段）。
    """
    if not template_path:
        return None
    p = pathlib.Path(template_path)
    if not p.exists():
        raise SkillError("TEMPLATE_NOT_FOUND")
    data = load_yaml(str(p))
    columns = data.get("columns")
    if not isinstance(columns, list) or not columns:
        raise SkillError("TEMPLATE_COLUMNS_REQUIRED")
    result = []
    for col in columns:
        if not isinstance(col, dict) or set(col) - {'field', 'rename'}:
            raise SkillError("TEMPLATE_COLUMN_INVALID")
        field = col.get("field")
        rename = col.get("rename", field)
        if not all(isinstance(v, str) and v.strip() and v == v.strip() for v in (field, rename)):
            raise SkillError("TEMPLATE_COLUMN_INVALID")
        if any(field == f or rename == h for f, h in result):
            raise SkillError("TEMPLATE_DUPLICATE_FIELD_OR_HEADER")
        result.append((field, rename))
    return result


# ─────────────────────────────────────────────
# Step 1: 动态 Token 管理（7天缓存）
# ─────────────────────────────────────────────
def get_token_cache_path(api_cfg: dict) -> pathlib.Path:
    raw = api_cfg.get("runtime", {}).get("token_cache_file", "~/.ssjh_token_cache.json")
    return pathlib.Path(raw).expanduser()


def load_cached_token(cache_path: pathlib.Path):
    if not cache_path.exists():
        return None
    try:
        cache = json.loads(cache_path.read_text())
        if time.time() < cache.get("expire_at", 0):
            return cache["token"]
    except Exception as exc:
        print(f"token cache unreadable ({type(exc).__name__}); will re-login", file=sys.stderr)
    return None


def save_token_cache(cache_path: pathlib.Path, token: str):
    cache = {"token": token, "expire_at": time.time() + 604800 - 1800}
    cache_path.write_text(json.dumps(cache))


def get_dynamic_token(api_cfg: dict, force_refresh: bool = False, budget=None) -> str:
    base_url   = api_cfg["api"]["base_url"]
    username   = api_cfg["credentials"]["username"]
    password   = api_cfg["credentials"]["password"]
    cache_path = get_token_cache_path(api_cfg)

    if not force_refresh and api_cfg.get("runtime", {}).get("cache_token", False):
        cached = load_cached_token(cache_path)
        if cached:
            return cached

    if requests is None: raise ImportError("requests")
    result = call_json('login', lambda timeout: requests.post(
        f"{base_url}/auth/obtain",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=urllib.parse.urlencode({"username": username, "password": password}), timeout=timeout,allow_redirects=False), api_cfg, budget=budget)
    token = result.get("data")
    if not isinstance(token,str) or not token: raise SkillError("LOGIN_TOKEN_INVALID",'schema',operation='login')
    if api_cfg.get("runtime", {}).get("cache_token", False):
        save_token_cache(cache_path, token)
    return token


# ─────────────────────────────────────────────
# Step 2: 创建任务
# ─────────────────────────────────────────────
def to_timestamp_ms(date_str: str, end_of_day: bool = False) -> int:
    if ":" in str(date_str):
        dt = datetime.datetime.strptime(str(date_str), "%Y-%m-%d %H:%M:%S")
    else:
        suffix = " 23:59:59" if end_of_day else " 00:00:00"
        dt = datetime.datetime.strptime(str(date_str) + suffix, "%Y-%m-%d %H:%M:%S")
    return int(dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp() * 1000)


def build_search_condition(cfg):
    from query_contract import normalize, task_payload
    return task_payload(normalize(cfg),cfg["profiler"]["name"])["searchCondition"]


def build_profilers(cfg):
    from query_contract import normalize, task_payload
    return task_payload(normalize(cfg),cfg["profiler"]["name"])["profilers"]


def create_task(token: str, cfg: dict, api_cfg: dict, prepared=None) -> int:
    base_url = api_cfg["api"]["base_url"]
    es_cfg   = api_cfg["es"]
    task_cfg = cfg.get("task", {})
    rt_cfg   = api_cfg.get("runtime", {})
    api_task = api_cfg.get("task", {})

    prepared = prepared or {"searchCondition":build_search_condition(cfg),"profilers":build_profilers(cfg)}
    sc = prepared["searchCondition"]
    profilers = prepared["profilers"]

    task_name = (task_cfg.get("name") or "").strip()
    if not task_name:
        obj_name = cfg["profiler"]["name"]
        task_name = f"OpenClaw_{obj_name}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}"

    # 邮箱：任务配置优先，其次 api_config 默认值
    email = (task_cfg.get("email") or api_task.get("email", "")).strip()

    schedule = {
        "emails":    email,
        "isTemp":    1,
        "unit":      "d",
        "interval":  1,
        "emailMode": api_task.get("email_mode", 2),
    }

    es_config = {
        "host":      es_cfg["host"],
        "cluster":   es_cfg["cluster"],
        "indexName": es_cfg["index"],
    }

    post_data = {
        "name":               task_name,
        "jobType":            "1",
        "sheetName":          api_task.get("sheet_name", "API任务"),
        "searchCondition":    json.dumps(sc, ensure_ascii=False),
        "profiltersListStr":  json.dumps(profilers, ensure_ascii=False),
        "schedule":           json.dumps(schedule, ensure_ascii=False),
        "es":                 json.dumps(es_config, ensure_ascii=False),
    }

    if requests is None: raise ImportError("requests")
    result = call_json('create', lambda timeout: requests.post(
        f"{base_url}/project/job/add",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": token},
        data=urllib.parse.urlencode(post_data), timeout=timeout,allow_redirects=False), api_cfg)
    job_id = result.get("data")
    if type(job_id) is not int or job_id<=0:
        raise SkillError("CREATE_RESPONSE_INVALID_DO_NOT_REPLAY",'schema',operation='create',outcome_uncertain=True)
    return job_id



# ─────────────────────────────────────────────
# 校验配置
# ─────────────────────────────────────────────
def validate_configs(cfg: dict, api_cfg: dict):
    errors = []
    creds = api_cfg.get("credentials", {})
    if not creds.get("username") or creds["username"].startswith("${"):
        errors.append("credentials.username 未设置（请配置环境变量 SSJH_USERNAME）")
    if not creds.get("password") or creds["password"].startswith("${"):
        errors.append("credentials.password 未设置（请配置环境变量 SSJH_PASSWORD）")
    if not creds.get("fixed_token") or creds["fixed_token"].startswith("${"):
        errors.append("credentials.fixed_token 未设置（请配置环境变量 SSJH_FIXED_TOKEN）")

    search = cfg.get("search", {})
    if not search.get("start_date"):
        errors.append("search.start_date 未填写")
    if not search.get("end_date"):
        errors.append("search.end_date 未填写")
    if not search.get("source"):
        errors.append("search.source 未填写")

    profiler = cfg.get("profiler", {})
    if not profiler.get("name"):
        errors.append("profiler.name 未填写")
    if not profiler.get("keywords") and not any([
        profiler.get("weibo_uid"), profiler.get("biz"), profiler.get("xhs_acc_id"),
        profiler.get("bili_acc_id"), profiler.get("sv_acc_id"), profiler.get('kuaishou_uid'), profiler.get('shipinhao_uid'), profiler.get('zhihu_uid')
    ]):
        errors.append("profiler 至少需要填写 keywords 或一个账号 ID")

    if not cfg.get("output", {}).get("file"):
        errors.append("output.file 未填写")

    if errors:
        raise SkillError("REQUIRED_CONFIG_OR_CREDENTIAL_MISSING",next_action="运行 doctor，补全配置与凭证后重试原请求。")



# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main():
    # All command-line creation paths go through the precheck and confirmation gate.
    from workflow import main as guarded_main
    return guarded_main()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
