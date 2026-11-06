#!/usr/bin/env python3
"""共用底座：API 客户端、批量并发、断点续跑、成本台账、截断显式报错。

所有阶段脚本都走这里，不要各自造轮子。
凭据只从环境变量读取，模型统一走 bmc-llm-relay（blueai 模型网关，OpenAI 兼容）：
  BASE_URL：LLM_RELAY_BASE_URL（如 https://bmc-llm-relay.bluemediagroup.cn/v1）
  Key：LLM_RELAY_API_KEY
Embedding 可用 EMBEDDING_BASE_URL / EMBEDDING_API_KEY 单独指定，缺省沿用 LLM_RELAY_*。

2026-11-03 起不再回退 ARK_* / OPENAI_*：缺 LLM_RELAY_* 直接报错，避免静默改走火山方舟
计费与配额。ARK_BASE_URL/ARK_API_KEY 只用于 update_ma.sh/create_all.sh 调方舟 Managed
Agent 控制面（创建/更新 Agent、Environment），与模型调用无关。另有两个可选开关：
  EMBEDDING_IS_MULTIMODAL  true/false，显式指定是否走 /embeddings/multimodal 协议；
                           未设置时按模型名是否以 doubao-embedding-vision 开头猜测。
  PRICING_OVERRIDE_JSON    JSON object，追加企业模型的单价到 PRICING（未知模型默认按 0 计费）。

2026-09-28 从 Anthropic 原生 /v1/messages 切到 OpenAI 兼容 chat/completions。
BASE_URL 应包含版本前缀（如 https://bmc-llm-relay.bluemediagroup.cn/v1），本模块只追加资源路径。
system 从顶层字段挪到 messages[0]{role=system}，
usage 字段从 input_tokens/output_tokens 换成 prompt_tokens/completion_tokens，
停止原因从 stop_reason 换成 finish_reason。prompt caching 暂不支持，缓存
相关 usage 字段保留为 0，避免上层聚合逻辑报错。

2026-11-05 升级到 blueai-canonical-event-registry 3.0.0-rc.14：传输层（bmc-llm-relay
流式 + 整体时限 watchdog + 多模态 embedding）保持 MA 实现；run_batches 版本绑定缓存
（C2_CACHE_CONTEXT）、Embedder 向量校验与原子落盘、report 等其余逻辑与 rc.14 对齐。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence
from c2_runtime import atomic_json, digest, file_hash

# 每百万 token 单价(USD 估算,仅用于成本台账。换模型时在这里补)。
# doubao-seed-evolving 方舟原价 6/30 RMB per M token,按 7 汇率换算成 USD。
PRICING = {
    "doubao-seed-evolving": (0.857, 4.286),
    # doubao-embedding-vision 文本输入 ¥0.7 / M token，按 7 汇率折算。
    "doubao-embedding-vision": (0.1, 0.0),
    "DeepSeek-V4-Pro": (2.0, 8.0),
    "DeepSeek-V4-Flash": (0.5, 2.0),
    "DeepSeek-V4.1-Flash": (0.5, 2.0),
    "claude-opus-4-7": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "text-embedding-3-small": (0.02, 0.0),
    "Doubao-embedding": (0.0, 0.0),
}

# 模型原生单次输出上限(bmc-llm-relay 模型列表标注)。各阶段脚本的 --max-tokens 默认值是上游
# 按 claude-sonnet-4-6 调校的(16000/24000)，换成推理型模型后推理 token 也计入 max_tokens，
# 原值会在正常批量下截断(W44 full 的 03 实体归一首批 50 个即撞 16000)。这里不再人为压低：
# 表内模型一律按原生上限请求，脚本传入值更大时以脚本为准；表外模型保持脚本传入值。
# 换模型时在这里补原生上限，不要改各阶段脚本(它们与上游 rc.14 保持一致)。
MODEL_MAX_OUTPUT_TOKENS = {
    "DeepSeek-V4.1-Flash": 384000,
}


def effective_max_tokens(model: str, requested: int) -> int:
    native = MODEL_MAX_OUTPUT_TOKENS.get(model)
    if native is None:
        lowered = model.lower()
        native = next((v for k, v in MODEL_MAX_OUTPUT_TOKENS.items() if k.lower() == lowered), None)
    return max(requested, native) if native else requested


# 企业自有模型（BYOK）不在上表中：可通过 PRICING_OVERRIDE_JSON 环境变量追加单价，
# 格式同 PRICING（JSON object，value 为 [input_price, output_price]）；未配置时未知模型按 (0.0, 0.0) 计费。
_pricing_override_raw = os.environ.get("PRICING_OVERRIDE_JSON")
if _pricing_override_raw:
    PRICING.update(
        {key: tuple(value) for key, value in json.loads(_pricing_override_raw).items()}
    )


def api_url(base: str, resource: str) -> str:
    """把资源路径追加到已含版本前缀的 OpenAI 兼容 API 根地址。"""
    return f"{base.rstrip('/')}/{resource.lstrip('/')}"


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def price_of(model: str) -> tuple[float, float]:
    # 模型 ID 大小写不统一（环境变量 Doubao-Seed-Evolving vs 表内 doubao-seed-evolving），按小写匹配
    name = model.lower()
    for key, value in PRICING.items():
        if key.lower() in name:
            return value
    return (0.0, 0.0)


def _interrupt_response(response: object) -> None:
    """Interrupt a blocking urllib SSL/SSE read from a watchdog thread."""
    candidates = [response]
    current = response
    for attr in ("fp", "raw", "_sock"):
        current = getattr(current, attr, None)
        if current is None:
            break
        candidates.append(current)
    for candidate in reversed(candidates):
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass


class Relay:
    """bmc-llm-relay（OpenAI 兼容）chat/completions 客户端，线程安全累计用量。"""

    def __init__(self, model: str, max_tokens: int, system: str,
                 temperature: float = 0.0, timeout: int = 600, retries: int = 3):
        self.base = (os.environ.get("LLM_RELAY_BASE_URL") or "").rstrip("/")
        self.key = os.environ.get("LLM_RELAY_API_KEY") or ""
        if not self.base or not self.key:
            raise SystemExit(
                "缺少 bmc-llm-relay(blueai 模型网关)凭据：需要 LLM_RELAY_BASE_URL 与 "
                "LLM_RELAY_API_KEY。凭据只从环境变量读取，不要写进文件；不回退 ARK_*。")
        self.model = model
        self.max_tokens = effective_max_tokens(model, max_tokens)
        self.system = system
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries
        self.lock = threading.Lock()
        # cache_read/cache_creation 保留字段以兼容上层聚合(方舟暂无对应能力,恒为 0)
        self.usage = {"input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        self.calls = 0
        # 推理 token(usage.completion_tokens_details.reasoning_tokens)单独累计，不进 usage，
        # 避免改变上层成本台账的字段集合；截断报错里带上本次的拆分，便于区分「推理吃预算」与「正文过长」。
        self.reasoning_tokens = 0

    def call(self, user_text: str, extra: str = "") -> str:
        cap = self.max_tokens
        payload_body = {
            "model": self.model,
            "max_tokens": cap,
            "temperature": self.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": user_text + extra},
            ],
        }
        body = json.dumps(payload_body, ensure_ascii=False).encode()
        content_parts: list[str] = []
        usage: dict = {}
        finish_reason = ""
        for attempt in range(self.retries):
            try:
                request_started = time.monotonic()
                req = urllib.request.Request(
                    api_url(self.base, "chat/completions"),
                    data=body,
                    headers={
                        "Authorization": f"Bearer {self.key}",
                        "content-type": "application/json",
                        "Accept": "text/event-stream",
                    },
                )
                content_parts = []
                usage = {}
                finish_reason = ""
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    deadline_expired = threading.Event()
                    remaining = self.timeout - (time.monotonic() - request_started)

                    def abort_at_deadline() -> None:
                        deadline_expired.set()
                        _interrupt_response(resp)

                    if remaining <= 0:
                        abort_at_deadline()
                        raise TimeoutError(
                            f"方舟流式响应超过整体时限 {self.timeout}s"
                        )
                    watchdog = threading.Timer(remaining, abort_at_deadline)
                    watchdog.daemon = True
                    watchdog.start()
                    try:
                        for raw_line in resp:
                            if deadline_expired.is_set():
                                raise TimeoutError(
                                    f"方舟流式响应超过整体时限 {self.timeout}s"
                                )
                            line = raw_line.decode("utf-8").strip()
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            chunk = json.loads(data)
                            if chunk.get("error"):
                                raise RuntimeError(
                                    f"方舟流式响应错误: {chunk['error']!r}"
                                )
                            chunk_usage = chunk.get("usage")
                            if isinstance(chunk_usage, dict):
                                usage = chunk_usage
                            choices = chunk.get("choices") or []
                            if not choices:
                                continue
                            choice = choices[0]
                            if choice.get("finish_reason"):
                                finish_reason = str(choice["finish_reason"])
                            delta = choice.get("delta") or {}
                            content = delta.get("content")
                            if isinstance(content, str):
                                content_parts.append(content)
                        if deadline_expired.is_set():
                            raise TimeoutError(
                                f"方舟流式响应超过整体时限 {self.timeout}s"
                            )
                    except (OSError, ValueError):
                        if deadline_expired.is_set():
                            raise TimeoutError(
                                f"方舟流式响应超过整体时限 {self.timeout}s"
                            )
                        raise
                    finally:
                        watchdog.cancel()
                break
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                json.JSONDecodeError,
            ):
                if attempt == self.retries - 1:
                    raise
                time.sleep(2 ** attempt)
        with self.lock:
            self.calls += 1
            # OpenAI 兼容协议字段名换算成上层聚合期望的 input/output_tokens
            self.usage["input_tokens"] += usage.get("prompt_tokens", 0) or 0
            self.usage["output_tokens"] += usage.get("completion_tokens", 0) or 0
            details = usage.get("completion_tokens_details") or {}
            reasoning = (details.get("reasoning_tokens", 0) or 0) if isinstance(details, dict) else 0
            self.reasoning_tokens += reasoning
        # 截断必须显式报错。当成解析失败去重试，只会再截断一次。
        if finish_reason == "length":
            raise ValueError(f"输出被 max_tokens={cap} 截断，"
                             f"需调大 max_tokens 或减小批量"
                             f"（completion_tokens={usage.get('completion_tokens', 0) or 0}，"
                             f"reasoning_tokens={reasoning}，"
                             f"正文 {sum(len(p) for p in content_parts)} 字符）")
        return "".join(content_parts)

    def cost(self) -> float:
        pin, pout = price_of(self.model)
        return (self.usage["input_tokens"] * pin
                + self.usage["output_tokens"] * pout) / 1_000_000

    def call_json(self, user_text: str, attempts: int = 5) -> dict:
        """调用并解析。空返回或解析失败会重试——串行阶段一次失败会毁掉整轮。

        attempts 曾是 3、退避 1/2s，总重试窗口只有 3 秒。实测「网关返回空内容」
        是并发下的瞬时故障（同一个块单独跑 4/4 成功、8 路并发 7/8 成功，输出稳定
        在 7.5K 字符、离 max_tokens 差 8 倍），3 秒窗口盖不住一次网关抖动，
        于是被误判成「块内容有问题」而去拆块——拆块对这类失败无效且会切散事件。
        改成 5 次、退避 2/4/8/16s（窗口 30 秒）后，瞬时故障自己就恢复了。
        """
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                text = self.call(user_text)
                if not text.strip():
                    raise ValueError("模型返回空内容")
                return parse_json(text)
            except (ValueError, json.JSONDecodeError) as exc:
                if isinstance(exc, ValueError) and "max_tokens" in str(exc):
                    # 截断不在这里重试——它和「空返回」需要不同的处置，交给调用方决定。
                    # 07 的做法是：原样重试一次，仍截断才在块内对半拆。
                    raise
                last = exc
                if attempt < attempts - 1:
                    time.sleep(2 ** (attempt + 1))
        raise RuntimeError(f"连续 {attempts} 次无法取得可解析输出：{last!r}")


class Embedder:
    """向量化客户端，带磁盘缓存。缓存键是文本哈希 + 模型名。

    timeout 曾是 120s。实测该端点吞吐波动极大（同样参数在不同时段实测
    0.21 / 0.73 / 2.3 条每秒），单请求耗时随之从几十秒到 20 分钟以上。
    对照实验：955 条真实标题，timeout=120 在 1232s 后超时中断（只取到 832 条），
    timeout=600 完整跑完 955 条。调 batch 或并发都无效，只能放宽等待。
    """

    def __init__(self, model: str = "Doubao-embedding",
                 cache_path: Path | None = None, timeout: int = 600,
                 retries: int = 4):
        self.base = (os.environ.get("EMBEDDING_BASE_URL")
                     or os.environ.get("LLM_RELAY_BASE_URL") or "").rstrip("/")
        self.key = (os.environ.get("EMBEDDING_API_KEY")
                    or os.environ.get("LLM_RELAY_API_KEY") or "")
        if not self.base or not self.key:
            raise SystemExit("缺少 embedding 凭据：需要 EMBEDDING_BASE_URL / EMBEDDING_API_KEY，"
                             "或沿用 bmc-llm-relay 的 LLM_RELAY_BASE_URL / LLM_RELAY_API_KEY；"
                             "不回退 ARK_*。")
        self.model = model
        self.timeout = timeout
        self.retries = retries
        self.cache_path = cache_path
        self.cache: dict[str, list[float]] = {}
        self.hits = 0
        self.misses = 0
        self.usage = {"input_tokens": 0}
        if cache_path and cache_path.exists():
            try:
                blob = json.loads(cache_path.read_text(encoding="utf-8"))
                if blob.get("model") == model:
                    self.cache = {k: v for k, v in blob.get("vectors", {}).items()
                                  if self.valid_vector(v)}
            except (ValueError, AttributeError):
                self.cache = {}

    @staticmethod
    def valid_vector(vector):
        return (isinstance(vector, list) and bool(vector) and any(vector)
                and all(not isinstance(x, bool) and isinstance(x, (int, float))
                        and math.isfinite(x) for x in vector))

    @staticmethod
    def _key(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def embed(self, texts: Sequence[str], batch: int = 64,
              concurrency: int = 1) -> list[list[float]]:
        """生成向量；vision 模型按官方多模态协议逐条请求。

        `/embeddings/multimodal` 会把同一 input 列表融合为一个向量，不能把多条
        独立文本塞进一次请求。这里强制切成单条，再由 concurrency 控制并发。
        显式指定旧 embedding 模型时仍沿用标准 `/embeddings` 批量协议。

        企业自有模型（BYOK）名称不遵循 `doubao-embedding-vision-*` 命名规则，
        无法靠模型名判断是否走多模态协议；可用环境变量 EMBEDDING_IS_MULTIMODAL
        （true/false）显式指定，优先级高于按模型名猜测。
        """
        pending = [t for t in texts if self._key(t) not in self.cache]
        unique = list(dict.fromkeys(pending))
        override = _env_bool("EMBEDDING_IS_MULTIMODAL")
        is_multimodal = (
            override
            if override is not None
            else self.model.lower().startswith("doubao-embedding-vision")
        )
        effective_batch = 1 if is_multimodal else batch
        chunks = [
            unique[i:i + effective_batch]
            for i in range(0, len(unique), effective_batch)
        ]
        lock = threading.Lock()
        done = [0]

        def fetch(chunk: list[str]) -> None:
            if is_multimodal:
                payload_body = {
                    "model": self.model,
                    "input": [{"type": "text", "text": chunk[0]}],
                    "encoding_format": "float",
                }
                resource = "embeddings/multimodal"
            else:
                payload_body = {"model": self.model, "input": chunk}
                resource = "embeddings"
            body = json.dumps(payload_body, ensure_ascii=False).encode()
            req = urllib.request.Request(
                api_url(self.base, resource), data=body,
                headers={"Authorization": f"Bearer {self.key}",
                         "content-type": "application/json"})
            for attempt in range(self.retries):
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.load(resp)
                    break
                except (urllib.error.URLError, TimeoutError, OSError,
                        json.JSONDecodeError):
                    if attempt == self.retries - 1:
                        raise
                    time.sleep(2 ** attempt)
            data = payload.get("data")
            if isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = data
            else:
                raise ValueError("embedding 返回 data 不是对象或列表")
            if len(items) != len(chunk):
                raise ValueError(
                    f"embedding 返回 {len(items)} 个向量，期望 {len(chunk)} 个")
            # rc.14: 协议按 index 对应结果，返回顺序不等于输入顺序。MA 兼容：relay 部分路由
            # 不回 index，全部缺省时按位置对应；只要有一条带 index 就要求全部合法且覆盖。
            if any("index" in i for i in items):
                if any(type(i.get("index")) is not int for i in items):
                    raise ValueError("Embedding response has missing or invalid indices")
                indexed = {i["index"]: i.get("embedding") for i in items}
                if set(indexed) != set(range(len(chunk))):
                    raise ValueError("Embedding response failed coverage validation")
                vectors = [indexed[n] for n in range(len(chunk))]
            else:
                vectors = [i.get("embedding") for i in items]
            if (any(not self.valid_vector(vec) for vec in vectors)
                    or len({len(vec) for vec in vectors}) != 1):
                raise ValueError("Embedding response failed coverage/vector validation")
            with lock:
                usage = payload.get("usage") or {}
                self.usage["input_tokens"] += (
                    usage.get("prompt_tokens")
                    or usage.get("input_tokens")
                    or usage.get("total_tokens")
                    or 0
                )
                for text, vec in zip(chunk, vectors):
                    self.cache[self._key(text)] = vec
                self.misses += len(chunk)
                done[0] += 1
                if done[0] % 20 == 0:
                    # 分批落盘：整轮跑完才存的话，一次超时会丢掉全部已取向量
                    self.save()
                    print(f"[embed] {done[0]}/{len(chunks)} 批", flush=True)

        if concurrency > 1 and len(chunks) > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(fetch, c) for c in chunks]
                try:
                    for fut in as_completed(futures):
                        fut.result()
                except Exception:
                    self.save()  # 已取到的先落盘，重跑能续上
                    raise
        else:
            try:
                for chunk in chunks:
                    fetch(chunk)
            except Exception:
                self.save()
                raise
        out = []
        for text in texts:
            key = self._key(text)
            if key in self.cache and text not in pending:
                self.hits += 1
            out.append(self.cache[key])
        return out

    def save(self) -> None:
        if not self.cache_path:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.cache_path,
                    {"model": self.model, "count": len(self.cache), "vectors": self.cache})

    def cost(self) -> float:
        pin, _ = price_of(self.model)
        return self.usage["input_tokens"] * pin / 1_000_000


def repair_unescaped_quotes(text: str) -> str:
    """转义字符串值内部的裸 ASCII 双引号。

    实测模型会写出 "underlying_event":"官方辟谣"许昌暴雨"为谣言" 这种输出。
    判据：串内遇到 " 时向后跳过空白，若下一个字符不是 , : } ] 或结尾，它就是内容而非收尾。
    """
    out = []
    in_str = False
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = in_str
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
                continue
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j >= len(text) or text[j] in ",:}]":
                in_str = False
                out.append(ch)
            else:
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


def parse_json(text: str) -> dict:
    """模型有时会加 markdown 围栏或前后缀说明，都剥掉。"""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()
    for candidate in (text, None):
        if candidate is None:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                break
            candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                return json.loads(repair_unescaped_quotes(candidate))
            except json.JSONDecodeError:
                continue
    raise json.JSONDecodeError("无法解析模型输出", text, 0)


def make_batches(items: Sequence[dict], size: int, id_field: str = "record_id",
                 tag: str = "B") -> list[dict]:
    """顺序切批。批大小一旦确定不要中途改——改了 batch_id 编号会错位，续跑对不上。"""
    out = []
    for i in range(0, len(items), size):
        chunk = items[i:i + size]
        out.append({"batch_id": f"{tag}{i // size:04d}",
                    "ids": [str(r[id_field]) for r in chunk],
                    "items": chunk})
    return out


def run_batches(batches: list[dict], worker: Callable[[dict], dict],
                raw_dir: Path, concurrency: int = 8,
                label: str = "batch") -> tuple[list[dict], list[dict]]:
    """Version-bound, atomic batch cache. Invalid/unversioned cache never hits."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    context = os.environ.get("C2_CACHE_CONTEXT")
    if not context:
        # Conservative direct-script identity. Unified runner passes its immutable
        # input/config/code digest, allowing safe reuse across attempt directories.
        script_dir = Path(__file__).resolve().parent
        root = raw_dir.parent.parent
        context = digest({"code": {p.name: file_hash(p) for p in script_dir.glob("*.py")},
                          "argv": sys.argv[1:],
                          "work": {p.name: file_hash(p) for p in (root / "work").glob("*") if p.is_file()}})
    if concurrency < 1 or len({b["batch_id"] for b in batches}) != len(batches):
        raise ValueError("Invalid concurrency or duplicate batch IDs")
    results, failures, todo = {}, [], []
    def key(batch):
        return digest({"context": context, "batch": batch})
    for batch in batches:
        bid = batch["batch_id"]
        path = raw_dir / (bid + ".json")
        meta = raw_dir / "_input_hashes" / (bid + ".json")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            receipt = json.loads(meta.read_text(encoding="utf-8"))
            if receipt["input_hash"] == key(batch) and receipt["output_hash"] == digest(payload):
                results[bid] = payload
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        todo.append(batch)
    print(f"[{label}] {len(batches)} batches, {len(results)} cached, {len(todo)} pending", flush=True)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        jobs = {pool.submit(worker, b): b for b in todo}
        for future in as_completed(jobs):
            batch = jobs[future]; bid = batch["batch_id"]
            try:
                payload = future.result()
                atomic_json(raw_dir / (bid + ".json"), payload)
                atomic_json(raw_dir / "_input_hashes" / (bid + ".json"),
                            {"input_hash": key(batch), "output_hash": digest(payload)})
                results[bid] = payload
            except Exception as exc:
                failures.append({"batch_id": bid, "error_type": type(exc).__name__})
                print(f"[{label}] {bid} failed: {type(exc).__name__}", flush=True)
    return [results[b["batch_id"]] for b in batches if b["batch_id"] in results], failures


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> int:
    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig 让 Excel 直接认中文
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({f: row.get(f, "") for f in fields})
    return len(rows)


