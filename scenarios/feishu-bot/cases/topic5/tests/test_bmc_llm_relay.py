"""topic5 模型资源统一走 bmc-llm-relay（对齐 topic6 2026-11-03 方案）。

回归：沙箱语义调用走方舟模型 doubao-seed-2-1-lite-ultra-vision-260920 返回 404，n07b 失败；
Agent 用方舟原生 doubao-seed-evolving。改为：沙箱只读 LLM_RELAY_*（不回退 ARK_*），
直连模型按原版 claude-haiku-4-5-20251001，DataHub 传 gpt-5.4-mini；两个 Agent 走 BYOK
DeepSeek-V4.1-Flash，Gateway 每条 user.message 带 X-User-Model-Header-*。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_CASE = Path(__file__).resolve().parent.parent
RES = _CASE / "ma-resources"
ORCH = RES / "skills" / "pipeline-orchestrator"
for p in (str(_CASE), str(ORCH)):
    if p not in sys.path:
        sys.path.insert(0, p)

import config  # noqa: E402
import test_host_stdlib as _base  # noqa: E402


def _env():
    return json.loads((RES / "environment.json").read_text(encoding="utf-8"))["config"]["env"]


class SandboxRelayTest(unittest.TestCase):
    def test_environment_injects_relay_not_ark(self):
        env = _env()
        self.assertEqual(env["LLM_RELAY_BASE_URL"], "${LLM_RELAY_BASE_URL}")
        self.assertEqual(env["LLM_RELAY_API_KEY"], "${LLM_RELAY_API_KEY}")
        for key in ("ARK_API_KEY", "ARK_BASE_URL"):
            self.assertNotIn(key, env)

    def test_models_follow_original(self):
        env = _env()
        self.assertEqual(env["TOPIC5_SEMANTIC_MODEL"], "claude-haiku-4-5-20251001")
        self.assertEqual(env["DATAHUB_ANNOTATE_MODEL"], "gpt-5.4-mini")
        self.assertNotIn("doubao", json.dumps(env).lower())

    def test_semantic_reads_relay_only(self):
        import semantic
        with patch.dict(os.environ, {"ARK_BASE_URL": "https://ark", "ARK_API_KEY": "k",
                                     "OPENAI_BASE_URL": "https://o", "OPENAI_API_KEY": "k"}, clear=True):
            self.assertFalse(semantic.llm_available())
        with patch.dict(os.environ, {"LLM_RELAY_BASE_URL": "https://relay/v1/",
                                     "LLM_RELAY_API_KEY": "rk"}, clear=True):
            self.assertTrue(semantic.llm_available())
            self.assertEqual(semantic._base_key(), ("https://relay/v1", "rk"))
            self.assertEqual(semantic._model(), "claude-haiku-4-5-20251001")

    def test_request_has_no_ark_only_thinking_param(self):
        import semantic
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def __iter__(self):
                return iter([b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                             b"data: [DONE]\n"])

        def fake(req, timeout=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return Resp()

        with patch.dict(os.environ, {"LLM_RELAY_BASE_URL": "https://relay/v1",
                                     "LLM_RELAY_API_KEY": "rk"}, clear=True), \
             patch.object(semantic.urllib.request, "urlopen", side_effect=fake):
            self.assertEqual(semantic.chat("s", "u", thinking="disabled"), "ok")
        self.assertEqual(captured["url"], "https://relay/v1/chat/completions")
        self.assertNotIn("thinking", captured["body"])


class AgentByokTest(unittest.TestCase):
    def test_agents_use_relay_like_topic6(self):
        topic6 = json.loads((_CASE.parent / "topic6" / "ma-resources" / "agents" / "coordinator.json")
                            .read_text(encoding="utf-8"))["model"]
        for name in ("coordinator.json", "validator.json"):
            model = json.loads((RES / "agents" / name).read_text(encoding="utf-8"))["model"]
            self.assertEqual(model, topic6, name)

    def test_config_reads_byok_header(self):
        cfg = config.load_case_config({
            "ARK_API_KEY": "ak", "FEISHU_APP_ID": "a", "FEISHU_APP_SECRET": "s",
            "TOPIC5_COORDINATOR_AGENT_ID": "agent", "TOPIC5_ENVIRONMENT_ID": "env",
            "BYOK_MODEL_AUTH_HEADER_NAME": "Authorization", "BYOK_MODEL_AUTH_HEADER_VALUE": "Bearer x"})
        self.assertEqual((cfg.model_auth_header_name, cfg.model_auth_header_value),
                         ("Authorization", "Bearer x"))

    def test_gateway_header_required_when_agent_uses_relay(self):
        gateway_src = (_CASE / "gateway.py").read_text(encoding="utf-8")
        self.assertIn("def _byok_model_headers", gateway_src)
        self.assertIn("extra_headers=_byok_model_headers(config)", gateway_src)

    def test_every_session_message_carries_header(self):
        holder = types.SimpleNamespace()
        _base.RunnerFinalResultParsingTest.setUpClass.__func__(holder)
        runner_mod = holder.runner
        src = (_CASE / "topic5_runner.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("self._ark.send_message("), 1)
        self.assertIn("extra_headers=self._extra_headers", src)
        sent = []

        class Ark:
            async def send_message(self, sid, text, extra_headers=None):
                sent.append(extra_headers)

        runner = runner_mod.Topic5Runner(Ark(), object(), object(),
                                         runner_mod.Topic5Config("a", "e"),
                                         extra_headers={"X-User-Model-Header-Authorization": "v"})
        asyncio.run(runner._send_message("s", "hi"))
        self.assertEqual(sent, [{"X-User-Model-Header-Authorization": "v"}])


class SharedTopicXConfigTest(unittest.TestCase):
    """topic5/topic6 共用 ~/.arkagent/cases/topicX/config.env；topic5 专属的飞书 Bot 以
    TOPIC5_FEISHU_APP_ID/SECRET 区分，未设置时回退通用 FEISHU_APP_*（不影响 topic6）。"""

    BASE = {"ARK_API_KEY": "ak", "FEISHU_APP_ID": "cli_topic6", "FEISHU_APP_SECRET": "s6",
            "TOPIC5_COORDINATOR_AGENT_ID": "agent", "TOPIC5_ENVIRONMENT_ID": "env"}

    def test_prefixed_feishu_bot_wins(self):
        cfg = config.load_case_config({**self.BASE, "TOPIC5_FEISHU_APP_ID": "cli_topic5",
                                       "TOPIC5_FEISHU_APP_SECRET": "s5"})
        self.assertEqual((cfg.feishu_app_id, cfg.feishu_app_secret), ("cli_topic5", "s5"))

    def test_falls_back_to_generic_feishu_bot(self):
        cfg = config.load_case_config(self.BASE)
        self.assertEqual((cfg.feishu_app_id, cfg.feishu_app_secret), ("cli_topic6", "s6"))

    def test_deploy_maps_prefixed_bot_into_sandbox(self):
        script = (_CASE / "update_ma.sh").read_text(encoding="utf-8")
        source = script.index('source "$CONFIG_FILE"')
        mapping = script.index('FEISHU_APP_ID="${TOPIC5_FEISHU_APP_ID:-${FEISHU_APP_ID:-}}"')
        check = script.index("for name in \\\n  ARK_API_KEY")
        self.assertLess(source, mapping)
        self.assertLess(mapping, check)
        self.assertIn('FEISHU_APP_SECRET="${TOPIC5_FEISHU_APP_SECRET:-${FEISHU_APP_SECRET:-}}"', script)
        for name in ("TOPIC5_FEISHU_APP_ID", "TOPIC5_FEISHU_APP_SECRET"):
            self.assertIn(name, script.split("ISOLATED_TEST_ENV_KEYS=(", 1)[1].split("\n)", 1)[0])


class DeployScriptTest(unittest.TestCase):
    def test_deploy_requires_relay_and_byok(self):
        script = (_CASE / "update_ma.sh").read_text(encoding="utf-8")
        for name in ("LLM_RELAY_BASE_URL", "LLM_RELAY_API_KEY",
                     "BYOK_MODEL_AUTH_HEADER_NAME", "BYOK_MODEL_AUTH_HEADER_VALUE"):
            self.assertIn(name, script.split("ISOLATED_TEST_ENV_KEYS=(")[0])
            self.assertIn(name, script.split("ISOLATED_TEST_ENV_KEYS=(", 1)[1].split("\n)", 1)[0])

    def test_model_probe_uses_relay(self):
        script = (RES / "create_all.sh").read_text(encoding="utf-8")
        probe = script[script.index("# ---- 沙箱模型可达性探测 ----"):]
        probe = probe[:probe.index("\ndone")]
        self.assertIn("${LLM_RELAY_BASE_URL%/}/chat/completions", probe)
        self.assertIn("$LLM_RELAY_API_KEY", probe)
        self.assertIn("DATAHUB_ANNOTATE_MODEL", script)


if __name__ == "__main__":
    unittest.main()
