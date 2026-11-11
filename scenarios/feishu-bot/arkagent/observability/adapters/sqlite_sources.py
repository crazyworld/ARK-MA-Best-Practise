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

    def list_refs(self, *, limit: int = 20, status: Optional[str] = None) -> list[TraceRef]:
        where, params = ("WHERE status = ?", (status,)) if status else ("", ())
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

    def list_refs(self, *, limit: int = 20, status: Optional[str] = None) -> list[TraceRef]:
        where, params = ("WHERE status = ?", (status,)) if status else ("", ())
        with closing(_connect(self._db_path)) as conn:
            rows = conn.execute(
                f"SELECT * FROM task_runs {where} ORDER BY created_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._ref(conn, row, detail=False) for row in rows]
