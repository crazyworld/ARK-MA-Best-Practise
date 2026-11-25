"""飞书文档交付适配层 — 供 delivery_draft handler 接真（n20_delivery_draft）。

原生插图：图表以**本地 PNG** 进飞书文档。飞书 docx 的 image block
不接受外链 URL，必须走三步：
  1) 建空 image block（block_type=27）拿 block_id；
  2) POST /open-apis/drive/v1/medias/upload_all 传本地二进制（parent_type=docx_image、
     parent_node=block_id）拿 file_token；
  3) PATCH blocks/{block_id} 用 replace_image.token=file_token 回填，图才显示。
需权限 drive:drive（上传 media）。

设计（类比 semantic.py / charts.py，零三方依赖，用 urllib）：
  - 真路径：拿 tenant/user access token → 新建 docx → MD 内容层转飞书 block 写入（图片先
    上传 media 再回填）→ 转移所有权 → 导出后回读核对（章节齐全 / 图 block 生效 /
    绑定报告版本一致）→ 返回 feishu_doc（含 doc_token/url/revision_id/report_version/readback）。
  - 诊断适配器：可构造 degraded=True 的同构占位产物供单元测试；生产 handler 会 fail-closed，
    不允许该产物进入 n21/n23/n24。

幂等：同 Task + 同报告版本只建一份 draft（由引擎 idempotency=feishu:{task_id}:{resource_lock}:{report_version}
处理）；本层若命中已存在回执（external_receipts.doc_token）则复用、不新建。

凭据只从环境变量读（生产由 MA Vault 注入）：
  FEISHU_APP_ID / FEISHU_APP_SECRET（tenant token），或 FEISHU_USER_ACCESS_TOKEN（用户态）。
  FEISHU_USER_OPEN_ID（任务发起人，所有权转移目标；缺失时交付 fail-closed）。
  FEISHU_WEB_BASE（可选，用户访问域名；默认 https://www.feishu.cn）。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Optional

_BASE = (os.environ.get("FEISHU_OPENAPI_BASE") or "https://open.feishu.cn").rstrip("/")
_WEB_BASE = (os.environ.get("FEISHU_WEB_BASE") or "https://www.feishu.cn").rstrip("/")
_DOCUMENT_BODY_WIDTH = 800
_GRID_GAP = 24


def _table_column_widths(rows: list[list[str]], available_width: int) -> list[int]:
    """Fit columns to their container, reserving more space for long text."""
    if not rows:
        return []
    count = len(rows[0])
    if not count:
        return []
    weights = []
    for index, heading in enumerate(rows[0]):
        if "排名" in heading:
            weight = 0.5
        elif any(term in heading for term in ("代表", "内容", "正文", "备注", "关键词", "过滤词")):
            weight = 4.5
        elif "标题" in heading:
            weight = 3.0
        elif any(term in heading for term in ("占比", "比例", "条数", "数量", "数值", "互动量")):
            weight = 1.0
        elif "URL" in heading.upper() or "链接" in heading:
            weight = 1.2
        else:
            lengths = [
                sum(2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
                    for char in row[index])
                for row in rows if index < len(row)
            ]
            weight = max(1.2, min(2.5, max(lengths, default=0) / 8))
        weights.append(weight)
    # Native tables need readable minimum widths. If an unusually wide table
    # cannot fit, preserve that minimum and let Feishu provide horizontal scroll.
    total = max(round(available_width), 50 * count)
    remaining = total - 50 * count
    shares = [remaining * weight / sum(weights) for weight in weights]
    widths = [50 + int(share) for share in shares]
    for index in sorted(range(count), key=lambda i: shares[i] % 1, reverse=True)[:total - sum(widths)]:
        widths[index] += 1
    return widths


def _layout_table_widths(blocks: list[dict], available_width: int = _DOCUMENT_BODY_WIDTH):
    """Yield expected table widths in document order, including nested columns."""
    for block in blocks:
        if block.get("block_type") == 31:
            yield _table_column_widths(block.get("_table_rows") or [], available_width)
        if block.get("_children"):
            yield from _layout_table_widths(block["_children"], max(50, available_width - 32))
        if block.get("_columns"):
            ratios = block.get("_width_ratios", [50, 50])
            for children, ratio in zip(block["_columns"], ratios):
                width = round(max(50, available_width - _GRID_GAP) * ratio / sum(ratios))
                yield from _layout_table_widths(children, width)


def _app_creds() -> tuple[str, str]:
    return (os.environ.get("FEISHU_APP_ID") or "",
            os.environ.get("FEISHU_APP_SECRET") or "")


def _user_token() -> str:
    return os.environ.get("FEISHU_USER_ACCESS_TOKEN") or ""


def api_available() -> bool:
    """是否具备飞书真调用条件：有用户态 token，或 app id+secret。"""
    if _user_token():
        return True
    app_id, app_secret = _app_creds()
    return bool(app_id and app_secret)


# ── HTTP ─────────────────────────────────────────────────────
def _http_error(error: urllib.error.HTTPError, token: str = "") -> RuntimeError:
    """Retain actionable API diagnostics without headers or credential bodies."""
    try:
        payload = json.loads(error.read(16384).decode("utf-8"))
    except (ValueError, UnicodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    detail = payload.get("error") or {}
    if not isinstance(detail, dict):
        detail = {}
    message = str(payload.get("msg") or "request rejected")
    for value in (token, _user_token(), _app_creds()[1]):
        if value:
            message = message.replace(value, "[redacted]")
    path = str(error.url).split("?", 1)[0]
    return RuntimeError(
        f"飞书 HTTP {error.code} {path} code={payload.get('code')} "
        f"msg={message[:300]} log_id={detail.get('log_id') or ''}"
    )


def _req(method: str, path: str, token: str, body: Optional[dict] = None,
         timeout: int = 30) -> dict:
    """Retry rejected requests, without replaying ambiguous non-idempotent writes."""
    url = f"{_BASE}{path}"
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    })
    # A 429 explicitly rejects this request. A 503 may hide an applied write:
    # only reads, absolute replacements, or requests with a client token replay.
    replacements = {
        "update_text_elements", "update_table_property",
        "update_grid_column_width_ratio", "replace_image",
    }
    safe_replay = (
        method.upper() in ("GET", "HEAD")
        or bool(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("client_token"))
        or (method.upper() == "PATCH" and bool(body) and set(body) <= replacements)
    )
    deadline = time.monotonic() + 60
    for attempt in range(6):
        try:
            with urllib.request.urlopen(
                req, timeout=min(timeout, max(0.1, deadline - time.monotonic()))
            ) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as error:
            retryable = error.code == 429 or (error.code == 503 and safe_replay)
            delay = 2 ** attempt
            retry_after = (error.headers or {}).get("Retry-After")
            if retry_after:
                try:
                    seconds = float(retry_after)
                except (TypeError, ValueError):
                    try:
                        seconds = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        seconds = delay
                if math.isfinite(seconds):
                    delay = max(delay, seconds)
            if not retryable or attempt == 5 or delay > deadline - time.monotonic():
                try:
                    raise _http_error(error, token) from error
                finally:
                    error.close()
            error.close()
            time.sleep(delay)
    if payload.get("code") not in (0, None):
        raise RuntimeError(f"飞书 API 错误 code={payload.get('code')} msg={payload.get('msg')}")
    return payload.get("data") or {}


def _upload_media(token: str, local_path: str, parent_node: str,
                  timeout: int = 60) -> str:
    """上传本地文件到 drive/v1/medias/upload_all（multipart），返回 file_token。

    零三方依赖：手写 multipart/form-data。parent_type=docx_image、parent_node=image block_id。
    """
    with open(local_path, "rb") as f:
        file_bytes = f.read()
    file_name = os.path.basename(local_path)
    boundary = f"----topic5bndry{uuid.uuid4().hex}"
    fields = {
        "file_name": file_name,
        "parent_type": "docx_image",
        "parent_node": parent_node,
        "size": str(len(file_bytes)),
    }
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
        parts.append(f"{v}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
        .encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    url = f"{_BASE}/open-apis/drive/v1/medias/upload_all"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _http_error(error, token) from error
    if payload.get("code") not in (0, None):
        raise RuntimeError(
            f"飞书 media 上传失败 code={payload.get('code')} msg={payload.get('msg')}")
    tok = (payload.get("data") or {}).get("file_token")
    if not tok:
        raise RuntimeError(f"飞书 media 上传未返回 file_token：{payload}")
    return tok


def _tenant_token() -> str:
    """优先用户态 token；否则用 app id/secret 换 tenant_access_token。"""
    ut = _user_token()
    if ut:
        return ut
    app_id, app_secret = _app_creds()
    if not (app_id and app_secret):
        raise RuntimeError("飞书凭据缺失（FEISHU_APP_ID/SECRET 或 FEISHU_USER_ACCESS_TOKEN）")
    url = f"{_BASE}/open-apis/auth/v3/tenant_access_token/internal"
    req = urllib.request.Request(url, method="POST", data=json.dumps(
        {"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _http_error(error) from error
    tok = payload.get("tenant_access_token")
    if not tok:
        raise RuntimeError(f"取 tenant_access_token 失败：{payload}")
    return tok


# ── MD → 飞书 docx block ──────────────────────────────────────
# docx v1 block_type 为整数枚举（旧版用字符串键会被真实 API 拒绝）。
_BLOCK_TEXT = 2
_BLOCK_IMAGE = 27
_BLOCK_TABLE = 31
_HEADING_BLOCK_TYPE = {1: 3, 2: 4, 3: 5, 4: 6}
_HEADING_KEY = {1: "heading1", 2: "heading2", 3: "heading3", 4: "heading4"}


def _text_block(content: str) -> dict:
    return {"block_type": _BLOCK_TEXT,
            "text": {"elements": _inline_elements(content)}}


# 报告装配器用 `*{{muted}}…*` 标记说明类文字；飞书 text_color 7 = 灰色。
MUTED_MARK = "{{muted}}"
MUTED_TEXT_COLOR = 7


def _inline_elements(content: str) -> list[dict]:
    """Translate common Markdown emphasis and hyperlinks into native text runs."""
    pattern = re.compile(r"\*\*(.+?)\*\*|\[([^\]]+)\]\(([^)]+)\)|\*([^*]+)\*")
    elements, position = [], 0
    def add(text, style=None):
        if text:
            run = {"content": text.replace(r"\|", "|")}
            if style:
                run["text_element_style"] = style
            elements.append({"text_run": run})
    for match in pattern.finditer(content):
        add(content[position:match.start()])
        if match.group(1) is not None:
            add(match.group(1), {"bold": True})
        elif match.group(2) is not None:
            url = match.group(3)
            add(match.group(2), {"link": {"url": url}} if url.startswith(("https://", "http://")) else None)
        else:
            text = match.group(4)
            if text.startswith(MUTED_MARK):
                # 灰色斜体说明（KS_Feishu_Format：说明类文字 rgb(143,149,158) 斜体）。
                add(text[len(MUTED_MARK):], {"italic": True, "text_color": MUTED_TEXT_COLOR})
            else:
                add(text, {"italic": True})
        position = match.end()
    add(content[position:])
    return elements or [{"text_run": {"content": ""}}]


def _split_table_row(line: str) -> list[str]:
    """拆分 Markdown 表格行，支持转义竖线。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]
    cells = re.split(r"(?<!\\)\|", text)
    return [cell.strip().replace(r"\|", "|") for cell in cells]


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _table_block(rows: list[list[str]]) -> dict:
    column_size = max(len(row) for row in rows)
    normalized = [row + [""] * (column_size - len(row)) for row in rows]
    return {
        "block_type": _BLOCK_TABLE,
        "table": {"property": {
            "row_size": len(normalized),
            "column_size": column_size,
            "header_row": True,
        }},
        "_table_rows": normalized,
    }


