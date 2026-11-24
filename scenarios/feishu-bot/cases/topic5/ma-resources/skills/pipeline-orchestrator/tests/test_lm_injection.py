"""FIX-56：客户 LM 错误案例按节点注入。"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import semantic  # noqa: E402


class LmInjectionTest(unittest.TestCase):
    def test_write_report_contains_nsr_case_and_capped(self):
        text = semantic.lm_text("write_report")
        self.assertIn(semantic.LM_HEADING, text)
        self.assertIn("#13", text)
        self.assertLessEqual(len(text), 4000)

    def test_unknown_scope_raises_and_none_keeps_system(self):
        with self.assertRaises(KeyError):
            semantic.lm_text("nope")
        self.assertEqual(semantic.with_lm("SYS", None), "SYS")

    def test_index_ids_exist_in_source(self):
        with open(os.path.join(semantic.LM_DIR, "lm_error_cases.md"), encoding="utf-8") as f:
            ids = {int(i) for i in re.findall(r"^## 错误案例 #(\d+)", f.read(), re.M)}
        with open(os.path.join(semantic.LM_DIR, "lm_index.json"), encoding="utf-8") as f:
            index = json.load(f)
        for scope, cases in index["scopes"].items():
            self.assertTrue(set(cases) <= ids, scope)
            self.assertNotIn(1, cases)          # 已废弃案例不收录
            for superseded in index.get("superseded", {}):
                self.assertNotIn(int(superseded), cases, scope)   # D3：与现行规则冲突的案例不注入

    def test_chat_appends_to_system(self):
        seen = {}

        def fake_base():
            raise RuntimeError("stop")
        with patch.object(semantic, "_base_key", side_effect=lambda: (_ for _ in ()).throw(RuntimeError("x"))):
            with patch.object(semantic, "with_lm", wraps=semantic.with_lm) as wrapped:
                with self.assertRaises(RuntimeError):
                    semantic.chat("SYS", "u", lm_scope="fix_report")
                seen["args"] = wrapped.call_args.args
        self.assertEqual(seen["args"], ("SYS", "fix_report"))


if __name__ == "__main__":
    unittest.main()
