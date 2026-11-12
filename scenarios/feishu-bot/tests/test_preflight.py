"""启动权限自检:缺权限要明确告知,不确定要如实说"不确定",绝不把"没查到"说成"缺权限"。

响应结构取自 2026-11-12 对真实应用 GET /open-apis/application/v6/scopes 的实测。
"""
import json

from arkagent.feishu import USER_ID_SCOPE, ScopeProbe, classify_app_scopes
from arkagent.preflight import DOC_SCOPES, UserIdWatcher, evaluate, requirements, run_preflight


def scopes_response(*names, status=1, scope_type="tenant"):
    return json.dumps({"data": {"scopes": [
        {"scope_name": n, "grant_status": status, "scope_type": scope_type} for n in names
    ]}}).encode()


def granted(*names):
    return classify_app_scopes(True, 0, "", scopes_response(*names))


def test_classify_collects_only_granted_scopes():
    raw = json.dumps({"data": {"scopes": [
        {"scope_name": "a:b", "grant_status": 1, "scope_type": "tenant"},
        {"scope_name": "c:d", "grant_status": 2, "scope_type": "tenant"},
        {"scope_name": "a:b", "grant_status": 1, "scope_type": "tenant"},
    ]}}).encode()
    probe = classify_app_scopes(True, 0, "", raw)
    assert probe.status == "ok" and probe.granted == frozenset({"a:b"})   # 未授予的不算,重复项合并


def test_user_identity_scopes_do_not_count_for_the_bot():
    # 真实场景:同一个 scope 只在 user 身份下开通,tenant 下没有。bot 以应用身份调用,必须判缺失。
    raw = json.dumps({"data": {"scopes": [
        {"scope_name": "docx:document:create", "grant_status": 1, "scope_type": "user"},
        {"scope_name": "docx:document:write_only", "grant_status": 1, "scope_type": "tenant"},
    ]}}).encode()
    probe = classify_app_scopes(True, 0, "", raw)
    assert probe.granted == frozenset({"docx:document:write_only"})
    reports = evaluate("cli_x", probe, requirements({"SESSION_DOC_PARENT_TOKEN": "t"}))
    assert any("docx:document:create" in r and "未开通(应用身份)" in r for r in reports)


def test_only_user_scopes_means_unknown_not_a_false_all_clear():
    raw = scopes_response("contact:user.employee_id:readonly", scope_type="user")
    assert classify_app_scopes(True, 0, "", raw).status == "unknown"


def test_classify_failures_are_unknown_never_missing():
    assert classify_app_scopes(False, 99991672, "denied", None).status == "unknown"
    assert classify_app_scopes(True, 0, "", b"not json").status == "unknown"
    assert classify_app_scopes(True, 0, "", scopes_response()).status == "unknown"   # 空列表:无法判断


def test_all_granted_is_silent():
    assert evaluate("cli_x", granted(USER_ID_SCOPE), requirements({})) == []


def test_missing_user_id_scope_is_reported_with_impact_and_link():
    reports = evaluate("cli_x", granted("im:message"), requirements({}))
    assert len(reports) == 1
    text = reports[0]
    assert USER_ID_SCOPE in text and "私聊" in text and "发布新版本" in text
    assert "clientID=cli_x" in text and "contact%3Auser.employee_id%3Areadonly" in text


def test_doc_scopes_only_required_when_doc_sink_is_enabled():
    only_id = granted(USER_ID_SCOPE)
    assert evaluate("cli_x", only_id, requirements({})) == []                           # 没启用文档:不要求
    reports = evaluate("cli_x", only_id, requirements({"SESSION_DOC_PARENT_TOKEN": "wikcnX"}))
    assert len(reports) == 1 and all(s in reports[0] for s in DOC_SCOPES)
    full = granted(USER_ID_SCOPE, *DOC_SCOPES)
    assert evaluate("cli_x", full, requirements({"SESSION_DOC_PARENT_TOKEN": "wikcnX"})) == []


def test_partially_missing_doc_scopes_list_only_what_is_missing():
    probe = granted(USER_ID_SCOPE, "docx:document:create")
    reports = evaluate("cli_x", probe, requirements({"SESSION_DOC_PARENT_TOKEN": "t"}))
    assert "docx:document:write_only" in reports[0] and "scopes=docx%3Adocument%3Awrite_only" in reports[0]
    assert "scopes=docx%3Adocument%3Acreate" not in reports[0]


def test_unknown_probe_says_not_confirmed_not_missing():
    reports = evaluate("cli_x", ScopeProbe("unknown", "网络挂了"), requirements({}))
    assert "权限未确认" in reports[0] and "不代表缺权限" in reports[0] and "权限缺失" not in reports[0]


def test_preflight_never_raises_and_never_blocks(capsys):
    class Boom:
        def list_app_scopes(self):
            raise RuntimeError("网络挂了")

    run_preflight(Boom(), None, "cli_x", {})                      # 不抛
    assert "权限未确认" in capsys.readouterr().out


def test_sender_without_support_is_unknown(capsys):
    run_preflight(object(), None, "cli_x", {})
    assert "权限未确认" in capsys.readouterr().out


def test_watcher_warns_once_only_for_private_without_user_id(caplog):
    watcher = UserIdWatcher("cli_x")
    with caplog.at_level("WARNING"):
        watcher.observe("", "group")
        watcher.observe("u1", "p2p")
        assert not caplog.records
        watcher.observe("", "p2p")
        watcher.observe("", "p2p")
    assert len(caplog.records) == 1 and USER_ID_SCOPE in caplog.records[0].message
