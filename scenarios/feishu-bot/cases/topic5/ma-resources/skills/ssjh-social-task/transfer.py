"""Page journal and streaming exports. Ambiguous cursors restart the same job."""
from errors import SkillError
import csv
import hashlib
import json
import os
import pathlib
import sqlite3
import uuid
from itertools import zip_longest

import durable as d
import xlsx_text as xt
from transport import call_json


def require(ok, code):
    if not ok: raise SkillError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class PageStore:
    def __init__(self, root, job_id, expected_count, max_records, provider_binding):
        require(type(expected_count) is int and type(max_records) is int and 0<=expected_count<=max_records,'INVALID_OR_EXCESS_TASK_COUNT')
        self.root=pathlib.Path(root)/'pages';self.root.mkdir(parents=True,exist_ok=True)
        self.file=self.root/'manifest.json'
        self.binding=dict(job_id=job_id,expected_count=expected_count,max_records=max_records,provider=provider_binding)
        self.m=d.read_json(self.file) if self.file.exists() else dict(schema_version=1,binding=self.binding,generation=0,pages=[],count=0,next_cursor=None,pending=None,complete=False)
        require(self.m.get('binding')==self.binding,'PAGE_STORE_BINDING_MISMATCH')

    def save(self):d.write_json(self.file,self.m)

    def page_path(self,seq):
        return self.root/f"generation-{self.m['generation']:04d}"/f'{seq:08d}.json'

    def validate_page(self,env,seq,cursor,index,count):
        require(env.get('job_id')==self.binding['job_id'] and env.get('sequence')==seq and env.get('request_cursor')==cursor,'PAGE_ENVELOPE_MISMATCH')
        rows=env.get('docs');next_cursor=env.get('next_cursor')
        require(isinstance(rows,list) and all(isinstance(r,dict) for r in rows),'DOWNLOAD_RECORD_SCHEMA_ERROR')
        require(next_cursor is None or isinstance(next_cursor,str),'SCROLL_ID_SCHEMA_ERROR')
        require(count+len(rows)<=self.binding['expected_count'],'DOWNLOAD_COUNT_EXCEEDS_TASK_COUNT')
        try:
            with index:
                for row in rows:
                    identity=row.get('id')
                    require(type(identity) in (str,int) and str(identity)!='','RECORD_ID_REQUIRED_FOR_RECOVERY')
                    index.execute('INSERT INTO ids VALUES (?)',(str(identity),))
        except sqlite3.IntegrityError:
            raise SkillError('DUPLICATE_RECORD_ID') from None
        complete=not rows or not next_cursor
        count+=len(rows)
        if complete:require(count==self.binding['expected_count'],'DOWNLOAD_COUNT_MISMATCH')
        return count,complete

    def rebuild_index(self,index):
        index.execute('CREATE TABLE IF NOT EXISTS ids (id TEXT PRIMARY KEY)')
        index.execute('DELETE FROM ids');index.commit()
        count=0;cursor=None;complete=False
        for seq,item in enumerate(self.m['pages']):
            require(not complete,'PAGES_AFTER_TERMINAL_PAGE')
            path=self.page_path(seq)
            require(d.digest(path)==item['sha256'],'PAGE_HASH_MISMATCH')
            env=d.read_json(path)
            count,complete=self.validate_page(env,seq,cursor,index,count)
            require(item['count']==len(env['docs']),'PAGE_COUNT_MISMATCH')
            cursor=env['next_cursor']
        require(count==self.m['count'] and cursor==self.m['next_cursor'] and complete==self.m['complete'],'PAGE_MANIFEST_MISMATCH')

    def accept(self,env,index):
        seq=len(self.m['pages']);cursor=self.m['next_cursor']
        count,complete=self.validate_page(env,seq,cursor,index,self.m['count'])
        path=self.page_path(seq)
        self.m['pages'].append(dict(sha256=d.digest(path),count=len(env['docs'])))
        self.m.update(count=count,next_cursor=env['next_cursor'],pending=None,complete=complete)
        self.save();d.fault('after_page_manifest')

    def restart_ambiguous(self,index):
        d.write_json(self.root/f"generation-{self.m['generation']:04d}-abandoned.json",self.m)
        self.m.update(generation=self.m['generation']+1,pages=[],count=0,next_cursor=None,pending=None,complete=False,
                      recovery_reason='AMBIGUOUS_CURSOR_RESTARTED_SAME_JOB')
        self.save();self.rebuild_index(index)

    def download(self,api,http_get,on_progress=None):
        page_size=api.get('runtime',{}).get('page_size',100)
        require(type(page_size) is int and 1<=page_size<=100,'INVALID_PAGE_SIZE')
        # Observed supplier responses use 500 rows despite size=100. The
        # requested size is a hint, separate from our validated response cap.
        response_cap=api.get('runtime',{}).get('max_response_records',500)
        require(type(response_cap) is int and 1<=response_cap<=500,'INVALID_RESPONSE_PAGE_CAP')
        index=sqlite3.connect(str(self.root/'validation.sqlite3'))
        try:
            self.rebuild_index(index)
            if self.m['pending']:
                pending=self.m['pending'];seq=len(self.m['pages'])
                require(pending==dict(sequence=seq,cursor=self.m['next_cursor']),'PENDING_PAGE_BINDING_MISMATCH')
                orphan=self.page_path(seq)
                if orphan.exists():self.accept(d.read_json(orphan),index)
                else:self.restart_ambiguous(index)
            while not self.m['complete']:
                seq=len(self.m['pages']);cursor=self.m['next_cursor']
                self.m['pending']=dict(sequence=seq,cursor=cursor);self.save()
                d.fault('after_page_intent')
                params=dict(jobId=self.binding['job_id'],token=api['credentials']['fixed_token'],size=page_size,pLimit=1)
                if cursor:params['scrollId']=cursor
                payload=call_json('download', lambda timeout:http_get(api['api']['count_base_url'].rstrip('/')+'/search',params=params,timeout=timeout,allow_redirects=False),api)
                d.fault('after_page_response')
                require(isinstance(payload,dict) and payload.get('success') is True,'DOWNLOAD_API_ERROR')
                data=payload.get('data')
                require(isinstance(data,dict) and isinstance(data.get('docs'),list),'DOWNLOAD_SCHEMA_ERROR_EXPECTED_DATA_DOCS')
                require(len(data['docs'])<=response_cap,'PROVIDER_PAGE_SIZE_EXCEEDED')
                env=dict(job_id=self.binding['job_id'],sequence=seq,request_cursor=cursor,docs=data['docs'],next_cursor=data.get('scrollId'))
                # Atomic page write precedes the journal update; an orphan can be adopted.
                d.write_json(self.page_path(seq),env);d.fault('after_page_file')
                self.accept(env,index)
                if on_progress:on_progress(self.m['count'])
            return self.m['count']
        finally:index.close()

    def iter_rows(self):
        require(self.m['complete'],'DOWNLOAD_NOT_COMPLETE')
        for seq,item in enumerate(self.m['pages']):
            path=self.page_path(seq);require(d.digest(path)==item['sha256'],'PAGE_HASH_MISMATCH')
            yield from d.read_json(path)['docs']


