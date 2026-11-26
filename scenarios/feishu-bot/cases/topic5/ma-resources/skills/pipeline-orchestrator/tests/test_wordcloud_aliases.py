"""n15 词云：模型只看短编号，程序映射回真实帖子 ID（job_436842fb74da n15）。

真实 ID 为 32 位哈希，模型逐字回写把 ...58977159aae... 抄成 ...58977119aae...，三次
重试同错 → 「须按顺序记录本批全部全文阅读」。短编号让模型无需复制哈希。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wordcloud_adapter as wa  # noqa: E402

REAL = ["e0bdf5b7dd23cd3fa0a332facc84e005", "949579a9d66b8d5046afbeb6a9bb46eb",
        "666b0df52245d58977159aae114b60d0"]


class PostAliasesTest(unittest.TestCase):
    def setUp(self):
        self.a = wa.PostAliases(REAL)

    def test_round_trip(self):
        self.assertEqual([self.a.alias(i) for i in REAL], ["P1", "P2", "P3"])
        self.assertEqual(self.a.real_list(["P1", "p2", " P3 "]), REAL)

    def test_unknown_alias_left_for_validator(self):
        # 模型自造编号不能被「修正」成真 ID：原样返回，由契约校验拒绝。
        self.assertEqual(self.a.real("P9"), "P9")
        self.assertEqual(self.a.real("666b0df52245d58977119aae114b60d0"),
                         "666b0df52245d58977119aae114b60d0")

    def test_real_id_passthrough(self):
        # 模型若照抄了真实 ID（未改写），仍应能通过。
        self.assertEqual(self.a.real(REAL[2]), REAL[2])

    def test_views_hide_real_ids(self):
        post = {"id": REAL[0], "text": "t"}
        self.assertEqual(self.a.post_view(post)["id"], "P1")
        self.assertEqual(post["id"], REAL[0])  # 不改原对象
        reads = [{"post_id": REAL[1], "note": "n"}]
        self.assertEqual(self.a.reads_view(reads)[0]["post_id"], "P2")
        self.assertEqual(reads[0]["post_id"], REAL[1])
        topics = [{"title": "x", "post_ids": [REAL[2]]}]
        self.assertEqual(self.a.topics_view(topics)[0]["post_ids"], ["P3"])

    def test_restore_topics(self):
        data = {"topics": [{"title": "x", "post_ids": ["P1", "P3"]}, {"title": "y"}]}
        self.a.restore_topics(data)
        self.assertEqual(data["topics"][0]["post_ids"], [REAL[0], REAL[2]])

    def test_prompts_never_contain_real_ids_for_reads(self):
        src = open(wa.__file__, encoding="utf-8").read()
        body = src[src.index("def analyze("):]
        # 发给模型的帖子、已读记录、白名单都必须经过别名视图
        self.assertIn("aliases.post_view(post)", body)
        self.assertIn("aliases.reads_view(reads)", body)
        self.assertIn("view.reads_view(reads)", body)
        self.assertNotIn("json.dumps(reads, ensure_ascii=False)", body)


if __name__ == "__main__":
    unittest.main()
