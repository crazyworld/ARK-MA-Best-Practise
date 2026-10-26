from __future__ import annotations

import json
from pathlib import Path


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


# ---- 方舟原生模型切换（BYOK relay 不稳定时）----

NATIVE_AGENT_MODEL = "deepseek-v4-1-flash-261017"


def test_all_agents_use_ark_native_model_without_byok_fields():
    """方舟原生模型不接受 token_limits（400: cannot be specified for ark provider），
    provider/base_url/protocol 也必须一起去掉，否则仍走 BYOK。"""
    for name in ("coordinator", "annotator", "insighter", "validator"):
        config = json.loads(
            (RESOURCE_DIR / "agents" / f"{name}.json").read_text(encoding="utf-8")
        )
        assert config["model"] == {"id": NATIVE_AGENT_MODEL}, name


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
    agent.write_text(json.dumps({"model": {"id": NATIVE_AGENT_MODEL}}), encoding="utf-8")
    assert gateway._byok_model_headers(_cfg(), agent) is None
    # 仓库里的真实 coordinator.json 也必须得到 None
    assert gateway._byok_model_headers(_cfg()) is None


def test_byok_header_kept_for_byok_model(tmp_path):
    """干净特性：切回 BYOK 时只改 JSON，Header 自动恢复。"""
    gateway = _load_gateway()
    agent = tmp_path / "coordinator.json"
    agent.write_text(
        json.dumps({"model": {"id": "DeepSeek-V4.1-Flash", "provider": "bmc-llm-relay"}}),
        encoding="utf-8",
    )
    assert gateway._byok_model_headers(_cfg(), agent) == {
        "X-User-Model-Header-Authorization": "Bearer sk-x"
    }
    assert gateway._byok_model_headers(_cfg("", ""), agent) is None


def test_native_model_cost_is_priced():
    """原生 ID 小写后不含 'deepseek-v4.1-flash'，没有新 key 会被记成 0 成本。

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
        matched = [k for k in pricing if k.lower() in NATIVE_AGENT_MODEL.lower()]
        assert matched and pricing[matched[0]] != (0.0, 0.0), rel