def write_raw(store,path):
    count=0
    with path.open('w',encoding='utf-8') as f:
        f.write('[')
        for row in store.iter_rows():
            if count:f.write(',\n')
            json.dump(row,f,ensure_ascii=False);count+=1
        f.write(']\n');f.flush();os.fsync(f.fileno())
    return count


def table_value(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False) if isinstance(value,(dict,list)) else value


def verify_table(store,path,fields,headers):
    """Read back each cell to catch truncation, precision loss and omitted rows."""
    sentinel=object()
    if path.suffix=='.csv':
        with path.open(encoding='utf-8-sig',newline='') as f:
            reader=csv.reader(f)
            require(next(reader,None)==headers,'EXPORT_HEADER_MISMATCH')
            expected=([str(table_value(row.get(k))) if row.get(k) is not None else '' for k in fields] for row in store.iter_rows())
            for actual,wanted in zip_longest(reader,expected,fillvalue=sentinel):
                require(actual==wanted,'EXPORT_CELL_VALUE_MISMATCH')
    else:
        import openpyxl
        wb=openpyxl.load_workbook(path,read_only=True,data_only=False)
        reader=None;overflow=None
        try:
            reader=wb.active.iter_rows(values_only=True)
            require(list(next(reader,()))==headers,'EXPORT_HEADER_MISMATCH')
            expected=([xt.encode(table_value(row.get(k)), n, c)[0] for c,k in enumerate(fields,1)] for n,row in enumerate(store.iter_rows(),2))
            for actual,wanted in zip_longest(reader,expected,fillvalue=sentinel):
                require(actual is not sentinel and wanted is not sentinel,'EXPORT_ROW_COUNT_MISMATCH')
                require(len(actual)<=len(fields),'EXPORT_CELL_VALUE_MISMATCH')
                actual=list(actual)+[None]*(len(fields)-len(actual))
                # Excel has a single empty cell representation; source JSON keeps distinctions.
                require([None if x=='' else x for x in actual]==[None if x=='' else x for x in wanted],'EXPORT_CELL_VALUE_MISMATCH')
            overflow=iter(wb[xt.SHEET].iter_rows(values_only=True)) if xt.SHEET in wb.sheetnames else iter(())
            if xt.SHEET in wb.sheetnames:
                require(list(next(overflow,()))==xt.HEADERS,'EXPORT_OVERFLOW_MISMATCH')
            for n,row in enumerate(store.iter_rows(),2):
                for c,k in enumerate(fields,1):
                    value=table_value(row.get(k))
                    _,chunks=xt.encode(value,n,c)
                    actual=[]
                    for wanted in chunks:
                        found=next(overflow,None)
                        require(found is not None and list(found)==wanted,'EXPORT_OVERFLOW_MISMATCH')
                        actual.append(list(found))
                    if chunks:require(xt.decode(actual)==value,'EXPORT_OVERFLOW_MISMATCH')
            require(next(overflow,None) is None,'EXPORT_OVERFLOW_MISMATCH')
        finally:
            for iterator in (reader,overflow):
                if hasattr(iterator,'close'):iterator.close()
            wb.close()


