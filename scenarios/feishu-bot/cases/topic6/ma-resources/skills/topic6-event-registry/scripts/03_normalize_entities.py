#!/usr/bin/env python3
"""阶段 3：Entity 归一。

建 entity_registry，把 actor 名称解析成 actor_id + parent_entity_id。
这是「同组合不同成员」问题的正确解法：靠实体谱系表达，不为每个团体写规则。
注意 parent_entity_id 只表达谱系，绝不能据此进入 SAME_EVENT。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from c2_runtime import cached_request, digest, file_hash, unique_ids

sys.path.insert(0, str(Path(__file__).resolve().parent))
from relay import (Relay, make_batches, parse_json, read_jsonl, report,
                   run_batches, write_csv, write_jsonl)

SYSTEM = """你维护一个实体注册表，把不同写法的实体名归一，并识别实体之间的从属关系。

任务：给定一批实体名和已有注册表，判断每个名字是已有实体的别名，还是新实体。若是新实体，判断它是否从属于某个已有实体。

从属关系的含义：
- 个人属于组合 / 团体（成员关系）
- 子品牌属于母品牌
- 作品属于系列
- 分公司属于集团

三条硬性约束：
1. 归一只针对"同一个东西的不同写法"（简称、昵称、全称、错别字）。不同的人、不同作品、不同届次绝不能归一。
2. 从属关系只表达谱系，不表达"是同一个实体"。王俊凯属于 TFBOYS，但王俊凯不是 TFBOYS。
3. 拿不准是别名还是新实体时，判为新实体。错误归一比多一个实体严重得多。

只输出 JSON，不要任何解释。"""

USER_TMPL = """已有实体注册表：
{registry}

待判定的实体名：
{names}

输出格式：
{{"resolutions":[{{"name":"","decision":"ALIAS_OF|NEW_ENTITY","entity_id":null,"canonical_name":null,"type":null,"parent_entity_name":null,"reason":""}}]}}

