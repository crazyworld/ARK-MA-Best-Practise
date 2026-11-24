"""Offline baseline check on a caller-selected filesystem. No credentials/network.

Creates and removes only its own temporary child directory. This does not attest
Agent discovery, private access isolation, container restart persistence or TLS.
"""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile


def check(workspace, runtime='unspecified'):
    root=Path(workspace).expanduser().resolve()
    report=dict(schema_version='1.0',runtime_declared=runtime,
        python=platform.python_version(),os=platform.system(),checks=[],
        network='NOT_TESTED',authentication='NOT_TESTED',private_access_isolation='NOT_TESTED',
        runtime_acceptance='NOT_TESTED',restart_persistence='NOT_TESTED',real_credentials_read=False)
    def result(name,ok):report['checks'].append(dict(check=name,passed=bool(ok)))
    result('python_3_10_or_later',sys.version_info >= (3,10))
    for name,low,high in [('requests',(2,31),(3,0)),('PyYAML',(6,0),(7,0)),('openpyxl',(3,1),(4,0))]:
        try:
            version=importlib.metadata.version(name)
            match=re.match(r'^(\d+)\.(\d+)',version)
            value=tuple(map(int,match.groups())) if match else (0,0)
            result(name,low <= value < high)
            report['checks'][-1]['version']=version
        except importlib.metadata.PackageNotFoundError:result(name,False)
    result('workspace_exists',root.is_dir())
    if root.is_dir():
        try:
            with tempfile.TemporaryDirectory(prefix='ssjh-check-',dir=root) as folder:
                p=Path(folder).resolve()
                if p.parent != root:raise ValueError('SCRATCH_PATH_INVALID')
                lock=p/'exclusive.lock';lock.write_text('synthetic',encoding='utf-8')
                child="import sys\ntry:\n open(sys.argv[1],'x').close()\nexcept FileExistsError:\n sys.exit(0)\nsys.exit(2)"
                r=subprocess.run([sys.executable,'-B','-c',child,str(lock)],capture_output=True,timeout=15)
                result('cross_process_exclusive_create',r.returncode==0)
                src=p/'source';src.write_bytes(b'synthetic-content')
                link=p/'published';os.link(src,link)
                result('hardlink',link.read_bytes()==src.read_bytes())
                try:os.link(src,link);result('hardlink_no_overwrite',False)
                except FileExistsError:result('hardlink_no_overwrite',True)
                swap=p/'swap';swap.write_bytes(b'updated');os.replace(swap,link)
                result('atomic_replace',link.read_bytes()==b'updated' and src.read_bytes()==b'synthetic-content')
                db=p/'state.sqlite3'
                with sqlite3.connect(db) as c:
                    c.execute('create table t(v integer)');c.execute('insert into t values(1)')
                c.close()
                child="import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); n=c.execute('select v from t').fetchone()[0]; c.close(); sys.exit(0 if n==1 else 2)"
                r=subprocess.run([sys.executable,'-B','-c',child,str(db)],capture_output=True,timeout=15)
                result('sqlite_across_processes',r.returncode==0)
            result('scratch_removed',not p.exists())
        except Exception as exc:
            result('filesystem_probe',False)
            report['checks'][-1]['error_type']=type(exc).__name__
    report['ok']=all(row['passed'] for row in report['checks'])
    report['status']='BASELINE_CHECKS_PASSED' if report['ok'] else 'BASELINE_CHECKS_FAILED'
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True,help='Existing writable workspace or mounted volume')
    parser.add_argument('--runtime',choices=['codex','claude-code','openclaw','hermes','ada','unspecified'],default='unspecified')
    args=parser.parse_args()
    report=check(args.workspace,args.runtime)
    print(json.dumps(report,ensure_ascii=False))
    return 0 if report['ok'] else 2


if __name__=='__main__':sys.exit(main())
