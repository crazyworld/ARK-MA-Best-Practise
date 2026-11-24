"""Topic5 G1 需求问卷的纯数据契约。

本模块只依赖标准库，负责：
  - 构造飞书 schema 2.0 表单元素；
  - 把 card.action.trigger 的 form_value 归一化为 pipeline 可消费的需求快照；
  - 校验必填字段并派生 SSJH / 活动明细清洗需要的字段。

文件上传不由卡片承担。用户需在同一话题发送 .xlsx，Gateway 会把它挂载到
``/mnt/session/uploads/topic5_activity_detail.xlsx``；G1 提交时另行校验已挂载状态。
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

# 平台候选唯一事实源见 pipeline-orchestrator/platforms.py（FIX-17）。
# 卡片展示「中文名 → 中文名」；下游 ssjh 侧再转 SSJH 平台键。
def _platform_options() -> tuple[tuple[str, str], ...]:
    try:
        from pathlib import Path as _Path
        import sys as _sys
        _orch = _Path(__file__).resolve().parent / "ma-resources" / "skills" / "pipeline-orchestrator"
        if str(_orch) not in _sys.path:
            _sys.path.insert(0, str(_orch))
        import platforms as _platforms
        return tuple((name, name) for name in _platforms.ORDER)
    except Exception:
        # skill 未挂载时的兜底：与原版 KS 平台映射表一致
        return (
            ("小红书", "小红书"), ("抖音", "抖音"), ("快手", "快手"),
            ("视频号", "视频号"), ("微博", "微博"), ("微信", "微信"),
            ("知乎", "知乎"), ("B站", "B站"), ("新闻", "新闻"),
            ("论坛", "论坛"), ("电商", "电商"),
        )


PLATFORM_OPTIONS = _platform_options()
CONTENT_SCOPE_OPTIONS = (
    ("全部", "all"),
    ("仅 UGC", "ugc"),
    ("仅 PGC", "pgc"),
)
PLATFORM_LABELS = {value: label for label, value in PLATFORM_OPTIONS}
CONTENT_SCOPE_LABELS = {value: label for label, value in CONTENT_SCOPE_OPTIONS}

OPTIONAL_REQUIREMENT_DEFAULTS = {
    "business_questions": (
        "活动实际表现如何；哪些内容和参与方式有效；下次如何改进"
    ),
    "delivery_expectation": (
        "飞书营销活动复盘报告，包含可复核数据、图表和建议"
    ),
    "allowed_actions": (
        "读取本任务数据；调用已登记取数/标注/图表/飞书能力"
    ),
}

DEMO_REQUIREMENT_DEFAULTS = {
    "brand": "瑞幸",
    "activity_name": "瑞幸×暹罗厘普联名活动",
    "period_start": "2026-09-28",
    "period_end": "2026-10-07",
    "platforms": ["小红书"],
    "brand_query": "瑞幸",
    "brand_filter": "无",
    "activity_query": "瑞幸+(暹罗厘普|多栋|厘普|比格)",
    "activity_filter": "无",
    "product_query": "瑞幸+流心芝士拿铁",
    "content_scope": "all",
    **OPTIONAL_REQUIREMENT_DEFAULTS,
}

REQUIRED_FIELDS = (
    "brand",
    "activity_name",
    "period_start",
    "period_end",
    "platforms",
    "brand_query",
    "activity_query",
)

FIELD_LABELS = {
    "brand": "品牌名称",
    "activity_name": "活动名称",
    "period_start": "活动开始日期",
    "period_end": "活动结束日期",
    "platforms": "目标平台",
    "brand_query": "品牌取数关键词",
    "activity_query": "活动取数关键词",
    "product_query": "产品关键词",
    "content_scope": "内容范围",
    "business_questions": "希望回答的业务问题",
    "delivery_expectation": "交付期望",
    "allowed_actions": "允许执行的动作",
}


class IntakeValidationError(ValueError):
    """G1 表单字段不完整或不合法。"""


def validate_xlsx_container(
    data: bytes,
    *,
    max_entries: int = 2_000,
    max_uncompressed_bytes: int = 250 * 1024 * 1024,
) -> None:
    """校验 XLSX/ZIP 基本结构并拦截路径穿越、超大解压体积。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            names = {item.filename for item in infos}
            required = {"[Content_Types].xml", "xl/workbook.xml"}
            if not required.issubset(names):
                raise IntakeValidationError("文件不是有效的 .xlsx 工作簿")
            if len(infos) > max_entries:
                raise IntakeValidationError("Excel 内部文件数超过安全上限")
            total = 0
            for item in infos:
                path = item.filename.replace("\\", "/")
                if path.startswith("/") or ".." in path.split("/"):
                    raise IntakeValidationError("Excel 包含非法内部路径")
                total += int(item.file_size or 0)
                if total > max_uncompressed_bytes:
                    raise IntakeValidationError("Excel 解压后超过 250 MiB 安全上限")
    except zipfile.BadZipFile as error:
        raise IntakeValidationError("文件不是有效的 .xlsx 工作簿") from error


def _campaign_type_initial(current: Any) -> str:
    """关键词能力初判可预选；未确认（空/待数据推断/待确认）时不预选，由用户选择。"""
    text = str(current or "").strip()
    return "" if text in ("", PENDING_INFERENCE, "待确认") else text


def _campaign_type_options(current: Any) -> tuple[tuple[str, str], ...]:
    """下拉选项；能力预填的值不在示例清单里时追加到首位，避免 initial_option 失配。"""
    options: list[str] = list(CAMPAIGN_TYPE_OPTIONS)
    text = str(current or "").strip()
    if text and text not in options and text != "待确认":
        options.insert(0, text)
    return tuple((item, item) for item in options)


