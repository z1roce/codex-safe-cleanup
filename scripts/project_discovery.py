"""Bounded, read-only adapters for known Codex index fields and manual scopes."""
import os
import json
from pathlib import Path
import re
import time

from safe_cleanup import SafetyError, absolute, no_links, utc_stamp, within, write_json

LIMIT=8*1024**2


def forbidden_roots():
    home=Path.home()
    bases={home,home/'OneDrive'}
    bases.update(Path(value) for name,value in os.environ.items() if name.lower().startswith('onedrive') and value)
    forbidden=set(bases)
    for base in bases:
        forbidden.update(base/name for name in ('Desktop','Documents','Downloads','Pictures','Videos','Music','文档','桌面','下载'))
    if os.name=='nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
                for name in ('Desktop','Personal','My Pictures','My Music','My Video','{374DE290-123F-4565-9164-39C4925E467B}'):
                    try:
                        value=winreg.QueryValueEx(key,name)[0]
                        forbidden.add(absolute(os.path.expandvars(value)))
                    except FileNotFoundError:continue
        except OSError:pass
    return forbidden


def load_object(path,warnings):
    try:
        no_links(path)
        if not path.exists():return {}
        if path.stat().st_size>LIMIT:raise ValueError('超过索引读取预算 8 MiB')
        with path.open('rb') as stream:raw=stream.read(LIMIT+1)
        if len(raw)>LIMIT:raise ValueError('索引读取时超过8MiB预算')
        value=json.loads(raw)
        if not isinstance(value,dict):raise ValueError('未知索引结构，需要对象')
        return value
    except (OSError,ValueError,SafetyError) as error:
        warnings.append({'path':str(path),'reason':str(error)})
        return {}


def context(state):
    warnings=[]
    data=load_object(Path(state)/'inventory-context.json',warnings)
    for field in ('projects','threads','preserve_paths','directory_explanations'):
        rows=data.get(field,[])
        if not isinstance(rows,list):
            warnings.append({'path':field,'reason':'未知字段结构，跳过'})
            rows=[]
        data[field]=[row for row in rows if isinstance(row,dict)]
    manual=load_object(Path(state)/'manual-projects.json',warnings).get('projects',[])
    if isinstance(manual,list):
        data['projects'] += [dict(row,source='用户手动登记，仅只读范围') for row in manual if isinstance(row,dict)]
    else:warnings.append({'path':'manual-projects.json','reason':'未知登记结构，跳过'})
    data['discovery_warnings']=warnings
    relocations=load_object(Path(state)/'project-relocations.json',warnings).get('relocations',[])
    data['relocations']=relocations if isinstance(relocations,list) else []
    return data


def valid_project(value,codex_roots):
    if not isinstance(value,str):raise SafetyError('项目路径字段不是字符串')
    p=no_links(absolute(value))
    if p==Path(p.anchor) or p in forbidden_roots() or p in codex_roots:
        raise SafetyError('通用用户目录或整盘，不能因聊天使用过就扫描全部内容')
    system=[os.environ.get(name) for name in ('SystemRoot','ProgramFiles','ProgramFiles(x86)','ProgramData')]
    if any(within(p,absolute(root)) for root in system if root):raise SafetyError('系统和软件安装目录不属于项目检查范围')
    if any(part.lower() in ('.git','.hg','.svn','.ssh','.aws','.gnupg','.agents','skills','plugins','sessions','backups','.sandbox','.sandbox-secrets','.codex-cleanup-staging') for part in p.parts):
        raise SafetyError('受保护内部目录不能登记为项目')
    if p.exists() and not p.is_dir():raise SafetyError('项目路径不是目录')
    return p


