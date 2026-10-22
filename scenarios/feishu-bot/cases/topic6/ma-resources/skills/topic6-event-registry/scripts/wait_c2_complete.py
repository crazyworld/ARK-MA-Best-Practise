#!/usr/bin/env python3
"""Wait for C2 event-archive background process without crossing the MA bash limit.

Same pattern as wait_datahub_batch.py: poll c2_status.json every few seconds,
return within --wait-seconds (default 105, capped at 110), print a JSON summary.

Exit code:
  0  = done or running (caller should re-invoke if status == "running")
  1  = failed

Agent prompt should call this with bash timeout=115, and re-invoke on "running".
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path


def _resolve_project_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path("/workspace") / path


def _pid_is_running(pid_path: Path) -> bool:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def inspect_c2(project: Path, run_id: int) -> dict:
    """Read c2_status.json and return a normalized status dict."""
    c2_dir = project / "04_标注" / "C2_事件归档" / "c2_run"
    status_path = c2_dir / "c2_status.json"
    pid_path = c2_dir.parent / "c2_runner.pid"

    if not status_path.exists():
        # C2 hasn't started writing status yet
        if _pid_is_running(pid_path):
            return {"status": "running", "detail": "c2_status.json not yet created, process alive"}
        return {"status": "missing", "detail": "c2_status.json not found, no running process"}

    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        return {"status": "running", "detail": f"c2_status.json unreadable: {err}"}

    c2_status = str(status.get("status", "")).strip().lower()
    c2_run_id = status.get("run_id")

    # Check run_id match if provided
    if run_id > 0 and c2_run_id is not None and int(c2_run_id) != run_id:
        return {
            "status": "running",
            "detail": f"run_id mismatch: expected {run_id}, found {c2_run_id}",
        }

    if c2_status == "done":
        return {
            "status": "done",
            "current_stage": status.get("current_stage", "done"),
            "completed_stages": status.get("completed_stages", []),
            "output_file": status.get("output_file", ""),
            "run_id": c2_run_id,
        }

    if c2_status == "failed":
        return {
            "status": "failed",
            "current_stage": status.get("current_stage", ""),
            "error": str(status.get("error", "unknown error"))[:300],
            "run_id": c2_run_id,
        }

    # running or pending — include progress info
    current_stage = status.get("current_stage", "")
    completed = status.get("completed_stages", [])

    # Try to read the latest log line for progress indication
    progress_hint = ""
    if current_stage:
        log_path = c2_dir / "logs" / f"{current_stage}.log"
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
                if lines:
                    progress_hint = lines[-1][:100]
            except OSError:
                pass

    return {
        "status": "running",
        "current_stage": current_stage,
        "completed_stages": completed,
        "progress": progress_hint,
        "run_id": c2_run_id,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--run-id", type=int, default=0,
                        help="Expected run_id; 0 = don't check")
    parser.add_argument("--wait-seconds", type=int, default=105,
                        help="Max seconds to poll (capped at 110)")
    parser.add_argument("--interval", type=int, default=5,
                        help="Seconds between checks (1-20)")
    args = parser.parse_args()

    wait_seconds = min(max(args.wait_seconds, 0), 110)
    interval = min(max(args.interval, 1), 20)
    project = _resolve_project_dir(args.project_dir)
    deadline = time.monotonic() + wait_seconds

    while True:
        result = inspect_c2(project, args.run_id)
        if result["status"] != "running" or time.monotonic() >= deadline:
            print(json.dumps(result, ensure_ascii=False))
            return 1 if result["status"] == "failed" else 0
        time.sleep(min(interval, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
