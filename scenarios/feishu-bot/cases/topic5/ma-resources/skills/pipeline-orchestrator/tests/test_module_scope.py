"""FIX-61：逐模块范围记录。"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import handlers  # noqa: E402
import module_scope  # noqa: E402


class ModuleScopeTest(unittest.TestCase):
    def test_scope_index_and_not_in_writing_evidence(self):
        bundle = {"sentiment_dist": {"positive": 1, "neutral": 0, "negative": 1},
                  "dimension_nsr": {}, "trend": {}}
        index = module_scope.attach(bundle, campaign="A", plan={"platforms": ["xiaohongshu"]},
                                    requirement={"brand": "x"}, period=("2026-10-01", "2026-10-07"),
                                    source_refs=[{"artifact_id": "denoised_data@v1"}, None])
        self.assertEqual(index["dimension_nsr"]["denominator"], "pos+neg")
        self.assertEqual(index["sentiment"]["requirement_sha"], module_scope.requirement_sha({"brand": "x"}))
        self.assertEqual(sum(bundle["sentiment_dist"].values()), 2)       # 分布 dict 未被污染
        self.assertNotIn("funnel", index)
        evidence = handlers._report_evidence({"campaigns": {"A": bundle}})
        self.assertNotIn("_scope_index", evidence["campaigns"]["A"])


if __name__ == "__main__":
    unittest.main()
