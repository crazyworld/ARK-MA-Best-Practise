"""topicx / topic5 / topic6 的只读 adapter。

直接以只读模式读 SQLite,而不是实例化各 case 的 Store:Store 构造时会建表、改权限,
观测路径不应有任何写副作用,通用层也不能依赖 ``cases/*``。多实例部署时换成对应的
共享存储 adapter 即可,service 层不受影响。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator, Optional

from ..model import (
    KIND_CONTROL,
    KIND_HC,
    SOURCE_DB,
    STATUS_OK,
    STATUS_PENDING,
    Span,
)
from .base import SessionRef, TraceRef


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _secs_to_ms(value: Any) -> Optional[int]:
    return int(value) * 1000 if value else None


def _get(row: sqlite3.Row, key: str, default: Any = "") -> Any:
    # 老库可能没有后加的列(如 task_id),缺列按默认值处理。
    return row[key] if key in row.keys() else default


def _where(*, status: Optional[str], chat_id: Optional[str]) -> tuple[str, tuple]:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if chat_id:
        clauses.append("chat_id = ?")
        params.append(chat_id)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), tuple(params)


def _hc_spans(conn: sqlite3.Connection, job_id: str) -> list[Span]:
    if not _columns(conn, "pipeline_hc_events"):
        return []
    spans = []
    for row in conn.execute(
        "SELECT * FROM pipeline_hc_events WHERE job_id = ? ORDER BY id", (job_id,)
    ):
        created = _secs_to_ms(row["created_at"]) or 0
        resolved = _secs_to_ms(row["resolved_at"])
        spans.append(
            Span(
                span_id=f"hc-{row['id']}",
                kind=KIND_HC,
                name=row["hc_kind"],
                started_at_ms=created,
                duration_ms=resolved - created if resolved else None,
                status=STATUS_OK if resolved else STATUS_PENDING,
                source=SOURCE_DB,
                # 不带 operator_label / user_note:含个人信息,P0 不需要。
                attrs={"decision": row["user_decision"] or None},
            )
        )
    return spans


class PipelineJobSource:
    """topic5 / topic6 的 ``pipeline_jobs`` 表(两者字段是超集 / 子集关系,按存在的列读)。"""

    def __init__(self, scenario: str, db_path: str) -> None:
        self._scenario = scenario
        self._db_path = db_path

    def _rows(self, where: str, params: tuple, limit: int = 20) -> Iterator[tuple[sqlite3.Connection, sqlite3.Row]]:
        with closing(_connect(self._db_path)) as conn:
            for row in conn.execute(
                f"SELECT * FROM pipeline_jobs {where} ORDER BY started_at DESC LIMIT ?",
                (*params, limit),
            ):
                yield conn, row

    def _ref(self, conn: sqlite3.Connection, row: sqlite3.Row, *, with_hc: bool) -> TraceRef:
        task_id = _get(row, "task_id")
        return TraceRef(
            # 无 task_id(旧单 topic 入口)时以 job_id 为根。
            trace_id=task_id or row["job_id"],
            root_kind="task" if task_id else "job",
            scenario=self._scenario,
            status=row["status"],
            started_at_ms=_secs_to_ms(row["started_at"]) or 0,
            finished_at_ms=_secs_to_ms(row["finished_at"]),
            last_error=row["last_error"] or "",
            chat_id=_get(row, "chat_id"),
            sessions=[SessionRef(row["ma_session_id"], "business")],
            db_spans=_hc_spans(conn, row["job_id"]) if with_hc else [],
            meta={
                "job_id": row["job_id"],
                "mode": row["mode"],
                "current_phase": row["current_phase"],
                "failure_scope": _get(row, "failure_scope") or None,
            },
        )

    def resolve(self, ident: str) -> Optional[TraceRef]:
        with closing(_connect(self._db_path)) as conn:
            cols = _columns(conn, "pipeline_jobs")
            keys = ["job_id", "ma_session_id"] + (["task_id"] if "task_id" in cols else [])
            for key in keys:
                row = conn.execute(
                    f"SELECT * FROM pipeline_jobs WHERE {key} = ? ORDER BY started_at DESC LIMIT 1",
                    (ident,),
                ).fetchone()
                if row:
                    return self._ref(conn, row, with_hc=True)
        return None

    def list_refs(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> list[TraceRef]:
        where, params = _where(status=status, chat_id=chat_id)
        return [self._ref(conn, row, with_hc=False) for conn, row in self._rows(where, params, limit)]


class TopicxSource:
    """topicx 的 ``task_runs``:一个 task = router session + 业务 session。"""

    def __init__(self, control_db_path: str, job_sources: dict[str, PipelineJobSource]) -> None:
        self._db_path = control_db_path
        self._job_sources = job_sources

    def _ref(self, conn: sqlite3.Connection, row: sqlite3.Row, *, detail: bool) -> TraceRef:
        sessions = []
        if row["router_session_id"]:
            sessions.append(SessionRef(row["router_session_id"], "router"))
        if row["business_session_id"]:
            sessions.append(SessionRef(row["business_session_id"], "business"))
        ref = TraceRef(
            trace_id=row["task_id"],
            root_kind="task",
            scenario=row["scenario"] or "(未路由)",
            status=row["status"],
            started_at_ms=_secs_to_ms(row["created_at"]) or 0,
            finished_at_ms=_secs_to_ms(row["finished_at"]),
            last_error=row["last_error"] or "",
            chat_id=_get(row, "chat_id"),
            user_id=_get(row, "user_id"),
            chat_type=_get(row, "chat_type"),
            sessions=sessions,
            meta={
                "mode": row["mode"],
                "route_reason": row["route_reason"] or None,
                "business_job_id": row["business_job_id"] or None,
            },
        )
        if detail:
            ref.db_spans = self._control_spans(conn, row["task_id"])
            source = self._job_sources.get(row["scenario"])
            job = source.resolve(row["business_job_id"]) if source and row["business_job_id"] else None
            if job:
                ref.db_spans += job.db_spans
                ref.meta["current_phase"] = job.meta.get("current_phase")
        return ref

    @staticmethod
    def _control_spans(conn: sqlite3.Connection, task_id: str) -> list[Span]:
        return [
            Span(
                span_id=f"ctl-{row['id']}",
                kind=KIND_CONTROL,
                name=row["event_type"],
                started_at_ms=_secs_to_ms(row["created_at"]) or 0,
                source=SOURCE_DB,
            )
            for row in conn.execute(
                "SELECT * FROM control_events WHERE task_id = ? ORDER BY id", (task_id,)
            )
        ]

    def resolve(self, ident: str) -> Optional[TraceRef]:
        with closing(_connect(self._db_path)) as conn:
            for key in ("task_id", "business_job_id", "business_session_id", "router_session_id"):
                row = conn.execute(
                    f"SELECT * FROM task_runs WHERE {key} = ? LIMIT 1", (ident,)
                ).fetchone()
                if row:
                    return self._ref(conn, row, detail=True)
        return None

    def list_refs(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> list[TraceRef]:
        where, params = _where(status=status, chat_id=chat_id)
        with closing(_connect(self._db_path)) as conn:
            rows = conn.execute(
                f"SELECT * FROM task_runs {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._ref(conn, row, detail=False) for row in rows]


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _iso_to_ms(value: Any) -> int:
    """通用 Gateway 的时间是 UTC 的 ISO 字符串(无时区后缀)。"""
    import calendar
    import time as _time

    try:
        return calendar.timegm(_time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
    except (ValueError, TypeError):
        return 0


class ConversationSource:
    """通用 ``Gateway`` 的 ``conversations`` / ``session_history``(分析师、ada-pm,各 case 一个库)。

    这类 bot 没有 task / job,只有"会话 → session"。一个 session 里有很多轮对话,所以这里一行 =
    一个会话,状态没有成败之分,统一标 ``active`` / ``archived``;失败原因只能从事件流里找。

    会话键 ``chat_id:thread_id:user_open_id`` 里的 ``user_open_id`` 只在本应用内有效,私聊归属要靠
    ``user_id`` 列(升级前的行没有,为空串,此时私聊不放行)。
    """

    def __init__(self, scenario: str, db_path: str) -> None:
        self._scenario = scenario
        self._db_path = db_path

    @staticmethod
    def _split_key(key: str) -> tuple[str, str, str]:
        chat_id, _, rest = key.partition(":")
        thread_id, _, open_id = rest.partition(":")
        return chat_id, "" if thread_id == "-" else thread_id, "" if open_id == "-" else open_id

    def _ref(self, row: sqlite3.Row, *, archived: bool) -> TraceRef:
        chat_id, _thread, open_id = self._split_key(row["conversation_key"])
        started = _iso_to_ms(_get(row, "created_at") or _get(row, "updated_at"))
        return TraceRef(
            trace_id=row["session_id"],
            root_kind="session",
            scenario=self._scenario,
            status="archived" if archived else "active",
            started_at_ms=started or _iso_to_ms(_get(row, "updated_at")),
            finished_at_ms=_iso_to_ms(_get(row, "archived_at")) if archived else None,
            chat_id=_get(row, "chat_id") or chat_id,
            user_id=_get(row, "user_id"),
            chat_type=_get(row, "chat_type"),
            sessions=[SessionRef(row["session_id"], "business")],
            meta={
                "user_open_id": open_id or None,
                "agent_id": _get(row, "agent_id") or None,
                **({"archive_reason": _get(row, "reason")} if archived else {}),
            },
        )

    def _all(self, conn: sqlite3.Connection, where: str, params: tuple, limit: int) -> list[TraceRef]:
        refs: list[TraceRef] = []
        for row in conn.execute(
            f"SELECT * FROM conversations {where} ORDER BY updated_at DESC LIMIT ?", (*params, limit)
        ):
            refs.append(self._ref(row, archived=False))
        if _has_table(conn, "session_history"):
            hwhere = where.replace("updated_at", "archived_at")
            for row in conn.execute(
                f"SELECT * FROM session_history {hwhere} ORDER BY archived_at DESC LIMIT ?", (*params, limit)
            ):
                refs.append(self._ref(row, archived=True))
        return refs

    def resolve(self, ident: str) -> Optional[TraceRef]:
        with closing(_connect(self._db_path)) as conn:
            row = conn.execute("SELECT * FROM conversations WHERE session_id = ? LIMIT 1", (ident,)).fetchone()
            if row:
                return self._ref(row, archived=False)
            if _has_table(conn, "session_history"):
                row = conn.execute(
                    "SELECT * FROM session_history WHERE session_id = ? ORDER BY id DESC LIMIT 1", (ident,)
                ).fetchone()
                if row:
                    return self._ref(row, archived=True)
        return None

    def list_refs(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> list[TraceRef]:
        with closing(_connect(self._db_path)) as conn:
            # conversation_key 以 "chat_id:" 开头。不能用 LIKE:chat_id 里的 "_" 是单字符通配符,
            # 会把只差一个字符的别的群也匹配进来——这是权限边界,必须精确前缀。
            where, params = (
                ("WHERE substr(conversation_key, 1, ?) = ?", (len(chat_id) + 1, f"{chat_id}:")) if chat_id else ("", ())
            )
            refs = self._all(conn, where, params, limit)
        if status:
            refs = [r for r in refs if r.status == status]
        refs.sort(key=lambda r: r.started_at_ms, reverse=True)
        return refs[:limit]


class DigitalEmployeeSource:
    """数字员工的 ``sessions`` / ``session_history``(``topic_bot_sessions.db``)。

    会话键 ``chat_id:thread_id`` **不含用户**(群聊按话题共享)。``sessions`` 表本身没有 user_id /
    chat_type,所以:
    - 群聊:按 chat_id 判断,与\"群聊内共享\"一致;
    - 私聊:键里 thread 为 ``-``,归属从 ``session_memory_scopes`` 反推——单聊的记忆作用域是
      ``scope_type='user'``、``scope_id=employee_id``(user_id 优先),与 ``IncomingMessage.employee_id``
      同一口径。反推不到(没有记忆作用域记录)就不放行。
    """

    def __init__(self, scenario: str, db_path: str) -> None:
        self._scenario = scenario
        self._db_path = db_path

    def _owner(self, conn: sqlite3.Connection, session_id: str) -> tuple[str, str]:
        """返回 (chat_type, user_id);反推不出来时 chat_type 为空。"""
        if not _has_table(conn, "session_memory_scopes"):
            return "", ""
        row = conn.execute(
            "SELECT scope_type, scope_id FROM session_memory_scopes WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return "", ""
        if row["scope_type"] == "user":
            return "p2p", row["scope_id"]
        return "group", ""

    def _ref(self, conn: sqlite3.Connection, row: sqlite3.Row, *, archived: bool) -> TraceRef:
        chat_id, _, thread = str(row["key"]).partition(":")
        chat_type, user_id = self._owner(conn, row["session_id"])
        if not chat_type:
            # 没有记忆作用域记录:按键形态保守判断。群聊键必带 thread(话题),私聊 thread 为 "-"。
            chat_type = "group" if thread not in ("", "-") else ""
        created = _secs_to_ms(_get(row, "created_at", 0)) or 0
        return TraceRef(
            trace_id=row["session_id"],
            root_kind="session",
            scenario=self._scenario,
            status="archived" if archived else "active",
            started_at_ms=created,
            finished_at_ms=_secs_to_ms(_get(row, "archived_at", 0)) if archived else None,
            chat_id=_get(row, "chat_id") or chat_id,
            user_id=user_id,
            chat_type=chat_type,
            sessions=[SessionRef(row["session_id"], "business")],
            meta={"thread": thread if thread != "-" else None, **({"archive_reason": _get(row, "reason")} if archived else {})},
        )

    def resolve(self, ident: str) -> Optional[TraceRef]:
        with closing(_connect(self._db_path)) as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id = ? LIMIT 1", (ident,)).fetchone()
            if row:
                return self._ref(conn, row, archived=False)
            if _has_table(conn, "session_history"):
                row = conn.execute(
                    "SELECT * FROM session_history WHERE session_id = ? ORDER BY id DESC LIMIT 1", (ident,)
                ).fetchone()
                if row:
                    return self._ref(conn, row, archived=True)
        return None

    def list_refs(
        self,
        *,
        limit: int = 20,
        status: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> list[TraceRef]:
        # 同上:精确前缀,不用 LIKE(chat_id 里的 "_" 是通配符)。
        like = (len(chat_id) + 1, f"{chat_id}:") if chat_id else ()
        where = "WHERE substr(key, 1, ?) = ?" if chat_id else ""
        with closing(_connect(self._db_path)) as conn:
            refs = [
                self._ref(conn, row, archived=False)
                for row in conn.execute(
                    f"SELECT * FROM sessions {where} ORDER BY created_at DESC LIMIT ?", (*like, limit)
                )
            ]
            if _has_table(conn, "session_history"):
                refs += [
                    self._ref(conn, row, archived=True)
                    for row in conn.execute(
                        f"SELECT * FROM session_history {where} ORDER BY archived_at DESC LIMIT ?", (*like, limit)
                    )
                ]
        if status:
            refs = [r for r in refs if r.status == status]
        refs.sort(key=lambda r: r.started_at_ms, reverse=True)
        return refs[:limit]