def _text_default(value: Any) -> str:
    """表单默认值：list 用「、」连接，period dict 转「start ~ end」，占位值不预填。"""
    if isinstance(value, dict):
        value = " ~ ".join(str(value.get(k) or "") for k in ("start", "end")).strip(" ~")
    elif isinstance(value, (list, tuple)):
        value = "、".join(str(item) for item in value if str(item).strip())
    text = str(value or "").strip()
    return "" if text == PENDING_INFERENCE else text


def _input(
    name: str,
    label: str,
    placeholder: str,
    *,
    required: bool = True,
    max_length: int = 300,
    default_value: str = "",
) -> dict:
    component = {
        "tag": "input",
        "name": name,
        "required": required,
        "input_type": "text",
        "max_length": max_length,
        "width": "fill",
        "label": {"tag": "plain_text", "content": label},
        "placeholder": {"tag": "plain_text", "content": placeholder},
    }
    if default_value:
        component["default_value"] = default_value
    return component


def _field_label(label: str) -> dict:
    return {"tag": "markdown", "content": f"**{label}**"}


def _date_picker(name: str, placeholder: str, *, initial_date: str = "") -> dict:
    component = {
        "tag": "date_picker",
        "name": name,
        "required": False,
        "width": "fill",
        "placeholder": {"tag": "plain_text", "content": placeholder},
    }
    if initial_date:
        component["initial_date"] = initial_date
    return component


def _select(
    name: str,
    label: str,
    options: tuple[tuple[str, str], ...],
    *,
    multiple: bool = False,
    required: bool = True,
    initial_option: str = "",
) -> dict:
    component = {
        "tag": "multi_select_static" if multiple else "select_static",
        "name": name,
        "required": required,
        "width": "fill",
        "placeholder": {"tag": "plain_text", "content": f"请选择{label}"},
        "options": [
            {"text": {"tag": "plain_text", "content": text}, "value": value}
            for text, value in options
        ],
    }
    if initial_option and not multiple:
        component["initial_option"] = initial_option
    return component


def build_requirement_form(
    event_id: int,
    action_value: Callable[[str], dict],
    defaults: Optional[dict[str, Any]] = None,
) -> dict:
    """构造 G1 需求问卷；提交按钮通过 action_value 注入幂等回调标识。"""
    values = dict(defaults or {})
    platform_defaults = values.get("platforms") or []
    if isinstance(platform_defaults, str):
        platform_defaults = [platform_defaults]
    return {
        "tag": "form",
        "name": f"requirement_form_{event_id}",
        "elements": [
            _input(
                "brand", "品牌名称*", "例如：瑞幸",
                required=False,
                default_value=str(values.get("brand") or ""),
            ),
            _input(
                "activity_name", "活动名称*", "例如：瑞幸×暹罗厘普联名活动",
                required=False,
                default_value=str(values.get("activity_name") or ""),
            ),
            _field_label("活动开始日期*"),
            _date_picker(
                "period_start", "请选择活动开始日期",
                initial_date=str(values.get("period_start") or ""),
            ),
            _field_label("活动结束日期*"),
            _date_picker(
                "period_end", "请选择活动结束日期",
                initial_date=str(values.get("period_end") or ""),
            ),
            _field_label("目标平台*"),
            _select(
                "platforms",
                "目标平台",
                PLATFORM_OPTIONS,
                multiple=not bool(platform_defaults),
                required=False,
                initial_option=str((platform_defaults or [""])[0]),
            ),
            _input(
                "brand_query", "品牌取数关键词*", "例如：瑞幸",
                required=False,
                default_value=str(values.get("brand_query") or ""),
            ),
            _input(
                "brand_filter",
                "品牌取数过滤词（可选）",
                "无过滤词可留空",
                required=False,
                default_value=str(values.get("brand_filter") or ""),
            ),
            _input(
                "activity_query",
                "活动取数关键词*",
                "例如：瑞幸+(暹罗厘普|多栋|厘普|比格)",
                max_length=500,
                required=False,
                default_value=str(values.get("activity_query") or ""),
            ),
            _input(
                "activity_filter",
                "活动取数过滤词（可选）",
                "无过滤词可留空",
                required=False,
                default_value=str(values.get("activity_filter") or ""),
            ),
            _input(
                "product_query", "产品关键词（可选）", "不做产品维度时可留空",
                required=False,
                default_value=str(values.get("product_query") or ""),
            ),
            _input(
                "business_questions",
                "希望回答的业务问题（可选）",
                "留空则默认分析活动表现、有效内容和后续改进",
                max_length=800,
                required=False,
                default_value=str(values.get("business_questions") or ""),
            ),
            _input(
                "delivery_expectation",
                "交付期望（可选）",
                "留空则默认交付含可复核数据、图表和建议的飞书复盘报告",
                max_length=500,
                required=False,
                default_value=str(values.get("delivery_expectation") or ""),
            ),
            _input(
                "allowed_actions",
                "允许执行的动作（可选）",
                "留空则按系统已登记的数据、标注、图表和飞书能力执行",
                max_length=500,
                required=False,
                default_value=str(values.get("allowed_actions") or ""),
            ),
            _field_label("内容范围（可选，默认全部）"),
            _select(
                "content_scope",
                "内容范围（可选，默认全部）",
                CONTENT_SCOPE_OPTIONS,
                required=False,
                initial_option=str(values.get("content_scope") or "all"),
            ),
            # Q1：活动主类型由用户或关键词生成能力确认，不再由数据推断。
            _field_label("活动类型（可参考关键词能力初判；未确认时记为待确认，依赖类型的模块暂不执行）"),
            _select(
                "campaign_type",
                "活动类型",
                _campaign_type_options(values.get("campaign_type")),
                required=False,
                initial_option=_campaign_type_initial(values.get("campaign_type")) or PENDING_INFERENCE,
            ),
            _input(
                "product_category", "产品类别（可选）", "例如：咖啡茶饮；留空由数据推断",
                required=False,
                default_value=_text_default(values.get("product_category")),
            ),
            _input(
                "campaign_goal", "活动核心目标（可选，最多 2 个）",
                "例如：品牌声量提升、产品种草；留空由数据推断",
                required=False,
                default_value=_text_default(values.get("campaign_goal")),
            ),
            _input(
                "activity_boundary", "活动边界（可选）", "本次活动包含的传播内容",
                max_length=500,
                required=False,
                default_value=_text_default(values.get("activity_boundary")),
            ),
            _input(
                "actual_event_period", "实际举办期（可选）",
                "例如：2026-09-30 ~ 2026-10-05；不覆盖统计窗口",
                required=False,
                default_value=_text_default(values.get("actual_event_period")),
            ),
            _input(
                "baseline_period", "品牌基准期（可选）",
                "例如：2026-08-29 ~ 2026-09-27；留空默认取活动开始日前30天（多活动以合并窗口开始日为参照）",
                required=False,
                default_value=_text_default(values.get("baseline_period")),
            ),
            _input(
                "brand_supplement_activity_terms", "补入品牌关键词的活动关键词（可选）",
                "未补入填「无」",
                required=False,
                default_value=_text_default(
                    values.get("brand_supplement_activity_terms")),
            ),
            {
                "tag": "button",
                "name": "requirement_submit",
                "text": {"tag": "plain_text", "content": "确认需求并进入方案"},
                "type": "primary_filled",
                "width": "fill",
                "action_type": "form_submit",
                "value": action_value("pass"),
            },
        ],
    }


