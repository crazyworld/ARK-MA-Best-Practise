#!/usr/bin/env python3
"""Unified C2 workflow: plan/run, input-bound human reviews, resume and publication.

MA 适配（Topic6，2026-11-05）：新增三个显式开关，默认值保持 rc.14 原语义。
  --front-workers N        跨平台 F1 同时跑的平台数（默认 1 = 原串行）；总并发 = N × --max-requests。
  --review-policy record   门禁不停：写 review_requests/*.json 并汇总到 c2_quality.review_pending，
                           由 Topic6 HC1/HC2 人工复核；strict（默认）= 原 WAITING_REVIEW 停等。
  --degraded-policy accept 07 块级降级/03 实体漏返不阻断正式输出，降级明细保留在 c2_quality；
                           block（默认）= 原 DEGRADED 阻断。
x2 边界带交模型裁定时透传 --model（MA relay 未开通 Claude）。
"""
from __future__ import annotations
import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid
from c2_runtime import atomic_json, digest, exclusive_lock, file_hash, unique_ids
from c2_quality import inspect_ownership
from relay import read_jsonl, write_csv, write_jsonl
from f1_extract import CLEAN

SCRIPTS = Path(__file__).resolve().parent
VERSION = "3.0.0-rc.14"


class StopWorkflow(Exception):
    def __init__(self, status, **details):
        self.status, self.details = status, details


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def hashes(root):
    result = {}
    for name in ("work", "out"):
        for p in sorted((root / name).rglob("*")):
            if p.is_file():
                result[p.relative_to(root).as_posix()] = file_hash(p)
    if (root / "run_manifest.json").exists():
        result["run_manifest.json"] = file_hash(root / "run_manifest.json")
    return result


def matches(root, expected):
    return bool(expected) and all((root / name).is_file() and file_hash(root / name) == sha for name, sha in expected.items())


def copy_payload(source, target):
    target.mkdir(parents=True, exist_ok=True)
    for name in ("work", "out"):
        if (source / name).exists():
            shutil.copytree(source / name, target / name, dirs_exist_ok=True)
    if (source / "run_manifest.json").exists():
        shutil.copyfile(source / "run_manifest.json", target / "run_manifest.json")


def invoke(command, env, log, timeout):
    proc = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=os.name != "nt")
    try:
        return proc.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise


class Pipeline:
    def __init__(self, root, config):
        self.root, self.config = root, config
        self.trace = []
        self.pending_reviews = []  # MA review-policy=record：未停等的审核项

    @property
    def record_reviews(self):
        return self.config.get("review_policy") == "record"

    @property
    def accept_degraded(self):
        return self.config.get("degraded_policy") == "accept"

    def node(self, name, parent, command=None, action=None, extra=None, before=None, tolerate=None):
        key = digest({"name": name, "input": hashes(parent), "config": self.config, "extra": extra})
        node = self.root / "stages" / name / key
        pointer = node / "receipt.json"
        previous = read(pointer) if pointer.exists() else {}
        if previous.get("status") == "COMPLETE" and matches(Path(previous["directory"]), previous["outputs"]):
            self.trace.append({"node": name, "checkpoint_hit": True, "receipt": str(pointer)})
            return Path(previous["directory"])
        attempt = node / "attempts" / uuid.uuid4().hex
        copy_payload(parent, attempt)
        if previous.get("directory") and (Path(previous["directory"]) / "raw").exists():
            shutil.copytree(Path(previous["directory"]) / "raw", attempt / "raw")
        if previous.get("directory") and (Path(previous["directory"]) / "ev").exists():
            shutil.copytree(Path(previous["directory"]) / "ev", attempt / "ev")
        result = {"node": name, "directory": str(attempt), "status": "RUNNING", "input_hash": key}
        atomic_json(pointer, result)
        start = time.monotonic()
        try:
            if before:
                before(attempt)
            if command:
                argv = command(attempt)
                env = {**os.environ, "C2_CACHE_CONTEXT": key, "C2_STRICT_REVIEW": "1", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
                with (attempt / "execution.log").open("w", encoding="utf-8") as log:
                    code = invoke([sys.executable, "-B", "-X", "utf8", str(SCRIPTS / argv[0]), *map(str, argv[1:])],
                                  env, log, self.config["stage_timeout"])
                if code and not (tolerate and tolerate(code, attempt)):
                    raise StopWorkflow("WAITING_REVIEW" if code == 3 else "FAILED", node=name,
                                       exit_code=code, log=str(attempt / "execution.log"),
                                       review_file=str(attempt / "out/x2_待裁定_边界带.csv") if code == 3 else None)
            if action:
                action(attempt)
            result.update(status="COMPLETE", outputs=hashes(attempt))
        except StopWorkflow as exc:
            result.update(status=exc.status, details=exc.details)
            raise
        except Exception as exc:
            result.update(status="FAILED", error_type=type(exc).__name__, error=str(exc))
            raise
        finally:
            result["wall_seconds"] = time.monotonic() - start
            atomic_json(attempt / "receipt.json", result)
            atomic_json(pointer, result)
            self.trace.append({"node": name, "checkpoint_hit": False, "receipt": str(pointer), "status": result["status"]})
        return attempt

    def script(self, name, parent, script, *flags):
        return self.node(name, parent, lambda d: [script, "--run-dir", d, *flags])

    def gate(self, name, parent, files, guidance):
        fingerprint = {f: file_hash(parent / f) for f in files}
        key = digest({"gate": name, "files": fingerprint, "config": self.config})
        acknowledgement = self.root / "reviews" / (key + ".json")
        if acknowledgement.exists() and read(acknowledgement).get("approved") is True:
            return
        request = {"gate": name, "id": key, "directory": str(parent), "files": fingerprint, "guidance": guidance}
        path = self.root / "review_requests" / (name + ".json")
        atomic_json(path, request)
        if self.record_reviews:
            self.pending_reviews.append({"gate": name, "request": str(path), "files": sorted(files)})
            return
        raise StopWorkflow("WAITING_REVIEW", gate=name, request=str(path), guidance=guidance)

    def execute(self, initial, member_verdict=None):
        cfg = self.config
        source = initial / "work/source.csv"
        def front_one(platform):
            flags = ["--input", source, "--platform", platform, "--model", cfg["chat_model"], "--concurrency", cfg["max_requests"]]
            if cfg["heat_col"]:
                flags += ["--heat-col", cfg["heat_col"]]
            script = "f1_extract.py" if cfg["frontend"] == "fused" else "00_clean_titles.py"
            return self.script("F1_" + platform, initial, script, *flags)
        platforms = [] if cfg.get('front_seed') else list(cfg["platforms"])
        # MA 提速：各平台前端互不依赖（独立节点目录与缓存），按 front_workers 并行；pool.map 保序，
        # 合流结果与串行一致。front_workers=1（默认）即 rc.14 原串行。
        workers = min(int(cfg.get("front_workers") or 1), len(platforms))
        if workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=workers) as pool:
                front = list(pool.map(front_one, platforms))
        else:
            front = [front_one(p) for p in platforms]
        def merge_front(d):
            ledger = []
            for part in front:
                if (part / "run_manifest.json").exists():
                    ledger.extend(read(part / "run_manifest.json").get("cost_ledger", []))
            atomic_json(d / "run_manifest.json", {"cost_ledger": ledger})
            for name in (["clean_titles", "eventness", "frames", "f1_repairs"] if cfg["frontend"] == "fused" else ["clean_titles"]):
                rows = [r for part in front for r in read_jsonl(part / "work" / (name + ".jsonl"))]
                if name != "f1_repairs":
                    unique_ids(rows)
                write_jsonl(d / "work" / (name + ".jsonl"), rows)
            for name in ("00_clean_titles.csv", "01_eventness.csv"):
                rows = []
                fields = None
                for part in front:
                    if (part / "out" / name).exists():
                        with (part / "out" / name).open(encoding="utf-8-sig", newline="") as h:
                            reader = csv.DictReader(h); fields = reader.fieldnames; rows.extend(reader)
                if fields:
                    write_csv(d / "out" / name, rows, fields)
        if cfg.get('front_seed'):
            from front_recovery import copy_seed
            current = self.node('front_reuse',initial,action=lambda d:copy_seed(cfg['front_seed'],d))
        else:
            current = self.node("front_merge", initial, action=merge_front, extra=[hashes(p) for p in front])
        self.gate("cleaning", current, ["out/00_clean_titles.csv"], "逐条核对原标题与清洗标题，重点检查回退、数字、情态和主体保留。")
        if cfg["frontend"] == "legacy":
            current = self.script("eventness", current, "01_eventness.py", "--model", cfg["chat_model"], "--concurrency", cfg["max_requests"])
        if cfg["frontend"] == "legacy":
            current = self.script("frames", current, "02_extract_frames.py", "--model", cfg["chat_model"], "--concurrency", cfg["max_requests"])
        if cfg.get('supplement_bundle'):
            current = self.script('front_evidence',current,'front_evidence.py','--source',cfg['input'],
                                  '--model',cfg['chat_model'],'--concurrency',cfg['max_requests'])
        from front_evidence import pending
        current = self.node('front_pending',current,action=pending)
        review_files=['work/eventness.jsonl','work/frames.jsonl','work/front_evidence_pending.jsonl']
        if cfg.get('supplement_bundle'):
            review_files += ['work/supplement_evidence.json','out/front_evidence_review.json']
        self.gate("non_events", current, review_files, "核对非事件、补证重判与重算Frame；未决保持UNCERTAIN，最终按原热点保留，不单因未决阻断。发现误排或事实错误仍需修正。新增证据用新目录及--reuse-front-run回流。")
        frames = list(read_jsonl(current / "work/frames.jsonl"))
        if cfg.get("supplement_bundle"):
            def evidence_attach(d):
                shutil.copyfile(initial / "work/supplement_evidence.json", d / "out/c2_supplement_evidence.json")
            current = self.node("supplement_attach", current, action=evidence_attach)
        if frames:
            def ev_cmd(d):
                return ["run_ev.py", "--frames", d / "work/frames.jsonl", "--run-dir", d / "ev", "--execute",
                        "--chat-model", cfg["chat_model"], "--embedding-model", cfg["embedding_model"],
                        "--chat-revision", cfg["chat_revision"], "--embedding-revision", cfg["embedding_revision"],
                        "--max-requests", cfg["max_requests"], "--stage-timeout", max(1, cfg["stage_timeout"] - 10),
                        "--schedule", "parallel" if cfg["max_requests"] >= 2 else "serial"]
            def ev_collect(d):
                result = read(d / "ev/ev_result.json")
                if result["status"] != "COMPLETE" and not (self.accept_degraded and result["status"] == "DEGRADED"):
                    raise StopWorkflow("DEGRADED", node="EV")
                bundle = Path(result["bundle"])
                if not matches(bundle, result["outputs"]):
                    raise ValueError("E/V output checksum mismatch")
                for p in (bundle / "work").iterdir():
                    shutil.copyfile(p, d / "work" / p.name)
                if (bundle / "out").exists():
                    shutil.copytree(bundle / "out", d / "out", dirs_exist_ok=True)
                manifest = read(d / "run_manifest.json") if (d / "run_manifest.json").exists() else {"cost_ledger": []}
                manifest["cost_ledger"].extend(read(bundle / "run_manifest.json").get("cost_ledger", []))
                atomic_json(d / "run_manifest.json", manifest)
            ev_tolerate = lambda code, d: (self.accept_degraded and code == 2 and
                                           read(d / "ev/ev_result.json").get("status") == "DEGRADED")
            current = self.node("EV", current, ev_cmd, ev_collect, tolerate=ev_tolerate)
            current = self.script("recall", current, "05_recall_candidates.py", "--top-k", cfg["top_k"])
            current = self.script("blocks", current, "06_build_blocks.py", "--cap", cfg["cap"], "--out-budget", cfg["out_budget"])
            def archive_coverage(d):
                source_rows = list(csv.DictReader(source.open(encoding="utf-8-sig", newline="")))
                quality = inspect_ownership(source_rows, list(read_jsonl(d / "work/block_events.jsonl")), list(read_jsonl(d / "work/eventness.jsonl")))
                if (quality["degraded_ids"] or quality["technical_missing_ids"]) and not self.accept_degraded:
                    raise StopWorkflow("DEGRADED", node="archive", quality=quality)
            current = self.node("archive", current,
                lambda d: ["07_block_archive.py", "--run-dir", d, "--model", cfg["chat_model"], "--concurrency", cfg["max_requests"]],
                action=archive_coverage)
            extra = file_hash(member_verdict) if member_verdict else None
            def member_before(d):
                if member_verdict:
                    (d / "out").mkdir(exist_ok=True)
                    shutil.copyfile(member_verdict, d / "out/x2_待裁定_边界带.csv")
            current = self.node("members", current,
                lambda d: ["x2_confidence_filter.py", "--run-dir", d, "--verdict", cfg["verdict"],
                           "--concurrency", cfg["max_requests"], *(["--apply-verdict"] if member_verdict else []),
                           *(["--model", cfg["chat_model"]] if cfg["verdict"] != "off" else [])],
                extra=extra, before=member_before,
                tolerate=lambda code, d: self.record_reviews and code == 3)
            x2_status = current / "out/x2_status.json"
            if x2_status.exists() and read(x2_status).get("status") == "WAITING_REVIEW":
                self.pending_reviews.append({"gate": "member_verdicts", "request": str(x2_status),
                                             "pending_ids": read(x2_status).get("pending_ids", [])})
            for direction in ("merge", "split"):
                current = self.script("review_" + direction, current, "x3_review_bidirectional.py", "--only", direction,
                                      "--model", cfg["chat_model"], "--concurrency", cfg["max_requests"])
        else:
            def empty(d):
                for name in ("frames_resolved", "entities", "block_events", "block_relations", "blocks", "skipped_edges"):
                    write_jsonl(d / "work" / (name + ".jsonl"), [])
                atomic_json(d / "work/embeddings.json", {"model": cfg["embedding_model"], "dim": 0, "vectors": {}, "texts": {}})
                write_csv(d / "out/块内归档结果.csv", [], ["event_id", "一级_事件名", "成员数"])
            current = self.node("empty_events", current, action=empty)
        from retain_original import retain_original
        current = self.node('retain_original', current,
                            action=lambda d: retain_original(d, cfg['window'], allow_degraded=self.accept_degraded))
        if cfg["previous_registry"]:
            current = self.script("window_link", current, "link_registry.py", "--prev-registry", initial / "work/previous_registry.jsonl",
                                  "--window", cfg["window"], "--model", cfg["chat_model"], "--concurrency", cfg["max_requests"])
        if cfg["mode"] == "single":
            current = self.script("ranking", current, "08_rank_events.py")
        current = self.script("registry", current, "09_commit_registry.py", "--window", cfg["window"])
        if cfg["previous_registry"]:
            def carry(d):
                previous = list(read_jsonl(initial / "work/previous_registry.jsonl"))
                new = list(read_jsonl(d / "out/registry_snapshot.jsonl"))
                seen = {e["event_id"] for e in new}
                new.extend({**e, "windows_since_update": int(e.get("windows_since_update", 0)) + 1} for e in previous if e["event_id"] not in seen)
                write_jsonl(d / "out/registry_snapshot.jsonl", new)
                manifest = read(d / "out/output_manifest.json")
                manifest.update(snapshot_events=len(new), prior_events_carried=sum(e["event_id"] not in seen for e in previous))
                atomic_json(d / "out/output_manifest.json", manifest)
            current = self.node("carry_history", current, action=carry)
        current = self.script("detail", current, "x4_detail_table.py", "--source", source,
                              *([] if self.accept_degraded else ["--strict-quality"]))
        with (current / "out/human_review.csv").open(encoding="utf-8-sig", newline="") as h:
            pending_names = list(csv.DictReader(h))
        evidence_pending=list(read_jsonl(current/'work/front_evidence_pending.jsonl'))
        retained = {r['record_id'] for r in read(current / 'out/original_retained.json')['records']}
        unresolved = [r for r in evidence_pending if r['record_id'] not in retained]
        supplement_review = False
        if cfg.get('supplement_bundle'):
            from supplement import attach_frames
            contexts = {r['record_id']: {} for r in read_jsonl(current / 'work/eventness.jsonl')}
            attach_frames(contexts, read(current / 'work/supplement_evidence.json'))
            supplement_review = any(rid not in retained and c.get('supplement_queries') for rid, c in contexts.items())
        if pending_names or supplement_review or unresolved:
            review_files = ["out/human_review.csv", "work/block_events.jsonl"]
            review_files.append('work/front_evidence_pending.jsonl')
            review_files.append('out/original_retained.json')
            if cfg.get("supplement_bundle"):
                review_files.append("work/supplement_evidence.json")
            self.gate("event_names", current, review_files, "核对事件名称与成员证据；original_retained.json中的原热点保留项不要求100%识别或合并。其余明确事实错误、误合仍需修正。")
        def finalize(d):
            events = {e["event_id"]: e for e in read_jsonl(d / "work/block_events.jsonl")}
            snapshot = list(read_jsonl(d / "out/registry_snapshot.jsonl"))
            for row in snapshot:
                if row["event_id"] in events:
                    row["identity_core"] = events[row["event_id"]].get("identity_core") or {}
                    row["embedding_model"] = cfg["embedding_model"]
                    row["embedding_revision"] = cfg["embedding_revision"]
            write_jsonl(d / "out/registry_snapshot.jsonl", snapshot)
            quality = read(d / "out/c2_quality.json")
            quality["review_completion"] = "DEFERRED_TO_CALLER" if self.pending_reviews else "COMPLETED_FOR_THIS_RUN"
            quality["review_policy"] = cfg.get("review_policy", "strict")
            quality["degraded_policy"] = cfg.get("degraded_policy", "block")
            quality["review_pending"] = self.pending_reviews
            quality["semantic_quality"] = "NOT_BENCHMARKED"
            quality['evidence_pending_ids']=[r['record_id'] for r in evidence_pending]
            quality['retention_policy'] = 'preserve-original-after-review'
            quality['business_disposition'] = 'ACCEPTED_WITH_ORIGINALS' if retained else 'ACCEPTED'
            retention_report = read(d / 'out/original_retained.json')
            quality['original_retained_count'] = len(retained)
            quality['unresolved_count'] = retention_report['unresolved_count']
            quality['related_records_split_count'] = len(retained) - retention_report['unresolved_count']
            atomic_json(d / "out/c2_quality.json", quality)
            atomic_json(d / "out/c2_provenance.json", {"version": VERSION, "input_sha256": cfg["source_sha256"],
                        "mode": cfg["mode"], "frontend": cfg["frontend"], "chat_model": cfg["chat_model"],
                        "chat_revision": cfg["chat_revision"], "embedding_model": cfg["embedding_model"],
                        "embedding_revision": cfg["embedding_revision"], "window": cfg["window"],
                        "supplement_sha256": cfg.get("supplement_sha256"),
                        "front_seed":cfg.get('front_seed'),
                        "stage_receipts": self.trace, "cost_completeness": "failed request costs may be unknown"})
            if (d / "run_manifest.json").exists():
                shutil.copyfile(d / "run_manifest.json", d / "out/c2_run_metrics.json")
        return self.node("finalize", current, action=finalize)


