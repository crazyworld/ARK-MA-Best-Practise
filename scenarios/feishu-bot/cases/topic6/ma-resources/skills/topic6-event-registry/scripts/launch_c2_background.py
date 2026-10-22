#!/usr/bin/env python3
"""Launch run_topic6_c2.py as a background process with proper PID management.

Replaces the nohup shell pattern that Agents had to compose manually:
    nohup python run_topic6_c2.py ... > log 2>&1 &
    echo $! > c2_runner.pid

This script handles:
  - Creating the output directory
  - Launching the C2 runner as a detached subprocess
  - Capturing PID to a file
  - Idempotent: if C2 is already running (PID alive), reports it and exits 0

Exit codes:
  0 = launched successfully (or already running)
  1 = launch failed

Usage:
  python launch_c2_background.py \
    --project-dir "{project_dir}" --mode {mode} --run-id {N}
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
C2_RUNNER = SCRIPT_DIR / "run_topic6_c2.py"


def _resolve_project_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path("/workspace") / path


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _read_pid(pid_path: Path) -> int:
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--mode", required=True, choices=["full", "skip_sampling", "demo"])
    parser.add_argument("--run-id", type=int, required=True)
    # Pass-through arguments for run_topic6_c2.py
    parser.add_argument("--demo-fast", action="store_true", default=False)
    args = parser.parse_args()

    project = _resolve_project_dir(args.project_dir)
    c2_dir = project / "04_标注" / "C2_事件归档"
    c2_dir.mkdir(parents=True, exist_ok=True)
    log_path = c2_dir / "c2_runner.log"
    pid_path = c2_dir / "c2_runner.pid"

    # Idempotent: check if already running
    existing_pid = _read_pid(pid_path)
    if existing_pid > 0 and _pid_is_running(existing_pid):
        result = {
            "status": "already_running",
            "pid": existing_pid,
            "log": str(log_path),
            "pid_file": str(pid_path),
        }
        print(json.dumps(result, ensure_ascii=False))
        return 0

    # Build command
    cmd = [
        sys.executable, str(C2_RUNNER),
        "--project-dir", str(project),
        "--mode", args.mode,
        "--run-id", str(args.run_id),
    ]
    if args.demo_fast:
        cmd.append("--demo-fast")

    # Launch as detached subprocess
    try:
        with open(log_path, "a", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach from parent process group
            )
    except Exception as err:
        result = {"status": "launch_failed", "error": str(err)}
        print(json.dumps(result, ensure_ascii=False))
        return 1

    # Write PID file atomically
    pid_tmp = pid_path.with_suffix(".tmp")
    pid_tmp.write_text(str(proc.pid), encoding="utf-8")
    os.replace(pid_tmp, pid_path)

    result = {
        "status": "launched",
        "pid": proc.pid,
        "log": str(log_path),
        "pid_file": str(pid_path),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
