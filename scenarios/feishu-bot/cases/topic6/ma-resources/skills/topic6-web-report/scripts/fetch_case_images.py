#!/usr/bin/env python3
"""Phase G:从 HC3 定稿的飞书文档里下载「营销观察」人工配图 → case-<N>.<ext>。

配图只来自用户在 HC3 阶段手工上传到飞书文档的图片(链路不自动配图)。本脚本按文档块顺序找到
`案例 N：` 标题,把该标题之后、下一个标题之前的第一张图片块下载为 `case-N.<ext>`,供
`md2source.py --case-images <dir>` 挂到对应卡片。没有图片的案例不报错——网页渲染占位,流程继续。

身份:tenant token(bot 创建文档、转 owner 时保留 full_access)。取值顺序:
  LARKSUITE_CLI_TENANT_ACCESS_TOKEN(Gateway 经 Vault 注入) → LARK_APP_ID + LARK_APP_SECRET 现换。
不打印任何 token。

用法:python fetch_case_images.py --doc <docx URL 或 document_id> --out <dir>
stdout:JSON {"status":"success","cases":[...],"downloaded":{N: path},"missing":[N...]}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx

FEISHU = "https://open.feishu.cn/open-apis"
IMAGE_BLOCK = 27
HEADING_TYPES = range(3, 12)  # heading1..heading9
CASE_TITLE_RE = re.compile(r"^\s*案例\s*(\d+)\s*[：:]")
EXT_BY_MIME = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
               "image/webp": ".webp", "image/gif": ".gif"}


def tenant_token() -> str:
    token = os.environ.get("LARKSUITE_CLI_TENANT_ACCESS_TOKEN", "").strip()
    if token:
        return token
    app_id = os.environ.get("LARK_APP_ID") or os.environ.get("FEISHU_APP_ID") or ""
    secret = os.environ.get("LARK_APP_SECRET") or os.environ.get("FEISHU_APP_SECRET") or ""
    if not (app_id and secret):
        raise RuntimeError("缺少 LARKSUITE_CLI_TENANT_ACCESS_TOKEN 或 LARK_APP_ID/LARK_APP_SECRET")
    resp = httpx.post(f"{FEISHU}/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": secret}, timeout=15)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 tenant token 失败 code={data.get('code')}: {data.get('msg')}")
    return data["tenant_access_token"]


def document_id(doc: str) -> str:
    m = re.search(r"/docx/([A-Za-z0-9]+)", doc)
    return m.group(1) if m else doc.strip()


def list_blocks(client: httpx.Client, doc_id: str) -> list[dict]:
    blocks, page_token = [], ""
    while True:
        params = {"page_size": 500, "document_revision_id": -1}
        if page_token:
            params["page_token"] = page_token
        data = client.get(f"/docx/v1/documents/{doc_id}/blocks", params=params).json()
        if data.get("code") != 0:
            raise RuntimeError(f"读取文档块失败 code={data.get('code')}: {data.get('msg')}")
        blocks.extend(data["data"].get("items", []))
        if not data["data"].get("has_more"):
            return blocks
        page_token = data["data"]["page_token"]


def block_text(block: dict) -> str:
    for key, value in block.items():
        if isinstance(value, dict) and "elements" in value:
            return "".join((el.get("text_run") or {}).get("content", "") for el in value["elements"])
    return ""


def case_image_tokens(blocks: list[dict]) -> tuple[list[int], dict[int, str]]:
    """按文档顺序:案例标题 → 直到下一个标题前的第一张图片。返回 (全部案例号, {案例号: image token})。"""
    by_id = {b["block_id"]: b for b in blocks}
    root = next((b for b in blocks if b.get("block_type") == 1), None)
    order: list[dict] = []

    def walk(block_id: str) -> None:  # 深度优先 = 文档阅读顺序(含 grid/callout 内的图)
        blk = by_id.get(block_id)
        if blk is None:
            return
        order.append(blk)
        for child in blk.get("children", []):
            walk(child)

    if root:
        for child in root.get("children", []):
            walk(child)
    else:
        order = blocks

    cases: list[int] = []
    tokens: dict[int, str] = {}
    current: int | None = None
    for blk in order:
        btype = blk.get("block_type")
        if btype in HEADING_TYPES:
            m = CASE_TITLE_RE.match(block_text(blk))
            current = int(m.group(1)) if m else None
            if current is not None and current not in cases:
                cases.append(current)
        elif btype == IMAGE_BLOCK and current is not None and current not in tokens:
            token = (blk.get("image") or {}).get("token")
            if token:
                tokens[current] = token
    return cases, tokens


def download(client: httpx.Client, token: str, out_base: Path) -> Path:
    resp = client.get(f"/drive/v1/medias/{token}/download", follow_redirects=True)
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if resp.status_code != 200 or not ctype.startswith("image/"):
        raise RuntimeError(f"下载图片失败 HTTP {resp.status_code} content-type={ctype or '-'}")
    path = out_base.with_suffix(EXT_BY_MIME.get(ctype, ".png"))
    path.write_bytes(resp.content)
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 HC3 人工配图 → case-<N>.<ext>")
    parser.add_argument("--doc", required=True, help="飞书 docx URL 或 document_id")
    parser.add_argument("--out", required=True, help="输出目录(case-N.<ext>)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        headers = {"Authorization": f"Bearer {tenant_token()}"}
        with httpx.Client(base_url=FEISHU, headers=headers, timeout=60) as client:
            cases, tokens = case_image_tokens(list_blocks(client, document_id(args.doc)))
            downloaded = {n: str(download(client, tok, out / f"case-{n}")) for n, tok in tokens.items()}
    except Exception as exc:  # 取图失败不阻断 Phase G:调用方按全部缺图渲染占位
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    missing = [n for n in cases if n not in downloaded]
    print(json.dumps({"status": "success", "cases": cases, "downloaded": downloaded,
                      "missing": missing}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
