"""HC（Human-in-the-loop）桥接层 — Gateway ↔ 编排引擎 的人工关卡契约。

薄 Agent + Durable Workflow 下，人工关卡的闭环是：
  引擎 HumanPause → 写 hc_pending.json（薄 Agent 据此输出 HC 卡片 JSON 结束 turn）
  → Gateway 落 human_tasks 发卡 → 用户点卡/回消息（HCx [pass/reject/remark]: note）
  → Gateway resume_job 注入结构化决定 → 引擎 resume 续跑。

本模块把「暂停节点 + 用户决定」翻译成引擎 resume 所需的 human_outputs，不改 DAG schema。
决定格式（project_memory user_confirmed）：严格 `HCx [pass/reject/remark]: [note]`。
remark 无内容只记录、不触发续跑（等待后续消息）；remark 带 note 视为追加说明后续跑。
"""
from __future__ import annotations

import json
import time
import re
from datetime import date
from pathlib import Path
from typing import Any, Optional

import feishu
from excel_source import normalize_source_file
import impact
import label_review
import traceability

# 人工关卡 node_id → HC 种类（供卡片展示 / resume --hc 定位）。
NODE_TO_HC = {
    "n01_hc_requirement": "G1",
    "n03_hc_plan": "G2",
    "n10_hc_opinion": "N10",
    "n17_validate": "G3",
    "n_hc7_escalate": "HC7",
    "n21_hc_user": "G4",
    "n_user_escalate": "USER_ESCALATE",
    "n03b_activity_pull": "HC_COLLECT",
}
HC_TO_NODE = {v: k for k, v in NODE_TO_HC.items()}

# 决定关键词 → 规范化。
_DECISION_RE = re.compile(
    r"^\s*(?:HC\w+|G\d)?\s*\[?\s*(pass|reject|remark|confirmed|issues|escalate|upload)\s*\]?\s*[:：]?\s*(.*)$",
    re.IGNORECASE | re.DOTALL)
_G1_REQUIRED = (
    "brand",
    "activity_name",
    "period_start",
    "period_end",
    "platforms",
    "brand_query",
    "activity_query",
    "content_scope",
    "business_questions",
    "delivery_expectation",
    "allowed_actions",
)


def parse_decision(text: str) -> tuple[str, str]:
    """解析 `HCx [pass/reject/remark]: note` → (decision, note)。非法格式回退 remark。"""
    m = _DECISION_RE.match(text or "")
    if not m:
        return "remark", (text or "").strip()
    return m.group(1).lower(), (m.group(2) or "").strip()


def hc_kind_for_node(node_id: str) -> str:
    return NODE_TO_HC.get(node_id, node_id)


def _is_external_permission_p0(issues: list[Any]) -> bool:
    return any(
        isinstance(issue, dict)
        and issue.get("severity") == "P0"
        and (
            issue.get("locus") == "feishu-permission"
            or issue.get("id") == "P0-postpublish-owner"
        )
        for issue in issues
    )