def _unwrap(value: Any) -> Any:
    """兼容飞书表单值的字符串、数组及 {value/key/id} 包装。"""
    if isinstance(value, dict):
        for key in ("value", "key", "id"):
            if key in value:
                return _unwrap(value[key])
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    return value


def _clean_text(value: Any) -> str:
    value = _unwrap(value)
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return "" if text.lower() in {"无", "none", "null", "n/a"} else text


def _clean_list(value: Any) -> list[str]:
    value = _unwrap(value)
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,，、\s]+", str(value or ""))
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def _clean_goals(value: Any) -> list[str]:
    """campaign_goal：最多 2 个（Phase1 v1.10:181）；未填记「待数据推断」。"""
    goals = [item for item in _clean_list_goal(value) if item != PENDING_INFERENCE]
    if len(goals) > MAX_CAMPAIGN_GOALS:
        raise IntakeValidationError(
            f"活动核心目标最多 {MAX_CAMPAIGN_GOALS} 个，当前 {len(goals)} 个："
            + "、".join(goals))
    return goals or [PENDING_INFERENCE]


def _clean_list_goal(value: Any) -> list[str]:
    # 目标文本里常有空格（「品牌声量提升」），不能像平台那样按空白拆。
    value = _unwrap(value)
    items = value if isinstance(value, list) else re.split(r"[,，、/／;；]+", str(value or ""))
    return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def _clean_period(value: Any) -> dict:
    """实际举办期：{start, end}，可只有一端；搜索发现的举办期不覆盖用户统计窗口。"""
    value = _unwrap(value)
    if isinstance(value, dict):
        start = _clean_text(value.get("start"))[:10]
        end = _clean_text(value.get("end"))[:10]
    elif isinstance(value, str) and value.strip():
        parts = [p.strip() for p in re.split(r"\s*(?:~|～|至|到)\s*", value.strip())]
        start = parts[0][:10]
        end = (parts[1] if len(parts) > 1 else "")[:10]
    else:
        return {}
    return {key: val for key, val in (("start", start), ("end", end)) if val}


def _load_traceability():
    """留痕清洗函数的唯一事实源在 pipeline-orchestrator/traceability.py（沙箱只挂载 skill）。"""
    from pathlib import Path as _Path
    import sys as _sys
    _orch = _Path(__file__).resolve().parent / "ma-resources" / "skills" / "pipeline-orchestrator"
    if str(_orch) not in _sys.path:
        _sys.path.insert(0, str(_orch))
    import traceability as _traceability
    return _traceability


_traceability = _load_traceability()
_clean_corrections = _traceability.clean_corrections
_clean_field_sources = _traceability.clean_field_sources
_clean_skill_invocations = _traceability.clean_skill_invocations


def query_terms(expression: str) -> list[str]:
    """把 ``瑞幸+(暹罗厘普|多栋)`` 拆成字面词，**仅用于提及判定**。

    警告（FIX-01）：拆分结果不能拿去逐个查询。SSJH 侧必须提交完整表达式一次，
    否则 ``+`` 的“且”语义丢失、重叠帖子被重复计数。
    """
    parts = re.split(r"[+|｜(),（）\s]+", expression or "")
    return list(dict.fromkeys(part.strip() for part in parts if part.strip()))


# ── 检索表达式预检（FIX-08）─────────────────────────────────────────
class QueryExpressionError(IntakeValidationError):
    """检索表达式语法或规模不合法。"""


