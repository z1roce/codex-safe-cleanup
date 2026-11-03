"""Exact, user-confirmed Codex cache files -> Windows Recycle Bin; no permanent purge."""
import os
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
MAX_BYTES=128*1024**2
MAX_FILE=128*1024**2


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

    def save(self,folder,m):write_json(folder/'manifest.json',m)

    def enabled(self):
        controls=self.state/'panel-controls.json'
        if controls.exists() and read_json(controls).get('manual_paused'):
            raise SafetyError('手动清理已暂停；恢复记录仍可使用')

    def preview(self,report_id,ids,progress=None):
        if not isinstance(report_id,str) or not re.fullmatch('[0-9a-f]{32}',report_id):raise SafetyError('请先重新检查')
        if not isinstance(ids,list) or not ids or len(ids)>100 or not all(isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
            raise SafetyError('请选择需要核对的文件，每批最多100项')
        with self.cleaner.locked():
            self.enabled()
            report=read_json(self.state/'codex-reports'/(report_id+'.json'))
            if report.get('scope_version')!=inventory.SCOPE_VERSION:raise SafetyError('旧扫描不能用于清理，请重新检查')
            candidates={x['id']:x for x in report.get('candidates',[])}
            entries=[];total=0
            for iid in ids:
                if iid not in candidates:raise SafetyError('包含未扫描或受保护的内容')
                row=candidates[iid];p=self.check(row['path'],allow_sync=True);s=snapshot(p,max_bytes=MAX_FILE)
                if any(s[k]!=row['file_stamp'][k] for k in ('ino','dev','size','mtime_ns')):
                    raise SafetyError('扫描后文件已改变，请重新检查')
                total+=s['size']
                if total>MAX_BYTES:raise SafetyError('超过单批128MiB校验上限，请分批')
                entries.append(dict(row,snapshot=s,sync_risk=is_sync(p,self.cleaner.cfg().get('sync_roots',[]))))
                if progress:progress('校验所选缓存',files=len(entries))
            plan={'id':key(),'created':utc_stamp(),'expires':time.time()+1200,'status':'preview','entries':entries}
            write_json(self.plans/(plan['id']+'.json'),plan)
            return {'id':plan['id'],'items':[{k:x[k] for k in ('name','path','purpose','impact','bytes','task_id','sync_risk')} for x in entries],
                    'sync_count':sum(x['sync_risk'] for x in entries),
                    'bytes':total,'note':'仅预览，尚未移动。勾选同意并确认后才进入回收站。'}

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
                if not same(snapshot(p,max_bytes=MAX_FILE),e['snapshot']):raise SafetyError('内容已改变，未开始清理')
                if p.stat().st_dev!=self.state.stat().st_dev:raise SafetyError('暂存和源文件必须同卷，保留文件')
            batch=self.records/key();batch.mkdir(parents=True,exist_ok=False)
            m={'id':batch.name,'created':utc_stamp(),'entries':[],'confirmation':ack}
            plan['status']='used';write_json(self.plans/(pid+'.json'),plan)
            self.save(batch,m)
            for old in plan['entries']:
                p=self.check(old['path'],allow_sync=sync_ack==SYNC_ACK);e={'id':key(),'original':str(p),'name':old['name'],
                    'snapshot':old['snapshot'],'status':'prepared'}
                m['entries'].append(e);self.save(batch,m)
                stage=batch/e['id']/p.name;stage.parent.mkdir()
                e['stage']=str(stage);self.save(batch,m)
                try:
                    with NativeLease(p) as lease:
                        with lease.reader() as f:
                            if not same(snapshot(p,f,max_bytes=MAX_FILE),e['snapshot']):raise SafetyError('文件变化，保留')
                        os.rename(p,stage)
                        e['status']='staged';self.save(batch,m)
                        with lease.reader() as f:
                            if not same(snapshot(stage,f,max_bytes=MAX_FILE),e['snapshot']):raise SafetyError('暂存校验未通过，请恢复')
                    result=windows_recycle.native_operation('recycle',stage,e['snapshot']['ino'])
                    e['recycle_result']=result;self.save(batch,m)
                    if not result.get('ok'):raise SafetyError('Windows 未确认进入回收站；不执行永久删除，请查看恢复记录')
                    e['status']='recycled';self.save(batch,m)
                    if progress:progress('已移入回收站',files=sum(v['status']=='recycled' for v in m['entries']))
                except Exception as err:
                    e['error']=str(err);self.save(batch,m);break
            return self.view(m)

    def get(self,bid):
        if not isinstance(bid,str) or not re.fullmatch('[0-9a-f]{32}',bid):raise SafetyError('无效记录')
        folder=no_links(self.records/bid)
        return folder,read_json(folder/'manifest.json')

    def restore(self,bid,progress=None):
        with self.cleaner.locked():
            folder,m=self.get(bid)
            for e in m['entries']:
                if e['status'] not in ('recycled','staged','prepared'):continue
                if e['status']=='prepared' and not Path(e.get('stage','')).is_file():continue
                dest=self.check(e['original'],exists=False,allow_sync=True)
                if dest.exists():raise SafetyError('原位置已有同名内容，不覆盖')
                r=e.get('recycle_result',{})
                if r.get('ok'):
                    source=no_links(absolute(r['recycle_path']))
                else:
                    source=no_links(absolute(e['stage']))
                    if not within(source,folder):raise SafetyError('损坏的暂存路径')
                if not same(snapshot(source,max_bytes=MAX_FILE),e['snapshot']):raise SafetyError('恢复内容已改变，请人工核对')
                dest.parent.mkdir(parents=True,exist_ok=True)
                if r.get('ok'):
                    result=windows_recycle.restore(r,dest,e['snapshot']['ino'])
                    e['restore_result']=result;self.save(folder,m)
                    if not result.get('ok'):raise SafetyError('Windows 未确认恢复结果，请核对记录')
                else:os.rename(source,dest)
                if not same(snapshot(dest,max_bytes=MAX_FILE),e['snapshot']):raise SafetyError('恢复校验不一致，保留现状')
                e['status']='restored';self.save(folder,m)
            return self.view(m)

    def view(self,m):
        return {'id':m['id'],'created':m['created'],'kind':'files',
                'entries':[{'name':e['name'],'original':e['original'],'bytes':e['snapshot']['size'],
                            'status':e['status'],'error':e.get('error')} for e in m['entries']],
                'failed':any(e.get('error') for e in m['entries']),
                'recoverable':any(e['status'] in ('recycled','staged') or (e['status']=='prepared' and Path(e.get('stage','')).is_file()) for e in m['entries'])}

    def history(self):
        return [self.view(read_json(p)) for p in sorted(self.records.glob('*/manifest.json'),key=lambda p:p.stat().st_mtime,reverse=True)][:30]
