"""Read-only static diagnostics; never probe by creating files or calling services."""
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from errors import describe, SkillError


def path_check(path):
    p=Path(path).expanduser().resolve(); parent=p.parent
    while not parent.exists() and parent!=parent.parent: parent=parent.parent
    return dict(exists=p.exists(),parent_exists=p.parent.exists(),
        parent_is_directory=parent.is_dir(),parent_access_estimate=os.access(parent,os.W_OK),
        actual_write_test='NOT_TESTED')


def doctor(config, api_config, run_dir=None, registry=None):
    report=dict(action='doctor',read_only=True,network='NOT_TESTED',authentication='NOT_TESTED',
        atomic_replace='NOT_TESTED',hardlink='NOT_TESTED',python_supported=sys.version_info>=(3,10),
        python_version='.'.join(map(str,sys.version_info[:3])),dependencies={},checks={},errors=[])
    for module, package in [('requests','requests'),('yaml','PyYAML'),('openpyxl','openpyxl')]:
        present=importlib.util.find_spec(module) is not None
        try: version=importlib.metadata.version(package) if present else None
        except importlib.metadata.PackageNotFoundError: version=None
        report['dependencies'][package]=dict(present=present,version=version)
    try:
        from data_validation import load_dictionary
        dictionary,digest=load_dictionary()
        report['checks']['data_dictionary']=dict(version=dictionary['version'],sha256=digest,fields=len(dictionary['fields']),platforms=len(dictionary['platforms']),platform_evidence={p:r['evidence_status'] for p,r in dictionary['platforms'].items()},official_supplier_dictionary=False)
    except Exception as exc:report['errors'].append(describe(exc,'doctor_dictionary'))
    try:
        from budget import private_settings
        private_settings(run_dir or Path(config).resolve().parent/'output')
        report['checks']['budget']='FIXED_CEILING_PRICING_VALIDATED'
        report['checks']['pricing_source']='ADMIN_OVERRIDE' if 'SSJH_PRICING_FILE' in os.environ else 'BUNDLED_INTERNAL'
        report['checks']['ledger_initialization']='ADMIN_DIRECTORY' if 'SSJH_BUDGET_DIR' in os.environ else 'DEFAULT_CREATED_ON_FIRST_BUDGET_OPERATION'
    except Exception as exc:report['errors'].append(describe(exc,'doctor_budget'))
    try:
        import ssjh_social_task as core
        import query_contract
        cfg,api=core.load_configs(config,api_config)
        query_contract.normalize(cfg)
        report['checks']['query']='SUPPORTED'
        creds=api.get('credentials',{})
        report['credentials']={k:isinstance(creds.get(k),str) and bool(creds[k]) and not creds[k].startswith('${')
            for k in ('username','password','fixed_token')}
        report['checks']['endpoint_transport']={key:('HTTP_UNENCRYPTED_CONFIGURED_HTTPS_NOT_VERIFIED' if urlsplit(api['api'][key]).scheme=='http'
            else 'HTTPS_CONFIGURED_NOT_TESTED' if urlsplit(api['api'][key]).scheme=='https' else 'INVALID_SCHEME') for key in ('base_url','count_base_url')}
        from transport import validate_endpoints
        validate_endpoints(api)
        core.validate_configs(cfg,api)
    except Exception as exc: report['errors'].append(describe(exc,'doctor_config'))
    try:
        if 'cfg' in locals():
            output=Path(cfg['output']['file'])
            if not output.is_absolute(): output=Path(config).resolve().parent/output
            if output.suffix not in ('.csv','.xlsx'): raise SkillError('OUTPUT_FORMAT_INVALID')
            report['checks']['output']=path_check(output)
            if not report['checks']['output']['parent_is_directory'] or not report['checks']['output']['parent_access_estimate']:
                raise SkillError('OUTPUT_PARENT_ACCESS_CHECK_FAILED','filesystem',next_action='检查输出父路径和访问权限；静态检查不代替实际写入验证。')
            template=cfg['output'].get('template','template.yaml')
            if template:
                columns=core.load_template(str(Path(config).resolve().parent/template))
                if not columns: raise SkillError('TEMPLATE_COLUMNS_INVALID')
                report['checks']['template_columns']=len(columns)
    except Exception as exc: report['errors'].append(describe(exc,'doctor_output'))
    saved={}
    if run_dir:
        try:
            state=Path(run_dir)/'state.json'
            if state.exists():
                saved=json.loads(state.read_text(encoding='utf-8-sig'))
                if not isinstance(saved,dict): raise SkillError('STATE_MAPPING_REQUIRED')
                report['checks']['state']='READABLE_JSON_OBJECT'
            else: report['checks']['state']='NOT_STARTED'
            report['checks']['run_path']=path_check(state)
        except Exception as exc:
            saved={};report['errors'].append(describe(exc,'doctor_state'))
    try:
        path=Path(registry or saved.get('registry_path') or Path.home()/'.ssjh-social-task/requests.sqlite3').expanduser().resolve()
        report['checks']['registry']=path_check(path)
        if path.exists():
            # Even SQLite mode=ro can create WAL shared-memory sidecars.
            with path.open('rb') as f: header=f.read(16)
            if header!=b'SQLite format 3\x00': raise SkillError('REGISTRY_HEADER_INVALID','state')
            report['checks']['registry'].update(header='SQLITE',integrity='NOT_TESTED')
    except Exception as exc: report['errors'].append(describe(exc,'doctor_registry'))
    required=['requests','PyYAML']+(['openpyxl'] if 'output' in locals() and output.suffix=='.xlsx' else [])
    report['ready_for_local_checks']=report['python_supported'] and not report['errors'] and all(report['dependencies'][x]['present'] for x in required)
    return report