def write_hc_pending(st, run_dir: Path) -> dict:
    """引擎暂停在人工关卡时，落 hc_pending.json 供薄 Agent 产卡。"""
    human = st.human or {}
    node_id = human.get("node", "")
    payload = {
        "run_id": st.run_id,
        "task_id": st.task_id,
        "node": node_id,
        "hc_kind": hc_kind_for_node(node_id),
        "prompt": human.get("prompt"),
        "expect": human.get("expect", []),
        "status": "awaiting_human",
    }
    if node_id == "n03_hc_plan":
        payload["review_context"] = {
            "analysis_plan": st.read_artifact("analysis_plan") or {},
            "execution_plan": st.read_artifact("execution_plan") or {},
            "selected_modules": st.read_artifact("selected_modules") or {},
            "artifact_refs": {
                key: st.artifact_ref(key)
                for key in ("analysis_plan", "execution_plan", "selected_modules")
            },
        }
    elif node_id == "n10_hc_opinion":
        # Forward the complete versioned artifacts, including review_items and
        # source_subject; the thin Coordinator must not rewrite review definitions.
        payload["review_context"] = {
            "opinion_candidates": st.read_artifact("opinion_candidates") or {},
            "ugc_category_list": st.read_artifact("ugc_category_list") or {},
            "artifact_refs": {
                key: st.artifact_ref(key)
                for key in ("opinion_candidates", "ugc_category_list")
            },
        }
    elif node_id == "n17_validate":
        qc_path = Path(run_dir) / "validation" / "qc_report.json"
        payload["review_context"] = {
            "report_ref": st.artifact_ref("report_md"),
            "qc_report_path": str(qc_path),
            "qc_report": (
                json.loads(qc_path.read_text(encoding="utf-8"))
                if qc_path.exists() else None
            ),
        }
        # FIX-36：用户内容返工后，复检对象为飞书回读文本（MD 仅作结构对照）。
        rework = st.read_artifact("user_rework") or {}
        if rework.get("change_type") == "content" and rework.get("readback_text"):
            payload["review_context"]["user_rework"] = True
            payload["review_context"]["review_subject"] = "feishu_readback"
            readback_path = Path(run_dir) / "validation" / "feishu_readback.txt"
            readback_path.parent.mkdir(parents=True, exist_ok=True)
            readback_path.write_text(rework["readback_text"], encoding="utf-8")
            payload["review_context"]["readback_path"] = str(readback_path)
    elif node_id == "n_hc7_escalate":
        qc_report = st.read_artifact("qc_report") or {}
        delivery_draft = st.read_artifact("delivery_draft") or {}
        post_publish = (
            delivery_draft.get("post_publish_validation")
            if isinstance(delivery_draft, dict) else {}
        ) or {}
        issues = list(post_publish.get("issues") or [])
        if not issues and isinstance(qc_report, dict):
            issues = list(qc_report.get("issues") or [])
        payload["review_context"] = {
            "escalation_reason": (
                "external_permission"
                if _is_external_permission_p0(issues)
                else "qc_retry_exhausted"
            ),
            "issues": issues,
            "qc_report": qc_report,
            "delivery_draft": delivery_draft,
            "qc_report_ref": st.artifact_ref("qc_report"),
            "delivery_ref": st.artifact_ref("delivery_draft"),
            "report_ref": st.artifact_ref("report_md"),
        }
    elif node_id == "n_user_escalate":
        confirmation = st.read_artifact("user_confirmation") or {}
        payload["review_context"] = {
            # FIX-36/37：返工超上限 → 澄清；否则为终验超时升级 Supervisor。
            "escalation_reason": ("user_rework_exhausted"
                                  if confirmation.get("reason") == "user_rework_exhausted" else "timeout"),
            "delivery_draft": st.read_artifact("delivery_draft") or {},
        }
    elif node_id == "n21_hc_user":
        draft = dict(st.read_artifact("delivery_draft") or {})
        readback = dict(draft.get("readback") or {})
        readback.pop("readback_text", None)          # 全文不进卡片
        draft["readback"] = readback
        qc = st.read_artifact("qc_report") or {}
        plan = st.read_artifact("confirmed_plan") or {}
        payload["review_context"] = {
            "delivery_draft": draft,
            "delivery_ref": st.artifact_ref("delivery_draft"),
            "report_ref": st.artifact_ref("report_md"),
            # FIX-40：质检通过时间、回读关键数字核对、豁免清单、布局核查清单。
            "qc_generated_at": ((qc.get("qc_metadata") or {}).get("generated_at") or qc.get("generated_at")),
            "numbers_check": readback.get("numbers_check") or {},
            "waivers": list(qc.get("waivers") or []),
            "checklist": delivery_checklist(plan),
        }
    p = Path(run_dir) / "hc_pending.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def clear_hc_pending(run_dir: Path) -> None:
    p = Path(run_dir) / "hc_pending.json"
    if p.exists():
        p.unlink()


def _parse_requirement(note: str) -> dict:
    """解析并校验 G1 表单 JSON，防止跨 Host/Agent 边界时字段丢失。"""
    raw = note or ""
    if raw.startswith("@"):
        path = Path(raw[1:])
        upload_root = Path("/mnt/session/uploads")
        if (
            path.parent != upload_root
            or path.name != str(path).split("/")[-1]
            or not path.name.startswith("topic5_requirement_")
            or path.suffix.lower() != ".json"
        ):
            raise ValueError("G1 需求文件路径非法")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError("G1 需求文件无法读取") from error
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("G1 需求载荷不是有效 JSON") from error
    if not isinstance(value, dict):
        raise ValueError("G1 需求载荷必须是 JSON 对象")
    missing = [key for key in _G1_REQUIRED if not value.get(key)]
    if missing:
        raise ValueError("G1 需求缺少必填字段: " + ", ".join(missing))
    for field in ("period_start", "period_end"):
        text = str(value[field])
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(f"G1 {field} 必须是 YYYY-MM-DD")
        try:
            date.fromisoformat(text)
        except ValueError as error:
            raise ValueError(f"G1 {field} 不是有效日期") from error
    if str(value["period_start"]) > str(value["period_end"]):
        raise ValueError("G1 活动开始日期不能晚于结束日期")
    if value.get("content_scope") not in ("all", "ugc", "pgc"):
        raise ValueError("G1 content_scope 必须是 all/ugc/pgc")
    return _with_traceability(value)


# FIX-09：需求留痕字段（原话/更正/来源/Skill 调用/交付要求）。
# 这些是附加字段：老载荷没有就补空，不参与必填校验，也不改变已有字段语义。
_TRACE_FIELDS = ("raw_request", "corrections", "field_sources",
                 "skill_invocations", "delivery_deadline", "special_notes",
                 "keyword_generation")  # FIX-07：关键词生成 Skill 的调用结果留痕


