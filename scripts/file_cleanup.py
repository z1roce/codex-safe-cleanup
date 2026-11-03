"""Exact, user-confirmed Codex cache files -> Windows Recycle Bin; no permanent purge."""
import os
import json
from pathlib import Path
import re
import time

import inventory
from safe_cleanup import (Cleaner, SafetyError, absolute, no_links, is_sync, within, snapshot,
                          same, read_json, write_json, key, utc_stamp)
from reviewed_cleanup import NativeLease
import windows_recycle

ACK='CONFIRMED_UNUSED_CODEX_FILES_RECYCLE'
SYNC_ACK='CONFIRMED_SELECTED_SYNC_FILES_IMPACT'
MAX_BYTES=64*1024**3
MAX_FILE=16*1024**3
MAX_SELECTION=2000
INTERNAL_BATCH=100
HASH_SECONDS=900


class FileCleanup:
    def __init__(self,state):
        self.cleaner=Cleaner(state);self.state=self.cleaner.state
        self.plans=self.state/'file-plans';self.records=self.state/'file-quarantine'

    def check(self,path,exists=True,allow_sync=False):
        p=no_links(absolute(path))
        if not inventory.candidate_info(p,self.state,allow_missing=not exists):raise SafetyError('不是已识别的 Codex 缓存或已核实任务产物，保护或未知文件不能处理')
        c=self.cleaner.cfg()
        if is_sync(p,c.get('sync_roots',[])) and not allow_sync:raise SafetyError('同步文件需要单独确认影响云端和其他设备')
        if any(within(p,absolute(x)) or within(absolute(x),p) for x in c.get('protect',[])):
            raise SafetyError('用户指定保护')
        for parent in p.parents:
            if (parent/'.codex-keep').exists():raise SafetyError('保留标记保护')
        if inventory.git_tracked_status(p) is not False:raise SafetyError('Git 跟踪内容或状态未知，保护')
        if exists and not p.is_file():raise SafetyError('文件已不存在或不是普通文件')
        return p

    def save(self,folder,m,entry=None):
        if m.get('version')!=3:
            write_json(folder/'manifest.json',m);return
        # Write only the changing file record during a transaction. Rewriting the
        # full authorized selection for every file would produce quadratic I/O.
        for row in ([entry] if entry is not None else m['entries']):
            path=folder/'entry-records'/(row['id']+'.json');new=not path.exists()
            write_json(path,row)
            if new:
                with no_links(folder/'entry-index.jsonl').open('ab') as stream:
                    stream.write((json.dumps(row['id'])+'\n').encode('ascii'));stream.flush();os.fsync(stream.fileno())
        if entry is None:
            write_json(folder/'manifest.json',{name:value for name,value in m.items() if name!='entries'})

    def file_snapshot(self,path,source=None,progress=None,phase='流式校验'):
        size=path.stat().st_size
        if size>MAX_FILE:raise SafetyError('单文件超过16GiB校验上限，继续保留')
        def update(done):
            if progress:progress(phase+' · '+path.name,hash_bytes=done,hash_total=size)
        return snapshot(path,source,max_bytes=MAX_FILE,progress=update,deadline=time.monotonic()+HASH_SECONDS)

    def prune_stage(self,entry):
        # Only remove empty, exact tool-created directories. Records and receipts stay.
        if not entry.get('stage'):return
        try:
            stage=no_links(absolute(entry['stage']))
            directories=[stage.parent]
            if entry.get('stage_root'):
                directories += [stage.parent.parent,stage.parent.parent.parent]
            for directory in directories:
                try:no_links(directory).rmdir()
                except FileNotFoundError:continue
        except (OSError,SafetyError) as error:
            entry['staging_cleanup_note']='暂存目录保留供核对：'+str(error)

    def enabled(self):
        controls=self.state/'panel-controls.json'
        if controls.exists() and read_json(controls).get('manual_paused'):
            raise SafetyError('手动清理已暂停；恢复记录仍可使用')

    def preview(self,report_id,ids,progress=None):
        if not isinstance(report_id,str) or not re.fullmatch('[0-9a-f]{32}',report_id):raise SafetyError('请先重新检查')
        if not isinstance(ids,list) or not ids or len(ids)>MAX_SELECTION or not all(isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
            raise SafetyError('一次授权最多2000个精确文件，请减少选择')
        with self.cleaner.locked():
            self.enabled()
            report=read_json(self.state/'codex-reports'/(report_id+'.json'))
            if report.get('scope_version')!=inventory.SCOPE_VERSION:raise SafetyError('旧扫描不能用于清理，请重新检查')
            candidates={x['id']:x for x in report.get('candidates',[])}
            if sum(candidates[i]['bytes'] for i in ids if i in candidates)>MAX_BYTES:raise SafetyError('本次选择超过64GiB，请减少选择')
            entries=[];total=0
            for iid in ids:
                if iid not in candidates:raise SafetyError('包含未扫描或受保护的内容')
                row=candidates[iid];p=self.check(row['path'],allow_sync=True)
                with NativeLease(p) as lease:
                    with lease.reader() as stream:s=self.file_snapshot(p,stream,progress,'预览校验')
                if any(s[k]!=row['file_stamp'][k] for k in ('ino','dev','size','mtime_ns')):
                    raise SafetyError('扫描后文件已改变，请重新检查')
                total+=s['size']
                if total>MAX_BYTES:raise SafetyError('本次选择超过64GiB校验预算，请减少选择')
                entries.append(dict(row,snapshot=s,sync_risk=is_sync(p,self.cleaner.cfg().get('sync_roots',[]))))
                if progress:progress('校验所选缓存',files=len(entries))
            plan={'id':key(),'created':utc_stamp(),'expires':time.time()+1200,'status':'preview','entries':entries}
            write_json(self.plans/(plan['id']+'.json'),plan)
            return {'id':plan['id'],'ids':ids,'items':[{k:x[k] for k in ('name','path','purpose','impact','bytes','task_id','sync_risk')} for x in entries],
                    'sync_count':sum(x['sync_risk'] for x in entries),
                    'bytes':total,'internal_batches':(len(entries)+INTERNAL_BATCH-1)//INTERNAL_BATCH,
                    'note':'仅预览，尚未移动。本次授权仅包含这份精确清单，内部自动分批执行。'}

    def execute(self,pid,ack,progress=None,sync_ack=None):
        if ack!=ACK:raise SafetyError('必须在预览后明确确认所选缓存已无用且不再引用')
        if not isinstance(pid,str) or not re.fullmatch('[0-9a-f]{32}',pid):raise SafetyError('无效预览')
        with self.cleaner.locked():
            self.enabled()
            plan=read_json(self.plans/(pid+'.json'))
            if plan['status']!='preview' or time.time()>plan['expires']:raise SafetyError('预览已使用或过期')
            if any(x.get('sync_risk') for x in plan['entries']) and sync_ack!=SYNC_ACK:
                raise SafetyError('所选文件位于同步目录，必须单独确认云端与其他设备的移除影响')
            # Check every selection before the first move.
            for e in plan['entries']:
                p=self.check(e['path'],allow_sync=sync_ack==SYNC_ACK)
                if not same(self.file_snapshot(p,progress=progress,phase='执行前复核'),e['snapshot']):raise SafetyError('内容已改变，未开始清理')
            if plan.get('resume_batch'):
                batch,m=self.get(plan['resume_batch'])
                allowed={entry['original']:entry['snapshot'] for entry in m.get('selection',[])}
                if any(entry['path'] not in allowed or not same(entry['snapshot'],allowed[entry['path']]) for entry in plan['entries']):
                    raise SafetyError('继续清单超出原始授权，停止处理')
                retry=[entry for entry in m['entries'] if entry['status']=='prepared' and not Path(entry.get('stage','')).is_file()]
                m.setdefault('attempt_history',[]).extend(retry)
                for entry in retry:
                    entry['status']='superseded';self.save(batch,m,entry)
                m['entries']=[entry for entry in m['entries'] if entry not in retry]
            else:
                batch=self.records/key();batch.mkdir(parents=True,exist_ok=False)
                m={'id':batch.name,'created':utc_stamp(),'entries':[],'confirmation':ack,'version':3,
                   'plan_id':pid,'selected_count':len(plan['entries']),'sync_confirmation':sync_ack,
                   'selection':[{'original':old['path'],'name':old['name'],'candidate_id':old['id'],'snapshot':old['snapshot']} for old in plan['entries']]}
            plan['status']='used';write_json(self.plans/(pid+'.json'),plan)
            self.save(batch,m)
            attempted=len(m['entries'])
            for index,old in enumerate(plan['entries'],start=attempted):
                if progress:progress('执行已授权清单',files=index,total_files=m['selected_count'],
                    batch=index//INTERNAL_BATCH+1,total_batches=(m['selected_count']+INTERNAL_BATCH-1)//INTERNAL_BATCH)
                p=absolute(old['path']);e={'id':key(),'original':str(p),'name':old['name'],
                    'candidate_id':old['id'],'snapshot':old['snapshot'],'status':'prepared','ordinal':index}
                m['entries'].append(e);self.save(batch,m,e)
                try:
                    # Reuse central staging on its volume. Other volumes use the verified
                    # project/data root, keeping Shell paths shorter than deep source paths.
                    if p.stat().st_dev==self.state.stat().st_dev:
                        stage=batch/e['id']/p.name
                    else:
                        scope=old.get('scope_root') or next((str(root) for root in inventory.roots() if within(p,root)),None)
                        if not scope:raise SafetyError('无法确认源卷暂存范围，保留文件')
                        stage_root=no_links(absolute(scope))
                        if not within(p,stage_root):raise SafetyError('无效源卷暂存范围')
                        e['stage_root']=str(stage_root)
                        stage=stage_root/'.codex-cleanup-staging'/batch.name/e['id']/p.name
                    e['stage']=str(stage);self.save(batch,m,e)
                    p=self.check(p,allow_sync=sync_ack==SYNC_ACK)
                    no_links(stage);stage.parent.mkdir(parents=True,exist_ok=False)
                    if p.stat().st_dev!=stage.parent.stat().st_dev:
                        raise SafetyError('无法建立同卷暂存区，保留源文件')
                    with NativeLease(p) as lease:
                        with lease.reader() as f:
                            if not same(self.file_snapshot(p,f,progress,'移动前校验'),e['snapshot']):raise SafetyError('文件变化，保留')
                        os.rename(p,stage)
                        e['status']='staged';self.save(batch,m,e)
                        with lease.reader() as f:
                            if not same(self.file_snapshot(stage,f,progress,'暂存校验'),e['snapshot']):raise SafetyError('暂存校验未通过，请恢复')
                        e['status']='recycling';self.save(batch,m,e)
                        result=windows_recycle.native_operation('recycle',stage,e['snapshot']['ino'],
                            receipt=batch/(e['id']+'.receipt'))
                        e['recycle_result']=result;self.save(batch,m,e)
                        if not result.get('ok'):raise SafetyError('Windows 未确认进入回收站；不执行永久删除，请查看恢复记录')
                        e['status']='recycled';self.save(batch,m,e)
                    self.prune_stage(e)
                    if e.get('staging_cleanup_note'):self.save(batch,m,e)
                    if progress:progress('已移入回收站',files=sum(v['status']=='recycled' for v in m['entries']))
                except Exception as err:
                    e['error']=str(err);self.save(batch,m,e);break
            return self.view(m)

    def remaining_plan(self,m):
        pid=m.get('plan_id')
        if not isinstance(pid,str) or not re.fullmatch('[0-9a-f]{32}',pid):raise SafetyError('记录没有有效的原始预览，只可核对恢复')
        plan=read_json(self.plans/(pid+'.json'))
        allowed={row['original']:row['snapshot'] for row in m.get('selection',[])}
        if len(plan['entries'])!=len(allowed) or any(row['path'] not in allowed or not same(row['snapshot'],allowed[row['path']]) for row in plan['entries']):
            raise SafetyError('原始预览与已确认清单不符，禁止继续')
        completed={entry['original'] for entry in m['entries'] if entry['status']!='prepared' or Path(entry.get('stage','')).is_file()}
        remaining=[entry for entry in plan['entries'] if entry['path'] not in completed]
        if not remaining:raise SafetyError('没有可以继续的未处理文件，需要恢复的内容请先查看恢复记录')
        return plan,remaining

    def resume(self,bid,ack,progress=None,sync_ack=None):
        # Reuse the original, frozen preview. A later scan can never add entries.
        with self.cleaner.locked():
            folder,m=self.get(bid)
            if m.get('confirmation')!=ACK:raise SafetyError('原操作没有可信确认记录')
            plan,remaining=self.remaining_plan(m)
            continuation=dict(plan,id=key(),status='preview',expires=time.time()+1200,entries=remaining,resume_batch=bid)
            write_json(self.plans/(continuation['id']+'.json'),continuation)
        return self.execute(continuation['id'],ack,progress,sync_ack)

    def resume_preview(self,bid):
        with self.cleaner.locked():
            folder,m=self.get(bid)
            plan,remaining=self.remaining_plan(m)
            items=[]
            for entry in remaining:
                path=self.check(entry['path'],allow_sync=True)
                items.append(dict(entry,sync_risk=is_sync(path,self.cleaner.cfg().get('sync_roots',[]))))
            return {'resume_batch':bid,'items':items,'ids':[e['id'] for e in items],
                    'sync_count':sum(e['sync_risk'] for e in items),'bytes':sum(e['bytes'] for e in items),
                    'internal_batches':(len(items)+INTERNAL_BATCH-1)//INTERNAL_BATCH}

    def get(self,bid):
        if not isinstance(bid,str) or not re.fullmatch('[0-9a-f]{32}',bid):raise SafetyError('无效记录')
        folder=no_links(self.records/bid)
        manifest=read_json(folder/'manifest.json')
        if isinstance(manifest,dict) and manifest.get('version')==3:
            files=list(no_links(folder/'entry-records').glob('*.json'))
            index=no_links(folder/'entry-index.jsonl')
            expected=set()
            if index.exists():
                with index.open('rb') as stream:raw=stream.read(1024**2+1)
                if len(raw)>1024**2:raise SafetyError('恢复记录索引超过读取预算')
                identifiers=[json.loads(line) for line in raw.splitlines()]
                if any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{32}',value) for value in identifiers):raise SafetyError('恢复记录索引损坏')
                expected=set(identifiers)
            if expected!={path.stem for path in files}:raise SafetyError('逐文件恢复记录缺失或索引不一致，保留现状')
            entries=[read_json(path) for path in files]
            if any(not isinstance(entry,dict) for entry in entries):raise SafetyError('逐文件恢复记录损坏')
            if any(entry.get('id')!=path.stem for path,entry in zip(files,entries)):raise SafetyError('逐文件记录身份不一致')
            manifest['entries']=sorted((entry for entry in entries if entry.get('status')!='superseded'),key=lambda entry:entry.get('ordinal',0))
        if not isinstance(manifest,dict) or manifest.get('id')!=bid or not isinstance(manifest.get('entries'),list):
            raise SafetyError('恢复记录结构损坏，保留现状')
        for entry in manifest['entries']:
            if not isinstance(entry,dict) or not isinstance(entry.get('snapshot'),dict) or not isinstance(entry.get('original'),str):
                raise SafetyError('恢复文件记录损坏，保留现状')
            saved=entry['snapshot']
            if not re.fullmatch('[0-9a-f]{64}',str(saved.get('sha256',''))) or any(not isinstance(saved.get(field),int) for field in ('size','ino','dev','mtime_ns')):
                raise SafetyError('恢复身份或内容校验记录损坏')
        return folder,manifest

    def restore(self,bid,progress=None):
        with self.cleaner.locked():
            folder,m=self.get(bid)
            if m.get('id')!=bid or m.get('confirmation')!=ACK or m.get('version') not in (None,2,3):raise SafetyError('不是支持的已确认操作记录')
            for e in m['entries']:
                if not re.fullmatch('[0-9a-f]{32}',e['id']):raise SafetyError('损坏的文件记录')
                if m.get('selection') and not any(row['original']==e['original'] and same(row['snapshot'],e['snapshot']) for row in m['selection']):
                    raise SafetyError('恢复记录与原始确认清单不符')
                if e['status'] not in ('recycled','staged','prepared','recycling','restoring'):continue
                if e['status']=='prepared' and not Path(e.get('stage','')).is_file():continue
                # Recovery uses the recorded identity and content, independent of current
                # discovery, Git state, keep markers or cleanup authorization.
                dest=no_links(absolute(e['original']))
                if dest==Path(dest.anchor):raise SafetyError('无效恢复目标')
                if e['status']=='restoring' and dest.is_file() and same(self.file_snapshot(dest,progress=progress,phase='核对已恢复文件'),e['snapshot']):
                    e['status']='restored';self.save(folder,m,e);self.prune_stage(e);continue
                if dest.exists():raise SafetyError('原位置已有同名内容，不覆盖')
                r=e.get('recycle_result',{})
                receipt=folder/(e['id']+'.receipt')
                if not r.get('ok') and receipt.exists():
                    try:
                        r=windows_recycle.read_receipt(receipt)
                        e['recycle_result']=r
                    except (OSError,UnicodeError,SafetyError) as error:
                        e['receipt_error']=str(error)
                    self.save(folder,m,e)
                stage=no_links(absolute(e['stage']))
                stage_root=no_links(absolute(e.get('stage_root',str(dest.parent))))
                if not within(dest,stage_root):raise SafetyError('损坏的暂存范围')
                expected=stage_root/'.codex-cleanup-staging'/bid/e['id']/dest.name
                legacy=folder/e['id']/dest.name
                if stage not in (expected,legacy):raise SafetyError('损坏的暂存路径')
                if stage.is_file():r={}  # Also handles a manual Shell restore to staging.
                elif not r.get('ok') and e['status'] in ('recycling','restoring'):
                    r=windows_recycle.locate_recycled(stage)
                    e['recycle_result']=r;self.save(folder,m,e)
                if r.get('ok'):
                    source=no_links(absolute(r['recycle_path']))
                else:source=stage
                with NativeLease(source) as lease:
                    with lease.reader() as stream:
                        if not same(self.file_snapshot(source,stream,progress,'恢复前校验'),e['snapshot']):
                            raise SafetyError('恢复内容或身份已改变，请人工核对')
                    no_links(dest)
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    e['status']='restoring';self.save(folder,m,e)
                    if r.get('ok'):
                        result=windows_recycle.restore(r,dest,e['snapshot']['ino'])
                        e['restore_result']=result;self.save(folder,m,e)
                        if not result.get('ok'):raise SafetyError('Windows 未确认恢复结果，请核对记录')
                    else:os.rename(source,dest)
                    with lease.reader() as stream:
                        if not same(self.file_snapshot(dest,stream,progress,'恢复后校验'),e['snapshot']):
                            raise SafetyError('恢复校验不一致，保留现状')
                e['status']='restored';self.save(folder,m,e)
                self.prune_stage(e)
            return self.view(m)

    def view(self,m):
        return {'id':m['id'],'created':m['created'],'kind':'files',
                'entries':[{'name':e['name'],'original':e['original'],'bytes':e['snapshot']['size'],
                            'status':e['status'],'error':e.get('error'),'staging_cleanup_note':e.get('staging_cleanup_note'),
                            'recoverable':e['status'] in ('recycled','staged','recycling','restoring') or (e['status']=='prepared' and Path(e.get('stage','')).is_file())} for e in m['entries']],
                'selected_count':m.get('selected_count',len(m['entries'])),
                'success_count':sum(e['status']=='recycled' for e in m['entries']),
                'success_bytes':sum(e['snapshot']['size'] for e in m['entries'] if e['status']=='recycled'),
                'restored_count':sum(e['status']=='restored' for e in m['entries']),
                'selected_bytes':sum(e['snapshot']['size'] for e in m.get('selection',m['entries'])),
                'actual_released_bytes':None,
                'failed_count':sum(bool(e.get('error')) and e['status']!='restored' for e in m['entries']),
                'completed_ids':[e['candidate_id'] for e in m['entries'] if e['status']=='recycled' and e.get('candidate_id')],
                'can_resume':bool(m.get('plan_id')) and (m.get('selected_count',len(m['entries']))>len(m['entries']) or any(e['status']=='prepared' and not Path(e.get('stage','')).is_file() for e in m['entries'])),
                'unprocessed_count':m.get('selected_count',len(m['entries']))-len(m['entries']),
                'unprocessed':[{'name':row['name'],'original':row['original']} for row in m.get('selection',[]) if row['original'] not in {e['original'] for e in m['entries']}],
                'failed':any(e.get('error') and e['status']!='restored' for e in m['entries']),
                'recoverable':any(e['status'] in ('recycled','staged','recycling','restoring') or (e['status']=='prepared' and Path(e.get('stage','')).is_file()) for e in m['entries'])}

    def history_page(self,offset=0,limit=30):
        if not isinstance(offset,int) or not 0<=offset<=1000000 or not 1<=limit<=100:raise SafetyError('记录分页参数无效')
        folders=sorted((p for p in self.records.glob('*') if re.fullmatch('[0-9a-f]{32}',p.name)),key=lambda p:p.stat().st_mtime,reverse=True)
        records=[]
        for folder in folders[offset:offset+limit]:
            try:
                _,manifest=self.get(folder.name);records.append(self.view(manifest))
            except (OSError,ValueError,SafetyError,KeyError,TypeError) as error:
                records.append({'id':folder.name,'kind':'files','created':None,'entries':[],'recoverable':False,
                                'corrupt':True,'error':str(error),'record_path':str(folder)})
        return {'records':records,'next_offset':offset+len(records) if offset+len(records)<len(folders) else None}

    def history(self):return self.history_page()['records']
