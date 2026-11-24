"""Durable local artifacts; fault() marks interruption points for offline tests."""
from errors import SkillError
import hashlib
import json
import os
import pathlib
import uuid


def fault(stage):
    pass


def digest(path):
    h = hashlib.sha256()
    with pathlib.Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'))


def sync_file(path):
    with pathlib.Path(path).open('r+b') as f:
        os.fsync(f.fileno())


def sync_dir(path):
    if os.name != 'nt':
        fd = os.open(str(path), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def write_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    with temp.open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(temp, path)
    sync_dir(path.parent)


def publish(temp, target, expected_hash):
    """Create-only atomic publication; never overwrite a racing or preexisting file."""
    temp, target = pathlib.Path(temp), pathlib.Path(target)
    if target.exists():
        if digest(target) != expected_hash:
            raise SkillError('EXPORT_TARGET_CONFLICT')
        return
    if digest(temp) != expected_hash:
        raise SkillError('EXPORT_TEMP_HASH_MISMATCH')
    try:
        os.link(temp, target)
    except FileExistsError:
        if digest(target) != expected_hash:
            raise SkillError('EXPORT_TARGET_CONFLICT') from None
    sync_dir(target.parent)
    # Leave the owned temporary file until the export manifest confirms publication.

