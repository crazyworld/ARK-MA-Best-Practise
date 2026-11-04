from __future__ import annotations

import json
from pathlib import Path

import pytest


RESOURCE_DIR = (
    Path(__file__).resolve().parents[1] / "cases" / "topic6" / "ma-resources"
)


def test_coordinator_declares_mcp_toolset_inside_tools():
    config = json.loads(
        (RESOURCE_DIR / "agents" / "coordinator.json").read_text(encoding="utf-8")
    )

    assert "mcp_toolset" not in config
    toolsets = [
        tool for tool in config["tools"] if tool["type"] == "mcp_toolset"
    ]
    assert len(toolsets) == 1
    assert toolsets[0]["mcp_server_name"] == "hot-topics"
    assert toolsets[0]["default_config"] == {
        "permission_policy": {"type": "always_allow"}
    }


def test_coordinator_mcp_servers_and_toolsets_are_one_to_one():
    config = json.loads(
        (RESOURCE_DIR / "agents" / "coordinator.json").read_text(encoding="utf-8")
    )

    server_names = {server["name"] for server in config["mcp_servers"]}
    toolset_names = {
        tool["mcp_server_name"]
        for tool in config["tools"]
        if tool["type"] == "mcp_toolset"
    }
    assert server_names == toolset_names


# ---- 模型统一走 bmc-llm-relay(blueai 模型网关,BYOK)----

RELAY_AGENT_MODEL = {
    "id": "DeepSeek-V4-Flash-0731",
    "provider": "bmc-llm-relay",
    "base_url": "https://bmc-llm-relay.bluemediagroup.cn/v1/chat/completions",
    "protocol": "openai_chat_completions",
    # BYOK 必须带 token_limits;按模型原生规格:上下文(仅指输入)1M,输出上限 384K 另计
    "token_limits": {
        "context_window": 1000000,
        "max_input_token_length": 1000000,
        "max_output_token_length": 384000,
    },
}


def test_all_agents_use_bmc_llm_relay_model():
    for name in ("coordinator", "annotator", "insighter", "validator"):
        config = json.loads(
            (RESOURCE_DIR / "agents" / f"{name}.json").read_text(encoding="utf-8")
        )
        assert config["model"] == RELAY_AGENT_MODEL, name


def test_environment_routes_script_models_through_relay():
    env = json.loads((RESOURCE_DIR / "environment.json").read_text(encoding="utf-8"))["config"]["env"]
    assert env["LLM_RELAY_BASE_URL"] == "${LLM_RELAY_BASE_URL}"
    assert env["LLM_RELAY_API_KEY"] == "${LLM_RELAY_API_KEY}"
    # 沙箱脚本不再需要方舟凭据:模型走 relay,ARK_* 只在 Gateway/部署侧调控制面
    assert "ARK_API_KEY" not in env and "ARK_BASE_URL" not in env
    assert env["C2_CHAT_MODEL_ID"] == "Doubao-Seed-Evolving"
    assert env["EMBEDDING_MODEL_ID"] == "Doubao-embedding"
    assert env["EMBEDDING_IS_MULTIMODAL"] == "false"


def test_sandbox_scripts_do_not_fall_back_to_ark_credentials():
    """模型调用只读 LLM_RELAY_*;ARK_*/OPENAI_* 回退会静默改走方舟计费与配额。"""
    for rel in (
        "skills/topic6-insight/scripts/pipeline_e.py",
        "skills/topic6-annotation/scripts/run_demo_routes.py",
        "skills/topic6-event-registry/scripts/relay.py",
        "skills/topic6-fetch-normalize/scripts/fetch_hot_topics.py",
    ):
        source = (RESOURCE_DIR / rel).read_text(encoding="utf-8")
        for name in ("ARK_API_KEY", "ARK_BASE_URL", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
            assert f'environ.get("{name}"' not in source, (rel, name)


def _load_gateway():
    import importlib.util
    import sys

    case_dir = RESOURCE_DIR.parent
    if str(case_dir) not in sys.path:
        sys.path.insert(0, str(case_dir))
    spec = importlib.util.spec_from_file_location(
        "topic6_gateway_for_model_switch_test", case_dir / "gateway.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cfg(name="Authorization", value="Bearer sk-x"):
    from types import SimpleNamespace

    return SimpleNamespace(model_auth_header_name=name, model_auth_header_value=value)


def test_byok_header_dropped_for_native_model(tmp_path):
    """原生模型收到 X-User-Model-Header-* 会 400 InvalidModelHeader（2026-10-27 实测）。"""
    gateway = _load_gateway()
    agent = tmp_path / "coordinator.json"
    agent.write_text(json.dumps({"model": {"id": "deepseek-v4-1-flash-261017"}}), encoding="utf-8")
    assert gateway._byok_model_headers(_cfg(), agent) is None
    assert gateway._byok_model_headers(_cfg("", ""), agent) is None


def test_repo_coordinator_sends_byok_header():
    """仓库里的 coordinator.json 走 relay:配置了 BYOK_* 就必须附加 Header,否则 relay 401。"""
    gateway = _load_gateway()
    assert gateway._byok_model_headers(_cfg()) == {
        "X-User-Model-Header-Authorization": "Bearer sk-x"
    }


def test_byok_header_kept_for_byok_model(tmp_path):
    """干净特性：切回 BYOK 时只改 JSON，Header 自动恢复。"""
    gateway = _load_gateway()
    agent = tmp_path / "coordinator.json"
    agent.write_text(
        json.dumps({"model": {"id": "DeepSeek-V4-Flash-0731", "provider": "bmc-llm-relay"}}),
        encoding="utf-8",
    )
    assert gateway._byok_model_headers(_cfg(), agent) == {
        "X-User-Model-Header-Authorization": "Bearer sk-x"
    }
    with pytest.raises(RuntimeError, match="BYOK_MODEL_AUTH_HEADER"):
        gateway._byok_model_headers(_cfg("", ""), agent)


def test_agent_model_cost_is_priced():
    """Agent 模型 ID 必须在两份 PRICING 里命中,否则成本静默记 0。

    两份 PRICING 都是 "key 小写包含于 model 小写" 匹配；pipeline_e.py 依赖沙箱模块
    不能直接 import，这里按源码解析 PRICING 字面量再套同一匹配规则。"""
    import ast

    for rel in (
        "skills/topic6-event-registry/scripts/relay.py",
        "skills/topic6-insight/scripts/pipeline_e.py",
    ):
        tree = ast.parse((RESOURCE_DIR / rel).read_text(encoding="utf-8"))
        pricing = next(
            ast.literal_eval(node.value)
            for node in ast.walk(tree)
            if (isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "PRICING")
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "PRICING" for t in node.targets))
        )
        matched = [k for k in pricing if k.lower() in RELAY_AGENT_MODEL["id"].lower()]
        assert matched and pricing[matched[0]] != (0.0, 0.0), rel
