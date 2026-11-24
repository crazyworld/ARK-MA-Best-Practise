"""验证:排障 Bot 的 adapter 能读 crazyworld 版 topic5 的 pipeline_jobs。

用 topic5 自己的 PipelineStore 建库并写入一条 job,再用排障 case 的 build_sources 读出来。
运行(在 scenarios/feishu-bot 下):
    PYTHONPATH=$PWD:$PWD/cases python docs/verification/diagnose-topic5/verify_topic5_adapter.py
只用临时目录,不碰真实库,不需要任何密钥。
"""
import os
import sys
import tempfile

from topic5.pipeline_store import PipelineStore

from diagnose.gateway import build_sources


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "topic5_pipeline.db")
        store = PipelineStore(db)
        job = store.create_job(
            chat_id="oc_verify_chat", thread_id="", user_open_id="ou_verify",
            ma_session_id="sesn-verify-0001", mode="full", project_dir=tmp,
            task_id="task-verify-1",
        )
        sources = build_sources({"topic5": db})
        assert "topic5" in sources, "build_sources 没有识别 topic5"
        refs = sources["topic5"].list_refs(limit=10, chat_id="oc_verify_chat")
        assert [r.sessions[0].session_id for r in refs] == ["sesn-verify-0001"], refs
        hit = sources["topic5"].resolve("sesn-verify-0001") or sources["topic5"].resolve(job.job_id)
        assert hit and hit.scenario == "topic5" and hit.chat_id == "oc_verify_chat", hit
        # 群聊范围:别的群查不到
        assert sources["topic5"].list_refs(limit=10, chat_id="oc_other") == []
        print("PASS: topic5 pipeline_jobs 可被排障 adapter 读取(list_refs / resolve / 群范围过滤)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