decision=ALIAS_OF 时必须给已有的 entity_id。
decision=NEW_ENTITY 时给 canonical_name 和 type，parent_entity_name 填父实体的名字（没有就 null）。
每个待判定的 name 必须出现且仅出现一次。"""


def collect_names(frames: list[dict]) -> list[dict]:
    """按出现次数降序，让高频实体先建立 canonical 形态。"""
    bag: dict[tuple[str, str], dict] = {}
    for frame in frames:
        for actor in frame.get("actor") or []:
            name = actor.get("name", "").strip()
            if not name:
                continue
            key = (name, actor.get("type", "OTHER"))
            entry = bag.setdefault(key, {"name": name, "type": key[1], "record_ids": []})
            entry["record_ids"].append(frame["record_id"])
    return sorted(bag.values(), key=lambda e: -len(e["record_ids"]))


class Registry:
    def __init__(self) -> None:
        self.entities: dict[str, dict] = {}
        self.by_name: dict[str, str] = {}
        self.seq = 0

    def next_id(self) -> str:
        self.seq += 1
        return f"ENT{self.seq:04d}"

    def add(self, canonical: str, etype: str, parent_name: str | None) -> str:
        existing = self.by_name.get(canonical)
        if existing:
            return existing
        eid = self.next_id()
        self.entities[eid] = {"entity_id": eid, "canonical_name": canonical,
                              "type": etype or "OTHER", "parent_entity_id": None,
                              "parent_entity_name": parent_name or None,
                              "aliases": [], "source_records": []}
        self.by_name[canonical] = eid
        return eid

    def alias(self, eid: str, name: str) -> None:
        entity = self.entities.get(eid)
        if entity and name != entity["canonical_name"] and name not in entity["aliases"]:
            entity["aliases"].append(name)
        self.by_name[name] = eid

    def resolve_parents(self) -> int:
        """父实体名转 ID。指向不存在的父实体时新建一个 GROUP。"""
        linked = 0
        for entity in list(self.entities.values()):
            pname = entity.get("parent_entity_name")
            if not pname or pname == entity["canonical_name"]:
                entity["parent_entity_id"] = None
                continue
            pid = self.by_name.get(pname) or self.add(pname, "GROUP", None)
            if pid != entity["entity_id"]:
                entity["parent_entity_id"] = pid
                linked += 1
        return linked

    def render(self, limit: int = 400) -> str:
        rows = sorted(self.entities.values(), key=lambda e: -len(e["source_records"]))[:limit]
        if not rows:
            return "（空）"
        return "\n".join(
            f"{e['entity_id']}\t{e['canonical_name']}\t{e['type']}"
            f"\t父实体={e.get('parent_entity_name') or '-'}"
            f"\t别名={'/'.join(e['aliases']) or '-'}" for e in rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--batch-size", type=int, default=50)
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--model-revision", default=None,
                    help="Pinned deployment revision for cache identity; defaults to model label")
    args = ap.parse_args()

    src = args.run_dir / "work" / "frames.jsonl"
    if not src.exists():
        raise SystemExit(f"缺少 {src}，先跑 02_extract_frames.py")
    frames = list(read_jsonl(src))
    unique_ids(frames)
    if args.batch_size < 1:
        raise SystemExit("batch-size must be positive")
    names = collect_names(frames)
    print(f"共 {len(names)} 个待归一实体名，来自 {len(frames)} 条 Frame")

    relay = Relay(args.model, args.max_tokens, SYSTEM)
    registry = Registry()
    batches = make_batches(names, args.batch_size, id_field="name", tag="EN")
    raw_dir = args.run_dir / "raw" / "entities"
    cache_hits = 0
    missing_names = 0

    auto_split: list[str] = []

    def resolve(batch: dict, depth: int = 0) -> None:
        """单批归一并写入注册表。截断兜底与 07 archive_split 一致：
        先原样重试一次（输出长度随机），仍截断才对半拆；两半串行处理，
        后半批能看到前半批新建的实体，注册表逐批累积的语义不变。拆到 1 个名字仍截断才失败。"""
        nonlocal cache_hits, missing_names
        lines = "\n".join(
            f"{e['name']}\t{e['type']}\t出现={len(e['record_ids'])}次"
            for e in batch["items"])
        user_text = USER_TMPL.format(registry=registry.render(), names=lines)
        request = {"schema": "c2-entity-request/1", "model": args.model,
                   "revision": args.model_revision or args.model,
                   "endpoint_hash": digest(relay.base), "system": SYSTEM,
                   # 记生效值(relay 按模型原生上限抬高后的值)，改原生上限表时缓存随之失效
                   "user": user_text, "max_tokens": relay.max_tokens,
                   "temperature": relay.temperature,
                   "implementation_hash": file_hash(Path(__file__)),
                   "registry": registry.entities, "batch": batch}
        call = lambda: cached_request(raw_dir, request, lambda: relay.call_json(user_text))
        payload, hit = None, False
        try:
            payload, hit = call()
        except ValueError as exc:
            if "max_tokens" not in str(exc):
                raise
            if depth == 0:
                print(f"[entities] {batch['batch_id']} 输出撞到 max_tokens，原样重试一次：{exc}",
                      flush=True)
                try:
                    payload, hit = call()
                except ValueError as exc2:
                    if "max_tokens" not in str(exc2):
                        raise
            if payload is None:
                items = batch["items"]
                if len(items) <= 1:
                    raise
                mid = len(items) // 2
                auto_split.append(f"{batch['batch_id']}(L{depth + 1}:{mid}+{len(items) - mid})")
                print(f"[entities] {batch['batch_id']} 仍截断，对半拆 {len(items)}→"
                      f"{mid}+{len(items) - mid}（第 {depth + 1} 层）", flush=True)
                for suffix, part in (("a", items[:mid]), ("b", items[mid:])):
                    resolve({"batch_id": f"{batch['batch_id']}{suffix}",
                             "ids": [str(e["name"]) for e in part], "items": part}, depth + 1)
                return
        cache_hits += hit
        returned = [r.get("name") for r in payload.get("resolutions", [])]
        expected = [r["name"] for r in batch["items"]]
        if sorted(returned) != sorted(expected):
            # Preserve legacy local fallback but let a later attempt repair omissions.
            (raw_dir / (digest(request) + ".json")).unlink(missing_ok=True)
        by_name = {e["name"]: e for e in batch["items"]}
        handled: set[str] = set()
        for row in payload.get("resolutions", []):
            name = str(row.get("name", "")).strip()
            if name not in by_name or name in handled:
                continue
            handled.add(name)
            item = by_name[name]
            eid = None
            if str(row.get("decision", "")).upper() == "ALIAS_OF":
                eid = str(row.get("entity_id") or "").strip()
                if eid not in registry.entities:
                    eid = None
            if not eid:
                canonical = str(row.get("canonical_name") or name).strip() or name
                eid = registry.add(canonical, str(row.get("type") or item["type"]).upper(),
                                   str(row.get("parent_entity_name") or "").strip() or None)
            registry.alias(eid, name)
            registry.entities[eid]["source_records"].extend(item["record_ids"])
        # 漏返的自成实体，绝不丢
        for name in by_name:
            if name not in handled:
                missing_names += 1
                eid = registry.add(name, by_name[name]["type"], None)
                registry.alias(eid, name)
                registry.entities[eid]["source_records"].extend(by_name[name]["record_ids"])
        print(f"[entities] {batch['batch_id']} 完成，注册表 {len(registry.entities)} 个实体",
              flush=True)

    # 注册表是逐批累积的，必须串行——本批的判定要看到前面批次建立的实体
    for batch in batches:
        resolve(batch)
    if auto_split:
        print(f"[entities] 截断自动拆批 {len(auto_split)} 次：{', '.join(auto_split)}", flush=True)

    linked = registry.resolve_parents()
    for entity in registry.entities.values():
        entity["source_records"] = sorted(set(entity["source_records"]))

    # 回填 actor_id / parent_entity_id 到 Frame
    unresolved = 0
    for frame in frames:
        resolved = []
        for actor in frame.get("actor") or []:
            eid = registry.by_name.get(actor.get("name", "").strip())
            if not eid:
                unresolved += 1
                resolved.append({**actor, "actor_id": None, "parent_entity_id": None})
                continue
            resolved.append({**actor, "actor_id": eid,
                             "parent_entity_id": registry.entities[eid]["parent_entity_id"]})
        frame["actor"] = resolved
        frame["actor_ids"] = [a["actor_id"] for a in resolved if a["actor_id"]]

    write_jsonl(args.run_dir / "work" / "entities.jsonl", registry.entities.values())
    write_jsonl(args.run_dir / "work" / "frames_resolved.jsonl", frames)
    write_csv(args.run_dir / "out" / "entity_registry.csv",
              [{**e, "aliases": "/".join(e["aliases"]),
                "source_records": len(e["source_records"])}
               for e in registry.entities.values()],
              ["entity_id", "canonical_name", "type", "parent_entity_id",
               "parent_entity_name", "aliases", "source_records"])

    print(f"实体 {len(registry.entities)} 个，其中有父实体 {linked} 个；"
          f"未解析 actor 引用 {unresolved} 处")
    report(relay, "03_normalize_entities", args.run_dir / "run_manifest.json",
           {"entities": len(registry.entities), "linked": linked,
            "input_sha256": file_hash(src), "cache_hits": cache_hits,
            "missing_names": missing_names, "unresolved_actors": unresolved,
            "auto_split": auto_split, "reasoning_tokens": relay.reasoning_tokens,
            "quality_status": "DEGRADED" if missing_names or unresolved else "COMPLETE"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
