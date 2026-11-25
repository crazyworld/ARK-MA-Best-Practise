"""Phase1 需求受理结果的确定性交接（原版 Phase1_需求梳理 v1.12 Step 0–2）。

协调器按 campaign-keyword-generation Skill 完成理解 / 澄清 / 口径生成后，把完整返回写成
JSON 文件，再调用本脚本：

  python3 intake_result.py emit --result <keyword_generation_result.json> [--request <request.json>]
  python3 intake_result.py emit --no-description          # 用户只唤起、未描述需求（Step 0）

本脚本只做程序能做的事，不替 Skill 做任何业务判断：
  - 校验 Skill 返回的结构与 ready 的必填口径（contract.md「返回约定」）；
  - 旧 legacy_status 按 contract 映射为 status；
  - 落调用留痕 invocation.json（skill_name / skill_version / resolved_skill_path /
    rule_baseline / executed_at / result_path / dependency_state，原版 Step2 第 5 条）；
  - 打印唯一一行 ``INTAKE_RESULT {json}`` 供 Gateway 消费（转达问题 / 发模板 / 冻结需求）。

ready 时不在此生成需求快照：字段映射与表达式预检在 Gateway 的 topic5_intake 完成，
校验失败会把原因回注给协调器，由 Skill 携带 prior_result 修正，避免两处维护映射。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SKILL_NAME = "campaign-keyword-generation"
STATUSES = ("ready", "need_clarification", "blocked")
# contract.md「旧调用方适配」：ready→ready_for_confirmation；blocked→dependency_unavailable。
LEGACY_STATUS = {
    "ready_for_confirmation": "ready",
    "need_clarification": "need_clarification",
    "dependency_unavailable": "blocked",
}
# ready 要求品牌与各活动两套必填口径就绪（contract.md:165）及品牌/活动/完整日期/平台明确。
READY_REQUIRED = ("brand", "campaign_name", "period_start", "period_end", "platforms",
                  "brand_keyword", "activity_keywords")
# Gateway 映射需求快照需要的 Skill 返回字段（其余证据只留在完整结果文件里）。
HANDOFF_FIELDS = (
    "resolved_request", "campaign_name", "campaign_type", "activity_boundary",
    "brand_keyword", "activity_keywords", "product_keywords", "brand_filter",
    "activity_filter", "filter_export_status", "dependency_state", "confirmation_state",
    "missing_fields",
)
CST = timezone(timedelta(hours=8))


class IntakeResultError(ValueError):
    """Skill 返回不满足交接契约；协调器须按 Skill 修正后重新 emit。"""


def skill_dir() -> Path:
    """Skill 与编排器平铺挂载在同级（/mnt/skills/<skill>/）。"""
    return Path(__file__).resolve().parent.parent / SKILL_NAME


def skill_metadata(directory: Path | None = None) -> dict[str, str]:
    """从 SKILL.md frontmatter 读取实际挂载版本，不信任模型自报版本。"""
    path = (directory or skill_dir()) / "SKILL.md"
    if not path.is_file():
        raise IntakeResultError(f"未挂载 {SKILL_NAME}：缺少 {path}")
    head = path.read_text(encoding="utf-8").split("\n---", 1)[0]

    def field(name: str) -> str:
        match = re.search(rf"^\s*{name}:\s*\"?([^\"\n]+)\"?\s*$", head, re.MULTILINE)
        return match.group(1).strip() if match else ""

    return {
        "skill_name": field("name") or SKILL_NAME,
        "skill_version": field("version"),
        "rule_baseline": field("rule_baseline"),
        "resolved_skill_path": str(path),
    }


def _has_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_value(item) for item in value)
    return value not in (None, "") and str(value).strip() not in ("", "无")


def normalize_status(result: dict) -> str:
    status = str(result.get("status") or "").strip()
    if status in STATUSES:
        return status
    legacy = str(result.get("legacy_status") or status).strip()
    if legacy in LEGACY_STATUS:
        return LEGACY_STATUS[legacy]
    raise IntakeResultError(
        f"Skill 返回 status 非法：{status or '空'}（只接受 ready/need_clarification/blocked）")


def ready_missing(result: dict) -> list[str]:
    """ready 前置口径缺口；非空即不能交接（Skill 应改返回 need_clarification/blocked）。"""
    request = result.get("resolved_request") or {}
    if not isinstance(request, dict):
        request = {}
    period = request.get("campaign_period") or {}
    if not isinstance(period, dict):
        period = {}
    values = {
        "brand": request.get("brand"),
        "campaign_name": result.get("campaign_name") or request.get("campaign_name"),
        "period_start": period.get("start") or request.get("period_start"),
        "period_end": period.get("end") or request.get("period_end"),
        "platforms": request.get("platforms"),
        "brand_keyword": result.get("brand_keyword") or request.get("brand_keyword"),
        "activity_keywords": result.get("activity_keywords") or request.get("activity_keywords"),
    }
    return [name for name in READY_REQUIRED if not _has_value(values[name])]


def build_intake_result(result: dict, *, result_path: str, metadata: dict,
                        executed_at: str) -> dict:
    """把 Skill 完整返回压成 Gateway 交接载荷；不改写任何口径。"""
    if not isinstance(result, dict):
        raise IntakeResultError("Skill 返回必须是 JSON 对象")
    status = normalize_status(result)
    out: dict[str, Any] = {
        "status": status,
        "result_path": result_path,
        "skill": {**metadata, "executed_at": executed_at},
        "summary": str(result.get("summary") or result.get("understanding") or "").strip(),
        "dependency_state": result.get("dependency_state") or {},
    }
    if status == "need_clarification":
        questions = [str(q).strip() for q in (result.get("clarification_questions") or [])
                     if str(q).strip()]
        if not questions:
            raise IntakeResultError("need_clarification 必须给出 clarification_questions")
        out["questions"] = questions
        out["missing_fields"] = list(result.get("missing_fields") or [])
    elif status == "blocked":
        details = (result.get("dependency_state") or {}).get("details") if isinstance(
            result.get("dependency_state"), dict) else ""
        out["reason"] = str(result.get("reason") or details or "").strip()
        if not out["reason"]:
            raise IntakeResultError("blocked 必须说明无可用核心式或依赖故障的具体原因")
    else:
        missing = ready_missing(result)
        if missing:
            raise IntakeResultError(
                "ready 缺少必填口径：" + "、".join(missing)
                + "（关键缺项应返回 need_clarification，无可用核心式应返回 blocked）")
        out["result"] = {key: result.get(key) for key in HANDOFF_FIELDS if key in result}
    return out


def emit(result_path: Path | None, request_path: Path | None = None, *,
         no_description: bool = False, now: datetime | None = None) -> dict:
    executed_at = (now or datetime.now(CST)).isoformat(timespec="seconds")
    if no_description:
        return {"status": "need_input", "reason": "no_description", "executed_at": executed_at}
    if result_path is None or not result_path.is_file():
        raise IntakeResultError(f"Skill 结果文件不存在：{result_path}")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise IntakeResultError(f"Skill 结果不是有效 JSON：{error}") from error
    metadata = skill_metadata()
    payload = build_intake_result(
        result, result_path=str(result_path), metadata=metadata, executed_at=executed_at)
    invocation = {
        **metadata,
        "executed_at": executed_at,
        "result_path": str(result_path),
        "request_path": str(request_path) if request_path else "",
        "status": payload["status"],
        "dependency_state": payload.get("dependency_state") or {},
    }
    (result_path.parent / "invocation.json").write_text(
        json.dumps(invocation, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["emit"])
    parser.add_argument("--result", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--no-description", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = emit(args.result, args.request, no_description=args.no_description)
    except IntakeResultError as error:
        # 不输出 INTAKE_RESULT：协调器须按 Skill 修正结果后重新 emit。
        print(f"INTAKE_INVALID {error}")
        return 2
    print("INTAKE_RESULT " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