MAX_QUERY_GROUPS = 200
# FIX-12 多活动：G1 活动列表最多 5 行；相邻活动间隔超 30 天给拆分建议。
MAX_CAMPAIGNS = 5
SPLIT_GAP_DAYS = 30
# FIX-10：业务属性字段未填时的占位（原版需求确认单用语）；campaign_goal 最多 2 个。
PENDING_INFERENCE = "待数据推断"
MAX_CAMPAIGN_GOALS = 2
# KS_Report_Required_Fields_List.md:53 主类型示例（G1 下拉选项，可自由填写其他值）。
# Q1：9 类为关键词 Skill 的参考主类型，不是封闭枚举（表外/复合选「其他」并写明）；
# 「待数据推断」保留为「尚未确认」的占位：不再由数据推断填定（KS_Classification_Rules v0.9）。
CAMPAIGN_TYPE_OPTIONS = (
    "IP联名", "品牌跨界联名", "明星代言或合作", "话题挑战或互动",
    "新品发布或产品推广", "促销或节日营销", "线下活动或快闪",
    "公益或品牌主题", "赛事赞助或合作", "其他", PENDING_INFERENCE,
)


def parse_expression(expression: str) -> list[list[str]]:
    """解析 SSJH 检索表达式，返回展开后的布尔组列表（DNF）。

    语法：``+`` 表示“且”，``|`` 表示“或”，``(`` ``)`` 分组。
    规则（客户契约）：
      - 括号必须配对；
      - 同一层不得混用 ``+`` 与 ``|``（混用必须加括号）；
      - 不允许空项；
      - 展开组数 ≤ 200。

    返回形如 ``[['瑞幸', '暹罗厘普'], ['瑞幸', '多栋']]``：每个子列表是一组 AND 词。
    """
    text = (expression or "").strip()
    if not text:
        return []
    tokens = _tokenize(text)
    state = {"pos": 0}

    def peek() -> Optional[str]:
        return tokens[state["pos"]] if state["pos"] < len(tokens) else None

    def parse_operand() -> list[list[str]]:
        """一个操作数：字面词，或一对括号包住的子表达式。"""
        token = peek()
        if token is None:
            raise QueryExpressionError(f"表达式不完整：{expression}")
        if token == "(":
            state["pos"] += 1
            groups = parse_sequence(in_paren=True)
            if peek() != ")":
                raise QueryExpressionError(f"括号不配对：{expression}")
            state["pos"] += 1
            return groups
        if token in ("+", "|", ")"):
            raise QueryExpressionError(f"表达式缺少检索词：{expression}")
        state["pos"] += 1
        word = token.strip()
        if not word:
            raise QueryExpressionError(f"表达式含空检索词：{expression}")
        return [[word]]

    def parse_sequence(in_paren: bool) -> list[list[str]]:
        """同级解析：全 ``|`` 取并集，全 ``+`` 取笛卡尔积，混用报错。"""
        groups = parse_operand()
        operator: Optional[str] = None
        while True:
            token = peek()
            if token is None:
                if in_paren:
                    raise QueryExpressionError(f"括号不配对：{expression}")
                break
            if token == ")":
                if not in_paren:
                    raise QueryExpressionError(f"括号不配对：{expression}")
                break
            if token not in ("+", "|"):
                raise QueryExpressionError(f"表达式无法解析：{expression}")
            if operator and token != operator:
                raise QueryExpressionError(
                    f"同层混用“+”与“|”必须加括号：{expression}")
            operator = token
            state["pos"] += 1
            rhs = parse_operand()
            groups = _union(groups, rhs) if token == "|" else _cross(groups, rhs)
            if len(groups) > MAX_QUERY_GROUPS:
                raise QueryExpressionError(
                    f"表达式展开组数超过上限 {MAX_QUERY_GROUPS}：{expression}")
        return groups

    result = parse_sequence(in_paren=False)
    if state["pos"] != len(tokens):
        raise QueryExpressionError(f"表达式末尾有多余内容：{expression}")
    return result


def _tokenize(text: str) -> list[str]:
    """切分为 运算符 / 括号 / 字面词；全角符号归一化为半角。"""
    tokens = []
    for token in re.findall(r"[+|｜()（）]|[^+|｜()（）]+", text):
        if token in ("｜",):
            tokens.append("|")
        elif token in ("（",):
            tokens.append("(")
        elif token in ("）",):
            tokens.append(")")
        else:
            tokens.append(token)
    return tokens


def _union(left: list[list[str]], right: list[list[str]]) -> list[list[str]]:
    out = [list(g) for g in left]
    for group in right:
        if list(group) not in out:
            out.append(list(group))
    return out


def _cross(left: list[list[str]], right: list[list[str]]) -> list[list[str]]:
    out = []
    for a in left:
        for b in right:
            merged = list(a) + [w for w in b if w not in a]
            out.append(merged)
    return out


def validate_query_expression(expression: str, label: str) -> list[list[str]]:
    """预检并返回展开组；非法时抛 QueryExpressionError（带字段名与位置提示）。"""
    try:
        return parse_expression(expression)
    except QueryExpressionError as error:
        raise QueryExpressionError(f"{label}：{error}") from None


def query_coverage_warnings(brand_query: str, activity_query: str) -> list[str]:
    """校验品牌口径是否覆盖活动口径（FIX-08）。

    活动帖必须能命中品牌式，否则“活动声量 ÷ 品牌声量”可能 >100%。
    返回告警文案（不阻断；由 G1 摘要提示用户，声量贡献参考据此不输出）。
    """
    if not (brand_query and activity_query):
        return []
    try:
        brand_groups = parse_expression(brand_query)
        activity_groups = parse_expression(activity_query)
    except QueryExpressionError:
        return []
    warnings = []
    for group in activity_groups:
        covered = any(set(b) <= set(group) for b in brand_groups)
        if not covered:
            warnings.append(
                "活动检索式分支 " + " + ".join(group) + " 未被品牌检索式覆盖："
                "该分支的帖子可能不计入品牌声量，声量贡献参考将不输出"
            )
    return warnings