def prepare(args):
    if getattr(args, "supplement_bundle", None):
        from supplement import validate_bundle
        validate_bundle(read(args.supplement_bundle), args.input)
    with args.input.open(encoding="utf-8-sig", newline="") as h:
        reader = csv.DictReader(h); fields = reader.fieldnames or []; rows = list(reader)
    if not rows or len(fields) != len(set(fields)) or "title" not in fields:
        raise ValueError("Empty input or invalid title/header schema")
    for row in rows:
        if None in row or any(v is None for v in row.values()):
            raise ValueError("Malformed CSV row")
        if "record_id" not in row:
            row["record_id"] = row.get("row_id", "")
        if not row["title"].strip():
            raise ValueError("Blank title")
        if "platform" not in row:
            row["platform"] = args.platform or ""
    unique_ids(rows)
    canonical = lambda p: CLEAN.ALIASES.get(p.lower(), p)
    platforms = sorted({canonical(r["platform"]) for r in rows})
    if args.mode == "single":
        selected = canonical(args.platform) if args.platform else (platforms[0] if len(platforms) == 1 else None)
        if selected not in platforms:
            raise ValueError("Single mode requires a platform present in the input")
        rows = [r for r in rows if canonical(r["platform"]) == selected]
        platforms = [selected]
    if any(p not in CLEAN.PLATFORM_RULES for p in platforms):
        raise ValueError("Unsupported/blank platform")
    if args.heat_col and args.heat_col not in fields:
        raise ValueError("Explicit heat column not present")
    fields += [f for f in ("record_id", "platform") if f not in fields]
    config = {k: str(v.resolve()) if isinstance(v, Path) else v for k, v in vars(args).items()
              if k not in {"command", "execute", "member_verdict"}}
    config.update(version=VERSION, source_sha256=file_hash(args.input), platforms=platforms,
                  supplement_sha256=file_hash(args.supplement_bundle) if getattr(args, "supplement_bundle", None) else None,
                  previous_sha256=file_hash(args.previous_registry) if args.previous_registry else None,
                  code={p.name: file_hash(p) for p in sorted(SCRIPTS.glob("*.py"))},
                  endpoint_hashes={k: digest(os.environ.get(k, "")) for k in ("LLM_RELAY_BASE_URL", "EMBEDDING_BASE_URL")})
    config['references']={p.relative_to(SCRIPTS.parent).as_posix():file_hash(p)
                          for p in sorted((SCRIPTS.parent/'references').rglob('*')) if p.is_file()}
    if getattr(args,'reuse_front_run',None):
        if not args.supplement_bundle:
            raise ValueError('A front revision requires a source-bound supplement bundle')
        from front_recovery import inspect_seed
        config['front_seed']=inspect_seed(args.reuse_front_run,rows,config)
    return rows, fields, config