def write_table(store,path,template):
    if template:fields=[x[0] for x in template]
    else:
        fields=list(dict.fromkeys(k for row in store.iter_rows() for k in row))
    headers=[x[1] for x in template] if template else fields
    count=0
    if path.suffix=='.csv':
        with path.open('w',newline='',encoding='utf-8-sig') as f:
            writer=csv.writer(f);writer.writerow(headers)
            for row in store.iter_rows():writer.writerow([table_value(row.get(k,'')) for k in fields]);count+=1
            f.flush();os.fsync(f.fileno())
        with path.open(encoding='utf-8-sig',newline='') as f:
            require(sum(1 for _ in csv.reader(f))-1==count,'EXPORT_ROW_COUNT_MISMATCH')
    else:
        import openpyxl
        from openpyxl.cell import WriteOnlyCell
        from openpyxl.styles import Font,PatternFill
        wb=openpyxl.Workbook(write_only=True);ws=wb.create_sheet('data')
        require(all(not xt.unsafe(x) for x in headers),'EXPORT_HEADER_NOT_EXCEL_SAFE')
        overflow=None
        header_cells=[]
        for value in headers:
            cell=WriteOnlyCell(ws,value=value);cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='2F5496');header_cells.append(cell)
        ws.append(header_cells)
        for row in store.iter_rows():
            cells=[]
            for field in fields:
                value=table_value(row.get(field,''))
                value,chunks=xt.encode(value,count+2,len(cells)+1)
                if chunks:
                    if overflow is None:
                        overflow=wb.create_sheet(xt.SHEET);overflow.append(xt.HEADERS)
                    for chunk in chunks:overflow.append(chunk)
                cell=WriteOnlyCell(ws,value=value)
                if isinstance(value,str):cell.data_type='s'
                cells.append(cell)
            ws.append(cells);count+=1
        wb.save(path);d.sync_file(path)
        check=openpyxl.load_workbook(path,read_only=True)
        try:require(sum(1 for _ in check.active.rows)-1==count,'EXPORT_ROW_COUNT_MISMATCH')
        finally:check.close()
    verify_table(store,path,fields,headers)
    return count