def _with_traceability(value: dict) -> dict:
    """保证 requirement_confirmed 带上留痕字段，缺省补空结构。

    下游（Validator 核对最新更正、词云 call-brief）依赖这些字段存在；
    补齐空值比让调用方到处写 `or {}` 更稳。
    """
    out = dict(value)
    out["raw_request"] = str(value.get("raw_request") or "").strip()
    out["corrections"] = traceability.clean_corrections(value.get("corrections"))
    out["field_sources"] = traceability.clean_field_sources(value.get("field_sources"))
    out["skill_invocations"] = traceability.clean_skill_invocations(
        value.get("skill_invocations"))
    out["delivery_deadline"] = str(value.get("delivery_deadline") or "").strip()
    out["special_notes"] = str(value.get("special_notes") or "").strip()
    # FIX-07：关键词生成结果原样保留（原始输出 / Skill 版本 / 调用时间），
    # 供词云 call-brief 与 Validator 第 6 项核对最新更正。
    raw_kw = value.get("keyword_generation")
    out["keyword_generation"] = dict(raw_kw) if isinstance(raw_kw, dict) else {}
    return out


def _parse_source_file(note: str) -> dict:
    """G2 note 为空或 "auto" 时返回空（自动采集）；否则必须是含 source_file 的 JSON。"""
    if str(note or "").strip().lower() in ("", "auto", "auto_collect"):
        return {}
    return _parse_source_file_json(note)


def _parse_source_file_json(note: str) -> dict:
    try:
        value = json.loads(note or "")
    except (TypeError, ValueError) as error:
        raise ValueError("G2 通过后缺少活动明细文件引用") from error
    source = value.get("source_file") if isinstance(value, dict) else None
    if not isinstance(source, dict) or not source.get("mount_path"):
        raise ValueError("G2 通过后缺少活动明细文件引用")
    mount_path = str(source["mount_path"])
    if Path(mount_path).name != mount_path or not mount_path.lower().endswith(".xlsx"):
        raise ValueError("G2 source_file.mount_path 非法")
    return normalize_source_file(source)


# FIX-33：轮次一 DataHub 路由确认
ANNOTATION_ROUTE_NODE = "n09_annotate_r1"


def _resume_annotation_route(decision: str, note: str) -> dict:
    """把用户的路由决定写入 annotation_route_decision，并让 n09 重跑。

    pass/confirmed → 用户接受改用直接 API（small_engine）；
    其他 → 用户选择补 DATAHUB_API_KEY 后重试，决策记为 datahub（仍要求 DataHub）。
    """
    route = "small_engine" if decision in ("pass", "confirmed") else "datahub"
    return {
        "annotation_route_decision": {
            "route": route, "decision": decision, "note": str(note or "").strip(),
        },
        "_control": {"next": ANNOTATION_ROUTE_NODE},
    }


def delivery_checklist(plan: dict) -> list[str]:
    """终验布局核查清单（FIX-40，原版终验消息模板），按平台数与内容范围生成。"""
    single = len(plan.get("platforms") or []) <= 1
    scope = str(plan.get("content_scope") or "all").lower()
    items = ["篇首整体洞察框（📊）存在",
             "传播概况单平台不分栏" if single else "传播概况：平台分布与内容类型两图并排",
             "词云左右两栏均有内容（词云图 + 解读）",
             "观点分析正负双栏",
             "图片均已显示、无本地路径"]
    if scope in ("ugc", "pgc"):
        items.append(f"仅分析 {scope.upper()} 内容的口径说明已标注")
    return items


def _revision_changes(note) -> list:
    """FIX-60：从 note JSON 中取 Coordinator 生成并经用户确认的结构化变更 changes。"""
    payload = note if isinstance(note, dict) else None
    if payload is None and str(note or "").strip().startswith("{"):
        try:
            payload = json.loads(note)
        except ValueError:
            payload = None
    changes = (payload or {}).get("changes") if isinstance(payload, dict) else None
    return [c for c in changes or [] if isinstance(c, dict) and c.get("field")]


def _revision_control(st, changes: list, gate_status: str) -> dict:
    """按影响矩阵精确失效并从最早受影响节点重跑；行级重标复用 FIX-30 的 annotation_rework_plan。"""
    plan = impact.plan_revision(changes)
    st.counters["rerun_epoch"] = st.counters.get("rerun_epoch", 0) + 1
    extra = {"revision_plan": plan}
    stage = "round1" if plan["rerun_rows"]["round1"] else ("round2" if plan["rerun_rows"]["round2"] else "")
    if stage and not plan["full"]:
        extra["annotation_rework_plan"] = {"stage": stage, "scope": "rows",
                                           "rerun_ids": plan["rerun_rows"][stage], "source": "revision"}
    if plan["requires_authorization"]:
        # 日期/关键词/平台变化涉及重新取数计费，回到方案确认重新授权。
        return {**extra, "_control": {"next": "n02_plan", "gate_status": gate_status,
                                      "invalidate_artifacts": plan["invalidate"]}}
    return {**extra, "_control": {"next": impact.entry_node(plan["invalidate"]), "gate_status": gate_status,
                                  "invalidate_artifacts": plan["invalidate"]}}


# FIX-38：交接后修正（mode=post_handoff_fix）：n19 → n17 → n20（原位修正）→ n21 → n23 → n24。
POST_HANDOFF_ENTRY = "n19_fix"


