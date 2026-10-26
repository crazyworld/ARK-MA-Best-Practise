#!/usr/bin/env python3
"""把 Files API 同步下来的 topic6 产物（mock 数据）还原成项目目录树。

mock 目录是 artifact_sync 的扁平落盘：``{sha256(file_id)[:16]}__{文件名}``，同名文件
分属两轮（full 第一轮 / skip_sampling 第二轮），只能按各版 manifest.json 记录的
sha256 定位回项目内相对路径。

- round=sample：第一轮（500 条样本 → wide_table_full_r1）
- round=full  ：第二轮（全量 → wide_table_skip_sampling_r1 → E/F/G）

用法：
    python tools/mock_project.py --mock-dir ~/work/mock_data/ada_topic6_mock_data \\
        --out /tmp/t6 --round full
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

DEFAULT_MOCK_DIR = Path.home() / "work" / "mock_data" / "ada_topic6_mock_data"
# 标志文件 + 取哪一版 manifest。第一轮之后的 manifest 仍保留第一轮宽表，但 C0/C3 等
# 节点已被第二轮覆盖，所以第一轮必须取「首个」含宽表的版本；第二轮取最新版（含 E/F/G）。
ROUND_MARKERS = {
    "sample": ("05_合并/wide_table_full_r1.xlsx", min),
    "full": ("05_合并/wide_table_skip_sampling_r1.xlsx", max),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_tree(mock_dir: Path, round_name: str) -> tuple[str, dict[str, Path]]:
    """返回 (项目名, {项目内相对路径: mock 文件})，manifest 选取规则见 ROUND_MARKERS。"""
    marker, pick = ROUND_MARKERS[round_name]
    by_hash = {
        _sha256(f): f for f in mock_dir.iterdir()
        if f.is_file() and not f.name.endswith("manifest.json")
    }
    candidates = []
    for path in mock_dir.glob("*manifest.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        tree = {
            item["source"]: item["sha256"]
            for pub in manifest.get("publications", [])
            for item in pub.get("files", [])
        }
        if marker in tree:
            candidates.append((manifest["updated_at"], manifest["project"], tree))
    if not candidates:
        raise FileNotFoundError(f"{mock_dir} 中没有包含 {marker} 的 manifest")
    _, project, tree = pick(candidates, key=lambda c: c[0])
    missing = sorted(src for src, digest in tree.items() if digest not in by_hash)
    if missing:
        raise FileNotFoundError(f"manifest 引用的文件在 mock 目录中缺失: {missing}")
    return project, {src: by_hash[digest] for src, digest in tree.items()}


def materialize(
    mock_dir: Path,
    out_root: Path,
    round_name: str,
    include: tuple[str, ...] = ("",),
) -> Path:
    """把该轮中前缀命中 include 的文件复制到 out_root/{project}/，返回项目目录。"""
    project, tree = load_tree(mock_dir, round_name)
    project_dir = out_root / project
    for rel, src in tree.items():
        if any(rel.startswith(prefix) for prefix in include):
            dest = project_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
    return project_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="topic6 mock 产物 → 项目目录树")
    parser.add_argument("--mock-dir", type=Path, default=DEFAULT_MOCK_DIR)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--round", choices=sorted(ROUND_MARKERS), default="full")
    args = parser.parse_args()
    print(materialize(args.mock_dir, args.out, args.round))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
