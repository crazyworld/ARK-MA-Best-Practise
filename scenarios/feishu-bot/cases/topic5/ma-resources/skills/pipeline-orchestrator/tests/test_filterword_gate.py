"""活动过滤词核对（n04）：剔除词与活动词分离；用户决定后重跑 n04 并真正生效。

回归：活动过滤词留空时，代码拿活动关键词当剔除词，1024 行全部命中；
且人工决定后直接跳到 n05，run_record/resource_lock 从未产出，用户的选择也不生效。
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../../.."))

import shutil
import tempfile
import unittest

import engine
import handlers
import hc_bridge
import topic5_intake as intake
from _util import load_dag, make_state

FORM = {
    "brand": "沃尔玛", "activity_name": "玩总联名", "period_start": "2026-06-01",
    "period_end": "2026-06-30", "platforms": ["小红书"], "brand_query": "沃尔玛",
    "activity_query": "沃尔玛+玩具总动员", "activity_filter": "", "content_scope": "all",
}
ROWS = [
    {"title": "沃尔玛玩具总动员", "content": "好吃", "topics": []},
    {"title": "沃尔玛玩具总动员", "content": "招聘兼职", "topics": []},
]


class FilterwordScopeTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_empty_activity_filter_is_not_checked(self):
        plan = intake.normalize_requirement(FORM)
        result = handlers._check_activity_filterwords(plan, ROWS, self.st)
        self.assertEqual(result["decision"], "no_hits")

    def test_activity_filter_hits_pause_for_user(self):
        plan = intake.normalize_requirement({**FORM, "activity_filter": "兼职"})
        with self.assertRaises(handlers.HumanPause) as caught:
            handlers._check_activity_filterwords(plan, ROWS, self.st)
        self.assertIn("1 条", caught.exception.prompt)

    def test_campaign_level_filter_overrides_plan_filter(self):
        plan = intake.normalize_requirement({**FORM, "activity_filter": "不存在的词", "campaign_lines": [
            "玩总联名；2026-06-01；2026-06-30；沃尔玛+玩具总动员；兼职"]})
        self.assertEqual(plan["campaigns"][0]["activity_filter"], "兼职")
        with self.assertRaises(handlers.HumanPause):
            handlers._check_activity_filterwords(plan, ROWS, self.st)

    def test_exclude_removes_only_filter_hits(self):
        plan = intake.normalize_requirement({**FORM, "activity_filter": "兼职"})
        self.st.write_artifact("filterword_decision", {"decision": "exclude"})
        kept, removed = handlers._apply_filterword_decision(plan, list(ROWS), self.st)
        self.assertEqual((len(kept), removed), (1, 1))

    def test_exclude_with_empty_filter_removes_nothing(self):
        plan = intake.normalize_requirement(FORM)
        self.st.write_artifact("filterword_decision", {"decision": "exclude"})
        kept, removed = handlers._apply_filterword_decision(plan, list(ROWS), self.st)
        self.assertEqual((len(kept), removed), (2, 0))


class FilterwordResumeTest(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.st = make_state(self.ws, current_node="n04_run_init")
        self.st.status = "awaiting_human"
        self.st.human = {"node": "n04_run_init", "prompt": "x", "expect": ["filterword_decision"]}

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_keep_and_exclude_are_recorded(self):
        for decision in ("keep", "exclude"):
            out = hc_bridge.build_human_outputs(self.st, decision, "")
            self.assertEqual(out["filterword_decision"]["decision"], decision)

    def test_ambiguous_decision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "keep|exclude"):
            hc_bridge.build_human_outputs(self.st, "pass", "")

    def test_resume_reruns_n04_instead_of_skipping_it(self):
        ran = []

        def fake_init(node, st, ctx):
            ran.append(node["id"])
            raise RuntimeError("stop here")

        original = handlers._REGISTRY["run_init"]
        handlers._REGISTRY["run_init"] = fake_init
        try:
            self.st.write_artifact("confirmed_plan", {"confirmed": True})
            self.st.frontier = []
            engine.resume(load_dag(), self.st,
                          human_outputs=hc_bridge.build_human_outputs(self.st, "keep", ""))
        finally:
            handlers._REGISTRY["run_init"] = original
        self.assertIn("n04_run_init", ran)

    def test_gate_has_a_card_kind(self):
        self.assertEqual(hc_bridge.hc_kind_for_node("n04_run_init"), "HC_FILTER")

    def test_new_data_requires_a_new_decision(self):
        self.assertIn("filterword_decision", hc_bridge.REDO_INVALIDATE)


if __name__ == "__main__":
    unittest.main()
