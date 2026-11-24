"""FIX-38：发布后原位修正（KS_Feishu_Format §十）。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feishu  # noqa: E402

OLD = "## 一、品牌传播表现\n\n声量 100 条。\n\n## 二、活动传播表现\n\nNSR +20.0%。\n"


class PlanPatchTest(unittest.TestCase):
    def test_text_change_is_patch(self):
        plan = feishu.plan_patch(OLD, OLD.replace("+20.0%", "+25.0%"))
        self.assertEqual(plan["mode"], "patch")
        self.assertEqual([o["op"] for o in plan["ops"]], ["replace_text"])

    def test_chapter_change_is_rebuild(self):
        plan = feishu.plan_patch(OLD, OLD + "\n## 三、经验总结\n\n内容。\n")
        self.assertEqual(plan["mode"], "rebuild")

    def test_identical_has_no_ops(self):
        self.assertEqual(feishu.plan_patch(OLD, OLD)["ops"], [])


class PatchDocumentTest(unittest.TestCase):
    def _fake(self, calls, children):
        def fake_req(method, path, token, body=None, timeout=30):
            calls.append((method, path, body))
            if method == "GET" and path.endswith("/blocks/doc1"):
                return {"block": {"children": list(children)}}
            if method == "POST" and path.endswith("/children"):
                return {"children": [{"block_id": "new"} for _ in body["children"]]}
            return {}
        return fake_req

    def test_replace_text_on_same_block(self):
        plan = feishu.plan_patch(OLD, OLD.replace("+20.0%", "+25.0%"))
        calls = []
        children = [f"b{i}" for i in range(4)]
        with patch("feishu._req", side_effect=self._fake(calls, children)):
            feishu.patch_document("t", "doc1", plan["ops"])
        patch_calls = [c for c in calls if c[0] == "PATCH"]
        self.assertEqual(len(patch_calls), 1)
        self.assertTrue(patch_calls[0][1].endswith("/blocks/b3"))
        self.assertIn("+25.0%", str(patch_calls[0][2]))

    def test_replace_image_uploads_then_patches(self):
        png = os.path.join(tempfile.mkdtemp(), "c.png")
        open(png, "wb").write(b"x")
        op = {"op": "replace_image", "index": 1, "block": {"block_type": 27, "_local_path": png}}
        calls = []
        with patch("feishu._req", side_effect=self._fake(calls, ["b0", "img"])), \
                patch("feishu._upload_media", return_value="ftok") as upload:
            feishu.patch_document("t", "doc1", [op])
        upload.assert_called_once()
        self.assertIn({"replace_image": {"token": "ftok"}}, [c[2] for c in calls if c[0] == "PATCH"])

    def test_container_replaced_at_same_position(self):
        op = {"op": "replace_block", "index": 2, "block": {"block_type": 2, "text": {"elements": []}}}
        calls = []
        with patch("feishu._req", side_effect=self._fake(calls, ["b0", "b1", "grid", "b3"])):
            feishu.patch_document("t", "doc1", [op])
        delete = next(c for c in calls if c[0] == "DELETE")
        self.assertEqual(delete[2], {"start_index": 2, "end_index": 3})
        insert = next(c for c in calls if c[0] == "POST" and c[1].endswith("/children"))
        self.assertEqual(insert[2]["index"], 2)

    def test_patch_failure_falls_back_to_rebuild(self):
        published = {"doc_token": "doc1", "markdown": OLD}
        new = OLD.replace("+20.0%", "+25.0%")
        with patch("feishu._tenant_token", return_value="t"), \
                patch("feishu.patch_document", side_effect=RuntimeError("api")), \
                patch("feishu._rebuild_in_place", return_value=0) as rebuild, \
                patch("feishu.grant_access", return_value={"transferred": True, "transfer_error": "",
                                                           "collaborator_added": False,
                                                           "collaborator_error": ""}), \
                patch("feishu._readback_check", return_value={"ok": True, "revision_id": 2}), \
                patch.dict(os.environ, {"FEISHU_ROOT_FOLDER_TOKEN": ""}, clear=False):
            result = feishu.create_delivery(new, title="T", task_id="t", report_version="1.1",
                                            published=published)
        rebuild.assert_called_once()
        self.assertEqual(result["doc_token"], "doc1")
        self.assertEqual(result["rework"]["mode"], "rebuild")
        self.assertIn("原位修正失败", result["rework"]["reason"])


if __name__ == "__main__":
    unittest.main()
