"""用户上传活动明细 Excel → clean_campaign 行结构适配。

权威样例列来自 ``瑞幸暹罗厘普联名数据.xlsx``。默认按 Gateway 校验过的飞书附件引用
下载到沙箱；兼容旧任务的 Ark Files 挂载。confirmed_plan.source_file 是运行期引用。
没有 source_file 时返回 None，让 handler fail-closed；
已声明文件但不可读则抛错，禁止静默换源。
"""
from __future__ import annotations

import os
import re
import hashlib
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import platforms

_CST = timezone(timedelta(hours=8))
# 平台映射唯一事实源见 platforms.py（FIX-17）；此处不再维护局部映射表。
_CONTENT_MODE = {"UGC": "UGC", "PGC": "PGC", "未知": "UKN", "UKN": "UKN"}
_MAX_FILE_BYTES = 50 * 1024 * 1024
# FIX-11：活动过滤词并入品牌过滤词的误伤阈值（原版 KS_Data_Processing：5%）。
MERGE_HIT_THRESHOLD = 0.05


def normalize_source_file(source: dict) -> dict:
    """Validate and retain both legacy mounts and authenticated Feishu sources."""
    mount = str(source.get("mount_path") or "")
    if Path(mount).name != mount or not mount.lower().endswith(".xlsx"):
        raise ValueError("excel_source: source_file.mount_path 非法")
    result = {"name": str(source.get("name") or mount), "mount_path": mount}
    transport = source.get("transport")
    if transport in (None, "", "ark_files"):
        return result
    if transport != "feishu_resource":
        raise ValueError("excel_source: 不支持的文件传输方式")
    for key, pattern in (("message_id", r"om_[A-Za-z0-9_]+"),
                         ("file_key", r"file_[A-Za-z0-9_-]+"),
                         ("sha256", r"[0-9a-f]{64}")):
        value = source.get(key)
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"excel_source: 飞书附件 {key} 无效")
        result[key] = value
    result["transport"] = transport
    return result