def md_to_blocks(markdown: str) -> list[dict]:
    """把内容层 MD 转飞书 docx block（标题/段落/图片/表格）。

    Markdown 表格的分隔行不会写入文档，首行作为原生表格表头。图片 path 若为**存在的
    本地文件**，产空 image block 并带内部提示，由 create_delivery 上传 media 后回填；
    否则降级为 text 行 `[图] alt: path` 保证可追溯。所有下划线前缀的内部提示键在真正
    发送前剥离。
    """
    blocks: list[dict] = []
    lines = (markdown or "").splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip():
            i += 1
            continue
        grid = re.fullmatch(r"<!--\s*grid:(50:50|column|end)\s*-->", line.strip())
        if grid:
            # FIX-50：报告装配器的双栏标记，交 layout_blocks 生成 grid。
            blocks.append({"block_type": 0, "_grid_marker": grid.group(1)})
            i += 1
            continue
        if line.strip().startswith("<!--") and line.strip().endswith("-->"):
            i += 1
            continue
        if (i + 1 < len(lines) and "|" in line
                and _is_table_separator(lines[i + 1])):
            rows = [_split_table_row(line)]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            if any(len(row) != len(rows[0]) for row in rows):
                raise ValueError("Markdown表格列数不一致，请转义正文竖线")
            blocks.append(_table_block(rows))
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            level = len(m.group(1))
            bt = _HEADING_BLOCK_TYPE.get(level, 6)
            key = _HEADING_KEY.get(level, "heading4")
            blocks.append({"block_type": bt, key: {
                "elements": _inline_elements(m.group(2).strip())}})
            i += 1
            continue
        img = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", line)
        if img:
            alt, path = img.group(1), img.group(2)
            if path and os.path.isfile(path):
                blocks.append({"block_type": _BLOCK_IMAGE, "image": {},
                               "_local_path": path, "_alt": alt})
            elif path.startswith(("https://", "http://")):
                blocks.append(_text_block(f"[图] {alt}: {path}"))
            else:
                raise ValueError("报告引用的本地图片不存在，禁止改写为路径文字")
            i += 1
            continue
        marker = re.match(r"^\s*>\s*\[!(insight|tip|warn)\]\s*(.*)", line)
        if marker:
            # FIX-46：显式 Callout 标记；后续连续 `>` 行并入同一个 Callout。
            parts = [marker.group(2)]
            while i + 1 < len(lines) and re.match(r"^\s*>", lines[i + 1]) \
                    and not re.match(r"^\s*>\s*\[!", lines[i + 1]):
                i += 1
                parts.append(re.sub(r"^\s*>\s?", "", lines[i]))
            blocks.append(_callout_block(marker.group(1), "".join(p.strip() for p in parts)))
            i += 1
            continue
        bullet = re.match(r"^\s*[-+*]\s+(.*)", line)
        ordered = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        quote = re.match(r"^\s*>\s?(.*)", line)
        if bullet or ordered or quote:
            match = bullet or ordered or quote
            key, bt = ("bullet", 12) if bullet else ("ordered", 13) if ordered else ("quote", 15)
            blocks.append({"block_type": bt, key: {"elements": _inline_elements(match.group(1))}})
        else:
            blocks.append(_text_block(line.strip()))
        i += 1
    return blocks


