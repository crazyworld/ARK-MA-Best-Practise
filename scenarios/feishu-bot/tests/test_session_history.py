"""/new 等重置前把 会话键 → session 归档,否则旧 session 在本机永久失联,排障无从查起。"""
import importlib.util
import sys
from pathlib import Path

from arkagent.store import ConversationKey, GatewayStore

_SHARED = Path(__file__).resolve().parents[1] / "cases" / "digital-employee" / "shared.py"


def _key(user="ou_a"):
    return ConversationKey(tenant_key="t", chat_id="oc_1", thread_id="th", user_open_id=user)


def _history(store):
    return store._db.execute(
        "SELECT chat_id, thread_id, user_open_id, user_id, session_id, reason FROM session_history ORDER BY id"
    ).fetchall()


def test_reset_archives_old_session_before_delete():
    store = GatewayStore(":memory:")
    store.save_session(_key(), "sesn-old", "agent-1", user_id="u_a")
    store.reset_session(_key())
    assert store.get_session(_key()) is None
    assert _history(store) == [("oc_1", "th", "ou_a", "u_a", "sesn-old", "reset")]


def test_replacing_a_different_session_archives_the_old_one():
    store = GatewayStore(":memory:")
    store.save_session(_key(), "sesn-1", "agent-1", user_id="u_a")
    store.save_session(_key(), "sesn-2", "agent-1", user_id="u_a")
    assert [h[4:] for h in _history(store)] == [("sesn-1", "replaced")]
    assert store.get_session(_key()) == "sesn-2"


def test_saving_same_session_again_does_not_archive_or_reset_created_at():
    store = GatewayStore(":memory:")
    store.save_session(_key(), "sesn-1", "agent-1", user_id="u_a")
    created = store._db.execute("SELECT created_at FROM conversations").fetchone()[0]
    store.save_session(_key(), "sesn-1", "agent-1")  # 不带 user_id:不能把已记录的清空
    assert _history(store) == []
    row = store._db.execute("SELECT created_at, user_id FROM conversations").fetchone()
    assert row == (created, "u_a")


def test_reset_with_nothing_to_archive_is_noop():
    store = GatewayStore(":memory:")
    store.reset_session(_key())
    assert _history(store) == []


def test_reset_all_archives_every_row_with_split_identity():
    store = GatewayStore(":memory:")
    store.save_session(_key("ou_a"), "s1", "agent-1", user_id="u_a")
    store.save_session(ConversationKey("t", "oc_2", "", "ou_b"), "s2", "agent-1")
    assert store.reset_all_sessions() == 2
    rows = {h[4]: h for h in _history(store)}
    assert rows["s1"][:4] == ("oc_1", "th", "ou_a", "u_a")
    assert rows["s2"][:4] == ("oc_2", "", "ou_b", "")  # '-' 占位还原成空串
    assert all(h[5] == "reset_all" for h in rows.values())


def test_old_database_without_new_columns_is_migrated(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE conversations (conversation_key TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
        " agent_id TEXT NOT NULL, agent_version TEXT, updated_at TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO conversations VALUES ('oc_1:th:ou_a', 'sesn-legacy', 'agent-1', NULL, '2026-01-01')")
    conn.commit()
    conn.close()
    store = GatewayStore(str(db))
    assert store.get_session(_key()) == "sesn-legacy"  # 老数据仍可读
    store.reset_session(_key())
    assert _history(store)[0][4] == "sesn-legacy"  # 老数据重置时也能归档


def _load_shared():
    spec = importlib.util.spec_from_file_location("de_shared_hist", _SHARED)
    module = importlib.util.module_from_spec(spec)
    sys.modules["de_shared_hist"] = module
    spec.loader.exec_module(module)
    return module


def test_digital_employee_reset_and_replace_are_archived(tmp_path):
    shared = _load_shared()
    store = shared.SqliteSessionMap(str(tmp_path / "s.db"))
    key = shared.GroupConversationKey("t", "oc_g", "th")
    store.save(key, "sesn-1")
    store.save(key, "sesn-1")  # 话题别名重复绑定同一个 session:不是替换
    store.save(key, "sesn-2")
    store.reset(key)
    rows = store._conn.execute("SELECT chat_id, thread_id, session_id, reason FROM session_history ORDER BY id").fetchall()
    assert rows == [("oc_g", "th", "sesn-1", "replaced"), ("oc_g", "th", "sesn-2", "reset")]
    assert store.get(key) is None
