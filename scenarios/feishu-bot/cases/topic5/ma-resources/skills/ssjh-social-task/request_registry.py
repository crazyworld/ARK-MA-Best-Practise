"""One shared local SQLite registry across run directories; not server idempotency."""
from errors import SkillError
import hashlib
import json
import pathlib
import sqlite3
import time
import contextlib


def valid_id(value):
    if not isinstance(value,str) or not value.strip() or value!=value.strip() or len(value)>256 or any(ord(c)<32 for c in value):
        raise SkillError('STABLE_REQUEST_ID_REQUIRED')
    return value


class Registry:
    def __init__(self,path):
        self.path=pathlib.Path(path)

    @contextlib.contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        conn=sqlite3.connect(str(self.path),timeout=30)
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, contract_hash TEXT NOT NULL, payload TEXT NOT NULL)')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self,request_id,contract_hash=None):
        valid_id(request_id)
        with self.connect() as conn:
            row=conn.execute('SELECT contract_hash,payload FROM requests WHERE request_id=?',(request_id,)).fetchone()
        if row is None:return None
        if contract_hash is not None and row[0]!=contract_hash:
            raise SkillError('REQUEST_ID_BOUND_TO_DIFFERENT_CONTRACT')
        return json.loads(row[1])

    def claim(self,request_id,contract_hash,payload):
        """Commit uncertainty before POST; exactly one local contender wins."""
        valid_id(request_id)
        record=dict(payload,request_id=request_id,contract_hash=contract_hash,state='CREATE_OUTCOME_UNKNOWN',
                    task_name='SSJH_'+hashlib.sha256(request_id.encode()).hexdigest()[:24],created_epoch=time.time())
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT contract_hash,payload FROM requests WHERE request_id=?',(request_id,)).fetchone()
            if row:
                if row[0]!=contract_hash:raise SkillError('REQUEST_ID_BOUND_TO_DIFFERENT_CONTRACT')
                return json.loads(row[1]),False
            conn.execute('INSERT INTO requests VALUES (?,?,?)',(request_id,contract_hash,json.dumps(record,ensure_ascii=False)))
        return record,True

    def attach(self,request_id,contract_hash,job_id,reference=None):
        if type(job_id) is not int or job_id<=0:raise SkillError('INVALID_JOB_ID')
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT contract_hash,payload FROM requests WHERE request_id=?',(request_id,)).fetchone()
            if not row or row[0]!=contract_hash:raise SkillError('REQUEST_REGISTRY_BINDING_MISMATCH')
            record=json.loads(row[1])
            if record.get('job_id') not in (None,job_id):raise SkillError('REQUEST_ALREADY_BOUND_TO_ANOTHER_JOB')
            record.update(job_id=job_id,state='SUPPLIER_RUNNING')
            if reference:record['reconciliation_ref']=reference
            conn.execute('UPDATE requests SET payload=? WHERE request_id=?',(json.dumps(record,ensure_ascii=False),request_id))
        return record
