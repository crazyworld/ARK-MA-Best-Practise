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
# 报告撰写/经验总结/返修模型（原版 KS_Content_Analysis_Prompt.md:142「推荐 claude-sonnet-4-6 或以上」）。
_DEFAULT_REPORT_MODEL = "claude-sonnet-4-6"
# 未显式传 timeout 时的整体时限（秒）；可由 TOPIC5_SEMANTIC_TIMEOUT_SEC 覆盖。
_DEFAULT_TIMEOUT_SEC = 180


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
        raise TimeoutError(f"relay 流式响应超过整体时限 {seconds}s")

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


def report_model() -> str:
    """报告撰写类调用的模型：TOPIC5_REPORT_MODEL → 默认 claude-sonnet-4-6。"""
    return os.environ.get("TOPIC5_REPORT_MODEL") or _DEFAULT_REPORT_MODEL


def _overall_timeout(explicit: int | None) -> int:
    """调用方显式给出的时限优先（长报告需要更长时间），环境变量只覆盖默认值。"""
    if explicit:
        return int(explicit)
    return int(os.environ.get("TOPIC5_SEMANTIC_TIMEOUT_SEC") or _DEFAULT_TIMEOUT_SEC)



def _escape_inner_quotes(text: str) -> str:
    """修复字符串值里未转义的英文双引号（模型引用原文：「"reason": "角色设定"汉堡主理鹅"，属于…"」）。

    在字符串内遇到 `"`：其后第一个非空白字符是 , : } ] 或文本结束才视为闭合引号，
    否则按字面量转义。只在严格解析失败后使用。
    """
    out, in_str, i, n = [], False, 0, len(text)
    while i < n:
        ch = text[i]
        if in_str and ch == "\\" and i + 1 < n:
            out.append(text[i:i + 2]); i += 2; continue
        if ch == '"':
            if not in_str:
                in_str = True
            else:
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j >= n or text[j] in ",:}]":
                    in_str = False
                else:
                    out.append('\\"'); i += 1; continue
        out.append(ch); i += 1
    return "".join(out)


def _json_candidates(text: str) -> list[str]:
    """候选 JSON 文本：整体、各 ```json 围栏段（流式偶发输出中途重来，取最后完整的一段）、
    首尾括号截取。"""
    text = (text or "").strip()
    fenced = [m.strip() for m in re.findall(r"```(?:json)?\s*(.*?)(?:```|$)", text, re.S) if m.strip()]
    stripped = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text)).strip()
    out = [stripped] + list(reversed(fenced))
    for base in list(out):
        for lo, hi in (("{", "}"), ("[", "]")):
            start, end = base.find(lo), base.rfind(hi)
            if 0 <= start < end:
                out.append(base[start:end + 1])
            # 中途重来：最后一个起始括号到末尾
            last = base.rfind("\n" + lo)
            if last > 0 and base.rfind(hi) > last:
                out.append(base[last + 1:base.rfind(hi) + 1])
    return list(dict.fromkeys(out))


def parse_json(text: str) -> Any:
    """解析模型输出的 JSON：严格解析 → 放宽控制字符 → 修复未转义引号，依次尝试各候选段。

    原版 annot_r1/annot_r2 由 Agent 读报错后修正；这里在程序侧兜住模型常见的格式瑕疵
    （字符串内未转义的引号、换行等控制字符、围栏/说明文字、流式输出中途重来），
    语义内容不改。全部失败才抛错。
    """
    candidates = _json_candidates(text)
    for repair in (lambda s: s, _escape_inner_quotes):
        for cand in candidates:
            try:
                return json.loads(repair(cand), strict=False)
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
         temperature: float = 0.2, timeout: int | None = None, retries: int | None = None,
         thinking: str | None = None, lm_scope: str | None = None,
         model: str | None = None, reasoning_effort: str | None = None) -> str:
    """bmc-llm-relay OpenAI 兼容 chat/completions（流式）。无凭据直接抛错，由调用方降级。

    thinking 参数保留签名兼容；relay 的 GPT 模型不接受 thinking 字段（400），请求不再携带。
    """
    system = with_lm(system, lm_scope)
    base, key = _base_key()
    if not base or not key:
        raise RuntimeError("缺少 bmc-llm-relay 凭据（LLM_RELAY_BASE_URL/LLM_RELAY_API_KEY）")
    payload = {
        "model": model or _model(),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
        "stream_options": {"include_usage": True},
        # 仅显式传入时携带（目前只有整篇报告/返修/经验合成）。relay 对 claude-sonnet-4-6
        # 默认开启思考：13 万 token 输入下思考吃光 16384/32768 全部输出预算、正文为 0
        # （job_436842fb74da n16 实测）；reasoning_effort=none 立即输出正文。
        **({"reasoning_effort": reasoning_effort} if reasoning_effort else {}),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    url = f"{base}/chat/completions"
    # relay→Bedrock 偶发 400「model identifier is invalid」/5xx/断流：同一请求稍后重放即成功
    # （job_436842fb74da n07b 实测：同模型 12/12 探测正常，单批连续 2 次 400）。
    # 默认 4 次、退避 2/4/8s；可由 TOPIC5_RELAY_RETRIES 调整。
    retries = int(retries or os.environ.get("TOPIC5_RELAY_RETRIES") or RELAY_RETRIES)
    for attempt in range(retries):
        parts: list[str] = []
        finish = ""
        overall_timeout = _overall_timeout(timeout)
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
                                f"relay 流式响应超过整体时限 {overall_timeout}s"
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
                                f"relay 流式错误: {chunk['error']!r}"
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
                # 输出预算不够（来源审核 13 个类别时 2048 就会截断）：按 2 倍重试，
                # 上限 MAX_TOKENS_CEILING；仍截断才抛，避免把「预算不足」当成内容失败。
                if max_tokens < MAX_TOKENS_CEILING and attempt < retries - 1:
                    max_tokens = min(max_tokens * 2, MAX_TOKENS_CEILING)
                    payload["max_tokens"] = max_tokens
                    body = json.dumps(payload, ensure_ascii=False).encode()
                    time.sleep(1)
                    continue
                raise ValueError(f"输出被 max_tokens={max_tokens} 截断")
            if not parts:
                # relay 偶发返回 200 + 空流（无任何 content 片段）：上游静默失败，
                # 不是「模型没话说」。当作可重试，重试用尽才抛。
                if attempt < retries - 1:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise RuntimeError("relay 返回空内容流（无 content 片段）")
            return "".join(parts)
        except urllib.error.HTTPError as error:
            # 把 relay 响应体带进异常：只剩「HTTP Error 400: Bad Request」无法定位原因。
            try:
                detail = error.read().decode("utf-8", "replace")[:500]
            except Exception:  # 响应体不可读时保留原状态码
                detail = ""
            error.msg = (f"{error.msg} model={payload['model']} "
                         f"input_chars={len(system) + len(user)} body={detail}")
            # relay 的 400 也出现过偶发（job_bf3c6b52db7b n09b 事后重放成功），保持重试。
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** (attempt + 1))
    return ""


# relay 调用重试次数（见 chat()）。
RELAY_RETRIES = 4
# 截断后自动放大输出预算的上限（见 chat()）。
MAX_TOKENS_CEILING = 16384

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