def post_handoff_fix_outputs(st, note: str) -> dict:
    """把修正请求写成 P0 返修项；交付链失效，报告与统计保留（返修基于当前版本）。"""
    text = str(note or "").strip()
    if not text:
        raise ValueError("交接后修正需要写明修改内容")
    handoff = st.read_artifact("handoff") or {}
    if not handoff.get("validated"):
        raise ValueError("任务尚未完成交接，不能走交接后修正")
    prior = st.read_artifact("qc_report") or {}
    qc = dict(prior) if isinstance(prior, dict) else {}
    amendments = list(handoff.get("amendments") or [])
    seq = len(amendments) + 1
    qc["issues"] = list(qc.get("issues") or []) + [{
        "id": f"P0-postfix-{seq}", "severity": "P0", "title": "交接后用户修正请求",
        "evidence": text, "locus": "report_md", "status": "open", "fix_required": True}]
    qc["has_p0"] = True
    qc["p0_count"] = sum(1 for i in qc["issues"] if i.get("severity") == "P0" and i.get("status") != "resolved")
    st.counters["qc_rounds"] = 0
    pending = {"seq": seq, "request": text, "requested_at": int(time.time()),
               "from_revision_id": handoff.get("bound_revision_id"),
               "from_report_version": handoff.get("report_version")}
    return {"artifacts": {"qc_report": qc, "post_handoff_fix": {"pending": pending,
                                                                "history": amendments}},
            "invalidate": list(_DELIVERY_INVALIDATE) + ["user_confirmation"]}


# FIX-36：G4 返工区分版式/内容；返工轮次独立计数，上限后澄清需求变化还是质量问题。
USER_REWORK_MAX = 3
_DELIVERY_INVALIDATE = ["delivery_draft", "external_receipts", "feishu_doc",
                        "delivery_record", "task_result", "handoff"]


def _parse_rework(note) -> tuple[str, str]:
    """note 可为 JSON {"change_type": "layout|content", "note": "..."}；缺省/不确定按内容（不漏检）。"""
    payload = note if isinstance(note, dict) else None
    if payload is None:
        try:
            payload = json.loads(note) if str(note or "").strip().startswith("{") else None
        except ValueError:
            payload = None
    if isinstance(payload, dict):
        change = "layout" if payload.get("change_type") == "layout" else "content"
        return change, str(payload.get("note") or "")
    return "content", str(note or "")


def _g4_rework(st, confirmation: dict, note) -> dict:
    change_type, text = _parse_rework(note)
    rounds = st.counters.get("user_rework_rounds", 0) + 1
    st.counters["user_rework_rounds"] = rounds
    confirmation = {**confirmation, "note": text, "change_type": change_type, "rework_round": rounds}
    if rounds > USER_REWORK_MAX:
        # 第 3 轮之后约一次沟通：澄清是需求变化还是质量问题（n_user_escalate）。
        return {"user_confirmation": {**confirmation, "status": "escalate", "reason": "user_rework_exhausted"},
                "_control": {"next": "n_user_escalate", "gate_status": "escalated"}}
    draft = st.read_artifact("delivery_draft") or {}
    readback = (draft.get("readback") if isinstance(draft, dict) else {}) or {}
    rework = {"change_type": change_type, "round": rounds, "note": text,
              "readback_text": readback.get("readback_text") or ""}
    if change_type == "layout":
        # 版式修改：不改正文、不经 n19/n17，直接在原文档重发并回到 G4。
        return {"user_confirmation": confirmation, "user_rework": rework,
                "_control": {"next": "n20_delivery_draft", "gate_status": "rejected",
                             "invalidate_artifacts": list(_DELIVERY_INVALIDATE)}}
    changes = _revision_changes(note)
    if changes:
        # FIX-60：用户补证/数据修订 → 按影响矩阵精确重算，而非只改报告文字。
        return {"user_confirmation": confirmation, "user_rework": rework,
                **_revision_control(st, changes, "rejected")}
    prior_qc = st.read_artifact("qc_report") or {}
    qc = dict(prior_qc) if isinstance(prior_qc, dict) else {}
    qc["issues"] = list(qc.get("issues") or []) + [{
        "id": "P0-g4-user-reject", "severity": "P0", "title": "用户终验退回（内容修改）",
        "evidence": text or "用户要求修订最终交付", "locus": "G4"}]
    qc["has_p0"] = True
    qc["p0_count"] = int(qc.get("p0_count") or 0) + 1
    return {"user_confirmation": confirmation, "qc_report": qc, "user_rework": rework,
            "_control": {"next": "n19_fix", "gate_status": "rejected",
                         "invalidate_artifacts": list(_DELIVERY_INVALIDATE)}}


# FIX-35：HC7 三选一（KS_Workflow HC-7：放宽该项并留痕 / 补充数据后重做 / 终止项目）
HC7_RELAX, HC7_REDO, HC7_TERMINATE = "relax", "redo_with_data", "terminate"
REDO_INVALIDATE = [
    "run_record", "resource_lock", "context_snapshot", "brand_stats", "brand_stats_baseline",
    "cleaned_data", "denoised_data", "noise_review", "annotated_round1", "r1_review",
    "opinion_candidates", "confirmed_opinion_labels", "ugc_category_list", "annotated_data",
    "r2_review", "wordcloud_data", "stats_bundle", "chart_urls", "wordcloud_insight",
    "report_md", "qc_report", "delivery_draft", "external_receipts", "feishu_doc",
]


