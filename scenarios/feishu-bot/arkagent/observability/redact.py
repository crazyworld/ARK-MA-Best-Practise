"""观测输出的脱敏。

凭据一旦进入 trace 就会被看板、CLI、诊断 Agent 多处复用,所以在进入模型之前统一脱敏,
而不是让每个出口各自处理。
"""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# 键名规则:只认"凭据类"字段,不能用"含 token 子串"——模型用量字段 input_tokens / output_tokens /
# cache_read_input_tokens / total_tokens 的名字里都有 token,那是**计数**,不是机密(2026-11-12 实测:
# 旧规则把整份文档里的模型用量全替换成了 [REDACTED])。
#
# 区分办法:凭据的名字里 token 是**单数**(access_token、refresh_token、token),用量是**复数**(…_tokens)。
# 这是启发式:单数的 token 一律当凭据,复数的 tokens 一律当计数,不再往下细分。
_SENSITIVE_KEY = re.compile(
    r"(?<![A-Za-z])(?:"
    r"(?:access|refresh|id|auth|bearer|session|api|user|app|tenant)[-_]?token(?!s)"
    r"|token(?!s)"                      # 字段名恰好是 token
    r"|secret|password|passwd|authorization|api[-_]?key|credential|cookie"
    r"|auth[-_]?header[-_]?value"       # BYOK_MODEL_AUTH_HEADER_VALUE:请求级模型鉴权头的值
    r")",
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
    # JSON 文本:"access_token": "xxx"(键、值都带引号;上面的规则要求键后紧跟 = 或 :,匹配不到)
    (
        re.compile(
            r"(?i)(\"[A-Za-z0-9_-]*(?:token(?!s)|secret|password|passwd|api[-_]?key|authorization|credential|cookie"
            r"|auth[-_]?header[-_]?value)[A-Za-z0-9_-]*\"\s*:\s*)\"[^\"]*\""
        ),
        r'\1"' + REDACTED + '"',
    ),
    # FOO_TOKEN=xxx / apiKey: xxx
    (
        re.compile(
            r"(?i)\b([A-Za-z0-9_]*(?:token(?!s)|secret|password|passwd|api_?key)[A-Za-z0-9_]*)"
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
