"""FIX-41：项目文件夹（查找/创建复用、先转文件夹再转文档、降级与 P0/P1）。"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feishu  # noqa: E402
import handlers  # noqa: E402


def _fake(existing=None, folder_transfer_ok=True, folder_collab_ok=True, order=None):
    order = order if order is not None else []

    def fake_req(method, path, token, body=None, timeout=30):
        if method == "GET" and path.startswith("/open-apis/drive/v1/files?"):
            return {"files": existing or [], "has_more": False}
        if path.endswith("/create_folder"):
            order.append("create_folder")
            return {"token": "fld_new"}
        if method == "POST" and path.endswith("/documents"):
            order.append(("doc", body.get("folder_token")))
            return {"document": {"document_id": "doc1"}}
        if "transfer_owner" in path:
            kind = "folder" if "type=folder" in path else "docx"
            order.append(("transfer", kind))
            if kind == "folder" and not folder_transfer_ok:
                raise RuntimeError("denied")
            return {}
        if "/members?type=folder" in path:
            if not folder_collab_ok:
                raise RuntimeError("denied")
            return {}
        if method == "GET" and path.endswith("/documents/doc1"):
            return {"document": {"revision_id": 1}}
        if method == "GET" and "/blocks" in path:
            return {"items": []}
        return {}
    return fake_req


class FolderTest(unittest.TestCase):
    def _run(self, fake, root="fld_root"):
        env = {"FEISHU_USER_OPEN_ID": "ou_x", "FEISHU_ROOT_FOLDER_TOKEN": root}
        with patch.dict(os.environ, env, clear=False), \
                patch("feishu._tenant_token", return_value="t"), patch("feishu._req", side_effect=fake):
            return feishu.create_delivery("", title="T", task_id="t", report_version="1.0",
                                          project_name="瑞幸联名")

    def test_create_then_transfer_folder_before_doc(self):
        order = []
        result = self._run(_fake(order=order))
        self.assertEqual(order[0], "create_folder")
        self.assertEqual(order[1], ("doc", "fld_new"))
        self.assertLess(order.index(("transfer", "folder")), order.index(("transfer", "docx")))
        self.assertEqual(handlers._folder_issues(result), [])

    def test_reuse_existing_folder(self):
        order = []
        self._run(_fake(existing=[{"type": "folder", "name": "瑞幸联名", "token": "fld_old"}], order=order))
        self.assertNotIn("create_folder", order)
        self.assertIn(("doc", "fld_old"), order)

    def test_folder_fallback_p1_and_both_fail_p0(self):
        result = self._run(_fake(folder_transfer_ok=False))
        self.assertEqual(handlers._folder_issues(result)[0]["severity"], "P1")
        result = self._run(_fake(folder_transfer_ok=False, folder_collab_ok=False))
        issue = handlers._folder_issues(result)[0]
        self.assertEqual((issue["severity"], issue["id"]), ("P0", "P0-postpublish-owner"))

    def test_no_root_folder_is_p1_and_continues(self):
        with patch.dict(os.environ, {"FEISHU_USER_OPEN_ID": "ou_x"}, clear=False):
            os.environ.pop("FEISHU_ROOT_FOLDER_TOKEN", None)
            with patch("feishu._tenant_token", return_value="t"), patch("feishu._req", side_effect=_fake()):
                result = feishu.create_delivery("", title="T", task_id="t", report_version="1.0")
        self.assertEqual(handlers._folder_issues(result)[0]["id"], "P1-postpublish-no-folder")


if __name__ == "__main__":
    unittest.main()