def export_artifacts(run_path,store,output,template):
    run_path=pathlib.Path(run_path);output=pathlib.Path(output).resolve()
    targets=[run_path/'records.json',output]
    require(len(set(targets))==2,'EXPORT_OUTPUT_COLLIDES_WITH_RAW_DATA')
    spec=fingerprint(dict(binding=store.binding,pages=store.m['pages'],output=str(output),template=template,table_contract=xt.TABLE_CONTRACT))
    manifest_path=run_path/'export.json'
    if manifest_path.exists():
        manifest=d.read_json(manifest_path)
        if manifest['spec']!=spec:
            legacy=fingerprint(dict(binding=store.binding,pages=store.m['pages'],output=str(output),template=template,table_contract=xt.LEGACY_TABLE_CONTRACT))
            require(manifest['spec']==legacy and manifest['state']=='PREPARING' and not any(p.exists() for p in targets),'EXPORT_CONTRACT_CHANGED')
            require([item['target'] for item in manifest['artifacts']]==[str(p) for p in targets],'EXPORT_TARGET_BINDING_MISMATCH')
            for item,target in zip(manifest['artifacts'],targets):
                temp=pathlib.Path(item['temp'])
                require(temp.parent==target.parent and temp.name.startswith('.'+target.stem+'.') and temp.name.endswith('.partial'+target.suffix),'EXPORT_TEMP_BINDING_MISMATCH')
            # Preserve failed evidence; only a never-published, identical export can migrate.
            d.write_json(run_path/('export-legacy-'+uuid.uuid4().hex+'.json'),manifest)
            manifest=dict(spec=spec,state='PREPARING',artifacts=[dict(target=str(p),temp=str(p.with_name('.'+p.stem+'.'+uuid.uuid4().hex+'.partial'+p.suffix))) for p in targets])
            d.write_json(manifest_path,manifest)
    else:
        require(not any(p.exists() for p in targets),'OUTPUT_ALREADY_EXISTS_RECONCILE_OR_CHOOSE_NEW_PATH')
        for p in targets:p.parent.mkdir(parents=True,exist_ok=True)
        manifest=dict(spec=spec,state='PREPARING',artifacts=[dict(target=str(p),temp=str(p.with_name('.'+p.stem+'.'+uuid.uuid4().hex+'.partial'+p.suffix))) for p in targets])
        d.write_json(manifest_path,manifest)
    require([item['target'] for item in manifest['artifacts']]==[str(p) for p in targets],'EXPORT_TARGET_BINDING_MISMATCH')
    for item,target in zip(manifest['artifacts'],targets):
        temp=pathlib.Path(item['temp'])
        require(temp.parent==target.parent and temp.name.startswith('.'+target.stem+'.') and temp.name.endswith('.partial'+target.suffix),'EXPORT_TEMP_BINDING_MISMATCH')
    if manifest['state']=='PREPARING':
        require(not any(p.exists() for p in targets),'EXPORT_TARGET_CONFLICT')
        for i,item in enumerate(manifest['artifacts']):
            path=pathlib.Path(item['temp'])
            count=write_raw(store,path) if i==0 else write_table(store,path,template)
            require(count==store.binding['expected_count'],'EXPORT_ROW_COUNT_MISMATCH')
            item.update(sha256=d.digest(path),count=count)
            d.fault('after_export_file_'+str(i))
        manifest['state']='READY';d.write_json(manifest_path,manifest);d.fault('after_export_ready')
    require(manifest['state'] in {'READY','PUBLISHED'},'EXPORT_MANIFEST_STATE_INVALID')
    for i,item in enumerate(manifest['artifacts']):
        d.publish(item['temp'],item['target'],item['sha256']);d.fault('after_publish_'+str(i))
    manifest['state']='PUBLISHED';d.write_json(manifest_path,manifest);d.fault('after_export_published')
    for item in manifest['artifacts']:
        temp=pathlib.Path(item['temp'])
        if temp.exists():temp.unlink()
    return manifest
