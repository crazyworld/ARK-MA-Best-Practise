"""Gateway 侧妙搭兜底发布（Phase H 不可抗力 backup）测试。

不调真实 lark-cli/飞书：用假 runner 记录命令，断言发布顺序、目标选择与安全约束；
runner 侧用真实 PipelineStore + 假 Ark 事件，断言只在环境故障时兜底、Agent 行为不变。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from cases.topic6 import topic6_runner
from cases.topic6.miaoda_publisher import (
    MiaodaFallbackError,
    MiaodaPublisher,
    PublishResult,
    locate_web_artifact,
)
from cases.topic6.pipeline_store import STATUS_DONE, STATUS_FAILED, PipelineStore
from cases.topic6.topic6_runner import Topic6Config as RunnerConfig
from cases.topic6.topic6_runner import Topic6Runner

HTML = b"<!doctype html><html><head><meta charset='utf-8'><title>t</title></head><body>W47</body></html>"
SHA = hashlib.sha256(HTML).hexdigest()
PROJECT = "W47热点周报_20261116-20261122"
SOURCE = "08_UI发布/热点报告_2026-W47_web/index.html"
URL = "https://bluefocus.feishu.cn/page/NewPage"
TOKEN = "u-secret-token-should-never-leak"


def _job_root(tmp_path: Path, *, published_at="2026-11-23T08:02:43.409862Z", sha=SHA, html=HTML) -> Path:
    root = tmp_path / "job_x"
    (root / ".objects").mkdir(parents=True)
    (root / ".objects/aaa__index.html").write_bytes(html)
    manifest = {
        "project": PROJECT,
        "publications": [
            {"node": "report", "status": "complete", "published_at": "2026-11-23T02:35:57Z",
             "files": [{"source": "07_报告/x.md", "sha256": "0"}]},
            {"node": "web", "status": "complete", "published_at": published_at,
             "files": [{"source": SOURCE, "output": f"{PROJECT}/{SOURCE}", "sha256": sha, "size": len(html)}]},
        ],
    }
    (root / ".objects/bbb__manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    (root / "index.json").write_text(json.dumps({"files": {
        "f1": {"filename": "index.html", "object_path": ".objects/aaa__index.html"},
        "f2": {"filename": "manifest.json", "object_path": ".objects/bbb__manifest.json"},
    }}))
    return root


# ---------------------------------------------------------------- locate_web_artifact

def test_locate_web_artifact_uses_latest_web_publication(tmp_path):
    artifact = locate_web_artifact(_job_root(tmp_path))
    assert artifact.sha256 == SHA
    assert artifact.app_name == "热点报告_2026-W47"  # 与 prompt 09 应用名规则一致


def test_locate_web_artifact_rejects_sha_mismatch(tmp_path):
    with pytest.raises(MiaodaFallbackError, match="不一致"):
        locate_web_artifact(_job_root(tmp_path, sha="f" * 64))


def test_locate_web_artifact_rejects_stale_round(tmp_path):
    with pytest.raises(MiaodaFallbackError, match="还没有生成新网页"):
        locate_web_artifact(_job_root(tmp_path), min_published_at="2026-11-23T09:00:00.000000Z")


def test_locate_web_artifact_accepts_unchanged_rebuild_hash(tmp_path):
    """本轮重建与上轮逐字节相同：manifest 未重发，但本轮工具输出里有同一 sha256 → 接受。"""
    artifact = locate_web_artifact(
        _job_root(tmp_path), min_published_at="2026-11-23T09:00:00.000000Z",
        round_sha256s=frozenset({SHA}),
    )
    assert artifact.sha256 == SHA


# ---------------------------------------------------------------- MiaodaPublisher

class FakeCli:
    """记录所有子进程调用；按命令返回预置结果。"""

    def __init__(self, *, existing_apps=(), push_fail_once=False, release_status=("publishing", "finished")):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.existing_apps = list(existing_apps)
        self.push_fail_once = push_fail_once
        self.release_status = list(release_status)

    def __call__(self, argv, cwd=None, env=None, capture_output=True, text=True, timeout=None):
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        ok = lambda data: subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True, "data": data}), "")  # noqa: E731
        if argv[0].endswith("python") or argv[0].endswith("python3") or "validate_html.py" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if argv[0] == "git":
            if argv[1] == "push" and self.push_fail_once:
                self.push_fail_once = False
                return subprocess.CompletedProcess(argv, 128, "", "fatal: could not read Username")
            if argv[1:3] == ["diff", "--cached"]:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        sub = argv[2]
        if sub == "+list":
            return ok({"items": self.existing_apps, "has_more": False, "page_token": ""})
        if sub == "+create":
            return ok({"app": {"app_id": "app_new"}})
        if sub == "+init":
            repo = Path(argv[argv.index("--dir") + 1])
            (repo / ".git").mkdir(parents=True)
            return ok({"pushed": True})
        if sub == "+git-credential-init":
            return ok({})
        if sub == "+release-create":
            return ok({"release_id": "rel1", "status": "publishing"})
        if sub == "+release-get":
            status = self.release_status.pop(0) if len(self.release_status) > 1 else self.release_status[0]
            return ok({"release_id": "rel1", "status": status, "online_url": URL if status == "finished" else ""})
        if sub == "+get":
            return ok({"app": {"app_id": "app_new"}})
        raise AssertionError(argv)

    def subs(self):
        return [c[2] if c[0] != "git" and "apps" in c else c[0] + " " + c[1] for c in self.calls
                if "validate_html.py" not in " ".join(c)]


def _publisher(tmp_path, cli: FakeCli) -> MiaodaPublisher:
    cli_bin = tmp_path / "lark-cli"
    cli_bin.write_text("#!/bin/sh\n")
    cli_bin.chmod(0o755)
    return MiaodaPublisher(cli_bin=str(cli_bin), app_id="cli_app", runner=cli, sleep=lambda _s: None,
                           work_root=tmp_path)


def test_publish_creates_new_app_with_skill_git_flow(tmp_path):
    cli = FakeCli()
    result = _publisher(tmp_path, cli).publish(locate_web_artifact(_job_root(tmp_path)), TOKEN)
    assert result == PublishResult("app_new", "rel1", URL, "create", SHA)
    assert cli.subs() == [
        "+list", "+create", "+init", "git add", "git diff", "git -c", "git push",
        "+release-create", "+release-get", "+release-get",
    ]
    create = next(c for c in cli.calls if "+create" in c)
    assert create[create.index("--name") + 1] == "热点报告_2026-W47"
    assert create[create.index("--app-type") + 1] == "html"
    # token 只经环境变量传递，不出现在任何命令行参数里；工作目录用完即删
    assert all(TOKEN not in " ".join(c) for c in cli.calls)
    assert all(e.get("LARKSUITE_CLI_USER_ACCESS_TOKEN") == TOKEN for e in cli.envs if e)
    assert not list(tmp_path.glob("miaoda_fallback_*"))


def test_publish_updates_app_matched_by_known_online_url(tmp_path):
    cli = FakeCli(existing_apps=[{"app_id": "app_old", "name": "热点报告_2026-W47", "online_url": URL}])
    result = _publisher(tmp_path, cli).publish(
        locate_web_artifact(_job_root(tmp_path)), TOKEN, known_online_urls=[URL]
    )
    assert result.app_id == "app_old" and result.operation == "update"
    assert "+create" not in cli.subs()


def test_publish_refuses_unconfirmed_same_name_app(tmp_path):
    cli = FakeCli(existing_apps=[{"app_id": "app_x", "name": "热点报告_2026-W47", "online_url": "https://o/page/x"}])
    with pytest.raises(MiaodaFallbackError, match="同名妙搭应用"):
        _publisher(tmp_path, cli).publish(locate_web_artifact(_job_root(tmp_path)), TOKEN)
    assert "+create" not in cli.subs() and "+init" not in cli.subs()


def test_publish_retries_push_after_git_credential_init(tmp_path):
    cli = FakeCli(push_fail_once=True)
    _publisher(tmp_path, cli).publish(locate_web_artifact(_job_root(tmp_path)), TOKEN)
    subs = cli.subs()
    assert subs.index("+git-credential-init") < len(subs) - subs[::-1].index("git push") - 1


def test_publish_failed_release_reports_ids(tmp_path):
    cli = FakeCli(release_status=("failed",))
    with pytest.raises(MiaodaFallbackError, match="app_id=app_new，release_id=rel1"):
        _publisher(tmp_path, cli).publish(locate_web_artifact(_job_root(tmp_path)), TOKEN)


def test_publish_without_cli_is_refused(tmp_path):
    publisher = MiaodaPublisher(cli_bin=str(tmp_path / "missing"), app_id="a")
    with pytest.raises(MiaodaFallbackError, match="TOPIC6_MIAODA_CLI_BIN"):
        publisher.publish(locate_web_artifact(_job_root(tmp_path)), TOKEN)


# ---------------------------------------------------------------- Runner 接入

def _msg(text):
    return {"type": "agent.message", "content": [{"type": "text", "text": text}]}


def _tool(cmd):
    return {"type": "agent.tool_use", "input": {"command": cmd}}


def _result(text):
    return {"type": "agent.tool_result", "content": [{"type": "text", "text": text}]}


ENV_FAIL_EVENTS = [
    _msg("[phase] H"),
    _tool("/usr/local/bin/lark-cli apps +list --as user --format json"),
    _result('{"ok": false, "error": {"message": "HTTP 502: credential resolve returned no substitution"}}'),
    _msg("[need_decision] Phase H 被环境凭据阻塞"),
]


class _Ark:
    def __init__(self, events, status="idle"):
        self.events = list(events)
        self.status = status
        self.sent = []

    async def list_session_events(self, _sid):
        return list(self.events)

    async def get_session(self, _sid):
        return {"status": self.status, "agent": {"model": {"id": "m"}}}

    async def send_message(self, sid, text, extra_headers=None):
        self.sent.append(text)


class _Feishu:
    def send_interactive_card(self, *_a):
        return "om-card"


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def _runner(tmp_path, loop, ark, fallback):
    store = PipelineStore(str(tmp_path / "topic6.db"))
    runner = Topic6Runner(ark, _Feishu(), store, RunnerConfig("a", "e", "m", ()), loop=loop)
    replies: list[str] = []

    async def _noop(*_a, **_k):
        return None

    async def _reply(_job_id, text):
        replies.append(text)

    runner._render_and_patch = _noop  # type: ignore[assignment]
    runner._reply_async_by_job = _reply  # type: ignore[assignment]
    runner._spawn_stream = lambda job, first_user_message: object()  # type: ignore[assignment]
    if fallback is not None:
        runner.bind_miaoda_fallback(fallback)
    job = store.create_job(chat_id="c1", thread_id="", user_open_id="u1",
                           ma_session_id="sesn-1", mode="full", project_dir=f"/workspace/Projects/{PROJECT}")
    store.update_phase(job.job_id, "H")
    return runner, store, job, replies


def _republish(loop, runner, store, job):
    store.mark_failed(job.job_id, "Phase H 妙搭发布失败")  # /republish 的真实前置：任务已收口
    loop.run_until_complete(runner.republish_job(store.get_job(job.job_id)))


def _drain(loop):
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if pending:
        loop.run_until_complete(asyncio.gather(*pending))


def _calls_fallback():
    calls = []

    async def fallback(job, min_published_at, round_sha256s=frozenset()):
        calls.append((job.job_id, min_published_at))
        fallback.round_sha256s = round_sha256s
        return PublishResult("app_new", "rel1", URL, "create", SHA)

    return calls, fallback


def test_no_fallback_configured_keeps_original_behavior(loop, tmp_path):
    runner, store, job, replies = _runner(tmp_path, loop, _Ark(ENV_FAIL_EVENTS), None)
    runner._progress[job.job_id] = topic6_runner._ProgressState()
    loop.run_until_complete(runner._handle_idle(job.job_id, ["[need_decision] Phase H 被环境凭据阻塞"]))
    assert store.get_job(job.job_id).status == STATUS_FAILED
    assert len(replies) == 1 and "妙搭发布失败" in replies[0]  # 只有原来那条提示


def test_first_round_env_failure_only_hints_republish(loop, tmp_path):
    calls, fallback = _calls_fallback()
    runner, store, job, replies = _runner(tmp_path, loop, _Ark(ENV_FAIL_EVENTS), fallback)
    runner._progress[job.job_id] = topic6_runner._ProgressState()
    loop.run_until_complete(runner._handle_idle(job.job_id, ["[need_decision] Phase H 被环境凭据阻塞"]))
    _drain(loop)
    assert calls == []
    assert store.get_job(job.job_id).status == STATUS_FAILED
    assert "/republish" in replies[-1] and "credential resolve" in replies[-1]


def test_republish_env_failure_triggers_gateway_fallback(loop, tmp_path):
    calls, fallback = _calls_fallback()
    ark = _Ark([_msg("old round")])
    runner, store, job, replies = _runner(tmp_path, loop, ark, fallback)
    _republish(loop, runner, store, job)
    # Agent 的 /republish 消息与原来一字不差
    assert runner._progress[job.job_id].republish is True
    ark.events += ENV_FAIL_EVENTS
    store.update_phase(job.job_id, "H")
    loop.run_until_complete(runner._handle_idle(job.job_id, ["[need_decision] Phase H 被环境凭据阻塞"]))
    _drain(loop)
    assert len(calls) == 1 and calls[0][1]  # 带本轮起始时间，拒绝旧网页
    reloaded = store.get_job(job.job_id)
    assert reloaded.status == STATUS_DONE and reloaded.online_url == URL
    assert any("Gateway 兜底发布" in r and URL in r for r in replies)


def test_republish_business_stop_is_not_fallback(loop, tmp_path):
    """Agent 已执行写操作（+create）后失败：不兜底，防止重复建应用。"""
    calls, fallback = _calls_fallback()
    ark = _Ark([])
    runner, store, job, _ = _runner(tmp_path, loop, ark, fallback)
    _republish(loop, runner, store, job)
    ark.events += [
        _msg("[phase] H"),
        _tool("lark-cli apps +create --as user --name 热点报告_2026-W47 --app-type html"),
        _result('{"ok": true}'),
        _tool("git push origin HEAD:sprint/default"),
        _result("HTTP 502: credential resolve returned no substitution"),
        _msg("[need_decision] push 失败"),
    ]
    store.update_phase(job.job_id, "H")
    loop.run_until_complete(runner._handle_idle(job.job_id, ["[need_decision] push 失败"]))
    _drain(loop)
    assert calls == [] and store.get_job(job.job_id).status == STATUS_FAILED


def test_republish_non_env_failure_is_not_fallback(loop, tmp_path):
    """预检失败/同名冲突等业务停止：不兜底。"""
    calls, fallback = _calls_fallback()
    ark = _Ark([])
    runner, store, job, _ = _runner(tmp_path, loop, ark, fallback)
    _republish(loop, runner, store, job)
    ark.events += [
        _msg("[phase] H"),
        _tool("python3 scripts/validate_html.py index.html"),
        _result('{"errors": ["missing local asset"]}'),
        _msg("[need_decision] 预检失败"),
    ]
    store.update_phase(job.job_id, "H")
    loop.run_until_complete(runner._handle_idle(job.job_id, ["[need_decision] 预检失败"]))
    _drain(loop)
    assert calls == []


def test_republish_successful_agent_publish_unchanged(loop, tmp_path):
    calls, fallback = _calls_fallback()
    ark = _Ark([])
    runner, store, job, _ = _runner(tmp_path, loop, ark, fallback)
    _republish(loop, runner, store, job)
    done = '{"phase":"H","release_status":"finished","online_url":"' + URL + '"}'
    ark.events += [_msg("[phase] H"), _msg(done)]
    store.update_phase(job.job_id, "H")
    loop.run_until_complete(runner._handle_idle(job.job_id, [done]))
    _drain(loop)
    assert calls == [] and store.get_job(job.job_id).status == STATUS_DONE


def test_republish_terminated_session_goes_straight_to_fallback(loop, tmp_path):
    calls, fallback = _calls_fallback()
    ark = _Ark([], status="terminated")
    runner, store, job, replies = _runner(tmp_path, loop, ark, fallback)
    store.mark_failed(job.job_id, "x")
    loop.run_until_complete(runner.republish_job(store.get_job(job.job_id)))
    _drain(loop)
    assert calls == [(job.job_id, "")]
    assert ark.sent == []  # 未向已终止的 Session 注入消息
    assert store.get_job(job.job_id).status == STATUS_DONE
    assert any("未重新读取飞书文档" in r for r in replies)


def test_republish_terminated_session_without_fallback_keeps_old_error(loop, tmp_path):
    ark = _Ark([], status="terminated")
    runner, store, job, _ = _runner(tmp_path, loop, ark, None)
    store.mark_failed(job.job_id, "x")
    with pytest.raises(topic6_runner.Topic6RunnerError, match="空闲"):
        loop.run_until_complete(runner.republish_job(store.get_job(job.job_id)))


def test_republish_env_probe_before_phase_h_marker_triggers_fallback(loop, tmp_path):
    """job_980f1999c493 第 3 轮真实事件形态：Agent 在 Phase G 收尾时就探测到 502，
    最后一条消息才是 `[phase] H`（其后无任何工具调用）。必须仍判为环境故障并兜底。"""
    calls, fallback = _calls_fallback()
    ark = _Ark([_msg("old round")])
    runner, store, job, replies = _runner(tmp_path, loop, ark, fallback)
    _republish(loop, runner, store, job)
    final = "[phase] H\n\n本轮重发结果与上一轮逐字节相同,Phase H 仍被同一环境阻塞,因此不输出发布 JSON。"
    ark.events += [
        _msg("[phase] G"),
        _tool("python fetch_case_images.py ... && node build-report.mjs source.json index.html"),
        _result("Generated index.html\nhtml_after=" + SHA),
        _tool("python - <<EOF ... EOF; /usr/local/bin/lark-cli apps +list --as user --format json"),
        _result('{"ok": false, "error": {"code": 502, "message": "HTTP 502: credential resolve returned no substitution"}}'),
        _msg(final),
    ]
    store.update_phase(job.job_id, "H")
    loop.run_until_complete(runner._handle_idle(job.job_id, [final]))
    _drain(loop)
    assert len(calls) == 1
    assert SHA in fallback.round_sha256s
    assert store.get_job(job.job_id).status == STATUS_DONE


def test_republish_previous_round_env_errors_are_ignored(loop, tmp_path):
    """上一轮的 502 不能让本轮（无环境故障的业务失败）误触发兜底。"""
    calls, fallback = _calls_fallback()
    ark = _Ark(list(ENV_FAIL_EVENTS))  # 历史轮次里的环境故障
    runner, store, job, _ = _runner(tmp_path, loop, ark, fallback)
    _republish(loop, runner, store, job)
    ark.events += [_msg("[phase] H"), _msg("[need_decision] 预检失败")]
    store.update_phase(job.job_id, "H")
    loop.run_until_complete(runner._handle_idle(job.job_id, ["[need_decision] 预检失败"]))
    _drain(loop)
    assert calls == []