def ids_hash(ids: Sequence[str]) -> str:
    """工作单元 ID 集合的哈希，不是全量输入文件哈希。
    用文件哈希的话，任何一条无关记录变动都会让整轮无法复用。"""
    return hashlib.sha1("\n".join(sorted(ids)).encode("utf-8")).hexdigest()[:16]


def log_run(manifest_path: Path, entry: dict) -> None:
    """append-only 写成本台账。覆盖写会丢掉返工阶段的真实花费。"""
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"cost_ledger": [], "runs": []}
    manifest.setdefault("cost_ledger", []).append(
        {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry})
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def report(relay: Relay, stage: str, manifest: Path | None = None,
           extra: dict | None = None) -> None:
    usage = relay.usage
    cost = relay.cost()
    print(f"[{stage}] 调用 {relay.calls} 次 | in {usage['input_tokens']} "
          f"out {usage['output_tokens']} "
          f"| 估算 ${cost:.4f}", flush=True)
    if manifest:
        log_run(manifest, {"stage": stage, "model": relay.model, "calls": relay.calls,
                           "cost_usd": round(cost, 5), **usage, **(extra or {})})


def require_full_coverage(expected: Sequence[str], got: Sequence[str],
                          stage: str) -> list[str]:
    """返回缺失的 ID。调用方必须处理缺失，不能静默丢数据。"""
    missing = [i for i in expected if i not in set(got)]
    if missing:
        print(f"[{stage}] 模型漏返 {len(missing)} 条，将走本地兜底：{missing[:5]}",
              file=sys.stderr, flush=True)
    return missing
