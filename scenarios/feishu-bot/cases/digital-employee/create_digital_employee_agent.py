"""创建飞书数字员工 Agent（支持群聊协作与单聊个人服务）。

跑一次，拿到 agent id，填到 GROUP_BOT_AGENT_ID 环境变量，供数字员工使用。
与主包 arkagent init 建的「客户A销售助手」是两个独立 Agent，互不影响。

运行：
  set -a && source ~/.arkagent/config.env && set +a   # 需要 ARK_API_KEY[/ARK_BASE_URL]
  python scenarios/feishu-bot/cases/digital-employee/create_digital_employee_agent.py

企业自有模型（BYOK Direct 模式，见《火山 Managed Agent BYOK 模型接入指南》）：
  GROUP_BOT_MODEL_MODE=byok_direct 时，以下变量均为必填：
    GROUP_BOT_MODEL_PROVIDER       接入前与火山约定并注册过的 provider 名称
    GROUP_BOT_MODEL_BASE_URL       企业模型完整服务地址（公网 Gateway 可访问）
    GROUP_BOT_MODEL_PROTOCOL       openai_chat_completions | anthropic_messages
    GROUP_BOT_MODEL_TOKEN_LIMITS   JSON，如 '{"context_window":128000,"max_input_token_length":128000,"max_output_token_length":4096}'
"""
from __future__ import annotations

import asyncio
import json
import os

from shared import GROUP_BOT_NAME, build_group_system

from arkagent.ark import ArkClient
from arkagent.gateway import ConfigStore, DigitalEmployee, MAControlPlane

# 默认群 Bot persona 在配置库里的稳定 id：重复运行 create/update 只 upsert 同一行，不累积记录。
DEFAULT_PERSONA_ID = "emp_group_bot_default"


def _load_byok_overrides() -> dict:
    """读取 BYOK Direct 模式的环境变量覆盖；model_mode 非 byok_direct 时返回空 dict。"""
    model_mode = (os.environ.get("GROUP_BOT_MODEL_MODE") or "ark").strip() or "ark"
    overrides: dict = {"model_mode": model_mode}
    if model_mode != "byok_direct":
        return overrides
    token_limits_raw = (os.environ.get("GROUP_BOT_MODEL_TOKEN_LIMITS") or "").strip()
    try:
        token_limits = json.loads(token_limits_raw) if token_limits_raw else None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GROUP_BOT_MODEL_TOKEN_LIMITS 不是合法 JSON：{exc}") from exc
    overrides.update(
        model_provider=(os.environ.get("GROUP_BOT_MODEL_PROVIDER") or "").strip() or None,
        model_base_url=(os.environ.get("GROUP_BOT_MODEL_BASE_URL") or "").strip() or None,
        model_protocol=(os.environ.get("GROUP_BOT_MODEL_PROTOCOL") or "").strip() or None,
        model_token_limits=token_limits,
    )
    return overrides


async def _main() -> None:
    api_key = (os.environ.get("ARK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("缺少 ARK_API_KEY。请先 export 或 source 主包 config.env。")
    base_url = (os.environ.get("ARK_BASE_URL") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    model_id = (os.environ.get("GROUP_BOT_MODEL_ID") or "doubao-seed-evolving").strip()
    byok_overrides = _load_byok_overrides()
    # bot 在飞书群里的显示名，写进 system prompt 供模型识别「@谁=在叫自己」；应与开放平台一致。
    bot_name = (os.environ.get("GROUP_BOT_DISPLAY_NAME") or "数字员工阿J").strip()

    ark = ArkClient(api_key, base_url)
    store = ConfigStore()
    plane = MAControlPlane(ark, store)
    try:
        # 走控制面 sync_employee：无 ark_agent_id → create_agent，并把 id/version 回填配置库。
        persona = store.get_employee(DEFAULT_PERSONA_ID) or DigitalEmployee(
            id=DEFAULT_PERSONA_ID, name=GROUP_BOT_NAME
        )
        persona.name = GROUP_BOT_NAME
        persona.identity_prompt = build_group_system(bot_name)
        persona.model_id = model_id
        for field_name, value in byok_overrides.items():
            setattr(persona, field_name, value)
        persona.ark_agent_id = ""  # 强制新建
        persona = await plane.sync_employee(persona)
    finally:
        await ark.aclose()
        store.close()

    print(f"已创建群聊共享 Agent：{persona.ark_agent_id}")
    print("请设置环境变量后再启动 demo：")
    print(f"  export GROUP_BOT_AGENT_ID={persona.ark_agent_id}")


if __name__ == "__main__":
    asyncio.run(_main())