# KS_Feishu_Format：整体洞察 📊 light-orange；各节 💡 orange。飞书 background_color 枚举。
CALLOUT_STYLE = {
    "insight": {"background_color": 2, "emoji_id": "bar_chart"},   # light-orange
    "tip": {"background_color": 3, "emoji_id": "bulb"},            # orange
    "warn": {"background_color": 4, "emoji_id": "warning"},        # light-yellow
}


def _chart_name(block: dict) -> str:
    return str(block.get("_local_path") or "") + " " + str(block.get("_alt") or "")


def _is_dist_pair(left: dict, right: dict) -> bool:
    """平台分布饼图与内容类型饼图（KS 传播概况：多平台 50:50 并排）。"""
    return ("_platform" in _chart_name(left) and "_content" in _chart_name(right))


def _callout_block(kind: str, text: str) -> dict:
    return {"block_type": 19, "callout": dict(CALLOUT_STYLE[kind]), "_explicit_callout": kind,
            "_children": [_text_block(text)]}


def _block_text(block):
    return "".join(el.get("text_run", {}).get("content", "")
                   for el in (block.get(_key_for(block)) or {}).get("elements", []))


def _grid(left, right, width_ratios=(50, 50)):
    return {"block_type": 24, "grid": {"column_size": 2},
            "_columns": [left, right], "_width_ratios": list(width_ratios)}


