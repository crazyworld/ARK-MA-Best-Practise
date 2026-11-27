"""原版资产完整性门禁：original/MANIFEST.json 里每个文件的 sha256 必须与磁盘一致。

用途：确认迁入的原版脚本/知识源没有被改动。改原版文件必须先更新 MANIFEST 并在
提交信息里说明理由——这是「直接 copy、不做逐个改写」的执行保证。

运行：python3 original_verify.py            # 校验
      python3 original_verify.py --print    # 打印清单
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "original"
MANIFEST = ROOT / "MANIFEST.json"


def load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def verify() -> list[str]:
    """返回问题列表；空列表表示全部一致。"""
    problems: list[str] = []
    data = load()
    files = data.get("files") or []
    if not files:
        problems.append("MANIFEST.json 没有任何文件条目")
    listed = set()
    for item in files:
        rel = str(item.get("path") or "")
        listed.add(rel)
        path = ROOT / rel
        if not path.is_file():
            problems.append(f"缺失: {rel}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            problems.append(f"被改动: {rel} (期望 {str(item.get('sha256'))[:12]}, 实际 {digest[:12]})")
    for path in sorted(ROOT.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_file() and path.name != "MANIFEST.json":
            rel = path.relative_to(ROOT).as_posix()
            if rel not in listed:
                problems.append(f"未登记: {rel}")
    return problems


def main(argv: list[str]) -> int:
    if "--print" in argv:
        for item in load().get("files") or []:
            print(f"{item.get('sha256','')[:12]}  {item.get('path')}  <- {item.get('origin')}")
        return 0
    problems = verify()
    if problems:
        print("原版资产校验失败：")
        for line in problems:
            print("  -", line)
        return 1
    print(f"原版资产校验通过：{len(load().get('files') or [])} 个文件 sha256 一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
