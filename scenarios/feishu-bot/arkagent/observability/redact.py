"""观测输出的脱敏。

凭据一旦进入 trace 就会被看板、CLI、诊断 Agent 多处复用,所以在进入模型之前统一脱敏,
而不是让每个出口各自处理。
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEY = re.compile(
    r"token|secret|password|passwd|authorization|api[-_]?key|credential|cookie",
    re.IGNORECASE,
)

_TEXT_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer xxx / Authorization=xxx
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s'\"]+"),
        r"\1" + REDACTED,
    ),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer " + REDACTED),
    # --token xxx / --api-key=xxx
    (
        re.compile(
            r"(?i)(--?(?:access[-_]?token|token|password|passwd|secret|api[-_]?key)"
            r"[=\s]+)[^\s'\"]+"
        ),
        r"\1" + REDACTED,
    ),
    # FOO_TOKEN=xxx / apiKey: xxx
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:token|secret|password|passwd|api_?key)[A-Za-z0-9_]*)"
            r"(\s*[=:]\s*)[^\s'\",;]+"
        ),
        r"\1\2" + REDACTED,
    ),
    # 常见凭据前缀:飞书 u-/t-、sk- 以及 JWT
    (re.compile(r"\b[ut]-[A-Za-z0-9_-]{20,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"), REDACTED),
)


def redact_text(text: str) -> str:
    for pattern, replacement in _TEXT_RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any) -> Any:
    """递归脱敏:敏感键的值整体替换,其余字符串按文本规则处理。"""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_KEY.search(str(key)) else redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
