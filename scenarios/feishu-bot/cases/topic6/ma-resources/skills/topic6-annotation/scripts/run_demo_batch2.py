#!/usr/bin/env python3
"""Demo mode Phase C batch 2: run R1~R5 routes + C2 fast concurrently.

Replaces the 18-line bash block that Agents had to compose:
    run_demo_routes.py ... &
    run_topic6_c2.py --demo-fast ... &
    wait both, check exit codes

This script handles:
  - Creating output directories
  - Launching both processes in parallel
  - Waiting for both to complete
  - Structured JSON output with success/failure details

Exit codes:
  0 = both succeeded
  1 = one or both failed

Usage:
  python run_demo_batch2.py \
    --project-dir "{project_dir}" \
    --input "{project_dir}/04_标注/_可用子集/usable_subset_demo_r{N}.xlsx" \
    --run-id {N}
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
# run_demo_routes.py is in topic6-annotation skill
ANNOTATION_SCRIPTS = SCRIPT_DIR.parent.parent / "topic6-annotation" / "scripts"
# run_topic6_c2.py is in topic6-event-registry skill (same directory as this script)
EVENT_REGISTRY_SCRIPTS = SCRIPT_DIR


def _resolve_project_dir(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path("/workspace") / path


def _run_process(
    name: str, cmd: list[str], log_path: Path, timeout: int,
) -> dict:
    """Run a subprocess, capture output to log, return result dict."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = time.time()
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            result = subprocess.run(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                timeout=timeout,
            )
        elapsed = time.time() - started_at
        return {
            "name": name,
            "status": "success" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "elapsed_seconds": round(elapsed, 1),
            "log": str(log_path),
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - started_at
        return {
            "name": name,
            "status": "timeout",
            "exit_code": -1,
            "elapsed_seconds": round(elapsed, 1),
            "log": str(log_path),
        }
    except Exception as err:
        elapsed = time.time() - started_at
        return {
            "name": name,
            "status": "error",
            "exit_code": -1,
            "error": str(err),
            "elapsed_seconds": round(elapsed, 1),
            "log": str(log_path),
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--input", required=True,
                        help="Path to usable_subset_demo_r{N}.xlsx")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=900,
                        help="Max seconds to wait for each process (default: 900)")
    args = parser.parse_args()

    project = _resolve_project_dir(args.project_dir)
    c2_dir = project / "04_标注" / "C2_事件归档"
    c2_dir.mkdir(parents=True, exist_ok=True)

    # Resolve script paths — support both /mnt/skills/ (MA sandbox) and local
    demo_routes_script = ANNOTATION_SCRIPTS / "run_demo_routes.py"
    c2_script = EVENT_REGISTRY_SCRIPTS / "run_topic6_c2.py"

    # Also check /mnt/skills/ paths (MA sandbox environment)
    if not demo_routes_script.exists():
        alt = Path("/mnt/skills/topic6-annotation/scripts/run_demo_routes.py")
        if alt.exists():
            demo_routes_script = alt
    if not c2_script.exists():
        alt = Path("/mnt/skills/topic6-event-registry/scripts/run_topic6_c2.py")
        if alt.exists():
            c2_script = alt

    routes_cmd = [
        sys.executable, str(demo_routes_script),
        "--project-dir", str(project),
        "--input", str(args.input),
        "--run-id", str(args.run_id),
    ]
    c2_cmd = [
        sys.executable, str(c2_script),
        "--project-dir", str(project),
        "--mode", "demo",
        "--run-id", str(args.run_id),
        "--demo-fast",
    ]

    routes_log = project / "04_标注" / "demo_routes.log"
    c2_log = c2_dir / "c2_runner.log"

    print(f"Starting demo batch 2: routes + C2 fast (timeout={args.timeout}s)", flush=True)

    # Launch both in parallel using subprocess + threading
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        routes_future = pool.submit(
            _run_process, "demo_routes", routes_cmd, routes_log, args.timeout,
        )
        c2_future = pool.submit(
            _run_process, "c2_fast", c2_cmd, c2_log, args.timeout,
        )
        routes_result = routes_future.result()
        c2_result = c2_future.result()

    all_ok = (
        routes_result["status"] == "success"
        and c2_result["status"] == "success"
    )
    summary = {
        "status": "success" if all_ok else "failed",
        "routes": routes_result,
        "c2": c2_result,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if not all_ok:
        # Print failure details to stderr for Agent visibility
        for r in [routes_result, c2_result]:
            if r["status"] != "success":
                print(
                    f"❌ {r['name']} {r['status']} (exit={r['exit_code']}), "
                    f"log: {r['log']}",
                    file=sys.stderr,
                )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