def layout_blocks(blocks: list[dict]) -> list[dict]:
    """Content→layout: callouts, cloud/topic pairs, image/data pairs, opinion columns."""
    result, i = [], 0
    first_paragraph = False
    description = False
    # FIX-46：文档里有显式 Callout 标记时，不再把首段机械转成 Callout；
    # 无标记的旧报告保留首段兜底（过渡期）。
    explicit = any(block.get("_explicit_callout") for block in blocks)
    while i < len(blocks):
        block = blocks[i]
        bt, text = block.get("block_type"), _block_text(block)
        if bt in _HEADING_BLOCK_TYPE.values():
            description = "数据说明" in text or (description and "整体洞察" not in text and bt > 4)
            first_paragraph = not description and (bt >= 5 or "整体洞察" in text)
            result.append(block)
            i += 1
            continue
        if block.get("_grid_marker") == "50:50":
            column_at = next((j for j in range(i + 1, len(blocks))
                              if blocks[j].get("_grid_marker") == "column"), None)
            end_at = next((j for j in range(i + 1, len(blocks))
                           if blocks[j].get("_grid_marker") == "end"), None)
            if column_at is None or end_at is None or not column_at < end_at:
                raise ValueError("报告双栏标记不完整（grid:50:50 / column / end）")
            result.append(_grid(blocks[i + 1:column_at], blocks[column_at + 1:end_at]))
            i = end_at + 1
            continue
        if block.get("_grid_marker"):
            raise ValueError(f"报告双栏标记孤立：{block['_grid_marker']}")
        if text.startswith("正面观点 TOP5"):
            stop = i + 1
            while stop < len(blocks) and blocks[stop].get("block_type") not in (*_HEADING_BLOCK_TYPE.values(), 27):
                stop += 1
            split = next((j for j in range(i + 1, stop)
                          if _block_text(blocks[j]).startswith("负面观点 TOP5")), None)
            if split is not None:
                result.append(_grid(blocks[i:split], blocks[split:stop]))
                i = stop
                continue
        if bt == 27:
            stop = i + 1
            if "词云" in str(block.get("_alt") or ""):
                while stop < len(blocks) and blocks[stop].get("block_type") in (2, 12, 13, 15):
                    stop += 1
                if stop > i + 1:
                    result.append(_grid([block], blocks[i + 1:stop]))
                    i = stop
                    continue
            elif stop < len(blocks) and (
                    blocks[stop].get("block_type") == 31
                    or (blocks[stop].get("block_type") == 27 and _is_dist_pair(block, blocks[stop]))):
                # FIX-52：图+图只对「平台分布 + 内容类型」这一对并排；其他相邻图通栏。
                right = [blocks[stop]]
                stop += 1
                # FIX-51：2.N.3 右栏 = 情感数据表 + 活动整体 NSR + 灰色口径注。
                if stop < len(blocks) and _block_text(blocks[stop]).startswith(("活动整体净情感度", "活动整体 NSR")):
                    right.append(blocks[stop])
                    stop += 1
                    if stop < len(blocks) and _block_text(blocks[stop]).startswith("NSR ="):
                        right.append(blocks[stop])
                        stop += 1
                image_name = str(block.get("_alt") or "") + " " + str(block.get("_local_path") or "")
                ratios = (55, 45) if "漏斗" in image_name or "mention_funnel" in image_name else (50, 50)
                result.append(_grid([block], right, ratios))
                i = stop
                continue
        if first_paragraph and bt == 2 and not explicit:
            result.append({"block_type": 19, "callout": {"background_color": 2, "emoji_id": "bulb"},
                           "_children": [block]})
            first_paragraph = False
        else:
            result.append(block)
        i += 1
    return result


def _flatten_blocks(blocks):
    for block in blocks:
        yield block
        yield from _flatten_blocks(block.get("_children") or [])
        for column in block.get("_columns") or []:
            yield from _flatten_blocks(column)


def _strip_internal(block: dict) -> dict:
    """剥离 image block 的内部提示键（_local_path/_alt），得到可发送的 block。"""
    result = {k: v for k, v in block.items() if not k.startswith("_")}
    if result.get("block_type") == _BLOCK_TABLE:
        table = dict(result["table"])
        prop = dict(table["property"])
        # The children-create API accepts at most 9 rows and 9 columns.
        # Keep the full source dimensions intact for expansion before filling.
        for dimension in ("row_size", "column_size"):
            prop[dimension] = min(prop[dimension], 9)
        table["property"] = prop
        result["table"] = table
    return result


# ── 真路径 ────────────────────────────────────────────────────
def _create_children(token: str, doc_token: str, blocks: list[dict],
                     batch: int = 50, parent_id: str = "", start_index: int = 0) -> list[dict]:
    """批量创建子 block（docx 单次 children 上限 50），返回每个已建 block（含 block_id），顺序对齐。"""
    created: list[dict] = []
    index = start_index
    for i in range(0, len(blocks), batch):
        chunk = [_strip_internal(b) for b in blocks[i:i + batch]]
        data = _req(
            "POST",
            f"/open-apis/docx/v1/documents/{doc_token}/blocks/{parent_id or doc_token}/children",
            token, {"children": chunk, "index": index})
        created.extend(data.get("children") or [])
        index += len(chunk)
    if len(created) != len(blocks):
        raise RuntimeError("飞书创建块回执数量与提交数量不一致")
    return created


