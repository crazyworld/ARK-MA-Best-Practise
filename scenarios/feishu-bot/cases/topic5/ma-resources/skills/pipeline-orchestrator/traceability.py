"""需求确认单留痕字段清洗（FIX-09）：更正记录、字段来源、Skill 调用记录。

唯一事实源在 skill 内：沙箱只挂载 skill 目录，hc_bridge 在 G1 续跑时直接使用；
Gateway 侧 topic5_intake 反向引用本模块，两端口径一致。
"""
from __future__ import annotations

from typing import Any


def _unwrap(value: Any) -> Any:
    """兼容飞书表单值的字符串、数组及 {value/key/id} 包装。"""
    if isinstance(value, dict):
        for key in ("value", "key", "id"):
            if key in value:
                return _unwrap(value[key])
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    return value


def clean_corrections(value: Any) -> list[dict]:
    """更正记录（FIX-09）：[{at, field, old, new, source}]，用于 Validator 核对最新值。"""
    value = _unwrap(value)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        item = _unwrap(item)
        if not isinstance(item, dict):
            continue
        out.append({
            "at": str(item.get("at") or item.get("time") or "").strip(),
            "field": str(item.get("field") or "").strip(),
            "old": item.get("old"),
            "new": item.get("new"),
            "source": str(item.get("source") or "user").strip(),
        })
    return out


def clean_field_sources(value: Any) -> dict[str, str]:
    """字段来源（FIX-09）：{字段: 原话|默认|推断|用户更正}。"""
    value = _unwrap(value)
    if not isinstance(value, dict):
        return {}
    allowed = {"原话", "默认", "推断", "用户更正",
               "utterance", "default", "inferred", "correction"}
    out = {}
    for key, source in value.items():
        text = str(source or "").strip()
        if text in allowed:
            out[str(key)] = text
    return out


def clean_skill_invocations(value: Any) -> list[dict]:
    """Skill 调用记录（FIX-09）：[{skill, version, at, ok, note}]。"""
    value = _unwrap(value)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        item = _unwrap(item)
        if not isinstance(item, dict):
            continue
        out.append({
            "skill": str(item.get("skill") or "").strip(),
            "version": str(item.get("version") or "").strip(),
            "at": str(item.get("at") or "").strip(),
            "ok": bool(item.get("ok", True)),
            "note": str(item.get("note") or "").strip(),
        })
    return out
