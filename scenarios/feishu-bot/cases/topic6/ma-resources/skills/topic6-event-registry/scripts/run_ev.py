#!/usr/bin/env python3
"""Candidate E/V runner. Default prints plan; --execute runs configured services.

Only this E/V segment has verified checkpoints. This is not a whole C2 runner.
Stage subprocesses own separate run directories and ledgers. A single coordinator
publishes outputs after checking both results against the fixed Frame snapshot.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

from c2_runtime import atomic_json, digest, exclusive_lock, file_hash, unique_ids
from relay import read_jsonl

SCRIPTS = Path(__file__).resolve().parent


def fingerprint():
    return {p.name: file_hash(p) for p in sorted(SCRIPTS.glob("*.py"))}


def stage_run(stage, root, snapshot, args, contract):
    stage_config = {"stage": stage, "input": file_hash(snapshot),
                    "contract": contract, "code": fingerprint()}
    key = digest(stage_config)
    directory = root / "stages" / stage / key
    receipt = directory / "receipt.json"
    if receipt.exists():
        try:
            old = json.loads(receipt.read_text(encoding="utf-8"))
        except ValueError:
            old = {}
        if old.get("status") == "COMPLETE" and old.get("outputs") and all(
                (directory / name).exists() and file_hash(directory / name) == expected
                for name, expected in old["outputs"].items()):
            return {**old, "checkpoint_hit": True}
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "work").mkdir(exist_ok=True)
    shutil.copyfile(snapshot, directory / "work/frames.jsonl")
    attempt = uuid.uuid4().hex
    if stage == "E":
        command = ["03_normalize_entities.py", "--model", args.chat_model,
                   "--model-revision", args.chat_revision or args.chat_model,
                   "--batch-size", str(args.entity_batch_size)]
        artifacts = ["work/frames_resolved.jsonl", "work/entities.jsonl", "run_manifest.json"]
    else:
        command = ["04_build_embeddings.py", "--model", args.embedding_model,
                   "--frame-input", "original",
                   "--model-revision", args.embedding_revision or args.embedding_model,
                   "--batch-size", str(args.embedding_batch_size),
                   "--concurrency", str(max(1, args.max_requests - (args.schedule == "parallel")))]
        artifacts = ["work/embeddings.json", "run_manifest.json"]
    command = [sys.executable, "-B", "-X", "utf8", str(SCRIPTS / command[0]),
               *command[1:], "--run-dir", str(directory)]
    start = time.monotonic()
    result = {"stage": stage, "stage_dir": str(directory), "attempt": attempt,
              "input_sha256": file_hash(snapshot), "status": "RUNNING", "checkpoint_hit": False}
    atomic_json(receipt, result)
    # Save every attempt independently; do not overwrite a previous failure log.
    with (directory / (attempt + ".log")).open("w", encoding="utf-8") as log:
        try:
            process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                     timeout=args.stage_timeout, check=False)
            result.update(exit_code=process.returncode,
                          status="COMPLETE" if process.returncode == 0 else "FAILED")
        except subprocess.TimeoutExpired:
            result.update(exit_code=None, status="FAILED", failure="STAGE_TIMEOUT")
    result["wall_seconds"] = round(time.monotonic() - start, 6)
    result["outputs"] = {name: file_hash(directory / name) for name in artifacts if (directory / name).exists()}
    if result["status"] == "COMPLETE" and len(result["outputs"]) != len(artifacts):
        result.update(status="FAILED", failure="MISSING_STAGE_OUTPUT")
    if result["status"] == "COMPLETE":
        entries = json.loads((directory / "run_manifest.json").read_text(encoding="utf-8"))["cost_ledger"]
        if entries and entries[-1].get("quality_status") == "DEGRADED":
            result["status"] = "DEGRADED"
    atomic_json(directory / (attempt + ".receipt.json"), result)
    atomic_json(receipt, result)
    return result


def validate_pair(snapshot, results):
    expected = list(read_jsonl(snapshot))
    ids = unique_ids(expected)
    e = Path(results["E"]["stage_dir"])
    v = Path(results["V"]["stage_dir"])
    resolved = list(read_jsonl(e / "work/frames_resolved.jsonl"))
    if unique_ids(resolved) != ids:
        raise ValueError("E record coverage/order mismatch")
    import importlib.util
    spec = importlib.util.spec_from_file_location("frame_embedding", SCRIPTS / "04_build_embeddings.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    embeddings = json.loads((v / "work/embeddings.json").read_text(encoding="utf-8"))
    if embeddings["input_sha256"] != file_hash(snapshot) or set(embeddings["vectors"]) != set(ids):
        raise ValueError("V snapshot/coverage mismatch")
    for before, after in zip(expected, resolved):
        if mod.frame_text(before) != mod.frame_text(after):
            raise ValueError("E modified facts consumed by V; parallel contract no longer holds")
        if embeddings["texts"][before["record_id"]] != mod.frame_text(before):
            raise ValueError("V text does not match input Frame")
    return e, v


def execute(args):
    root = args.run_dir.resolve()
    with exclusive_lock(root / ".ev.lock"):
        atomic_json(root / "ev_result.json", {"schema": "c2-ev/1", "scope": "E/V only", "status": "RUNNING"})
        original = args.frames.resolve()
        try:
            rows = list(read_jsonl(original))
            unique_ids(rows)
            snapshot = root / "snapshots" / (file_hash(original) + ".jsonl")
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if snapshot.exists() and file_hash(snapshot) != file_hash(original):
                raise ValueError("Immutable snapshot was modified")
            if not snapshot.exists():
                shutil.copyfile(original, snapshot)
            if file_hash(snapshot) != file_hash(original):
                raise ValueError("Input changed during snapshot creation")
        except (ValueError, OSError) as exc:
            atomic_json(root / "ev_result.json", {"schema": "c2-ev/1", "scope": "E/V only",
                                                  "status": "FAILED", "failure": str(exc)})
            return 2
        contract = {"chat_model": args.chat_model, "chat_revision": args.chat_revision,
                    "embedding_model": args.embedding_model, "embedding_revision": args.embedding_revision,
                    "entity_batch_size": args.entity_batch_size,
                    "embedding_batch_size": args.embedding_batch_size,
                    "endpoint_hashes": [digest(os.environ.get(k, "")) for k in
                                        ("LLM_RELAY_BASE_URL", "EMBEDDING_BASE_URL")]}
        result = {"schema": "c2-ev/1", "scope": "E/V only", "status": "RUNNING",
                  "input_sha256": file_hash(snapshot), "contract": contract,
                  "schedule": args.schedule, "max_inflight_requests": args.max_requests,
                  "attempt": uuid.uuid4().hex}
        atomic_json(root / "ev_result.json", result)
        started = time.monotonic()
        stages = {}
        try:
            if args.schedule == "parallel":
                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = {s: pool.submit(stage_run, s, root, snapshot, args, contract) for s in ("E", "V")}
                    stages = {s: job.result() for s, job in jobs.items()}
            else:
                stages = {s: stage_run(s, root, snapshot, args, contract) for s in ("E", "V")}
            result["stages"] = stages
            if any(r["status"] not in ("COMPLETE", "DEGRADED") for r in stages.values()):
                raise ValueError("STAGE_FAILED; successful sibling checkpoint preserved")
            e, v = validate_pair(snapshot, stages)
            ledger = []
            for r in stages.values():
                ledger.extend(json.loads((Path(r["stage_dir"]) / "run_manifest.json").read_text(encoding="utf-8"))["cost_ledger"])
            quality = "DEGRADED" if any(r["status"] == "DEGRADED" for r in stages.values()) else "COMPLETE"
            # Immutable result bundle + atomic receipt pointer. No mixed root work files.
            bundle = root / "results" / result["attempt"]
            (bundle / "work").mkdir(parents=True)
            for src in (snapshot, e / "work/frames_resolved.jsonl", e / "work/entities.jsonl", v / "work/embeddings.json"):
                shutil.copyfile(src, bundle / "work" / ("frames.jsonl" if src == snapshot else src.name))
            if (e / "out").exists():
                shutil.copytree(e / "out", bundle / "out")
            atomic_json(bundle / "run_manifest.json", {"cost_ledger": ledger,
                "cost_scope": "successful stage reports only; failed-call cost may be unknown"})
            result.update(status=quality, bundle=str(bundle),
                          outputs={p.relative_to(bundle).as_posix(): file_hash(p) for p in bundle.rglob("*") if p.is_file()})
        except Exception as exc:
            result.update(status="FAILED", failure=str(exc), stages=stages)
        result["wall_seconds"] = round(time.monotonic() - started, 6)
        result["evidence_level"] = "execution only; semantic quality and real-service performance not accepted"
        atomic_json(root / "attempts" / (result["attempt"] + ".json"), result)
        atomic_json(root / "ev_result.json", result)
        print(json.dumps({k: result[k] for k in ("status", "wall_seconds", "scope")}, ensure_ascii=False))
        return 0 if result["status"] == "COMPLETE" else 2


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--frames", required=True, type=Path)
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--chat-model", default="claude-sonnet-4-6")
    ap.add_argument("--embedding-model", default="Doubao-embedding")
    ap.add_argument("--chat-revision")
    ap.add_argument("--embedding-revision")
    ap.add_argument("--entity-batch-size", type=int, default=50)
    ap.add_argument("--embedding-batch-size", type=int, default=64)
    ap.add_argument("--max-requests", type=int, default=4)
    ap.add_argument("--stage-timeout", type=float, default=3600)
    ap.add_argument("--schedule", choices=["serial", "parallel"], default="parallel")
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    if min(args.entity_batch_size, args.embedding_batch_size, args.max_requests, args.stage_timeout) <= 0:
        ap.error("limits must be positive")
    if args.schedule == "parallel" and args.max_requests < 2:
        ap.error("parallel E/V needs max-requests >= 2; use serial for a single slot")
    if not args.execute:
        print(json.dumps({"stages": ["E", "V"], "schedule": args.schedule,
                          "max_requests": args.max_requests, "execution": "NOT_STARTED"}))
        return 0
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