def normalize_requirement(form_value: dict[str, Any]) -> dict:
    """归一化 G1 表单，并派生后续节点直接消费的计划字段。"""
    raw = dict(form_value or {})
    normalized = {
        "brand": _clean_text(raw.get("brand")),
        "activity_name": _clean_text(raw.get("activity_name")),
        "period_start": _clean_text(raw.get("period_start"))[:10],
        "period_end": _clean_text(raw.get("period_end"))[:10],
        "platforms": _clean_list(raw.get("platforms")),
        "brand_query": _clean_text(raw.get("brand_query")),
        "brand_filter": _clean_text(raw.get("brand_filter")),
        "activity_query": _clean_text(raw.get("activity_query")),
        "activity_filter": _clean_text(raw.get("activity_filter")),
        "product_query": _clean_text(raw.get("product_query")),
        "content_scope": _clean_text(raw.get("content_scope")).lower() or "all",
        "business_questions": (
            _clean_text(raw.get("business_questions"))
            or OPTIONAL_REQUIREMENT_DEFAULTS["business_questions"]
        ),
        "delivery_expectation": (
            _clean_text(raw.get("delivery_expectation"))
            or OPTIONAL_REQUIREMENT_DEFAULTS["delivery_expectation"]
        ),
        "allowed_actions": (
            _clean_text(raw.get("allowed_actions"))
            or OPTIONAL_REQUIREMENT_DEFAULTS["allowed_actions"]
        ),
        "budget_constraint": _clean_text(raw.get("budget_constraint")),
        # FIX-09：需求留痕字段（附加字段，旧 handler 会忽略）。
        "raw_request": _clean_text(raw.get("raw_request")),
        "corrections": _clean_corrections(raw.get("corrections")),
        "field_sources": _clean_field_sources(raw.get("field_sources")),
        "skill_invocations": _clean_skill_invocations(raw.get("skill_invocations")),
        "delivery_deadline": _clean_text(raw.get("delivery_deadline")),
        "special_notes": _clean_text(raw.get("special_notes")),
        # FIX-10：业务属性类可选字段（KS_Report_Required_Fields_List.md:45-47）。
        # 未填时记「待数据推断」，不冒充用户事实。
        "product_category": _clean_text(raw.get("product_category")) or PENDING_INFERENCE,
        "campaign_type": _clean_text(raw.get("campaign_type")) or PENDING_INFERENCE,
        "campaign_type_secondary": _clean_list(raw.get("campaign_type_secondary")),
        "campaign_type_state": _clean_text(raw.get("campaign_type_state")),
        "campaign_goal": _clean_goals(raw.get("campaign_goal")),
        "activity_boundary": _clean_text(raw.get("activity_boundary")),
        "actual_event_period": _clean_period(raw.get("actual_event_period")),
        # Phase1 v1.10:129：未补入填「无」。
        "brand_supplement_activity_terms": (
            _clean_text(raw.get("brand_supplement_activity_terms")) or "无"),
    }
    missing = [FIELD_LABELS[key] for key in REQUIRED_FIELDS if not normalized.get(key)]
    if missing:
        raise IntakeValidationError("缺少必填字段：" + "、".join(missing))
    if normalized["period_start"] > normalized["period_end"]:
        raise IntakeValidationError("活动开始日期不能晚于结束日期")
    if normalized["content_scope"] not in {"all", "ugc", "pgc"}:
        raise IntakeValidationError("内容范围必须是全部、仅 UGC 或仅 PGC")

    brand_words = query_terms(normalized["brand_query"]) or [normalized["brand"]]
    activity_terms = [
        term for term in query_terms(normalized["activity_query"])
        if term not in set(brand_words)
    ]
    product_words = [
        term for term in query_terms(normalized["product_query"])
        if term not in set(brand_words)
    ]
    # 检索表达式预检（FIX-08）：语法/括号/同层混用/组数上限。
    for field, label in (("brand_query", "品牌检索式"),
                         ("activity_query", "活动检索式"),
                         ("product_query", "产品检索式")):
        if normalized.get(field):
            validate_query_expression(normalized[field], label)
    coverage_warnings = query_coverage_warnings(
        normalized["brand_query"], normalized["activity_query"])
    # FIX-12：多活动。G1「活动列表」每行 = 活动名|开始|结束|活动表达式|活动过滤词（最多 5 行）。
    # 未填多行时退化为单活动，行为与原来完全一致。
    campaigns, merge_start, merge_end, split_suggestion = _build_campaigns(
        raw, normalized, activity_terms)
    normalized.update(
        {
            "brand_words": brand_words,
            "product_words": product_words,
            # FIX-01：SSJH 只提交完整表达式一次。keywords 不再是拆词结果，
            # 否则“+”的且语义丢失、重叠帖子会被重复计数。
            "keywords": [normalized["brand_query"]] if normalized["brand_query"] else [],
            "brand_query_terms": brand_words,
            "coverage_warnings": coverage_warnings,
            "campaigns": campaigns,
            # FIX-12：合并窗口 = 最早开始 ~ 最晚结束。
            "period_start": merge_start or normalized["period_start"],
            "period_end": merge_end or normalized["period_end"],
            "merge_window": {"start": merge_start, "end": merge_end},
            "split_suggestion": split_suggestion,
            # 缺陷清单 Q3：基准期起止写入需求确认单；后续取数/计算/图表统一读取，不再推算。
            **resolve_baseline(raw.get("baseline_period"), merge_start or normalized["period_start"],
                               merge_end or normalized["period_end"]),
            "business_questions": [
                item.strip()
                for item in re.split(
                    r"[；;。\\n]+", normalized["business_questions"]
                )
                if item.strip()
            ],
        }
    )
    return normalized


