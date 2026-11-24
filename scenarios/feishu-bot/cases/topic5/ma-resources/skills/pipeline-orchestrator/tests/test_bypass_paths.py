"""旁路/失败路径的 DAG 可达性（回归：n06 旁路后 n12 卡死 stuck）。

engine 的 exception_bypass 原先不写产物，下游 inputs 永不就绪 → Run stuck，
n12 里「基准不可用 → DataIssue + 拉升不可用」的降级分支永远到不了。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import handlers  # noqa: E402
from _util import compliant_pipeline_services, default_human_outputs, load_dag  # noqa: E402


def _run_with_failing(node_id: str):
    dag = load_dag()
    st = engine.create_run(dag, Path(tempfile.mkdtemp()), "t", "r")
    st.params["run_mode"] = "full"
    with compliant_pipeline_services():
        original = handlers._REGISTRY["ssjh_pull"]

        def failing(node, state, ctx):
            if node["id"] == node_id:
                raise RuntimeError("controlled-stat-tool down")
            return original(node, state, ctx)

        handlers._REGISTRY["ssjh_pull"] = failing
        engine.run(dag, st)
        steps = 0
        while st.status == "awaiting_human" and steps < 40:
            engine.resume(dag, st, human_outputs=default_human_outputs(st, st.human["node"]))
            steps += 1
    return st


class BypassPathTest(unittest.TestCase):
    def test_baseline_bypass_reaches_completion(self):
        st = _run_with_failing("n06_brand_baseline")
        self.assertEqual(st.nodes["n06_brand_baseline"]["status"], "bypassed")
        self.assertFalse([e for e in st.log if e.get("event") == "stuck"])
        self.assertEqual(st.status, "completed")
        baseline = st.read_artifact("brand_stats_baseline")
        self.assertTrue(baseline["unavailable"])
        self.assertIn("n06_brand_baseline bypassed", baseline["meta"]["data_issue"])

    def test_baseline_placeholder_drives_data_issue(self):
        """真实 aggregate 读到占位产物 → baseline unavailable + DataIssue。"""
        placeholder = engine.bypass_payload("n06_brand_baseline", RuntimeError("down"))
        from _util import make_state
        st = make_state(tempfile.mkdtemp())
        st.write_artifact("annotated_data", {"rows": [
            {"relevance": True, "ai_sentiment": 1, "interaction_cnt": 1}]})
        st.write_artifact("brand_stats_baseline", placeholder)
        bundle = handlers.aggregate({"id": "n12"}, st, {})["artifacts"]["stats_bundle"]
        self.assertEqual(bundle["baseline"]["status"], "unavailable")
        self.assertEqual(bundle["baseline_lift"]["status"], "unavailable")
        self.assertEqual(bundle["data_issues"][0]["affected_module"], "baseline_lift")

    def test_wordcloud_failure_pauses_not_stuck(self):
        st = _run_with_failing("n13_wordcloud_pull")
        self.assertEqual(st.status, "failed")
        self.assertEqual(st.nodes["n13_wordcloud_pull"]["status"], "failed")
        self.assertFalse([e for e in st.log if e.get("event") == "stuck"])


if __name__ == "__main__":
    unittest.main()