def _hc7_issues(st) -> list:
    draft = st.read_artifact("delivery_draft") or {}
    post = (draft.get("post_publish_validation") if isinstance(draft, dict) else {}) or {}
    issues = list(post.get("issues") or [])
    if not issues:
        qc = st.read_artifact("qc_report") or {}
        issues = list(qc.get("issues") or []) if isinstance(qc, dict) else []
    return issues


def _relax(st, note) -> dict:
    """勾选的 P0 改 waived 并记 waiver；全部 P0 豁免才放行交付，外部权限类 P0 不可放宽。"""
    payload = note if isinstance(note, dict) else {}
    if not payload:
        try:
            payload = json.loads(note or "{}")
        except (TypeError, ValueError):
            payload = {}
    ids = [str(i) for i in payload.get("issue_ids") or [] if str(i).strip()]
    reason = str(payload.get("reason") or "").strip()
    if not ids or not reason:
        raise ValueError("HC7 放宽须勾选至少一个检查项并填写理由")
    qc = dict(st.read_artifact("qc_report") or {})
    issues = [dict(i) for i in qc.get("issues") or [] if isinstance(i, dict)]
    by_id = {i.get("id"): i for i in issues}
    unknown = [i for i in ids if i not in by_id or by_id[i].get("severity") != "P0"]
    if unknown:
        raise ValueError(f"HC7 放宽的检查项不是当前 P0：{unknown}")
    if _is_external_permission_p0([by_id[i] for i in ids]):
        raise ValueError("外部权限类 P0 不允许放宽")
    now = int(time.time())
    waivers = list(qc.get("waivers") or [])
    for issue_id in ids:
        by_id[issue_id]["status"] = "waived"
        waivers.append({"issue_id": issue_id, "title": by_id[issue_id].get("title") or "",
                        "reason": reason, "operator": str(payload.get("operator") or ""), "at": now})
    remaining = [i for i in issues if i.get("severity") == "P0" and i.get("status") != "waived"]
    qc.update({"issues": issues, "waivers": waivers, "has_p0": bool(remaining), "p0_count": len(remaining)})
    target = "n20_delivery_draft" if not remaining else "n19_fix"
    return {"qc_report": qc, "_control": {"next": target, "gate_status": "waived" if not remaining else "escalated",
                                         "invalidate_artifacts": ["delivery_draft"] if not remaining else []}}


def _resume_hc7(st, expect, decision, note) -> dict:
    key = expect[0] if expect else "escalation_decision"
    record = {"decision": decision, "note": note if isinstance(note, str) else json.dumps(note, ensure_ascii=False),
              "escalated": True, "release_authorized": False}
    if decision == HC7_RELAX:
        out = _relax(st, note)
        record["waived"] = [w["issue_id"] for w in out["qc_report"]["waivers"]]
        return {key: record, **out}
    if decision == HC7_REDO:
        st.counters["qc_rounds"] = 0
        changes = _revision_changes(note)
        if changes:
            return {key: {**record, "redo": True}, **_revision_control(st, changes, "rejected")}
        st.counters["rerun_epoch"] = st.counters.get("rerun_epoch", 0) + 1
        return {key: {**record, "redo": True},
                "_control": {"next": "n02_plan", "gate_status": "rejected",
                             "invalidate_artifacts": REDO_INVALIDATE}}
    if decision == HC7_TERMINATE:
        # 已创建的飞书文档保留，不删除；Run 以 terminated 结束。
        return {key: {**record, "terminated": True},
                "_control": {"next": None, "gate_status": "terminated", "terminate": True}}
    target = "n19_fix"
    if decision in ("pass", "confirmed") and _is_external_permission_p0(_hc7_issues(st)):
        target = "n20_delivery_draft"
    return {key: record, "_control": {"next": target, "gate_status": "escalated"}}


# FIX-04：采集授权（HC-COLLECT，操作级 HC）
COLLECT_NODE = "n03b_activity_pull"


def _resume_collect(st, decision: str, note) -> dict:
    """pass + {campaigns:{cid:{max_records, confirmation_ref}}} → 写授权并重跑 n03b；
    upload + {source_file} → 用户改为上传活动明细，写入 confirmed_plan 后重跑 n03b（直接沿用）。"""
    if decision == "upload":
        source = _parse_source_file_json(note)
        plan = dict(st.read_artifact("confirmed_plan") or {})
        plan["source_file"] = source
        return {"confirmed_plan": plan, "_control": {"next": COLLECT_NODE}}
    if decision not in ("pass", "confirmed"):
        raise ValueError("未授权采集：可在当前话题上传活动明细 .xlsx，或授权后继续")
    payload = note if isinstance(note, dict) else json.loads(note or "{}")
    campaigns = payload.get("campaigns") or {}
    for cid, grant in campaigns.items():
        if not (isinstance(grant.get("max_records"), int) and grant["max_records"] > 0
                and str(grant.get("confirmation_ref") or "").strip()):
            raise ValueError(f"采集授权缺少数量上限或确认引用：{cid}")
    if not campaigns:
        raise ValueError("采集授权为空")
    return {"collection_authorization": {"campaigns": campaigns, "authorized_at": int(time.time())},
            "_control": {"next": COLLECT_NODE}}


