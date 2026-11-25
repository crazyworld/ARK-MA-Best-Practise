"""语义节点的 Ark Chat 客户端 + 离线降级。

语义类 handler（UGC 发现 / 观点候选 / 词云解读 / 报告撰写 / P0 修复）接真语义：
走 bmc-llm-relay（blueai 模型网关）OpenAI 兼容 chat/completions（对齐 Topic6 relay.py）。
无凭据时降级到确定性 fallback——**输出 schema 与真路径完全一致**，供下游节点
（聚合/图表/质检/交付）无差别消费；fallback payload 带 `degraded: true` 标记便于审计。

凭据只从环境变量读取，按以下顺序取第一个非空值：
  BASE_URL：LLM_RELAY_BASE_URL（如 https://bmc-llm-relay.bluemediagroup.cn/v1）
  Key：    LLM_RELAY_API_KEY
  模型：   TOPIC5_SEMANTIC_MODEL → 默认 claude-haiku-4-5-20251001（原版直连模型）
不回退 ARK_* / OPENAI_*：缺 LLM_RELAY_* 即视为模型不可用，避免静默改走方舟。
"""
from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@contextmanager
def _hard_deadline(seconds: int):
    """Interrupt blocking SSL reads even when a stream keeps trickling bytes."""
    if (
        seconds <= 0
        or threading.current_thread() is not threading.main_thread()
        or not hasattr(signal, "setitimer")
    ):
        yield
        return

    def on_timeout(_signum, _frame):
        raise TimeoutError(f"方舟流式响应超过整体时限 {seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, on_timeout)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def _base_key() -> tuple[str, str]:
    base = (os.environ.get("LLM_RELAY_BASE_URL") or "").rstrip("/")
    key = os.environ.get("LLM_RELAY_API_KEY") or ""
    return base, key


def llm_available() -> bool:
    """是否具备调真模型的凭据。无凭据时 handler 走确定性降级。"""
    base, key = _base_key()
    return bool(base and key)


def _model() -> str:
    return os.environ.get("TOPIC5_SEMANTIC_MODEL") or _DEFAULT_MODEL


def parse_json(text: str) -> Any:
    """剥 markdown 围栏/前后缀说明后解析 JSON；失败再按首尾花括号/方括号截取。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for lo, hi in (("{", "}"), ("[", "]")):
        start, end = text.find(lo), text.rfind(hi)
        if 0 <= start < end:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("无法解析模型输出为 JSON")


# FIX-56：客户 LM「错误案例与修正」按节点注入（原版 Producer Step2 必读）。
LM_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lm")
LM_HEADING = "历史错误与修正（必须遵守）"
_CASE_RE = re.compile(r"^## 错误案例 #(\d+)(.*?)$(.*?)(?=^## |\Z)", re.M | re.S)
_FIELD_RE = re.compile(r"\*\*(错误表现|预防规则)\*\*[：:](.*?)(?=\n\*\*|\n---|\Z)", re.S)


def lm_text(scope: str) -> str:
    """返回该节点适用的错误案例（错误表现 + 预防规则），按 lm_index 优先级截断到 max_chars。"""
    with open(os.path.join(LM_DIR, "lm_index.json"), encoding="utf-8") as f:
        index = json.load(f)
    ids = index["scopes"].get(scope)
    if ids is None:
        raise KeyError(f"lm_scope 未登记：{scope}")
    with open(os.path.join(LM_DIR, "lm_error_cases.md"), encoding="utf-8") as f:
        text = f.read()
    cases = {}
    for number, title, body in _CASE_RE.findall(text):
        fields = {k: " ".join(v.split()) for k, v in _FIELD_RE.findall(body)}
        cases[int(number)] = (f"#{number}{title.strip()}："
                              f"错误表现——{fields.get('错误表现', '')}；预防规则——{fields.get('预防规则', '')}")
    limit = int(index.get("max_chars") or 4000)
    out, used = [], len(LM_HEADING) + 4
    for case_id in ids:
        entry = cases.get(case_id)
        if not entry:
            continue
        if used + len(entry) + 1 > limit:
            remaining = limit - used - 1
            if remaining > 80:
                out.append(entry[:remaining - 1] + "…")
            break
        out.append(entry)
        used += len(entry) + 1
    return f"## {LM_HEADING}\n" + "\n".join(out) if out else ""


def with_lm(system: str, lm_scope: str | None) -> str:
    return system + "\n\n" + lm_text(lm_scope) if lm_scope else system


def chat(system: str, user: str, *, max_tokens: int = 4096,
         temperature: float = 0.2, timeout: int = 180, retries: int = 2,
         thinking: str | None = None, lm_scope: str | None = None) -> str:
    """bmc-llm-relay OpenAI 兼容 chat/completions（流式）。无凭据直接抛错，由调用方降级。

    thinking 参数保留签名兼容；relay 的 GPT 模型不接受 thinking 字段（400），请求不再携带。
    """
    system = with_lm(system, lm_scope)
    base, key = _base_key()
    if not base or not key:
        raise RuntimeError("缺少 bmc-llm-relay 凭据（LLM_RELAY_BASE_URL/LLM_RELAY_API_KEY）")
    payload = {
        "model": _model(),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    url = f"{base}/chat/completions"
    for attempt in range(retries):
        parts: list[str] = []
        finish = ""
        overall_timeout = int(
            os.environ.get("TOPIC5_SEMANTIC_TIMEOUT_SEC") or timeout
        )
        idle_timeout = int(
            os.environ.get("TOPIC5_SEMANTIC_IDLE_TIMEOUT_SEC") or 45
        )
        deadline = time.monotonic() + overall_timeout
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Authorization": f"Bearer {key}",
                "content-type": "application/json",
                "Accept": "text/event-stream",
            })
            with _hard_deadline(overall_timeout):
                with urllib.request.urlopen(
                    req, timeout=min(overall_timeout, idle_timeout)
                ) as resp:
                    for raw in resp:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"方舟流式响应超过整体时限 {overall_timeout}s"
                            )
                        line = raw.decode("utf-8").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)
                        if chunk.get("error"):
                            raise RuntimeError(
                                f"方舟流式错误: {chunk['error']!r}"
                            )
                        choices = chunk.get("choices") or []
                        if not choices:
                            continue
                        ch = choices[0]
                        if ch.get("finish_reason"):
                            finish = str(ch["finish_reason"])
                        delta = ch.get("delta") or {}
                        piece = delta.get("content")
                        if isinstance(piece, str):
                            parts.append(piece)
            if finish == "length":
                raise ValueError(f"输出被 max_tokens={max_tokens} 截断")
            return "".join(parts)
        except urllib.error.HTTPError as error:
            # 把 relay 响应体带进异常：只剩「HTTP Error 400: Bad Request」无法定位原因。
            try:
                detail = error.read().decode("utf-8", "replace")[:500]
            except Exception:  # 响应体不可读时保留原状态码
                detail = ""
            error.msg = (f"{error.msg} model={payload['model']} "
                         f"input_chars={len(system) + len(user)} body={detail}")
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    return ""


# 解析重试次数：对齐原版各 discovery/annot 脚本的 call_with_retry(tries=3) / range(3)（FIX-31）
CHAT_JSON_ATTEMPTS = 3


def chat_json(system: str, user: str, *, attempts: int = CHAT_JSON_ATTEMPTS, **kw) -> Any:
    """调模型并解析 JSON；空返回/解析失败重试。截断类错误不在此重试，直接抛。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            text = chat(system, user, **kw)
            if not text.strip():
                raise ValueError("模型返回空内容")
            return parse_json(text)
        except ValueError as exc:
            if "截断" in str(exc):
                raise
            last = exc
            if i < attempts - 1:
                time.sleep(2 ** (i + 1))
    raise RuntimeError(f"连续 {attempts} 次无法取得可解析输出：{last!r}")
