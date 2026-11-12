"""启动权限自检:缺权限要明确告知,不确定要如实说"不确定",绝不把"没探测到"说成"缺权限"。"""
from arkagent.feishu import USER_ID_SCOPE, ScopeProbe, classify_user_id_probe
from arkagent.preflight import UserIdWatcher, check_user_id_scope, format_report, run_preflight

# 2026-11-12 对真实应用的实测响应
MISSING_MSG = (
    "access denied: app cli_aa86f8eb11399ccc has not applied for the required scope(s): "
    "contact:user.employee_id:readonly"
)


def test_missing_scope_is_reported_with_scope_name_and_impact():
    probe = classify_user_id_probe(False, 99991672, MISSING_MSG, None)
    assert probe.status == "missing_scope" and probe.missing == (USER_ID_SCOPE,)
    report = format_report("cli_x", probe)
    assert USER_ID_SCOPE in report and "私聊" in report and "scope-apply" in report


def test_success_with_user_id_is_ok_and_silent():
    raw = b'{"data": {"user": {"user_id": "abc123"}}}'
    probe = classify_user_id_probe(True, 0, "", raw)
    assert probe.status == "ok" and format_report("cli_x", probe) is None


def test_success_without_user_id_is_unknown_not_missing():
    # 接口通了但没给 user_id:可能该用户不在通讯录权限范围内,不能断言"没开通"
    probe = classify_user_id_probe(True, 0, "", b'{"data": {"user": {"name": "x"}}}')
    assert probe.status == "unknown"
    assert "不代表缺权限" in format_report("cli_x", probe)


def test_other_errors_are_unknown_not_missing():
    assert classify_user_id_probe(False, 99992356, "invalid id", None).status == "unknown"
    assert classify_user_id_probe(False, None, "", None).status == "unknown"


def test_no_authorized_users_means_cannot_probe():
    result = check_user_id_scope(object(), type("C", (), {"authorized_open_ids": ()})())
    assert result.status == "unknown" and "AUTHORIZED_OPEN_IDS" in result.detail


def test_probe_uses_first_authorized_open_id():
    seen = []

    class Sender:
        def probe_user_id_scope(self, open_id):
            seen.append(open_id)
            return ScopeProbe("ok")

    check_user_id_scope(Sender(), type("C", (), {"authorized_open_ids": ("ou_a", "ou_b")})())
    assert seen == ["ou_a"]


def test_preflight_never_raises_and_never_blocks(capsys):
    class Boom:
        def probe_user_id_scope(self, open_id):
            raise RuntimeError("网络挂了")

    run_preflight(Boom(), type("C", (), {"authorized_open_ids": ("ou_a",)})(), "cli_x")  # 不抛
    assert "权限未确认" in capsys.readouterr().out


def test_watcher_warns_once_only_for_private_without_user_id(caplog):
    watcher = UserIdWatcher("cli_x")
    with caplog.at_level("WARNING"):
        watcher.observe("", "group")        # 群聊没有 user_id 不算
        watcher.observe("u1", "p2p")        # 有 user_id 不告警
        assert not caplog.records
        watcher.observe("", "p2p")
        watcher.observe("", "p2p")          # 只告警一次
    assert len(caplog.records) == 1 and USER_ID_SCOPE in caplog.records[0].message
