#!/usr/bin/env python3
"""阶段 4：Embedding。

默认 embed 的是 Frame 拼装文本。`--text-source clean_title` 切成只 embed 清洗后的标题，
这是「清洗能否替代 Frame」的对照开关——两种模式读同一份 frames.jsonl，
其余阶段完全不动，所以召回率差异只归因于文本来源这一处。

Embedding 只产出候选，分数只进入特征，不决定关系。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from c2_runtime import atomic_json, digest, file_hash, unique_ids

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay import Embedder, log_run, read_jsonl


def frame_text(frame: dict) -> str:
    actors = "、".join(a.get("name", "") for a in frame.get("actor") or []) or "-"
    parts = [
        f"主体：{actors}",
        f"动作：{frame.get('action') or '-'}",
        f"对象：{frame.get('object') or '-'}",
        f"类型：{frame.get('event_type') or '-'}",
        f"父事件线索：{frame.get('parent_hint') or '-'}",
        f"标题：{frame.get('title') or ''}",
    ]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--model", default="Doubao-embedding",
                    help="默认曾是 text-embedding-3-small，它要出海、慢且不稳。"
                         "同一批全新真实文本实测：3-small 1.01~2.53 条/秒，"
                         "Doubao-embedding 50.8 条/秒（约 20~50 倍）；1816 条从 20~30 分钟"
                         "降到 15.8 秒。召回质量在两个平台复验均持平略优："
                         "微博 68.4%→69.8%、知乎 93.4%→93.9%（基准由 3-small 的召回结果"
                         "产出，天然偏向它，Doubao 仍胜出）。维度 2560，与 1536 不通用，"
                         "换模型后 embeddings 要重算——缓存键含模型名，不会串。"
                         "网关另有 text-embedding-v4（1024 维、14 条/秒、批量上限 10）")
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--model-revision", default=None)
    ap.add_argument("--frame-input", choices=["auto", "original"], default="auto",
                    help="auto 保留旧版 resolved 优先/原始回退；original 为 E/V 固定原始快照")
    ap.add_argument("--text-source", choices=["frame", "clean_title"], default="frame",
                    help="frame=六字段拼装文本；clean_title=只用清洗后标题")
    ap.add_argument("--concurrency", type=int, default=8,
                    help="并发请求数。默认曾是 1（全串行），而文档示例用 4，等于没人用默认值。"
                         "注意：实测吞吐由端点自身状态决定，客户端参数几乎无效——"
                         "同样参数在不同时段实测 0.21 / 0.73 / 2.3 条每秒，差 10 倍；"
                         "batch 16/64、并发 16/32 之间无显著差异。所以并发只用来避免"
                         "「全串行」这个最坏情况，不要指望调它提速")
    args = ap.parse_args()

    src = args.run_dir / "work" / "frames.jsonl"
    if args.frame_input == "auto" and (args.run_dir / "work" / "frames_resolved.jsonl").exists():
        src = args.run_dir / "work" / "frames_resolved.jsonl"
    if not src.exists():
        raise SystemExit("缺少 frames_resolved.jsonl / frames.jsonl，先跑 02（或 03）")
    frames = list(read_jsonl(src))
    unique_ids(frames)
    if args.batch_size < 1 or args.concurrency < 1:
        raise SystemExit("batch-size and concurrency must be positive")

    cache_namespace = digest({"schema": "frame-text/1", "model": args.model,
        "revision": args.model_revision or args.model,
        "endpoint": os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("LLM_RELAY_BASE_URL"),
        "implementation": file_hash(Path(__file__)), "text_source": args.text_source})
    cache = args.run_dir / "work" / "embedding-cache" / (cache_namespace + ".json")
    embedder = Embedder(args.model, cache)
    if args.text_source == "frame":
        texts = [frame_text(f) for f in frames]
    else:
        texts = [f.get("title") or "" for f in frames]
    vectors = embedder.embed(texts, args.batch_size, args.concurrency)
    embedder.save()

    if len(vectors) != len(frames):
        raise SystemExit(f"向量数 {len(vectors)} 与 Frame 数 {len(frames)} 不一致")
    dimensions = {len(v) for v in vectors if isinstance(v, list)}
    if vectors and (len(dimensions) != 1 or 0 in dimensions or any(
            not isinstance(v, list) or any(isinstance(x, bool) or not isinstance(x, (int, float))
                or not math.isfinite(x) for x in v) or not any(v) for v in vectors)):
        raise SystemExit("Invalid embedding vectors: dimensions, finite values or zero norm")

    out = args.run_dir / "work" / "embeddings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(out, {
        "model": args.model,
        "model_revision": args.model_revision or args.model,
        "frame_schema_version": "frame-text/1",
        "input_sha256": file_hash(src),
        "text_source": args.text_source,
        "dim": len(vectors[0]) if vectors else 0,
        "count": len(vectors),
        "vectors": {f["record_id"]: v for f, v in zip(frames, vectors)},
        "texts": {f["record_id"]: t for f, t in zip(frames, texts)},
    })

    total = embedder.hits + embedder.misses or 1
    print(f"文本来源 {args.text_source}，向量 {len(vectors)} 条，"
          f"维度 {len(vectors[0]) if vectors else 0}，"
          f"缓存命中 {embedder.hits}/{total} = {embedder.hits / total:.1%}，"
          f"估算 ${embedder.cost():.5f}")
    log_run(args.run_dir / "run_manifest.json",
            {"stage": "04_build_embeddings", "model": args.model,
             "text_source": args.text_source,
             "count": len(vectors), "cache_hits": embedder.hits,
             "input_tokens": embedder.usage["input_tokens"],
             "cost_usd": round(embedder.cost(), 6)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