# FIX-58：数据异常严重项确认（有效量不足 / 多 Sheet 需合并）
DATA_EXCEPTION_NODE = "n07_clean"


def _resume_data_exception(decision: str, note: str) -> dict:
    """pass/confirmed → 用户知情后继续（报告披露数据量限制）；其他 → 保持暂停，等用户补数据。"""
    if decision not in ("pass", "confirmed"):
        raise ValueError("数据异常未确认继续：请补充/合并数据后重新提供活动明细")
    return {"data_exception_ack": {"decision": "continue", "note": str(note or "").strip()},
            "_control": {"next": DATA_EXCEPTION_NODE}}


# FIX-30：标注审核节点（与 G3 同构）
ANNOTATION_REVIEW_NODES = frozenset({"n09r_review_r1", "n11r_review_r2"})


def _resume_annotation_review(st, node_id: str, decision: str, note: str) -> dict:
    """轮次一/轮次二标注审核节点的 HB 校验（FIX-30，qc_metadata 结构同 G3）。

    校验要求（原版 KS_Annotation_Review§1 / QC §5/§7）：
      - actor_role == "Validator"
      - mode == "independent-agent"
      - 至少给出 accuracy（两个字段分别判定）
      - 有 verdict_count > 0 的判定记录
    校验通过后把整个报告写进对应的 artifact 键（r1_review / r2_review）。
    """
    art_key = "r1_review" if node_id == "n09r_review_r1" else "r2_review"
    # note 可以是 JSON 字符串也可以是已解析 dict（兼容测试 helper 直接传 dict 的场景）
    if isinstance(note, dict):
        review = note
    else:
        try:
            review = json.loads(note or "{}")
        except (ValueError, TypeError):
            raise ValueError(
                f"{node_id}: 审核结果必须是 JSON（包含 accuracy 与 qc_metadata）")
    if not isinstance(review, dict):
        raise ValueError(f"{node_id}: 审核结果必须是 JSON 对象")
    metadata = review.get("qc_metadata") or {}
    if metadata.get("actor_role") != "Validator":
        raise ValueError(f"{node_id}: qc_metadata.actor_role 必须是 Validator")
    if metadata.get("mode") != "independent-agent":
        raise ValueError(f"{node_id}: qc_metadata.mode 必须是 independent-agent")
    accuracy = review.get("accuracy")
    if not isinstance(accuracy, dict) or not accuracy:
        raise ValueError(f"{node_id}: 审核结果缺少 accuracy（至少一个字段的正确率）")
    verdict_count = review.get("verdict_count") or sum(
        item.get("total", 0) for item in
        (review.get("field_stats") or {}).values() if isinstance(item, dict))
    if not verdict_count:
        raise ValueError(f"{node_id}: 审核结果没有任何逐帖判定（verdict_count=0）")
    return {art_key: {**review, "decision": decision}}


def build_human_outputs(
    st,
    decision: str,
    note: str = "",
    requested_hc: str | None = None,
) -> Optional[dict]:
    """把用户决定翻译成当前暂停节点所需的 human_outputs。

    返回 None 表示「只记录 remark、不续跑」（remark 无内容，等待后续消息指令）。
    """
    human = st.human or {}
    node_id = human.get("node", "")
    expect = human.get("expect", []) or []
    decision = (decision or "").lower()
    current_hc = hc_kind_for_node(node_id)
    if requested_hc and requested_hc != current_hc:
        raise ValueError(
            f"过期 HC 回注：请求 {requested_hc}，当前等待 {current_hc}"
        )

    # remark 无内容：只记录，不触发续跑（project_memory 硬约束）。
    if decision == "remark" and not note.strip():
        return None

    # n09：DataHub 路由确认（FIX-33）。用户确认改用直接 API 后重跑本节点；
    # 拒绝（或未给出明确确认）时不写决策，保持暂停语义由调用方补 Key 后重试。
    if node_id == ANNOTATION_ROUTE_NODE:
        return _resume_annotation_route(decision, note)
    if node_id == DATA_EXCEPTION_NODE:
        return _resume_data_exception(decision, note)
    if node_id == COLLECT_NODE:
        return _resume_collect(st, decision, note)

    # n09r / n11r 标注审核：与 G3 同构——只接受独立 Validator 持久化的审核结果。
    if node_id in ANNOTATION_REVIEW_NODES:
        return _resume_annotation_review(st, node_id, decision, note)

