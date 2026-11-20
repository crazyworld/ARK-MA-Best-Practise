"""标注结果缓存（FIX-30 第 6 条）— 原版 annot_r1.py checkpoint 的可失效版本。

原版口径（annot_r1.py:13-15/61-66）：
  - 断点续跑：已有结果的行不再送模型；
  - 只缓存合法结果（情感 ∈ 三档、相关性 ∈ 是/否），失败行不落盘，下次重试。
本模块在原版基础上把键扩成 sha256(行内容指纹 + 任务名 + prompt 指纹)：
  - prompt（含活动上下文、候选类别、校准样例）一变，旧结果自动失效；
  - 行内容变化（重新清洗）同样失效，避免张冠李戴。
缓存是纯加速手段：命中与未命中的产出必须一致，不参与任何判定。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def row_fingerprint(payload_item: dict) -> str:
    """行指纹：只用送模型的内容（平台/标题/正文/识别文本），不含 id。"""
    content = {key: payload_item.get(key) for key in
               ("platform", "title", "content", "media_text")}
    return _digest(json.dumps(content, ensure_ascii=False, sort_keys=True))


class AnnotationCache:
    """按任务分文件存放：<run_dir>/annotation_cache/<task>.json。"""

    def __init__(self, run_dir: Path, task: str, prompt: str):
        self.path = Path(run_dir) / "annotation_cache" / f"{task}.json"
        self.task = task
        self.prompt_hash = _digest(prompt)
        self._entries: dict[str, dict] = self._load()
        self.hits = 0

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}  # 缓存损坏：当作无缓存，重新标注
        return data if isinstance(data, dict) else {}

    def key(self, payload_item: dict) -> str:
        return _digest(f"{row_fingerprint(payload_item)}|{self.task}|{self.prompt_hash}")

    def get(self, payload_item: dict) -> dict | None:
        value = self._entries.get(self.key(payload_item))
        if isinstance(value, dict):
            self.hits += 1
            return value
        return None

    def put(self, payload_item: dict, parsed: dict) -> None:
        self._entries[self.key(payload_item)] = dict(parsed)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._entries, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