def _stage_feishu_file(source: dict, path: Path) -> None:
    """Download the original attachment atomically, verifying the host checksum."""
    expected = source["sha256"]
    if path.is_file() and path.stat().st_size <= _MAX_FILE_BYTES:
        if hashlib.sha256(path.read_bytes()).hexdigest() == expected:
            return
    import feishu
    try:
        token = feishu._tenant_token()
    except RuntimeError:
        raise ValueError("excel_source: 飞书鉴权失败，请检查沙箱飞书凭据与权限") from None
    message_id = urllib.parse.quote(source["message_id"], safe="")
    file_key = urllib.parse.quote(source["file_key"], safe="")
    request = urllib.request.Request(
        f"{feishu._BASE}/open-apis/im/v1/messages/{message_id}/resources/{file_key}?type=file",
        headers={"Authorization": "Bearer " + token})
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
                temporary = Path(output.name)
                checksum, total = hashlib.sha256(), 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_FILE_BYTES:
                        raise ValueError("excel_source: 飞书附件超过 50 MiB")
                    output.write(chunk)
                    checksum.update(chunk)
        if total == 0 or checksum.hexdigest() != expected:
            raise ValueError("excel_source: 飞书附件 SHA256 与 Gateway 校验值不一致")
        os.replace(temporary, path)
    except urllib.error.HTTPError as error:
        raise ValueError(f"excel_source: 飞书附件下载失败 HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise ValueError(f"excel_source: 飞书附件下载失败 ({type(error).__name__})") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _integer_or_none(value: Any) -> int | None:
    """粉丝数/互动分项：缺失返回 None，不补 0（FIX-16 / FIX-59）。

    补 0 会让缺失粉丝数的内容被判成「素人」，达人层级随之失真。
    """
    if value in (None, "", "-", "--"):
        return None
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int:
    if value in (None, "", "-", "--"):
        return 0
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _publish_time_ms(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                numeric = int(float(text))
            except (TypeError, ValueError):
                return None
            return numeric if numeric > 10**12 else numeric * 1000
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CST)
    return int(dt.timestamp() * 1000)


def _keywords(value: Any) -> list[str]:
    return list(
        dict.fromkeys(
            item.strip().lstrip("#")
            for item in re.split(r"[|,，、\s]+", _text(value))
            if item.strip().lstrip("#")
        )
    )


def normalize_excel_row(row: dict[str, Any]) -> dict:
    """把一行中文 Excel 列转换为 pipeline 统一行结构。"""
    source_content_mode = _text(row.get("内容类型"))
    content_mode = _CONTENT_MODE.get(source_content_mode.upper(), "UKN")
    site_name = _text(row.get("站点名称"))
    interaction = _integer(row.get("总互动量"))
    if not interaction:
        interaction = sum(
            _integer(row.get(key))
            for key in ("转发数", "评论数", "点赞数", "收藏数")
        )
    item_id = _text(row.get("id"))
    return {
        "id": item_id,
        "fingerprint": item_id,
        "data_type": _text(row.get("数据类型")),
        "analysis_object": _text(row.get("分析对象")),
        "site_name": site_name,
        "_platform": platforms.normalize_slug(site_name)[0],
        "_platform_matched": platforms.normalize_slug(site_name)[1],
        "title": _text(row.get("标题")),
        "content": _text(row.get("内容")),
        "video_cover_text": _text(row.get("视频封面内容识别")),
        "audio_text": _text(row.get("音频内容识别")),
        "video_flower_text": _text(row.get("视频花字识别")),
        "video_text": _text(row.get("视频文本识别")),
        "content_mode": content_mode,
        "_source_content_mode": source_content_mode,
        "keywords": _keywords(row.get("话题")),
        "author": _text(row.get("作者")),
        "url": _text(row.get("url")),
        "publish_time": _publish_time_ms(row.get("发表时间")),
        "interaction_cnt": interaction,
        "share_count": _integer(row.get("转发数")),
        "comment_count": _integer(row.get("评论数")),
        "like_count": _integer(row.get("点赞数")),
        "read_count": _integer(row.get("阅读数")),
        "collect_count": _integer(row.get("收藏数")),
        "fans": _integer_or_none(row.get("粉丝数")),
        "_source": "user_uploaded_excel",
    }


def resolve_activity_xlsx(plan: dict) -> Optional[Path]:
    """解析已冻结的 Session 挂载路径；测试可用 TOPIC5_ACTIVITY_XLSX_PATH 覆盖。"""
    override = os.environ.get("TOPIC5_ACTIVITY_XLSX_PATH")
    if override:
        return Path(override).expanduser()
    source = plan.get("source_file") if isinstance(plan, dict) else None
    if not isinstance(source, dict) or not source.get("mount_path"):
        return None
    source = normalize_source_file(source)
    mount_path = source["mount_path"]
    if source.get("transport") == "feishu_resource":
        uploads_dir = Path(os.environ.get("TOPIC5_SESSION_UPLOADS_DIR", "/workspace/topic5_repro_inputs"))
        path = uploads_dir / mount_path
        _stage_feishu_file(source, path)
        return path
    uploads_dir = Path(os.environ.get("TOPIC5_SESSION_UPLOADS_DIR", "/mnt/session/uploads"))
    return uploads_dir / mount_path


def check_filterwords(rows: list[dict], filter_expr: Any) -> dict:
    """按活动过滤词在标题/正文/话题里做字面检索，不区分大小写（FIX-11）。

    原版 stats-aggregation/check_filterwords.py：取品牌数据之前先确认活动 Excel
    是否已按过滤词清洗。命中不等于脏数据，只把决定权交给用户。

    返回 {"hits": [{row_index, title, field, word}], "hit_rows": n,
          "total_rows": n, "ratio": float, "words": [...]}
    """
    if isinstance(filter_expr, (list, tuple)):
        words = [str(w).strip() for w in filter_expr if str(w).strip()]
    else:
        words = _keywords(filter_expr)
    total = len(rows or [])
    if not words or not total:
        return {"hits": [], "hit_rows": 0, "total_rows": total,
                "ratio": 0.0, "words": words}
    lowered = [(word, word.lower()) for word in words]
    hits = []
    hit_rows = set()
    for index, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        fields = {
            "标题": str(row.get("title") or ""),
            "正文": str(row.get("content") or ""),
            "话题": " ".join(str(item) for item in (row.get("topics") or [])),
        }
        for name, text in fields.items():
            haystack = text.lower()
            for word, needle in lowered:
                if needle and needle in haystack:
                    hits.append({"row_index": index, "title": fields["标题"],
                                 "field": name, "word": word})
                    hit_rows.add(index)
    return {
        "hits": hits,
        "hit_rows": len(hit_rows),
        "total_rows": total,
        "ratio": round(len(hit_rows) / total, 4) if total else 0.0,
        "words": words,
    }


def merge_filterword_impact(brand_total: int, filtered_total: int) -> dict:
    """A−B 误伤评估：活动过滤词并入品牌过滤词前后声量差（FIX-11）。

    原版口径：差值不超过 5% 可以并入；超过 5% 需逐词定位误伤词，交用户决定。
    """
    a = int(brand_total or 0)
    b = int(filtered_total or 0)
    if a <= 0:
        return {"brand_total": a, "filtered_total": b, "delta_ratio": 0.0,
                "within_threshold": True, "threshold": MERGE_HIT_THRESHOLD}
    delta = round((a - b) / a, 4)
    return {
        "brand_total": a,
        "filtered_total": b,
        "delta_ratio": delta,
        "within_threshold": delta <= MERGE_HIT_THRESHOLD,
        "threshold": MERGE_HIT_THRESHOLD,
    }


def load_activity_rows(plan: dict) -> Optional[dict]:
    """读取活动明细；无 source_file 返回 None，声明后缺文件/坏文件则报错。"""
    path = resolve_activity_xlsx(plan)
    if path is None:
        return None
    if not path.is_file():
        raise FileNotFoundError(f"excel_source: 已挂载活动明细不存在: {path}")

    try:
        from openpyxl import load_workbook
    except ImportError as error:
        raise RuntimeError("excel_source: 运行环境缺少 openpyxl") from error

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        header_values = next(rows, None)
        if not header_values:
            raise ValueError("excel_source: Excel 没有表头")
        headers = [_text(value) for value in header_values]
        has_analysis_object_column = "分析对象" in headers
        required = {
            "站点名称", "标题", "内容", "发表时间", "内容类型", "url",
        }
        # FIX-12：单一确认活动时「分析对象」列可缺（自动用活动名填充）；
        # 多活动时必须有，否则无法按活动拆分。
        single_confirmed = len({
            str(item.get("id")) for item in (plan.get("campaigns") or [])
            if isinstance(item, dict) and item.get("id")
        }) == 1
        if not has_analysis_object_column and not single_confirmed:
            required.add("分析对象")
        missing = sorted(required - set(headers))
        if missing:
            raise ValueError("excel_source: 缺少必要列: " + "、".join(missing))
        interaction_columns = {
            "总互动量", "转发数", "评论数", "点赞数", "收藏数",
        }
        if not interaction_columns.intersection(headers):
            raise ValueError("excel_source: 缺少互动量列（总互动量或互动分项）")

        content_scope = str(plan.get("content_scope") or "all").lower()
        expected_mode = {"ugc": "UGC", "pgc": "PGC"}.get(content_scope)
        expected_platforms = {
            platforms.to_slug(value) for value in (plan.get("platforms") or [])
        }
        expected_campaigns = {
            str(item.get("id"))
            for item in (plan.get("campaigns") or [])
            if isinstance(item, dict) and item.get("id")
        }
        # FIX-12：Excel 无「分析对象」列且只有一个确认活动时，自动用活动名填充并记日志。
        auto_filled_object = None
        if (not has_analysis_object_column) and len(expected_campaigns) == 1:
            auto_filled_object = next(iter(expected_campaigns))
        period_start = str(plan.get("period_start") or "").replace("-", "")
        period_end = str(plan.get("period_end") or "").replace("-", "")
        # FIX-12：每行按所属活动自身的周期过滤（Excel 有「分析对象」列时按活动查表，
        # 无该列时不启用活动窗口，回落到合并窗口）。
        campaign_windows = {}
        for item in plan.get("campaigns") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("id") or item.get("name") or "").strip()
            if not key:
                continue
            campaign_windows[key] = (
                str(item.get("period_start") or "").replace("-", "") or period_start,
                str(item.get("period_end") or "").replace("-", "") or period_end,
            )
        normalized: list[dict] = []
        # FIX-12：集合核对用周期过滤前的分析对象；按活动周期剔除的行单独计数。
        seen_campaigns: set[str] = set()
        period_dropped: dict[str, int] = {}
        source_row_count = 0
        unknown_sites: dict[str, int] = {}
        unknown_samples: list[str] = []
        for row_number, values in enumerate(rows, 2):
            source_row_count += 1
            raw = dict(zip(headers, values))
            row = normalize_excel_row(raw)
            if not any((row["id"], row["title"], row["content"], row["url"])):
                continue
            site_matched = bool(row.pop("_platform_matched", True))
            raw_site_name = row.get("site_name")
            missing_values = [
                key for key, value in (
                    ("站点名称", raw_site_name),
                    ("标题/正文", row["title"] or row["content"]),
                    ("发表时间", row["publish_time"]),
                    ("内容类型", row["_source_content_mode"]),
                    ("分析对象", row["analysis_object"] or auto_filled_object),
                    ("URL", row["url"]),
                )
                if not value or value == "unknown"
            ]
            if missing_values:
                raise ValueError(
                    f"excel_source: 第 {row_number} 行缺少或无法解析: "
                    + "、".join(missing_values)
                )
            # FIX-12：无「分析对象」列且单一确认活动 → 用活动名填充。
            if auto_filled_object and not row["analysis_object"]:
                row["analysis_object"] = auto_filled_object
            # FIX-17：映射表外的站点归「其他」，计入声量、不单独出饼图，
            # 不再整单 raise（原版 KS_Exception_Handling D：打印样本→归其他→超10%告知）。
            if not site_matched:
                unknown_sites[str(raw_site_name or "")] = (
                    unknown_sites.get(str(raw_site_name or ""), 0) + 1
                )
                if len(unknown_samples) < 5 and str(raw_site_name or "") not in unknown_samples:
                    unknown_samples.append(str(raw_site_name or ""))
            elif expected_platforms and row["_platform"] not in expected_platforms:
                # 已映射的标准平台若不在确认范围内，仍报错（口径冲突，不能静默吞掉）。
                raise ValueError(
                    f"excel_source: 第 {row_number} 行平台 {row['_platform']} "
                    "不在确认范围"
                )
            publish_ymd = datetime.fromtimestamp(
                row["publish_time"] / 1000, tz=_CST
            ).strftime("%Y%m%d")
            # FIX-12：多活动时按该行所属活动的周期判断；跨活动期的行直接剔除
            # （方案要求「某行日期在本活动周期之外、但在另一活动周期之内时被剔除」）。
            object_key = str(row.get("analysis_object") or "").strip()
            if object_key:
                seen_campaigns.add(object_key)
            window = campaign_windows.get(object_key)
            row_start, row_end = window if window else (period_start, period_end)
            if row_start and row_end and not (row_start <= publish_ymd <= row_end):
                if window:
                    period_dropped[object_key] = period_dropped.get(object_key, 0) + 1
                    continue
                raise ValueError(
                    f"excel_source: 第 {row_number} 行发表时间 {publish_ymd} "
                    "超出确认周期"
                )
            if expected_mode and row["content_mode"] != expected_mode:
                continue
            normalized.append(row)
    finally:
        workbook.close()

    if not normalized:
        raise ValueError("excel_source: 按内容范围过滤后没有可用活动明细")
    invalid_content_modes: dict[str, int] = {}
    for row in normalized:
        source_mode = str(row.get("_source_content_mode") or "").strip()
        if source_mode.upper() not in _CONTENT_MODE:
            invalid_content_modes[source_mode] = (
                invalid_content_modes.get(source_mode, 0) + 1
            )
    invalid_content_mode_count = sum(invalid_content_modes.values())
    if (
        invalid_content_mode_count
        and invalid_content_mode_count / source_row_count >= 0.05
    ):
        raise ValueError(
            "excel_source: 内容类型出现大量非 PGC/UGC/未知取值，"
            f"占比={invalid_content_mode_count / source_row_count:.2%}，"
            f"明细={invalid_content_modes}"
        )
    source_campaigns = {
        str(row.get("analysis_object") or "").strip()
        for row in normalized
        if str(row.get("analysis_object") or "").strip()
    }
    campaign_mapping = {value: value for value in source_campaigns}
    # 核对用过滤前集合：某活动的行全部因周期被剔除时不应误报「分组不一致」。
    if expected_campaigns and seen_campaigns and seen_campaigns != expected_campaigns:
        if len(expected_campaigns) == 1 and len(source_campaigns) == 1:
            source_id = next(iter(source_campaigns))
            confirmed_id = next(iter(expected_campaigns))
            campaign_mapping = {source_id: confirmed_id}
            for row in normalized:
                row["_source_analysis_object"] = row["analysis_object"]
                row["analysis_object"] = confirmed_id
        else:
            raise ValueError(
                "excel_source: Excel 分析对象与确认活动分组不一致；"
                f"Excel={sorted(source_campaigns)}，确认={sorted(expected_campaigns)}"
            )
    return {
        "rows": normalized,
        "meta": {
            "source": "user_uploaded_excel",
            "file_name": (plan.get("source_file") or {}).get("name") or path.name,
            "path": str(path),
            "content_scope": str(plan.get("content_scope") or "all").lower(),
            "platforms": sorted({row["_platform"] for row in normalized}),
            "unknown_sites": dict(unknown_sites),
            "unknown_site_samples": list(unknown_samples),
            "unknown_site_ratio": round(
                sum(unknown_sites.values()) / len(normalized), 4
            ) if normalized else 0.0,
            "source_row_count": source_row_count,
            "accepted_row_count": len(normalized),
            "invalid_content_modes_mapped_to_unknown": invalid_content_modes,
            "campaigns": sorted({row["analysis_object"] for row in normalized}),
            "source_campaigns": sorted(source_campaigns),
            # FIX-12：无「分析对象」列时自动填充的记录（方案要求写日志）。
            "auto_filled_analysis_object": auto_filled_object,
            "period_dropped_by_campaign": dict(period_dropped),
            "campaign_windows": {
                key: {"start": value[0], "end": value[1]}
                for key, value in campaign_windows.items()
            },
            "campaign_mapping": campaign_mapping,
            "period_start": period_start,
            "period_end": period_end,
        },
    }