def _build_campaigns(raw: dict, normalized: dict, activity_terms: list) -> tuple:
    """解析多活动列表，返回 (campaigns, merge_start, merge_end, split_suggestion)。

    FIX-12 / 原版 Phase1 多活动规则：
      - 每行 `活动名；开始；结束；活动表达式；活动过滤词`，最多 5 行（分号分隔）。
      - 第一章品牌数据取合并窗口（最早开始 ~ 最晚结束）；基准期见 resolve_baseline（Q3）。
      - 相邻活动间隔 >30 天给出拆分建议，但不阻断（客户要求提示而非拒绝）。
      - 未填多行 → 单活动，与原有行为完全一致。
    """
    rows = _unwrap(raw.get("campaign_lines") or raw.get("campaigns_input") or "")
    if isinstance(rows, str):
        rows = [line for line in rows.splitlines() if line.strip()]
    if not isinstance(rows, list):
        rows = []
    parsed = []
    for line in rows[:MAX_CAMPAIGNS]:
        parts = [""] * 5
        if isinstance(line, dict):
            name = _clean_text(line.get("name") or line.get("activity_name"))
            start = _clean_text(line.get("period_start"))[:10]
            end = _clean_text(line.get("period_end"))[:10]
            query = _clean_text(line.get("activity_query"))
            fw = line.get("filter_words")
            exclude = _clean_text(line.get("activity_filter"))
        else:
            # 字段分隔用分号（；/;）。检索式本身用 | 表示「或」，不能再拿 | 当字段分隔符，
            # 否则 `瑞幸+(A|B)` 会被拆碎（FIX_PLAN 原定的 | 分隔与 FIX-08 语法冲突）。
            parts = [item.strip() for item in re.split(r"[；;]", str(line))]
            parts += [""] * (5 - len(parts))
            name, start, end, query = parts[0], parts[1][:10], parts[2][:10], parts[3]
            fw = None
            exclude = _clean_text(parts[4])
        if not name:
            continue
        if start and end and start > end:
            raise IntakeValidationError(
                f"活动列表「{name}」开始日期晚于结束日期（{start} > {end}）")
        words = ([str(w).strip() for w in fw.split("|") if str(w).strip()]
                 if isinstance(fw, str) else [str(w).strip() for w in (fw or [])])
        if not words and isinstance(line, str) and parts[4]:
            words = [w.strip() for w in re.split(r"[|,，、]", parts[4]) if w.strip()]
        parsed.append({
            "id": name,
            "name": name,
            "period_start": start or normalized.get("period_start"),
            "period_end": end or normalized.get("period_end"),
            "activity_query": query or normalized.get("activity_query"),
            "filter_words": words or list(activity_terms),
            # 剔除词与活动匹配词分开存；空 = 用需求级 activity_filter，两者都空则不核对。
            "activity_filter": exclude,
        })
    if not parsed:
        return ([{
            "id": normalized["activity_name"],
            "name": normalized["activity_name"],
            "period_start": normalized.get("period_start"),
            "period_end": normalized.get("period_end"),
            "activity_query": normalized.get("activity_query"),
            "filter_words": list(activity_terms),
        }], None, None, None)

    starts = [c["period_start"] for c in parsed if c.get("period_start")]
    ends = [c["period_end"] for c in parsed if c.get("period_end")]
    # 多活动时各活动的活动表达式都要过一遍预检（FIX-08）。
    for camp in parsed:
        if camp.get("activity_query"):
            validate_query_expression(
                camp["activity_query"], f"活动「{camp['name']}」检索式")
    return (parsed,
            min(starts) if starts else None,
            max(ends) if ends else None,
            _split_suggestion(parsed))


BASELINE_DEFAULT_DAYS = 30


def resolve_baseline(user_value: Any, window_start: str, window_end: str) -> dict:
    """品牌基准期（缺陷清单 Q3）：用户已确认日期优先；未指定时取参照开始日前30天
    （单活动=活动开始日，多活动=合并窗口开始日），即开始日−30 至 −1 天，与活动期不重叠。"""
    period = _clean_period(user_value)
    if period.get("start") or period.get("end"):
        start, end = period.get("start", ""), period.get("end", "")
        if not (start and end):
            raise IntakeValidationError("品牌基准期需同时填写开始和结束日期")
        try:
            d0, d1 = datetime.strptime(start, "%Y-%m-%d"), datetime.strptime(end, "%Y-%m-%d")
        except ValueError as error:
            raise IntakeValidationError("品牌基准期日期格式须为 YYYY-MM-DD") from error
        if d0 > d1:
            raise IntakeValidationError("品牌基准期开始日期不能晚于结束日期")
        if window_start and end >= window_start[:10] and start <= (window_end or window_start)[:10]:
            raise IntakeValidationError("品牌基准期不能与活动统计窗口重叠")
        return {"baseline_start": start, "baseline_end": end, "baseline_source": "user"}
    if not window_start:
        return {}
    anchor = datetime.strptime(window_start[:10], "%Y-%m-%d")
    return {"baseline_start": (anchor - timedelta(days=BASELINE_DEFAULT_DAYS)).strftime("%Y-%m-%d"),
            "baseline_end": (anchor - timedelta(days=1)).strftime("%Y-%m-%d"),
            "baseline_source": "default"}


