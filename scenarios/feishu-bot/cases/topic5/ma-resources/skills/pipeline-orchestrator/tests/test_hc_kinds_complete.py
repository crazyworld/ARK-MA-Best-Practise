"""每个会暂停的节点都必须有卡片类型（Gateway 只认 NODE_TO_HC 里的种类）。

回归：n04 过滤词确认、n07 数据异常、n09 DataHub 路由、n09r/n11r 标注审核
暂停后 hc_kind 回落成节点 ID，Gateway 不识别，流水线停住且无人能续跑。
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest

import hc_bridge
from _util import load_dag

# 代码里会 raise HumanPause 的业务节点（人工门之外）。新增暂停点必须同时登记卡片类型。
PAUSING_WORK_NODES = (
    "n03b_activity_pull",   # 采集授权
    "n04_run_init",         # 活动过滤词命中
    "n07_clean",            # 数据异常 / 多 Sheet
    "n09_annotate_r1",      # DataHub 路由确认
    "n09r_review_r1",       # 轮次一标注审核
    "n11r_review_r2",       # 轮次二标注审核
)


class HcKindsCompleteTest(unittest.TestCase):
    def test_every_pausing_node_maps_to_an_hc_kind(self):
        dag = load_dag()
        nodes = [k for k, n in dag.nodes.items() if n.get("handler") == "human_gate"
                 or n.get("handler") in ("validator_gate", "opinion_policy_gate")]
        nodes += list(PAUSING_WORK_NODES)
        missing = sorted(n for n in nodes if hc_bridge.hc_kind_for_node(n) == n)
        self.assertEqual(missing, [])

    def test_hc_kinds_are_unique(self):
        kinds = list(hc_bridge.NODE_TO_HC.values())
        self.assertEqual(len(kinds), len(set(kinds)))


if __name__ == "__main__":
    unittest.main()