def _publish_blocks(token, doc_token, blocks, parent_id="", available_width=_DOCUMENT_BODY_WIDTH,
                    start_index: int = 0):
    created = _create_children(token, doc_token, blocks, parent_id=parent_id, start_index=start_index)
    _fill_tables(token, doc_token, blocks, created, available_width=available_width)
    images = _fill_images(token, doc_token, blocks, created)
    for submitted, got in zip(blocks, created):
        block_id = got.get("block_id")
        if submitted.get("_children"):
            images += _publish_blocks(token, doc_token, submitted["_children"], block_id,
                                      max(50, available_width - 32))
        if submitted.get("_columns"):
            columns = got.get("children") or []
            if len(columns) != 2:
                data = _req("GET", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{block_id}", token)
                columns = (data.get("block") or data).get("children") or []
            if len(columns) != 2:
                raise RuntimeError("飞书双栏Grid未返回两个Column")
            _req("PATCH", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{block_id}", token,
                 {"update_grid_column_width_ratio": {
                     "width_ratios": submitted.get("_width_ratios", [50, 50])}})
            ratios = submitted.get("_width_ratios", [50, 50])
            for column_id, children, ratio in zip(columns, submitted["_columns"], ratios):
                width = round(max(50, available_width - _GRID_GAP) * ratio / sum(ratios))
                images += _publish_blocks(token, doc_token, children, column_id, width)
    return images


def _fill_images(token: str, doc_token: str, blocks: list[dict],
                 created: list[dict]) -> int:
    """对每个 image block 上传本地 PNG 为 media 并 PATCH replace_image 回填。返回成功张数。

    best-effort：单张上传/回填失败只跳过该图（不阻断整篇交付）。
    """
    filled = 0
    for submitted, got in zip(blocks, created):
        if submitted.get("block_type") != _BLOCK_IMAGE:
            continue
        local_path = submitted.get("_local_path")
        block_id = got.get("block_id")
        if not (local_path and block_id and os.path.isfile(local_path)):
            continue
        try:
            file_token = _upload_media(token, local_path, parent_node=block_id)
            _req("PATCH",
                 f"/open-apis/docx/v1/documents/{doc_token}/blocks/{block_id}",
                 token, {"replace_image": {"token": file_token}})
            filled += 1
        except Exception as error:
            raise RuntimeError(f"飞书图片上传或回填失败: {type(error).__name__}") from error
    return filled


def _expand_table(token: str, doc_token: str, block_id: str,
                  row_count: int, column_count: int) -> dict:
    """Grow a small native table, reconciling its live dimensions first."""
    path = f"/open-apis/docx/v1/documents/{doc_token}/blocks/{block_id}"
    data = _req("GET", path, token)
    table = (data.get("block") or data).get("table") or {}
    prop = table.get("property") or {}
    current_rows = int(prop.get("row_size") or 0)
    current_columns = int(prop.get("column_size") or 0)
    if not (0 < current_rows <= row_count and 0 < current_columns <= column_count):
        raise RuntimeError("飞书表格扩容前尺寸与源表格不一致")
    for axis, start, target in (
        ("column", current_columns, column_count),
        ("row", current_rows, row_count),
    ):
        for index in range(start, target):
            client_token = uuid.uuid5(
                uuid.NAMESPACE_URL, f"{doc_token}/{block_id}/{axis}/{index}"
            )
            _req(
                "PATCH", f"{path}?client_token={client_token}", token,
                {f"insert_table_{axis}": {f"{axis}_index": -1}},
            )
    data = _req("GET", path, token)
    table = (data.get("block") or data).get("table") or {}
    prop = table.get("property") or {}
    if prop.get("row_size") != row_count or prop.get("column_size") != column_count:
        raise RuntimeError("飞书表格扩容后尺寸与源表格不一致")
    return table


def _write_table_cell(token: str, doc_token: str, cell_id: str, value: str) -> None:
    """Reuse the native paragraph; remove only verified empty trailing paragraphs."""
    path = f"/open-apis/docx/v1/documents/{doc_token}/blocks/{cell_id}"
    data = _req("GET", path, token)
    children = (data.get("block") or data).get("children") or []
    if not children:
        raise RuntimeError("飞书表格单元格缺少默认段落")
    # Earlier publishers inserted content before the default empty paragraph.
    # Never remove a nonempty or non-text block while repairing that layout.
    for child_id in children[1:]:
        data = _req("GET", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{child_id}", token)
        child = data.get("block") or data
        if child.get("block_type") != _BLOCK_TEXT or _block_text(child).strip():
            raise RuntimeError("飞书表格单元格含额外非空内容，禁止自动覆盖")
    _req(
        "PATCH", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{children[0]}",
        token, {"update_text_elements": {"elements": _inline_elements(value)}},
    )
    if len(children) > 1:
        _req("DELETE", f"{path}/children/batch_delete", token,
             {"start_index": 1, "end_index": len(children)})


def _fill_tables(token: str, doc_token: str, blocks: list[dict],
                 created: list[dict], *, available_width: int = _DOCUMENT_BODY_WIDTH) -> int:
    """把内部表格行数据写入飞书创建出的 table cell block。"""
    filled = 0
    for submitted, got in zip(blocks, created):
        if submitted.get("block_type") != _BLOCK_TABLE:
            continue
        rows = submitted.get("_table_rows") or []
        table = got.get("table") or {}
        if rows and (len(rows) > 9 or len(rows[0]) > 9):
            block_id = got.get("block_id")
            if not block_id:
                raise RuntimeError("飞书表格扩容缺少 block_id")
            table = _expand_table(
                token, doc_token, block_id, len(rows), len(rows[0])
            )
        cell_ids = table.get("cells") or []
        if len(cell_ids) != sum(len(row) for row in rows):
            block_id = got.get("block_id")
            if block_id:
                data = _req(
                    "GET",
                    f"/open-apis/docx/v1/documents/{doc_token}/blocks/{block_id}",
                    token,
                )
                table = (data.get("block") or data).get("table") or {}
                cell_ids = table.get("cells") or []
        values = [cell for row in rows for cell in row]
        if len(cell_ids) != len(values):
            raise RuntimeError(
                f"飞书表格单元格数量异常：expected={len(values)} got={len(cell_ids)}")
        widths = _table_column_widths(rows, available_width)
        for index, width in enumerate(widths):
            _req("PATCH", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{got['block_id']}",
                 token, {"update_table_property": {
                     "column_index": index, "column_width": width}})
        for cell_id, value in zip(cell_ids, values):
            _write_table_cell(token, doc_token, cell_id, value)
        filled += 1
    return filled


def grant_access(token: str, obj_token: str, obj_type: str, owner: str | None) -> dict:
    """转移所有权给操作人；失败降级为 full_access 协作者。obj_type=docx|folder。"""
    result = {"transferred": False, "transfer_error": "", "collaborator_added": False,
              "collaborator_error": ""}
    if not owner:
        result.update(transfer_error="owner_not_configured",
                      collaborator_error="collaborator_target_not_configured")
        return result
    try:
        _req("POST", f"/open-apis/drive/v1/permissions/{obj_token}/members/transfer_owner"
             f"?type={obj_type}&need_notification=false&remove_old_owner=false"
             "&old_owner_perm=full_access&stay_put=true", token,
             {"member_type": "openid", "member_id": owner})
        result["transferred"] = True
        return result
    except Exception as error:
        result["transfer_error"] = f"{type(error).__name__}: {error}"
    try:
        _req("POST", f"/open-apis/drive/v1/permissions/{obj_token}/members"
             f"?type={obj_type}&need_notification=false", token,
             {"member_type": "openid", "member_id": owner, "perm": "full_access", "type": "user"})
        result["collaborator_added"] = True
    except Exception as error:
        result["collaborator_error"] = f"{type(error).__name__}: {error}"
    return result


def ensure_project_folder(token: str, root_folder_token: str, project_name: str) -> str:
    """FIX-41：在根目录下按项目名查找文件夹，没有就创建；同名复用（幂等）。"""
    page_token = ""
    while True:
        query = f"?folder_token={root_folder_token}&page_size=200" + (
            f"&page_token={page_token}" if page_token else "")
        data = _req("GET", "/open-apis/drive/v1/files" + query, token)
        for item in data.get("files") or []:
            if item.get("type") == "folder" and item.get("name") == project_name:
                return item["token"]
        if not data.get("has_more"):
            break
        page_token = data.get("next_page_token") or ""
    created = _req("POST", "/open-apis/drive/v1/files/create_folder", token,
                   {"name": project_name, "folder_token": root_folder_token})
    folder = created.get("token")
    if not folder:
        raise RuntimeError(f"创建项目文件夹失败：{created}")
    return folder


def create_delivery(report_md: str, *, title: str, task_id: str,
                    report_version: str, reused_doc_token: str = "",
                    key_numbers: list | None = None, project_name: str = "",
                    published: dict | None = None) -> dict:
    """新建飞书 docx（或复用已有回执），写入内容（图片上传 media 回填），转移所有权，回读核对。

    返回 feishu_doc：{doc_token, url, revision_id, report_version, images_filled,
                      readback, degraded:False}。
    """
    token = _tenant_token()
    # FIX-41：配置了根目录时文档建在项目专属文件夹；未配置保持原行为并由上层记 P1。
    root_folder = os.environ.get("FEISHU_ROOT_FOLDER_TOKEN", "").strip()
    folder_token = (ensure_project_folder(token, root_folder, project_name or title)
                    if root_folder else "")

    # FIX-38：已发布过的文档在原文档上修正（链接、批注不变），不重新建文档。
    rework = {"mode": "create"}
    if published and published.get("doc_token") and not reused_doc_token:
        doc_token = published["doc_token"]
        plan = plan_patch(published.get("markdown") or "", report_md)
        if plan["mode"] == "patch":
            try:
                rework = {"mode": "patch", "ops": len(plan["ops"]),
                          "applied": patch_document(token, doc_token, plan["ops"])}
            except Exception as error:
                rework = {"mode": "rebuild", "reason": f"原位修正失败，退回重建：{type(error).__name__}: {error}"}
        else:
            rework = {"mode": "rebuild", "reason": plan["reason"]}
        images_filled = 0
        if rework["mode"] == "rebuild":
            images_filled = _rebuild_in_place(token, doc_token, layout_blocks(md_to_blocks(report_md)))
            rework["notice"] = "链接不变，内容重建"
        folder_token = published.get("folder_token") or folder_token
    # 幂等复用：命中已有回执则不新建。
    elif reused_doc_token:
        doc_token = reused_doc_token
    else:
        data = _req("POST", "/open-apis/docx/v1/documents", token,
                    {"title": title, **({"folder_token": folder_token} if folder_token else {})})
        doc = data.get("document") or {}
        doc_token = doc.get("document_id") or doc.get("document", {}).get("document_id")
        if not doc_token:
            raise RuntimeError(f"创建飞书文档失败：{data}")

    # 写入 block（批量 children），再对 image block 上传 media 回填。
    blocks = layout_blocks(md_to_blocks(report_md))
    if rework["mode"] == "create":
        images_filled = 0
        if blocks and not reused_doc_token:
            images_filled = _publish_blocks(token, doc_token, blocks)

    # FIX-41：先转移项目文件夹，再转移文档；两者均按「转移所有权→降级 full_access 协作者」处理。
    owner = os.environ.get("FEISHU_USER_OPEN_ID")
    folder_access = (grant_access(token, folder_token, "folder", owner)
                     if folder_token else {"skipped": True})
    doc_access = grant_access(token, doc_token, "docx", owner)
    ownership_transferred = doc_access["transferred"]
    ownership_error = doc_access["transfer_error"]
    collaborator_added = doc_access["collaborator_added"]
    collaborator_error = doc_access["collaborator_error"]

    # 导出后回读核对：重新读回 blocks，核对章节齐全、图 block 生效、版本一致。
    readback = _readback_check(token, doc_token, report_md, report_version, key_numbers)
    revision_id = readback.get("revision_id") or 1

    return {
        "doc_token": doc_token,
        "url": f"{_WEB_BASE}/docx/{doc_token}",
        "revision_id": revision_id,
        "report_version": report_version,
        "ownership_transferred": ownership_transferred,
        "ownership_target": owner,
        "folder_token": folder_token,
        "folder_configured": bool(root_folder),
        "folder_access": folder_access,
        "ownership_error": ownership_error,
        "collaborator_added": collaborator_added,
        "collaborator_perm": "full_access" if collaborator_added else "",
        "collaborator_error": collaborator_error,
        "delivery_access_granted": ownership_transferred or collaborator_added,
        "images_filled": images_filled,
        "rework": rework,
        "readback": readback,
        "degraded": False,
    }


# ── FIX-38 发布后原位修正（KS_Feishu_Format §十）─────────────────────────────
# 文本：原 block 上 update_text_elements；图片：上传后 replace_image（block 不变）；
# GRID/表格/Callout 等容器：整体删除后在原位置插入新块（不改 COLUMN）。
_TEXTUAL = {_BLOCK_TEXT, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}


def _root_children(token: str, doc_token: str) -> list[str]:
    data = _req("GET", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{doc_token}", token)
    return list((data.get("block") or data).get("children") or [])


def _headings(blocks: list[dict]) -> list[str]:
    return [_block_text(b).strip() for b in _flatten_blocks(blocks)
            if b.get("block_type") in _HEADING_BLOCK_TYPE.values()]


def _comparable(block: dict) -> str:
    return json.dumps(block, ensure_ascii=False, sort_keys=True, default=str)


def plan_patch(old_md: str, new_md: str) -> dict:
    """新旧 MD 的顶层 block 对比。章节增删或顶层块数变化 → rebuild；否则逐块生成 ops。"""
    old_blocks = layout_blocks(md_to_blocks(old_md))
    new_blocks = layout_blocks(md_to_blocks(new_md))
    if _headings(old_blocks) != _headings(new_blocks) or len(old_blocks) != len(new_blocks):
        return {"mode": "rebuild", "ops": [], "reason": "章节增删或结构变化"}
    ops = []
    for index, (old, new) in enumerate(zip(old_blocks, new_blocks)):
        if _comparable(old) == _comparable(new):
            continue
        kind = new.get("block_type")
        if kind == old.get("block_type") and kind in _TEXTUAL and not new.get("_children"):
            ops.append({"op": "replace_text", "index": index, "block": new})
        elif kind == old.get("block_type") == _BLOCK_IMAGE:
            ops.append({"op": "replace_image", "index": index, "block": new})
        else:
            ops.append({"op": "replace_block", "index": index, "block": new})
    return {"mode": "patch", "ops": ops, "reason": ""}


def patch_document(token: str, doc_token: str, ops: list[dict]) -> int:
    """按 ops 原位修正；任何 API 失败直接抛错（调用方退回重建，不静默失败）。返回执行条数。"""
    children = _root_children(token, doc_token)
    base = f"/open-apis/docx/v1/documents/{doc_token}/blocks"
    for op in sorted(ops, key=lambda o: o["index"]):
        index, block = op["index"], op["block"]
        if index >= len(children):
            raise RuntimeError("原位修正定位失败：线上文档顶层块少于预期")
        block_id = children[index]
        if op["op"] == "replace_text":
            key = next(k for k in block if not k.startswith("_") and isinstance(block[k], dict)
                       and "elements" in block[k])
            _req("PATCH", f"{base}/{block_id}", token,
                 {"update_text_elements": {"elements": block[key]["elements"]}})
        elif op["op"] == "replace_image":
            local_path = block.get("_local_path")
            if not (local_path and os.path.isfile(local_path)):
                raise RuntimeError("原位修正图片缺少本地 PNG")
            file_token = _upload_media(token, local_path, parent_node=block_id)
            _req("PATCH", f"{base}/{block_id}", token, {"replace_image": {"token": file_token}})
        else:
            _req("DELETE", f"{base}/{doc_token}/children/batch_delete", token,
                 {"start_index": index, "end_index": index + 1})
            _publish_blocks(token, doc_token, [block], start_index=index)
            children = _root_children(token, doc_token)
    return len(ops)


def _rebuild_in_place(token: str, doc_token: str, blocks: list[dict]) -> int:
    """链接不变、内容重建：清空根节点下全部块后重新发布。"""
    count = len(_root_children(token, doc_token))
    if count:
        _req("DELETE", f"/open-apis/docx/v1/documents/{doc_token}/blocks/{doc_token}/children/batch_delete",
             token, {"start_index": 0, "end_index": count})
    return _publish_blocks(token, doc_token, blocks)


def _expected_image_count(report_md: str) -> int:
    """报告 MD 中指向**存在本地文件**的图片数（真正会进飞书的 image block 数）。"""
    n = 0
    for alt, path in re.findall(r"!\[(.*?)\]\((.*?)\)", report_md or ""):
        if path and os.path.isfile(path):
            n += 1
    return n


def _list_document_blocks(token: str, doc_token: str) -> list[dict]:
    """Read every block page; tables can push tail chapters beyond page 1."""
    items: list[dict] = []
    page_token = ""
    while True:
        path = (
            f"/open-apis/docx/v1/documents/{doc_token}/blocks?page_size=500"
        )
        if page_token:
            path += "&page_token=" + urllib.parse.quote(page_token, safe="")
        data = _req("GET", path, token)
        items.extend(data.get("items") or [])
        if not data.get("has_more"):
            return items
        next_token = str(data.get("page_token") or "")
        if not next_token or next_token == page_token:
            raise RuntimeError("飞书文档 block 分页游标缺失或未推进")
        page_token = next_token


def _table_layout_check(items: list[dict], source_blocks: list[dict]) -> dict:
    """Verify effective native widths and one paragraph per table cell."""
    by_id = {block["block_id"]: block for block in items}
    ordered, visited = [], set()

    def walk(block):
        block_id = block["block_id"]
        if block_id in visited:
            return
        visited.add(block_id)
        ordered.append(block)
        for child_id in block.get("children") or []:
            if child_id in by_id:
                walk(by_id[child_id])

    for block in items:
        if block.get("block_type") == 1:
            walk(block)
    for block in items:
        walk(block)
    expected = list(_layout_table_widths(source_blocks))
    tables = [block for block in ordered if block.get("block_type") == 31]
    actual = [(block.get("table") or {}).get("property", {}).get("column_width") or []
              for block in tables]
    bad_cells = [
        block["block_id"] for block in items
        if block.get("block_type") == 32 and (
            len(block.get("children") or []) != 1
            or by_id.get(block["children"][0], {}).get("block_type") != 2)
    ]
    return {"ok": actual == expected and not bad_cells,
            "expected_widths": expected, "actual_widths": actual,
            "invalid_cells": bad_cells}


def numbers_check(text_all: str, key_numbers: list | None) -> dict:
    """关键数字回读核对（FIX-40）：每个 value_text 须出现在回读全文中。"""
    missing = [n for n in key_numbers or [] if str(n.get("value_text") or "") not in text_all]
    return {"checked": len(key_numbers or []), "missing": missing, "ok": not missing}


def _readback_check(token: str, doc_token: str, report_md: str,
                    report_version: str, key_numbers: list | None = None) -> dict:
    """导出后回读核对：章节齐全 / 图 block 生效 / 绑定版本一致 / 关键数字一致。"""
    data = _req("GET", f"/open-apis/docx/v1/documents/{doc_token}", token)
    doc = data.get("document") or {}
    revision_id = doc.get("revision_id")
    items = _list_document_blocks(token, doc_token)
    text_all = "\n".join(
        el.get("text_run", {}).get("content", "")
        for b in items for el in (b.get(_key_for(b), {}) or {}).get("elements", [])
        if isinstance(el, dict))
    source_blocks = md_to_blocks(report_md)
    want_headings = [_block_text(b) for b in source_blocks if b.get("block_type") in _HEADING_BLOCK_TYPE.values()]
    missing = [h.strip() for h in want_headings if h.strip() and h.strip() not in text_all]
    # 图核对：回读到的 image block 数 >= 期望本地图数即视为图引用生效。
    want_images = _expected_image_count(report_md)
    got_images = sum(1 for b in items if b.get("block_type") == _BLOCK_IMAGE)
    images_ok = got_images >= want_images
    expected = list(_flatten_blocks(layout_blocks(source_blocks)))
    layout_counts = {
        name: {"expected": sum(b.get("block_type") == bt for b in expected),
               "got": sum(b.get("block_type") == bt for b in items)}
        for name, bt in (("grid", 24), ("callout", 19), ("table", 31))
    }
    layout_counts["grid_column"] = {
        "expected": 2 * layout_counts["grid"]["expected"],
        "got": sum(b.get("block_type") == 25 for b in items),
    }
    layout_ok = all(count["got"] >= count["expected"] for count in layout_counts.values())
    table_layout = _table_layout_check(items, layout_blocks(source_blocks))
    numbers = numbers_check(text_all, key_numbers)
    return {
        "numbers_check": numbers,
        # FIX-36：内容类返工以飞书回读文本为复检对象。
        "readback_text": text_all,
        "revision_id": revision_id,
        "chapters_complete": not missing,
        "missing_chapters": missing,
        "images_referenced": images_ok,
        "expected_images": want_images,
        "got_images": got_images,
        "version_bound": report_version,
        "layout_counts": layout_counts,
        "layout_complete": layout_ok,
        "table_layout": table_layout,
        "ok": (not missing) and images_ok and layout_ok and table_layout["ok"] and numbers["ok"],
    }


def _key_for(block: dict) -> str:
    """回读 block 的内容键：优先按 block_type 命中的内容字段，回退空。"""
    bt = block.get("block_type")
    if bt == _BLOCK_TEXT:
        return "text"
    if bt in (12, 13, 15):
        return {12: "bullet", 13: "ordered", 15: "quote"}[bt]
    for lvl, code in _HEADING_BLOCK_TYPE.items():
        if bt == code:
            return _HEADING_KEY[lvl]
    return ""


# ── handoff 复读 ──────────────────────────────────────────────
def current_revision(doc_token: str) -> Optional[int]:
    """handoff 阶段复读文档当前 revision_id；无凭据返回 None。

    复读失败带原因抛出：n24 随之失败、可 /continue 重试（吞掉会让 n24 标 completed、
    run 再判 failed，retry 报「当前节点未失败」无法恢复）。
    """
    if not doc_token or not api_available():
        return None
    token = _tenant_token()
    try:
        data = _req("GET", f"/open-apis/docx/v1/documents/{doc_token}", token)
    except Exception as error:
        raise RuntimeError(f"handoff: 复读飞书文档 revision 失败（{doc_token}）：{error}") from error
    return (data.get("document") or {}).get("revision_id")


# ── 降级 ──────────────────────────────────────────────────────
def fallback(report_md: str, *, title: str, task_id: str,
             report_version: str) -> dict:
    """无凭据/失败降级：确定性占位 feishu_doc（schema 与真路径一致，degraded=True）。"""
    import hashlib
    h = hashlib.sha1(f"{task_id}:{report_version}".encode()).hexdigest()[:16]
    doc_token = f"docx_stub_{h}"
    want_headings = [x.strip() for x in re.findall(r"^#{1,4}\s+(.*)$", report_md or "", flags=re.M)]
    return {
        "doc_token": doc_token,
        "url": f"{_WEB_BASE}/docx/{doc_token}",
        "revision_id": 1,
        "report_version": report_version,
        "ownership_transferred": False,
        "ownership_target": None,
        "ownership_error": "diagnostic_fallback",
        "collaborator_added": False,
        "collaborator_perm": "",
        "collaborator_error": "diagnostic_fallback",
        "delivery_access_granted": False,
        "images_filled": 0,
        "readback": {
            "revision_id": 1,
            "chapters_complete": bool(want_headings),
            "missing_chapters": [],
            "images_referenced": True,
            "expected_images": 0,
            "got_images": 0,
            "version_bound": report_version,
            "ok": True,
        },
        "degraded": True,
    }