def _split_suggestion(campaigns: list) -> str | None:
    """相邻活动间隔超过 30 天时建议拆分（客户要求提示，不阻断）。"""
    if len(campaigns) < 2:
        return None
    ordered = sorted(
        (c for c in campaigns if c.get("period_start") and c.get("period_end")),
        key=lambda c: c["period_start"])
    for prev, nxt in zip(ordered, ordered[1:]):
        gap = _days_between(prev["period_end"], nxt["period_start"])
        if gap is not None and gap > SPLIT_GAP_DAYS:
            return (f"「{prev['name']}」与「{nxt['name']}」间隔 {gap} 天"
                    f"（超过 {SPLIT_GAP_DAYS} 天），建议拆分为两次分析；"
                    "本次仍按合并报告执行。")
    return None


def _days_between(start: str, end: str) -> int | None:
    try:
        d1 = datetime.strptime(start[:10], "%Y-%m-%d")
        d2 = datetime.strptime(end[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return (d2 - d1).days


def resolved_request_to_g1_defaults(resolved: dict) -> dict:
    """把 campaign-keyword-generation Skill 的 resolved_request 映射成 G1 表单默认值（FIX-07）。

    原版 Skill 输出 schema 见 references/contract.md：brand / campaign_name /
    campaign_period{start,end} / platforms / brand_keyword / brand_filter /
    activity_keywords / activity_filter / product_keyword / content_scope 等。
    用户可改可不改，提交后仍走 normalize_requirement 与 FIX-08 预检。
    """
    resolved = resolved or {}
    if not resolved:
        return {}
    period = resolved.get("campaign_period") or {}
    defaults = {
        "brand": resolved.get("brand") or "",
        "activity_name": resolved.get("campaign_name") or "",
        "period_start": str(period.get("start") or "")[:10] or "",
        "period_end": str(period.get("end") or "")[:10] or "",
        "platforms": list(resolved.get("platforms") or []),
        "brand_query": resolved.get("brand_keyword") or "",
        "activity_query": resolved.get("activity_keywords") or "",
    }
    # 过滤词与内容范围只在 Skill 明确给出时预填，不凭空补「无」（contract.md：
    # 没有依据的字段留空，不伪造证据）。
    for field, source in (("brand_filter", "brand_filter"),
                          ("activity_filter", "activity_filter"),
                          ("content_scope", "content_scope")):
        value = resolved.get(source)
        if isinstance(value, str) and value.strip():
            defaults[field] = (value.strip().lower()
                               if field == "content_scope" else value.strip())
    product = resolved.get("product_keyword")
    if product:
        defaults["product_query"] = product
    campaign_type = resolved.get("campaign_type")
    if isinstance(campaign_type, dict):
        # contract.md: campaign_type = {primary, secondary, basis, state}
        primary = str(campaign_type.get("primary") or "").strip()
        if primary:
            defaults["campaign_type"] = primary
        state = str(campaign_type.get("state") or "").strip()
        if state:
            defaults["campaign_type_state"] = state
        secondary = campaign_type.get("secondary") or []
        if secondary:
            defaults["campaign_type_secondary"] = list(secondary)
    elif campaign_type:
        defaults["campaign_type"] = campaign_type
    boundary = resolved.get("activity_boundary")
    if isinstance(boundary, dict):
        included = boundary.get("included")
        if included:
            defaults["activity_boundary"] = (
                included if isinstance(included, str)
                else "；".join(str(item) for item in included))
        actual = boundary.get("actual_event_period")
        if actual:
            defaults["actual_event_period"] = actual
    campaign_goal = resolved.get("campaign_goal") or resolved.get("goal")
    if campaign_goal:
        defaults["campaign_goal"] = campaign_goal
    return {key: value for key, value in defaults.items() if value not in ("", [])}


def summarize_requirement(requirement: dict) -> str:
    """卡片终态/日志使用的紧凑摘要，避免直接展示整段 JSON。"""
    scope = {"all": "全部", "ugc": "仅 UGC", "pgc": "仅 PGC"}.get(
        str(requirement.get("content_scope") or ""), "未指定"
    )
    platforms = "、".join(requirement.get("platforms") or [])
    return (
        f"{requirement.get('brand', '')} · {requirement.get('activity_name', '')} · "
        f"{requirement.get('period_start', '')}～{requirement.get('period_end', '')} · "
        f"{platforms} · {scope}"
    )


def _escape_markdown(value: Any) -> str:
    text = str(value or "").strip()
    for char in ("\\", "`", "*", "_", "[", "]", "<", ">"):
        text = text.replace(char, f"\\{char}")
    return text


def requirement_parsed_summary(requirement: dict) -> str:
    """G1 卡片顶部「业务解析」摘要（FIX-09）。

    原版 HC-T5-0 口径（Phase1_需求梳理 v1.10:157-161）：
      - 展示内容固定为：品牌/活动、各活动周期及合并窗口、平台、内容范围、
        分析重点、活动类型与边界。
      - **关键词与过滤词不向用户展示**，也不在对话中提及具体表达式（只写入确认单）。
      - 标出字段来源（原话 / 默认 / 推断），不把建议和默认值冒充用户原话。
      - 缺失或冲突字段标红要求补齐（这是补齐输入，不是确认环节）。

    返回飞书 markdown 片段；没有可展示内容时返回空串。
    """
    requirement = requirement or {}
    if not any(k in requirement for k in
               ("brand", "activity_name", "period_start", "platforms", "campaigns")):
        return ""
    sources = requirement.get("field_sources") or {}
    if not isinstance(sources, dict):
        sources = {}
    missing = [str(item) for item in (requirement.get("missing_fields") or [])]
    conflicts = requirement.get("conflicts") or []

    def tag(field: str) -> str:
        source = sources.get(field) or ""
        if source in ("原话", "utterance"):
            return "（原话）"
        if source in ("默认", "default"):
            return "（默认）"
        if source in ("推断", "inferred"):
            return "（推断）"
        if source in ("用户更正", "correction"):
            return "（已更正）"
        return ""

    def flag(field: str, label: str, value: str) -> str:
        """缺失/冲突字段标红；其余正常展示。"""
        if field in missing or any(
                str((c or {}).get("field") or "") == field if isinstance(c, dict)
                else str(c) == field for c in conflicts):
            return f'<font color="red">{label}：{value}（请补充）</font>'
        return f"{label}：{value}{tag(field)}"

    period = (f"{requirement.get('period_start') or '未填写'} ～ "
              f"{requirement.get('period_end') or '未填写'}")
    platforms = "、".join(
        PLATFORM_LABELS.get(str(v), str(v)) for v in requirement.get("platforms") or [])
    lines = []
    if requirement.get("brand") or requirement.get("activity_name"):
        lines.append(flag("brand", "品牌", requirement.get("brand") or "未填写"))
        lines.append(flag("activity_name", "活动",
                          requirement.get("activity_name") or "未填写"))
    lines.append(flag("period_start", "统计窗口", period))
    if platforms:
        lines.append(flag("platforms", "平台", platforms))
    if requirement.get("campaign_type"):
        lines.append(flag("campaign_type", "活动类型",
                          str(requirement["campaign_type"])))
    boundary = requirement.get("activity_boundary")
    if boundary:
        lines.append(flag("activity_boundary", "活动边界", str(boundary)))
    goals = requirement.get("campaign_goal") or []
    if isinstance(goals, str):
        goals = [goals]
    if goals:
        lines.append(flag("campaign_goal", "活动目标",
                          "、".join(str(g) for g in goals)))
    scope = CONTENT_SCOPE_LABELS.get(
        str(requirement.get("content_scope") or "all"), "全部")
    lines.append(f"内容范围：{scope}")
    questions = requirement.get("business_questions") or []
    if isinstance(questions, str):
        questions = [questions]
    if questions:
        lines.append("分析重点：" + "；".join(str(q) for q in questions))
    # 原版明确：关键词与过滤词不向用户展示，也不在对话里提及表达式。
    merged = requirement.get("merge_window") or {}
    if merged.get("start") and len(requirement.get("campaigns") or []) > 1:
        lines.insert(1, f"合并窗口：{merged['start']} ～ {merged['end']}")
    if not lines:
        return ""
    head = "**业务解析**（无需逐项确认，提交即通过；标红项请补充）"
    return head + "\n" + "\n".join(lines[:8])


def requirement_snapshot_rows(requirement: dict) -> list[tuple[str, str]]:
    """把冻结的 G1 requirement_confirmed 转成用户可读字段，不暴露内部 ID/路径。"""
    source_file = requirement.get("source_file") or {}
    platforms = "、".join(
        PLATFORM_LABELS.get(str(value), str(value))
        for value in requirement.get("platforms") or []
    )
    scope = CONTENT_SCOPE_LABELS.get(
        str(requirement.get("content_scope") or "all"),
        str(requirement.get("content_scope") or "all"),
    )
    questions = requirement.get("business_questions") or []
    if isinstance(questions, str):
        questions = [questions]
    return [
        ("活动明细文件", f"{source_file.get('name') or '未记录'}（已挂载）"),
        ("品牌", requirement.get("brand") or "未填写"),
        ("活动", requirement.get("activity_name") or "未填写"),
        (
            "活动周期",
            f"{requirement.get('period_start') or '未填写'} ～ "
            f"{requirement.get('period_end') or '未填写'}",
        ),
        ("目标平台", platforms or "未填写"),
        # Q3：基准期实际起止（默认值也写明来源）。
        ("品牌基准期", _baseline_text(requirement)),
        ("活动类型", requirement.get("campaign_type") or "待数据推断"),
        ("品牌取数关键词", requirement.get("brand_query") or "未填写"),
        ("品牌过滤词", requirement.get("brand_filter") or "无"),
        ("活动取数关键词", requirement.get("activity_query") or "未填写"),
        ("活动过滤词", requirement.get("activity_filter") or "无"),
        ("产品关键词", requirement.get("product_query") or "未填写"),
        ("内容范围", scope),
        ("业务问题", "；".join(str(item) for item in questions) or "未填写"),
        ("交付期望", requirement.get("delivery_expectation") or "未填写"),
        ("允许动作", requirement.get("allowed_actions") or "未填写"),
        # FIX-09：需求留痕（原话 / 交付截止 / 特殊说明），空值不展示占位。
        ("原话", requirement.get("raw_request") or "未记录"),
        ("交付截止", requirement.get("delivery_deadline") or "未填写"),
        ("特殊说明", requirement.get("special_notes") or "无"),
    ]


def _baseline_text(requirement: dict) -> str:
    start, end = requirement.get("baseline_start"), requirement.get("baseline_end")
    if not (start and end):
        return "未记录"
    suffix = "（默认前30天，可另行指定并确认）" if requirement.get("baseline_source") == "default" else ""
    return f"{start} ～ {end}{suffix}"


def format_requirement_snapshot(requirement: dict, *, heading: bool = True) -> str:
    """渲染飞书 Markdown；终态卡与运行进度卡复用同一份展示口径。"""
    lines = [
        f"- **{label}**：{_escape_markdown(value)}"
        for label, value in requirement_snapshot_rows(requirement)
    ]
    if heading:
        return "**G1 需求快照**\n" + "\n".join(lines)
    return "\n".join(lines)
