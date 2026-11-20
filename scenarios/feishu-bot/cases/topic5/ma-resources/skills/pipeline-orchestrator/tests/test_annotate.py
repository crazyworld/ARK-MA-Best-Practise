"""波次5：AI 标注接真（annotate：轮次一 n09 / 轮次二 n11）契约测试。

无 DataHub / 无 Ark 凭据时走确定性降级（degraded=True），验证：
  - 轮次一回填 ai_sentiment ∈ {1,0,-1} + relevance，出口被 aggregate 无差别消费；
  - 轮次二三独立任务(opinion/ugc_category/discussion_dimension)全覆盖、N/A 合法不算缺口；
  - 覆盖检查 ERROR（真路径缺口）→ handler raise；降级不 raise；
  - 路由：行数 < 阈值走小引擎/降级；≥ 阈值且 DataHub 可用走批量（此处 monkeypatch 验证）。
  - datahub 适配：CSV 无 BOM、llm_result JSON 解析、行级 _error 不抛。
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
import shutil
import unittest
from unittest.mock import patch

import annotate as annotate_mod
import datahub
import handlers
from _util import make_state


def _node(node_id, outputs):
    return {"id": node_id, "name": node_id, "outputs": outputs}


def _ctx():
    """轮次一标注上下文（FIX-27 必填三项）。"""
    return {
        "campaign_name": "立白×测试联名活动",
        "campaign_context": "活动边界：仅统计本次联名；统计窗口：2024-05-01 ~ 2024-05-31",
        "activity_keywords": "立白+(联名|测试)",
        "campaign_type": "IP联名",
    }


def _rows(n=6):
    out = []
    for i in range(n):
        out.append({
            "_dedup_key": f"k{i}", "content": f"立白活动很好用，功效不错 {i}",
            "title": f"标题{i}", "sentiment": (1, 0, -1)[i % 3],
            "interaction_cnt": 100 + i, "_platform": "weibo",
            "_publish_ymd": "20240501",
        })
    return out


class TestRound1Fallback(unittest.TestCase):
    def test_fallback_fills_sentiment_and_relevance(self):
        rows = _rows(6)
        res = annotate_mod.annotate_round1(rows, context=_ctx(), threshold=1000)
        self.assertEqual(res["round"], 1)
        self.assertTrue(res.get("degraded"))
        self.assertEqual(res["routed_via"], "fallback")
        for r in res["rows"]:
            self.assertIn(r["ai_sentiment"], (1, 0, -1))
            self.assertIn("relevance", r)
        cov = res["coverage"]
        self.assertEqual(cov["sentiment_missing"], 0)
        self.assertEqual(cov["relevance_missing"], 0)

    def test_round1_output_consumed_by_aggregate(self):
        """轮次一降级产物直接喂 aggregate：缺 AI 情感口径下不报错、产 stats_bundle。"""
        ws = tempfile.mkdtemp()
        try:
            st = make_state(ws)
            res = annotate_mod.annotate_round1(_rows(6), context=_ctx(), threshold=1000)
            st.write_artifact("annotated_data", {"rows": res["rows"]})
            st.write_artifact("confirmed_plan", {"brand": "立白",
                              "brand_words": ["立白"], "campaigns": [{"id": "c1"}]})
            out = handlers.aggregate(_node("n12", ["stats_bundle"]), st, {})
            sb = out["artifacts"]["stats_bundle"]
            self.assertIn("c1", sb["campaigns"])
            self.assertIn("NSR", sb["campaigns"]["c1"]["metrics"])
        finally:
            shutil.rmtree(ws, ignore_errors=True)


class TestRound2Fallback(unittest.TestCase):
    def test_three_tasks_covered(self):
        rows = _rows(6)
        res = annotate_mod.annotate_round2(
            rows, opinion_labels=["正面体验", "价格讨论"],
            ugc_categories=["测评分享", "晒单分享"], threshold=1000)
        self.assertEqual(res["round"], 2)
        self.assertTrue(res.get("degraded"))
        cov = res["coverage"]
        for task in ("opinion", "ugc_category", "discussion_dimension"):
            self.assertEqual(cov[task]["missing"], 0)
            self.assertEqual(cov[task]["status"], "DEGRADED")
        self.assertFalse(cov["has_error"])
        for r in res["rows"]:
            for task in ("opinion", "ugc_category", "discussion_dimension"):
                self.assertIn(task, r)

    def test_coverage_error_when_missing_true_path(self):
        """真路径（非降级）下存在 missing → check_coverage 标 ERROR + has_error。"""
        rows = _rows(4)
        for r in rows:
            r["relevance"] = True
            r["ai_sentiment"] = 1
        rows[0]["opinion"] = "正面体验"  # 仅一行有标签，其余缺口
        cov = annotate_mod.check_coverage(rows, degraded=False)
        self.assertEqual(cov["opinion"]["status"], "ERROR")
        self.assertTrue(cov["has_error"])

    def test_na_counts_as_labeled(self):
        rows = _rows(3)
        for r in rows:
            r["relevance"] = True
            r["ai_sentiment"] = 1
            r["opinion"] = "N/A"
            r["ugc_category"] = "N/A"
            r["discussion_dimension"] = "N/A"
        cov = annotate_mod.check_coverage(rows, degraded=False)
        self.assertEqual(cov["opinion"]["missing"], 0)
        self.assertEqual(cov["opinion"]["na"], 3)
        self.assertEqual(cov["opinion"]["status"], "OK")
        self.assertFalse(cov["has_error"])

    def test_task_prompts_declare_their_own_fields(self):
        """FIX-28：每个子任务只声明自己的字段，且维度固定三枚举。"""
        prompts = annotate_mod._r2_task_prompts(
            "立白联名", ["正面体验"], ["测评分享"], False, None, None)
        self.assertIn("opinion", prompts["opinion"])
        self.assertIn("正面", prompts["opinion"])
        self.assertNotIn("ugc_category", prompts["opinion"])
        self.assertIn("ugc_category", prompts["attr"])
        self.assertIn("discussion_dimension", prompts["attr"])
        for dim in ("品牌", "产品", "活动"):
            self.assertIn(dim, prompts["attr"])
        self.assertNotIn("功效/价格/包装", prompts["attr"])

    def test_apply_opinion_blank_leaves_missing(self):
        """FIX-28：留空即 missing；只有代码过滤得到的 N/A 才是合法 N/A。"""
        row = {"_annot_id": "a1"}
        annotate_mod._apply_opinion(row, {"opinion": ""}, ["正面体验"])
        self.assertNotIn("opinion", row)


class TestHandlerDispatch(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_handler_round1_from_cleaned(self):
        st = make_state(self.ws, params={"annotation_routing_threshold": 1000})
        st.write_artifact("cleaned_data", {"rows": _rows(5)})
        st.write_artifact("confirmed_plan", {
            "activity_name": "立白×测试联名活动", "activity_query": "立白+(联名|测试)",
            "period_start": "2024-05-01", "period_end": "2024-05-31",
            "activity_boundary": "仅统计本次联名", "campaign_type": "IP联名"})
        with self.assertRaisesRegex(RuntimeError, "禁止确定性伪标注"):
            handlers.annotate(_node("n09_annotate_r1", ["annotated_round1"]), st, {})

    def test_handler_round2_from_round1(self):
        st = make_state(self.ws, params={"annotation_routing_threshold": 1000})
        r1 = annotate_mod.annotate_round1(_rows(5), context=_ctx(), threshold=1000)
        st.write_artifact("annotated_round1", {"rows": r1["rows"]})
        st.write_artifact("confirmed_opinion_labels", {"labels": ["正面体验", "价格讨论"]})
        st.write_artifact("ugc_category_list", {"categories": ["测评分享", "晒单分享"]})
        with self.assertRaisesRegex(RuntimeError, "禁止确定性伪标注"):
            handlers.annotate(_node("n11_annotate_r2", ["annotated_data"]), st, {})

    def test_handler_round1_rejects_incomplete_true_coverage(self):
        st = make_state(self.ws, params={"annotation_routing_threshold": 1000})
        st.write_artifact("cleaned_data", {"rows": _rows(2)})
        st.write_artifact("confirmed_plan", {
            "activity_name": "立白×测试联名活动", "activity_query": "立白+(联名|测试)"})
        result = {
            "rows": _rows(2),
            "round": 1,
            "routed_via": "small_engine",
            "coverage": {
                "sentiment_missing": 1,
                "relevance_missing": 0,
            },
        }
        with patch("annotate.annotate_round1", return_value=result):
            with self.assertRaisesRegex(ValueError, "sentiment_missing=1"):
                handlers.annotate(
                    _node("n09_annotate_r1", ["annotated_round1"]),
                    st,
                    {},
                )


class TestRouting(unittest.TestCase):
    def test_small_engine_r1_retries_only_missing_rows(self):
        rows = _rows(2)
        annotate_mod._tag_ids(rows)
        requests = []

        def fake_chat_json(_system, user, **_kwargs):
            payload = __import__("json").loads(user)
            requests.append([item["id"] for item in payload])
            if len(requests) == 1:
                return [{
                    "id": rows[0]["_annot_id"],
                    "sentiment": "正面",
                    "relevant": True,
                }]
            return [{
                "id": rows[1]["_annot_id"],
                "sentiment": "中性",
                "relevant": True,
            }]

        with patch("semantic.chat_json", side_effect=fake_chat_json):
            annotate_mod._small_engine_r1(rows, _ctx())

        self.assertEqual(requests[0], ["k0", "k1"])
        self.assertEqual(requests[1], ["k1"])
        self.assertEqual([row["ai_sentiment"] for row in rows], [1, 0])

    def test_run_task_retries_only_missing_rows(self):
        """FIX-28：子任务只重试仍缺失的行。"""
        rows = _rows(2)
        for r in rows:
            r["relevance"] = True
            r["ai_sentiment"] = 1
        annotate_mod._tag_ids(rows)
        requests = []

        def fake_chat_json(_system, user, **_kwargs):
            payload = __import__("json").loads(user)
            requests.append([item["id"] for item in payload])
            target = rows[0] if len(requests) == 1 else rows[1]
            return [{"id": target["_annot_id"], "opinion": "正面体验"}]

        with patch("semantic.chat_json", side_effect=fake_chat_json):
            annotate_mod._run_task(
                rows, prompt="p",
                apply_fn=lambda row, parsed: annotate_mod._apply_opinion(
                    row, parsed, ["正面体验"]),
                is_missing=lambda row: not str(row.get("opinion") or "").strip())

        self.assertEqual(requests[0], ["k0", "k1"])
        self.assertEqual(requests[1], ["k1"])
        for r in rows:
            self.assertEqual(r["opinion"], "正面体验")

    def test_invalid_discussion_dimension_kept_missing(self):
        """FIX-14：枚举外的讨论维度（如「价格」）视为缺失，交覆盖检查判 ERROR。"""
        row = {"title": "t", "content": "c"}
        annotate_mod._apply_r2(row, {"discussion_dimension": "价格"})
        self.assertNotIn("discussion_dimension", row)
        annotate_mod._apply_r2(row, {"discussion_dimension": "品牌"})
        self.assertEqual(row["discussion_dimension"], "品牌")
        annotate_mod._apply_r2(row, {"discussion_dimension": "N/A"})
        self.assertEqual(row["discussion_dimension"], "N/A")

    def test_route_datahub_when_over_threshold(self):
        """行数 ≥ 阈值且 DataHub 可用 → 走批量；monkeypatch 避免真实网络。"""
        calls = {"dh": 0}
        orig_avail = datahub.api_available
        orig_batch = datahub.batch_annotate
        datahub.api_available = lambda: True

        def fake_batch(rows, prompt, **kw):
            calls["dh"] += 1
            return {r["_annot_id"]: {"sentiment": "正面", "relevant": True} for r in rows}

        datahub.batch_annotate = fake_batch
        try:
            rows = _rows(3)
            res = annotate_mod.annotate_round1(rows, context=_ctx(), threshold=2)  # 3 >= 2
            self.assertEqual(res["routed_via"], "datahub")
            self.assertEqual(calls["dh"], 1)
            for r in res["rows"]:
                self.assertEqual(r["ai_sentiment"], 1)
                self.assertTrue(r["relevance"])
        finally:
            datahub.api_available = orig_avail
            datahub.batch_annotate = orig_batch

    def test_datahub_failure_falls_back(self):
        orig_avail = datahub.api_available
        orig_batch = datahub.batch_annotate
        datahub.api_available = lambda: True

        def boom(rows, prompt, **kw):
            raise RuntimeError("datahub task failed")

        datahub.batch_annotate = boom
        try:
            res = annotate_mod.annotate_round1(_rows(3), context=_ctx(), threshold=2)
            # DataHub 抛错 → 无 Ark 凭据则降级（本地无凭据场景）。
            self.assertIn(res["routed_via"], ("fallback", "small_engine"))
        finally:
            datahub.api_available = orig_avail
            datahub.batch_annotate = orig_batch


class TestDatahubAdapter(unittest.TestCase):
    def test_csv_utf8_no_bom(self):
        rows = _rows(2)
        annotate_mod._tag_ids(rows)
        path = datahub._rows_to_csv(rows, ["content", "title"])
        try:
            with open(path, "rb") as f:
                head = f.read(3)
            self.assertNotEqual(head, b"\xef\xbb\xbf")  # 无 BOM
            with open(path, encoding="utf-8") as f:
                header = f.readline().strip()
            self.assertTrue(header.startswith("id,"))
        finally:
            os.remove(path)

    def test_parse_llm_result_variants(self):
        self.assertEqual(datahub._parse_llm_result('{"sentiment":"正面"}')["sentiment"], "正面")
        self.assertEqual(
            datahub._parse_llm_result('```json\n{"relevant":true}\n```')["relevant"], True)
        self.assertIn("_error", datahub._parse_llm_result(""))
        self.assertIn("_error", datahub._parse_llm_result("not json at all"))


if __name__ == "__main__":
    unittest.main()