# n17_validate: only an independently persisted Validator result may resume G3.
    if node_id == "n17_validate":
        qc_path = st.run_dir / "validation" / "qc_report.json"
        if not qc_path.exists():
            raise ValueError("G3 缺少独立 Validator 产出的 validation/qc_report.json")
        qc = json.loads(qc_path.read_text(encoding="utf-8"))
        report_ref = st.artifact_ref("report_md")
        if not isinstance(qc, dict) or qc.get("validator") != "topic5-validator":
            raise ValueError("G3 qc_report 缺少独立 Validator 身份")
        if qc.get("subject") != report_ref:
            raise ValueError("G3 qc_report 绑定的 ReportArtifact 不是当前报告版本")
        metadata = qc.get("qc_metadata") or {}
        input_keys = {
            item.get("key")
            for item in (metadata.get("input_artifacts") or [])
            if isinstance(item, dict) and item.get("sha256")
        }
        execution_ref = qc.get("execution_ref") or {}
        if (
            not qc.get("execution_source")
            or metadata.get("mode") != "independent-agent"
            or metadata.get("validator_id") != "topic5-validator"
            or not qc.get("rules_version")
            or not qc.get("validator_version")
            or not all(
                execution_ref.get(key)
                for key in ("task_id", "run_id", "attempt_id")
            )
            or not {
                "report_md", "requirement_confirmed", "stats_bundle",
                "chart_urls", "validator_rules",
            }.issubset(input_keys)
        ):
            raise ValueError(
                "G3 qc_report 缺少独立执行来源、资源版本、执行引用或完整输入绑定"
            )
        if bool(qc.get("has_p0")) != (int(qc.get("p0_count") or 0) > 0):
            raise ValueError("G3 qc_report 的 has_p0 与 p0_count 不一致")
        if decision in ("pass", "confirmed") and qc.get("has_p0"):
            raise ValueError("G3 当前 Validator 结果仍有 P0，禁止通过")
        # reject：已有 Validator P0 时直接采用其结果；仅在无 P0 时视为人工强制打回。
        if decision == "reject" and not qc.get("has_p0"):
            qc = dict(qc)
            qc["issues"] = list(qc.get("issues", [])) + [{
                "id": "P0-human-reject", "severity": "P0",
                "title": "人工质检打回", "evidence": note or "人工判定需返修",
                "locus": "human"}]
            qc["has_p0"] = True
            qc["p0_count"] = qc.get("p0_count", 0) + 1
        if note:
            qc = dict(qc)
            qc["human_note"] = note
        return {"qc_report": qc}

    # n21_hc_user（G4 用户终验）：pass→confirmed；reject→issues（回 n20 返修）；escalate→escalate。
    if node_id == "n21_hc_user":
        status = {"pass": "confirmed", "reject": "issues",
                  "escalate": "escalate", "confirmed": "confirmed",
                  "issues": "issues"}.get(decision)
        if status is None:
            raise ValueError(f"G4 不支持决定 {decision!r}")
        rep = st.read_artifact("report_md") or {}
        draft = st.read_artifact("delivery_draft") or {}
        confirmation = {
            "status": status,
            "report_version": rep.get("version") if isinstance(rep, dict) else None,
            "report_ref": st.artifact_ref("report_md"),
            "delivery_ref": st.artifact_ref("delivery_draft"),
            "revision_id": draft.get("revision_id") if isinstance(draft, dict) else None,
            "note": note,
        }
        if status == "confirmed":
            post_publish = (
                draft.get("post_publish_validation")
                if isinstance(draft, dict) else {}
            ) or {}
            if (
                not confirmation["revision_id"]
                or (isinstance(draft, dict) and draft.get("state") != "ready")
                or post_publish.get("has_p0")
            ):
                raise ValueError(
                    "G4 交付草稿未 ready、未绑定 revision 或发布后 Validator 仍有 P0"
                )
            if not draft.get("degraded"):
                live_revision = feishu.current_revision(str(draft.get("doc_token") or ""))
                if live_revision is None:
                    raise ValueError("G4 无法复读当前飞书 revision，禁止通过")
                if live_revision != confirmation["revision_id"]:
                    raise ValueError("G4 飞书文档已变化，需重新回读、质检并生成新终验对象")
            return {"user_confirmation": confirmation}
        if status == "issues":
            return _g4_rework(st, confirmation, note)
        return {"user_confirmation": confirmation}


    # FIX-35 HC7：放宽并留痕 / 补数据重做 / 终止项目；pass 保留为「再修一轮」（原语义）。
    if node_id == "n_hc7_escalate":
        return _resume_hc7(st, expect, decision, note)

    # G4 timeout escalation cannot substitute for the user's revision-bound acceptance.
    if node_id == "n_user_escalate":
        # FIX-36：返工超上限后的澄清——需求变化回 n00；质量问题再给一轮返工（回 G4 等用户修改）。
        if decision == "requirement_change":
            return {
                expect[0] if expect else "escalation_decision": {
                    "decision": decision, "note": note, "escalated": True, "release_authorized": False},
                "_control": {"next": "n00_intake", "gate_status": "rejected",
                             "invalidate_artifacts": ["requirement_confirmed"] + REDO_INVALIDATE},
            }
        if decision == "quality_issue":
            st.counters["user_rework_rounds"] = USER_REWORK_MAX - 1
        return {
            expect[0] if expect else "escalation_decision": {
                "decision": decision,
                "note": note,
                "escalated": True,
                "release_authorized": False,
            },
            "_control": {
                "next": "n21_hc_user",
                "gate_status": "escalated",
            },
        }

    # 需求确认 G1 / 方案确认 G2：pass 产确认产物；reject 视为需补充（note 必带）。
    if node_id == "n01_hc_requirement":
        if decision not in ("pass", "confirmed"):
            return {
                "requirement_confirmed": {"confirmed": False, "note": note},
                "_control": {
                    "next": "n00_intake",
                    "gate_status": "rejected",
                    "invalidate_artifacts": ["requirement_confirmed"],
                },
            }
        requirement = _parse_requirement(note)
        return {"requirement_confirmed": {
            **requirement,
            "confirmed": True,
            "validator_precheck": {
                "status": "passed",
                "rules": [
                    "required_fields", "date_range", "content_scope",
                ],
                "authority": "user",
            },
        }}
    if node_id == "n03_hc_plan":
        requirement = st.read_artifact("requirement_confirmed") or {}
        analysis_plan = st.read_artifact("analysis_plan") or {}
        execution_plan = st.read_artifact("execution_plan") or {}
        selected_modules = st.read_artifact("selected_modules") or {}
        refs = {
            key: st.artifact_ref(key)
            for key in ("analysis_plan", "execution_plan", "selected_modules")
        }
        plan_hashes = {
            value.get("plan_bundle_sha256")
            for value in (analysis_plan, execution_plan, selected_modules)
            if isinstance(value, dict)
        }
        if None in plan_hashes or len(plan_hashes) != 1:
            raise ValueError("G2 方案 Artifact 的 bundle hash 不一致")
        confirmed = decision in ("pass", "confirmed")
        source_file = {}
        if confirmed:
            source_file = (
                _parse_source_file(note)
                if note
                else (
                    requirement.get("source_file")
                    if isinstance(requirement.get("source_file"), dict)
                    else {}
                )
            )
            # FIX-04：activity_data_mode=auto（默认）时允许缺省，由 n03b 自动采集原帖；upload 保持原行为。
            if not source_file and str(st.params.get("activity_data_mode") or "auto") == "upload":
                raise ValueError("G2 通过后缺少活动明细文件引用")
        plan = {
            **(requirement if isinstance(requirement, dict) else {}),
            **({"source_file": source_file} if source_file else {}),
            "analysis_plan": analysis_plan,
            "execution_plan": execution_plan,
            "selected_modules": selected_modules,
            "confirmed": confirmed,
            "version": f"plan-v{refs['analysis_plan']['version']}",
            "subject": refs,
            "plan_bundle_sha256": next(iter(plan_hashes)),
            "scope": analysis_plan.get("scope") if isinstance(analysis_plan, dict) else {},
            "note": note,
            "validator_precheck": {
                "status": (
                    "passed"
                    if (execution_plan.get("preflight") or {}).get("status")
                    in ("READY", "READY_PENDING_INPUT")
                    else "failed"
                ) if isinstance(execution_plan, dict) else "failed",
                "rules": [
                    "plan_bundle_hash", "capability_registry",
                    "input_schema", "metric_policy",
                ],
                "authority": "user",
            },
        }
        if confirmed:
            preflight = execution_plan.get("preflight") if isinstance(execution_plan, dict) else {}
            if (preflight or {}).get("status") not in (
                "READY",
                "READY_PENDING_INPUT",
            ):
                raise ValueError("G2 能力/权限/输入预检未通过，禁止进入生产 Run")
            return {"confirmed_plan": plan}
        return {
            "confirmed_plan": plan,
            "_control": {
                "next": "n02_plan",
                "gate_status": "rejected",
                "invalidate_artifacts": [
                    "run_record", "resource_lock", "context_snapshot",
                ],
            },
        }

    # N10 jointly confirms opinion and UGC labels against exact candidate versions.
    if node_id == "n10_hc_opinion":
        prev = st.read_artifact("opinion_candidates") or {}
        labels = (prev.get("candidates") or prev.get("labels")
                  if isinstance(prev, dict) else None) or []
        ugc = st.read_artifact("ugc_category_list") or {}
        ugc_categories = (
            ugc.get("categories") if isinstance(ugc, dict) else None
        ) or []
        extra = [x.strip() for x in re.split(r"[,，、]", note) if x.strip()] if note else []
        confirmed = decision in ("pass", "confirmed")
        result = {
            "labels": list(labels) + (extra if confirmed else []),
            "ugc_categories": list(ugc_categories),
            "ugc_not_applicable": bool(
                isinstance(ugc, dict) and ugc.get("not_applicable")
            ),
            **label_review.confirmed_review(prev, ugc),
            "confirmed": confirmed,
            "note": note,
            "subject": {
                "opinion_candidates": st.artifact_ref("opinion_candidates"),
                "ugc_category_list": st.artifact_ref("ugc_category_list"),
            },
        }
        if confirmed:
            return {"confirmed_opinion_labels": result}
        return {
            "confirmed_opinion_labels": result,
            "_control": {
                "next": ["n08_ugc_discovery", "n09b_opinion_candidates"],
                "gate_status": "rejected",
                "invalidate_artifacts": [
                    "confirmed_opinion_labels", "annotated_data", "stats_bundle",
                    "chart_urls", "wordcloud_insight", "report_md", "qc_report",
                    "delivery_draft", "external_receipts", "feishu_doc",
                    "user_confirmation", "delivery_record", "task_result", "handoff",
                ],
            },
        }

    # 兜底：按 expect 产最小确认产物。
    return {k: {"confirmed": decision in ("pass", "confirmed"), "note": note}
            for k in (expect or ["confirmed"])}
