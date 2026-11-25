"""全量部署原地更新 Agent（ID 不变），不再先删后建。

回归 job_bf3c6b52db7b：21:32 一次全量部署删除了旧 coordinator/validator 并新建，进行中的
Session 仍绑定旧 coordinator（其 multiagent 指向旧 validator），n11r 委派 Validator 时
`could not create sub-agent ... 404 InvalidAgent.NotFound`，Run 无法继续。

方舟原地更新语义（2026-11-26 在 212 实测）：POST /agents/{id} 携带当前 version →
version+1、ID 不变；GET /agents/{id}?version=N 仍可取旧版本 —— 进行中的 Session
按其固定的版本继续运行，新 Session 取最新版本。
"""
from __future__ import annotations

import unittest
from pathlib import Path

RES = Path(__file__).resolve().parents[1] / "ma-resources"
SCRIPT = (RES / "create_all.sh").read_text(encoding="utf-8")


class AgentInPlaceUpdateTest(unittest.TestCase):
    def test_agents_are_not_deleted(self):
        self.assertNotIn('ark_delete "/agents/', SCRIPT)
        self.assertNotRegex(SCRIPT, r'\ndelete_agent_if_exists "topic5-')

    def test_upsert_posts_to_existing_id_with_version(self):
        body = SCRIPT[SCRIPT.index("upsert_agent() {"):]
        body = body[:body.index("\n}\n")]
        self.assertIn('ark_post "/agents/$aid"', body)
        self.assertIn(".version", body)
        self.assertIn('ark_post "/agents"', body)   # 不存在时才新建

    def test_both_agents_use_upsert(self):
        self.assertRegex(SCRIPT, r'AGENT_VALIDATOR_ID=\$\(upsert_agent "topic5-validator"')
        self.assertRegex(SCRIPT, r'AGENT_COORDINATOR_ID=\$\(upsert_agent "topic5-coordinator"')


if __name__ == "__main__":
    unittest.main()