def run(args):
    root = args.run_dir.resolve()
    if getattr(args,'reuse_front_run',None) and (root==args.reuse_front_run.resolve() or root.is_relative_to(args.reuse_front_run.resolve())):
        print(json.dumps({'status':'FAILED','error':'Revision must not overwrite or nest within its source run'}))
        return 2
    if not args.execute:
        print(json.dumps({"version": VERSION, "mode": args.mode, "frontend": args.frontend,
                          "execution": "NOT_STARTED", "gates": ["cleaning", "non_events", "member verdicts", "event_names if needed"]}))
        return 0
    with exclusive_lock(root / ".c2.lock"):
        start = time.monotonic()
        state = {"version": VERSION, "status": "RUNNING", "trace": []}
        atomic_json(root / "result.json", state)
        try:
            rows, fields, config = prepare(args)
            if (root / "config.json").exists() and read(root / "config.json") != config:
                raise ValueError("Run configuration/input/code/endpoint changed; use a new run directory")
            atomic_json(root / "config.json", config)
            initial = root / "input_snapshot"
            initial.mkdir(parents=True, exist_ok=True)
            source = initial / "work/source.csv"
            if not source.exists():
                write_csv(source, rows, fields)
                if args.previous_registry:
                    shutil.copyfile(args.previous_registry, initial / "work/previous_registry.jsonl")
                if getattr(args, "supplement_bundle", None):
                    shutil.copyfile(args.supplement_bundle, initial / "work/supplement_evidence.json")
                atomic_json(initial / "receipt.json", hashes(initial))
            if not matches(initial, read(initial / "receipt.json")):
                raise ValueError("Input snapshot modified")
            pipeline = Pipeline(root, config)
            try:
                last = pipeline.execute(initial, args.member_verdict)
            finally:
                state["trace"] = pipeline.trace
            output_hashes = {p.relative_to(last / "out").as_posix(): file_hash(p) for p in (last / "out").rglob("*") if p.is_file()}
            if (root / "out").exists():
                if not matches(root / "out", output_hashes):
                    raise ValueError("Published output changed; preserve it and use a new run directory")
            else:
                staged = root / (".publish-" + uuid.uuid4().hex)
                shutil.copytree(last / "out", staged)
                os.replace(staged, root / "out")
            state.update(status="COMPLETE", outputs=output_hashes, output_dir=str(root / "out"),
                         final_stage=str(last), input_records=len(rows),
                         review_gates="DEFERRED" if pipeline.pending_reviews else "COMPLETED",
                         review_pending=pipeline.pending_reviews,
                         semantic_evaluation="NOT_A_BENCHMARK", performance_evaluation="NOT_A_BENCHMARK")
        except StopWorkflow as exc:
            state.update(status=exc.status, **exc.details)
        except Exception as exc:
            state.update(status="FAILED", error_type=type(exc).__name__, error=str(exc))
        state["wall_seconds"] = time.monotonic() - start
        atomic_json(root / "history" / (uuid.uuid4().hex + ".json"), state)
        atomic_json(root / "result.json", state)
        print(json.dumps(state, ensure_ascii=False))
        return 0 if state["status"] == "COMPLETE" else 3 if state["status"] == "WAITING_REVIEW" else 2


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'revise':
        from review_revision import main as revise_main
        return revise_main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "supplement":
        from supplement import main as supplement_main
        return supplement_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run")
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--run-dir", type=Path, required=True)
    command.add_argument("--mode", choices=["single", "cross-platform"], default="single")
    command.add_argument("--platform")
    command.add_argument("--heat-col")
    command.add_argument("--frontend", choices=["fused", "legacy"], default="fused")
    command.add_argument("--window", required=True)
    command.add_argument("--previous-registry", type=Path)
    command.add_argument("--supplement-bundle", type=Path, help="Source-bound evidence bundle; forces final semantic review")
    command.add_argument('--reuse-front-run',type=Path,help='Create a new revision from verified pre-exclusion F1; no approvals/downstream results inherited')
    command.add_argument("--chat-model", default="claude-sonnet-4-6")
    command.add_argument("--embedding-model", default="Doubao-embedding")
    command.add_argument("--chat-revision", default="unspecified")
    command.add_argument("--embedding-revision", default="unspecified")
    command.add_argument("--max-requests", type=int, default=4)
    command.add_argument("--top-k", type=int, default=10)
    command.add_argument("--cap", type=int, default=60)
    command.add_argument("--out-budget", type=int, default=6000)
    command.add_argument("--stage-timeout", type=float, default=3600)
    command.add_argument("--verdict", choices=["off", "sonnet", "opus"], default="off")
    command.add_argument("--member-verdict", type=Path)
    command.add_argument("--execute", action="store_true")
    command.add_argument("--front-workers", type=int, default=1,
                         help="MA: 跨平台 F1 同时运行的平台数（默认 1 = 原串行）；总并发 = N × max-requests")
    command.add_argument("--review-policy", choices=["strict", "record"], default="strict",
                         help="MA: record = 门禁不停，审核项写入 c2_quality.review_pending 交调用方 HC")
    command.add_argument("--degraded-policy", choices=["block", "accept"], default="block",
                         help="MA: accept = 07/03 技术降级不阻断输出，降级明细保留在 c2_quality")
    resume = sub.add_parser("resume")
    resume.add_argument("--run-dir", type=Path, required=True)
    resume.add_argument("--member-verdict", type=Path)
    ack = sub.add_parser("review")
    ack.add_argument("--run-dir", type=Path, required=True)
    ack.add_argument("--gate", choices=["cleaning", "non_events", "event_names"], required=True)
    ack.add_argument("--reviewer", required=True)
    ack.add_argument("--note", required=True)
    args = ap.parse_args()
    if args.command == "review":
        with exclusive_lock(args.run_dir / ".c2.lock"):
            state = read(args.run_dir / "result.json")
            if state.get("status") != "WAITING_REVIEW" or state.get("gate") != args.gate:
                ap.error("This gate is not currently waiting for review")
            request = read(args.run_dir / "review_requests" / (args.gate + ".json"))
            if not args.reviewer.strip() or not args.note.strip() or not matches(Path(request["directory"]), request["files"]):
                ap.error("Review evidence changed or reviewer/note missing")
            atomic_json(args.run_dir / "reviews" / (request["id"] + ".json"),
                        {**request, "approved": True, "reviewer": args.reviewer, "note": args.note})
            print("Review recorded for this input fingerprint. Resume the same run.")
            return 0
    if args.command == "resume":
        config = read(args.run_dir / "config.json")
        allowed = {action.dest for action in command._actions}
        config = {k: v for k, v in config.items() if k in allowed}
        config.update(command="run", execute=True, member_verdict=args.member_verdict)
        for key in ("input", "run_dir", "previous_registry", "supplement_bundle", "reuse_front_run"):
            if config.get(key):
                config[key] = Path(config[key])
        args = argparse.Namespace(**config)
    if min(args.max_requests, args.top_k, args.cap, args.out_budget, args.stage_timeout,
           getattr(args, "front_workers", 1)) <= 0:
        ap.error("Limits must be positive")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