def discover(ctx,codex_roots):
    proposed=[];omitted=list(ctx.get('discovery_warnings',[]))
    visited=0
    def add(value,source,index_time=None):
        if value is None:return
        try:
            original=absolute(value) if isinstance(value,str) else None
            for relocation in ctx.get('relocations',[]):
                if not isinstance(relocation,dict):continue
                old=absolute(relocation['from'])
                if original is not None and within(original,old):
                    value=str(absolute(relocation['to'])/original.relative_to(old))
                    source+=' · 用户重新关联';index_time=relocation.get('registered_at');break
            p=valid_project(value,codex_roots)
            proposed.append({'path':str(p),'sources':[source],'index_time':index_time})
        except (OSError,ValueError,SafetyError,KeyError) as error:
            omitted.append({'path':str(value),'reason':str(error)})
    for project in ctx.get('projects',[]) or []:
        if not isinstance(project,dict):continue
        add(project.get('path'),project.get('source','Codex 登记项目')+' '+str(project.get('label') or ''),
            project.get('registered_at',ctx.get('captured_at')))
    for thread in ctx.get('threads',[]) or []:
        if isinstance(thread,dict):add(thread.get('cwd'),'Codex 历史任务工作目录',ctx.get('captured_at'))
    for codex in codex_roots:
        add(str(codex/'visualizations'),'Codex 各任务输出目录')
        add(str(codex/'worktrees'),'Codex 工作副本，仅盘点不删除')
        file=codex/'.codex-global-state.json'
        data=load_object(file,omitted)
        if not file.exists():omitted.append({'path':str(file),'reason':'本机项目索引缺失，仅覆盖其他已知来源'})
        try:stamp=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(file.stat().st_mtime))
        except OSError:stamp=None
        def paths(value,source,depth=0):
            nonlocal visited
            visited+=1
            if visited>20000:
                if visited==20001:omitted.append({'path':str(file),'reason':'本机索引达到20000成员预算，未继续'})
                return
            if depth>6:
                omitted.append({'path':str(file),'reason':'索引嵌套达到预算，未继续'});return
            if isinstance(value,str):add(value,source,stamp)
            elif isinstance(value,list):
                for child in value[:10000]:paths(child,source,depth+1)
            elif isinstance(value,dict):
                # Known project records use rootPaths. Thread maps contain paths.
                if 'rootPaths' in value:paths(value['rootPaths'],source,depth+1)
                elif 'path' in value:paths(value['path'],source,depth+1)
                else:
                    for child in list(value.values())[:10000]:
                        if isinstance(child,(dict,list)) or (isinstance(child,str) and re.match(r'^[A-Za-z]:[\\/]',child)):
                            paths(child,source,depth+1)
                        elif len(omitted)<500:omitted.append({'path':str(file),'reason':'未知索引成员，跳过'})
            elif value is not None:omitted.append({'path':str(file),'reason':'未知索引字段结构，跳过'})
        for field in ('local-projects','thread-workspace-root-hints','thread-projectless-output-directories'):
            if field in data:paths(data[field],'Codex 本机索引 '+field)
        if data and not any(field in data for field in ('local-projects','thread-workspace-root-hints','thread-projectless-output-directories')):
            omitted.append({'path':str(file),'reason':'未找到已知项目字段，本机结构可能已更新'})
    result=[]
    for item in sorted(proposed,key=lambda row:len(Path(row['path']).parts)):
        parent=next((row for row in result if within(Path(item['path']),Path(row['path']))),None)
        if parent:parent['sources']=list(dict.fromkeys(parent['sources']+item['sources']))
        else:result.append(item)
    return result,omitted


def register(state,path,codex_roots,relocate_from=None):
    p=valid_project(path,codex_roots)
    if within(p,Path(state)) or within(Path(state),p):raise SafetyError('手动项目不能覆盖工具状态目录')
    if not p.is_dir():raise SafetyError('手动登记需要当前可访问的具体目录')
    if relocate_from:
        old=valid_project(relocate_from,codex_roots)
        known,_=discover(context(state),codex_roots)
        if str(old) not in {row['path'] for row in known}:raise SafetyError('旧路径不是已关联项目，不能重新关联')
        if old.exists():raise SafetyError('旧路径仍然存在，请分别核对两个项目')
        file=Path(state)/'project-relocations.json';warnings=[]
        data=load_object(file,warnings)
        if warnings:raise SafetyError('已有重关联记录损坏，保留原样')
        rows=data.get('relocations',[])
        if not isinstance(rows,list) or len(rows)>=128:raise SafetyError('重关联记录格式无效或达到上限')
        rows.append({'from':str(old),'to':str(p),'registered_at':utc_stamp()})
        write_json(file,{'version':1,'relocations':rows})
        return {'path':str(p),'previous_path':str(old),'permission':'仅重新关联只读范围，历史清理记录保持原始路径'}
    file=Path(state)/'manual-projects.json'
    warnings=[];data=load_object(file,warnings)
    if warnings:raise SafetyError('已有登记文件无法读取，保留原样')
    rows=data.get('projects',[])
    if not isinstance(rows,list) or len(rows)>=128:raise SafetyError('手动登记格式无效或已达到128项目上限')
    if not any(row.get('path')==str(p) for row in rows):
        rows.append({'path':str(p),'registered_at':utc_stamp(),'label':p.name})
    write_json(file,{'version':1,'projects':rows})
    return {'path':str(p),'permission':'仅登记检查范围，未授予清理权限'}
