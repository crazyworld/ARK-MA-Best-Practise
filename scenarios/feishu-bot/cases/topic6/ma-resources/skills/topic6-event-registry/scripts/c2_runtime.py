"""Small deterministic primitives for the candidate runtime; no network calls."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    fd, temp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextlib.contextmanager
def exclusive_lock(path):
    """OS-released lock; a killed process cannot leave a stale logical lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another process owns this candidate run") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def unique_ids(rows, field="record_id"):
    ids = [r.get(field) for r in rows]
    if any(not isinstance(i, str) or not i.strip() or i != i.strip() for i in ids):
        raise ValueError(f"{field} must be nonempty, trimmed strings")
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate {field}")
    return ids


def cached_request(directory, request, call):
    """One atomic envelope per full request. Unversioned/invalid files never hit."""
    key = digest(request)
    path = Path(directory) / (key + ".json")
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if (cached.get("request_hash") == key and
                    digest(cached["payload"]) == cached.get("payload_hash")):
                return cached["payload"], True
        except (ValueError, KeyError, TypeError):
            pass
    payload = call()
    atomic_json(path, {"schema": "c2-request-cache/1", "request_hash": key,
                       "payload_hash": digest(payload), "payload": payload})
    return payload, False
