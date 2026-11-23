"""FIX-33：DataHub 不可用时先问用户，禁止自行降级。

原版依据：LM/错误案例与修正.md:302-306——数据量 ≥1000 走 DataHub，DATAHUB_API_KEY
未设置时「禁止自行降级，必须向用户确认 Key 来源或是否接受降级」。
只在需要 DataHub（行数 ≥ 阈值）的场景询问；行数低于阈值照常走小引擎。

运行：python3 -m unittest tests.test_annotation_route
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import annotate as A  # noqa: E402
import datahub  # noqa: E402
import handlers  # noqa: E402
import hc_bridge  # noqa: E402
import semantic  # noqa: E402
from handlers import HumanPause  # noqa: E402
from _util import load_dag, make_state  # noqa: E402

CTX = {"campaign_name": "A", "campaign_context": "B", "activity_keywords": "C"}


def _rows(n):
    return [{"id": f"x{i}", "title": "t", "content": f"c{i}"} for i in range(n)]


def _ok(_system, user, **_kw):
    return [{"id": item["id"], "sentiment": "正面", "campaign_relevant": "是"}
            for item in json.loads(user)]


class RouteTest(unittest.TestCase):
    def _run(self, n, *, threshold=1000, decision=None, available=False):
        with patch.object(datahub, "api_available", return_value=available), \
             patch.object(semantic, "llm_available", return_value=True), \
             patch.object(semantic, "chat_json", side_effect=_ok) as chat:
            result = A.annotate_round1(_rows(n), context=CTX, threshold=threshold,
                                       route_decision=decision)
        return result, chat

    def test_large_without_datahub_asks_user(self):
        with self.assertRaises(A.DataHubRouteRequired) as raised:
            self._run(1500)
        self.assertEqual(raised.exception.rows, 1500)
        self.assertIn("DATAHUB_API_KEY", str(raised.exception))
        self.assertIn("约 100 次调用", str(raised.exception))  # ceil(1500/15)

    def test_no_model_call_before_user_decides(self):
        with patch.object(datahub, "api_available", return_value=False), \
             patch.object(semantic, "chat_json") as chat:
            with self.assertRaises(A.DataHubRouteRequired):
                A.annotate_round1(_rows(1500), context=CTX)
        chat.assert_not_called()

    def test_small_dataset_goes_direct(self):
        result, chat = self._run(500)
        self.assertEqual(result["routed_via"], "small_engine")
        self.assertTrue(chat.called)

    def test_user_confirmed_small_engine_for_large(self):
        result, _ = self._run(1500, decision=A.ROUTE_SMALL_ENGINE)
        self.assertEqual(result["routed_via"], "small_engine")

    def test_datahub_decision_still_requires_datahub(self):
        """用户选择「补 Key」时仍要求 DataHub，不会被当作同意降级。"""
        with self.assertRaises(A.DataHubRouteRequired):
            self._run(1500, decision="datahub")


class HandlerTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws, params={"annotation_routing_threshold": 2})
        self.st.write_artifact("cleaned_data", {"rows": _rows(3)})
        self.st.write_artifact("confirmed_plan", {
            "activity_name": "A活动", "activity_query": "A+B", "activity_boundary": "背景"})
        self.node = {"id": "n09_annotate_r1", "outputs": ["annotated_round1"]}

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_handler_pauses_with_decision_expected(self):
        with patch.object(datahub, "api_available", return_value=False):
            with self.assertRaises(HumanPause) as raised:
                handlers.annotate(self.node, self.st, {})
        self.assertEqual(raised.exception.expect, ["annotation_route_decision"])
        self.assertIn("DataHub", raised.exception.prompt)

    def test_handler_honors_confirmed_decision(self):
        self.st.write_artifact("annotation_route_decision", {"route": "small_engine"})
        with patch.object(datahub, "api_available", return_value=False), \
             patch.object(semantic, "llm_available", return_value=True), \
             patch.object(semantic, "chat_json", side_effect=_ok):
            out = handlers.annotate(self.node, self.st, {})
        result = out["artifacts"]["annotated_round1"]
        self.assertEqual(result["routed_via"], "small_engine")
        self.assertEqual(result["route_decision"], "small_engine")


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws)
        self.st.human = {"node": "n09_annotate_r1"}

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_confirm_writes_small_engine_and_reruns_node(self):
        outs = hc_bridge.build_human_outputs(self.st, "pass", "同意直连")
        self.assertEqual(outs["annotation_route_decision"]["route"], "small_engine")
        self.assertEqual(outs["_control"]["next"], "n09_annotate_r1")

    def test_reject_keeps_datahub(self):
        outs = hc_bridge.build_human_outputs(self.st, "reject", "我去配置 Key")
        self.assertEqual(outs["annotation_route_decision"]["route"], "datahub")
        self.assertEqual(outs["_control"]["next"], "n09_annotate_r1")


class DagTest(unittest.TestCase):
    def test_n09_pause_capable(self):
        self.assertTrue(load_dag().nodes["n09_annotate_r1"].get("pause_capable"))


if __name__ == "__main__":
    unittest.main()
