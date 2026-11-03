#!/usr/bin/env python3
"""Codex-scoped inventory with bounded generated-artifact evidence; no deletion grant."""
import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
import stat
import sys
import time
import uuid
from types import SimpleNamespace

sys.dont_write_bytecode = True
from safe_cleanup import Cleaner, SafetyError, absolute, no_links, write_json, read_json, within, is_sync, PROTECTED_SUFFIXES, PROTECTED_NAMES, digest

SCOPE_VERSION = 8
MAX_FILES = 20000
MAX_SECONDS = 10
BLOCK_FLAGS = 0x400 | 0x1000 | 0x40000 | 0x400000
NESTED_PROTECTION = {'plugins', 'skills', 'sessions', 'backups', 'remote_plugin_catalog',
                     '.git', '.codex-keep', '.ssh', '.aws'}
PURPOSES = {
    'cache': ('Codex 缓存', '可能包含插件目录索引及工具运行记录；具体文件是否闲置尚未核实。'),
    'tmp': ('Codex 临时运行目录', '包含命令代理和运行锁；不能因为叫 tmp 就认定无用。'),
    '.tmp': ('Codex 同步与维护目录', '包含插件同步及维护锁，插件子目录不深入检查。'),
}
_GIT_INDEX_CACHE={}


def git_tracked_status(path):
    """Read the repository index, never update it or run hooks. None means unknown."""
    p=Path(path)
    repo=next((a for a in p.parents if (a/'.git').exists()),None)
    if repo is None:return False
    executable=shutil.which('git')
    if not executable:return None
    marker=repo/'.git';index=marker/'index' if marker.is_dir() else marker
    try:
        stamp=index.stat();cache_key=(str(repo),stamp.st_ino,stamp.st_mtime_ns,stamp.st_size)
    except OSError:cache_key=(str(repo),0,0)
    # A worktree .git file points elsewhere; its timestamp cannot validate the index.
    if not marker.is_dir() or cache_key not in _GIT_INDEX_CACHE:
        env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_OPTIONAL_LOCKS='0',GIT_TERMINAL_PROMPT='0')
        try:
            r=subprocess.run([executable,'-c','core.fsmonitor=false','-C',str(repo),'ls-files','--cached','-z'],
                             capture_output=True,timeout=5,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if r.returncode:return None
            if len(_GIT_INDEX_CACHE)>64:_GIT_INDEX_CACHE.clear()
            _GIT_INDEX_CACHE[cache_key]={os.path.normcase(str(repo/Path(n.decode('utf-8')))) for n in r.stdout.split(b'\0') if n}
        except (OSError,subprocess.TimeoutExpired,UnicodeError):return None
    return os.path.normcase(str(p)) in _GIT_INDEX_CACHE[cache_key]


def candidate_info(path, state=None, allow_missing=False):
    """Only a known regenerated Codex cache format, never arbitrary JSON/temp files."""
    p = absolute(str(path))
    for root in roots():
        if not within(p, root):
            continue
        parts = p.relative_to(root).parts
        if (len(parts) == 5 and parts[:3] == ('cache','computer-use','computer-use-app-catalog')
                and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', parts[3])
                and parts[4] in ('shell-apps.json','user-assist.json')
                and parts[3] != os.environ.get('CODEX_THREAD_ID')):
            return {'id':uuid.uuid5(uuid.NAMESPACE_URL,str(p)).hex[:16], 'path':str(p),
                    'name':'Codex 电脑操作应用清单缓存 · '+parts[4], 'task_id':parts[3],
                    'purpose':'由 Codex 电脑操作功能生成，重新列出应用时可重建；不属于配置、插件或聊天历史。',
                    'impact':'清理后下次需要重新生成清单；仍在使用该任务时请保留。'}
    if state is not None:
        for entry in artifact_entries(state):
            if entry.get('path') == str(p):
                row = inspect_artifact(entry,allow_missing,state=state)
                return row if row.get('selectable') else None
    return None


def artifact_entries(state):
    file = Path(state)/'artifact-registry.json'
    manual=read_json(file).get('artifacts',[]) if file.exists() else []
    discovered=Path(state)/'discovery-index.json'
    auto=read_json(discovered).get('artifacts',[]) if discovered.exists() else []
    combined={x['path']:x for x in auto}
    combined.update({x['path']:x for x in manual})
    return list(combined.values())


def metadata_signature(state,ctx=None):
    file=Path(state)/'artifact-registry.json'
    indexes=[]
    for root in roots():
        path=root/'.codex-global-state.json'
        try:
            info=no_links(path).stat();indexes.append([str(path),info.st_ino,info.st_size,info.st_mtime_ns])
        except (OSError,SafetyError):indexes.append([str(path),'unavailable'])
    return digest({'context':context(state) if ctx is None else ctx,
                   'local_indexes':indexes,
                   'manual_artifacts':read_json(file) if file.exists() else {}})


def inspect_artifact(entry,allow_missing=False,state=None):
    required=('path','scope_root','task_id','task_title','origin','purpose','reason','impact','category','stamp')
    if not all(k in entry for k in required):
        raise SafetyError('产物登记缺少来源证据或快照，不能用于清理')
    p=no_links(absolute(entry['path']));scope=no_links(absolute(entry['scope_root']))
    if scope==Path(scope.anchor) or scope==Path.home() or not within(p,scope):
        raise SafetyError('登记产物范围无效')
    if allow_missing and not p.exists():
        stamp=entry['stamp']
        info=SimpleNamespace(st_size=stamp['size'],st_ino=stamp['ino'],st_dev=stamp['dev'],
                             st_mtime_ns=stamp['mtime_ns'],st_nlink=1,st_mode=stat.S_IFREG,st_file_attributes=0)
    else:info=p.stat()
    row=dict(entry,id=uuid.uuid5(uuid.NAMESPACE_URL,str(p)).hex[:16],name=entry.get('name',p.name),
             bytes=info.st_size,human_name=entry.get('name',p.name),kind='file',status='review',
             selectable=False,confirmed=False,sample_names=[],file_count=1,partial=False,
             file_stamp={'ino':info.st_ino,'dev':info.st_dev,'size':info.st_size,'mtime_ns':info.st_mtime_ns})
    row['sync_risk']=is_sync(p)
    blocked=None
    if entry.get('keep'):blocked='当前交付物或明确保留内容，保护'
    elif state is not None and any(within(p,absolute(x['path'])) for x in context(state).get('preserve_paths',[])):
        blocked='项目索引中已明确保留的内容，保护'
    elif entry['category'] not in ('test_output','superseded_package','discarded_tool_download','bytecode_cache','task_dependency_cache'):
        blocked='产物用途未核实，继续保留'
    elif row['file_stamp']!=entry['stamp']:blocked='登记后内容或文件身份变化，重新核实前保留'
    elif not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:blocked='不是独立普通文件，保护'
    elif getattr(info,'st_file_attributes',0)&BLOCK_FLAGS:blocked='链接、云端或离线内容，保护'
    elif any(a.name.lower() in ('.agents','skills','plugins','sessions') for a in p.parents):
        blocked='Skills、插件或历史目录保护，只展示不一键清理'
    elif p.name.lower() in PROTECTED_NAMES or p.name.lower() in ('auth.json','config.json','config.toml','settings.json','settings.toml','.codex-global-state.json','session_index.jsonl') or p.suffix.lower() in (PROTECTED_SUFFIXES- {'.json','.txt','.zip','.pdf'})|{'.toml','.ini','.yaml','.yml'}:
        blocked='源码、配置、凭据、数据库或运行锁保护'
    elif any((a/'.codex-keep').exists() for a in p.parents):blocked='保留标记保护'
    elif git_tracked_status(p) is not False:blocked='Git 跟踪内容或索引状态未确认，保护'
    for root in roots():
        if any(within(p,root/n) for n in ('plugins','skills','sessions','rules','backups','.sandbox','.sandbox-secrets')):
            blocked='Codex 配置、插件、Skills、历史或备份保护'
    row['reason']=entry['reason']+('；'+blocked if blocked else '；生成证据已核实，但是否不再需要仍须用户确认')
    if row['sync_risk'] and not blocked:row['reason']+='；位于同步目录，执行前还需单独确认云端/其他设备会同步移除这些文件'
    row['selectable']=blocked is None
    return row


def roots():
    result = [absolute(str(Path.home() / '.codex'))]
    configured = os.environ.get('CODEX_HOME')
    if configured:
        other = no_links(absolute(configured))
        if other == Path(other.anchor) or other == Path.home():
            raise SafetyError('CODEX_HOME 不能指向整盘或整个用户目录')
        if other not in result:
            result.append(other)
    return [p for p in result if not any(p != q and within(p, q) for q in result)]


def context(state):
    from project_discovery import context as load_context
    return load_context(state)


def discover(state=None, ctx=None, extra=()):
    allowed = roots()
    if any(absolute(p) not in allowed for p in extra):
        raise SafetyError('仅允许当前用户 .codex 和明确配置的 CODEX_HOME；拒绝额外目录')
    return [{'path': str(p), 'sources': ['当前用户 .codex' if p == Path.home()/'.codex'
                                       else 'CODEX_HOME 明确配置的 Codex 目录']} for p in allowed]


def run_inventory(state, ctx=None, scopes=None, extra=(), progress=None, cancel=None,project=None,deep=False):
    state = Cleaner(state).state
    expected = discover(state, extra=extra)
    allowed = [Path(s['path']) for s in expected]
    if scopes is not None and any(absolute(x['path']) not in allowed for x in scopes):
        raise SafetyError('扫描范围必须是核实过的 Codex 数据根；拒绝其他目录')
    scopes = ([] if project else expected) if scopes is None else scopes
    started = time.perf_counter()
    items, skipped, coverage, candidates = [], [], [], []
    total_files = 0
    complete = True

    def checkpoint():
        if cancel is not None and cancel.is_set():
            raise SafetyError('本次扫描已取消；未清理文件，继续显示上次完成的结果')
        return total_files < MAX_FILES and time.perf_counter()-started < MAX_SECONDS

    def skip(path, reason):
        if len(skipped) < 500:
            skipped.append({'path': str(path), 'reason': reason})

    def count_tree(root):
        nonlocal total_files, complete
        size, count, samples = 0, 0, []
        partial = False
        todo = [root]
        while todo:
            if not checkpoint():
                complete = False
                return size, count, samples, True
            folder = todo.pop()
            try:
                with os.scandir(folder) as entries:
                    for e in entries:
                        if not checkpoint():
                            complete = False
                            return size, count, samples, True
                        p = Path(e.path)
                        info = e.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & BLOCK_FLAGS:
                            skip(p, '链接、云端占位或离线内容，未穿透也未下载')
                            partial = True
                            continue
                        if stat.S_ISDIR(info.st_mode):
                            if e.name.lower() in NESTED_PROTECTION:
                                skip(p, '受保护内容，不深入统计')
                                partial = True
                            else:
                                todo.append(p)
                        elif stat.S_ISREG(info.st_mode):
                            size += info.st_size
                            count += 1
                            total_files += 1
                            candidate = candidate_info(p)
                            if candidate:
                                # Windows DirEntry caches may report inode/link count as zero.
                                identity = p.stat()
                                if identity.st_nlink == 1:
                                    candidates.append(dict(candidate, bytes=identity.st_size,
                                        file_stamp={'ino':identity.st_ino,'dev':identity.st_dev,'size':identity.st_size,'mtime_ns':identity.st_mtime_ns},
                                        selectable=True, status='review', confirmed=False))
                            if len(samples) < 5:
                                samples.append(str(p.relative_to(root)))
                            if progress and total_files % 200 == 0:
                                progress('只统计 Codex 文件名和大小', files=total_files)
            except OSError as e:
                skip(folder, '无法完整读取，继续保留：'+type(e).__name__)
                partial = True
        if partial:
            complete = False
        return size, count, samples, partial

    for scope in scopes:
        root = Path(scope['path'])
        cover = dict(scope, status='complete', files=0, logical_bytes=0)
        coverage.append(cover)
        try:
            no_links(root)
            if not root.exists():
                cover['status'] = 'missing'
                continue
            with os.scandir(root) as entries:
                for e in entries:
                    if not checkpoint():
                        complete = False
                        cover['status'] = 'partial_limit'
                        break
                    p = Path(e.path)
                    info = e.stat(follow_symlinks=False)
                    row = {'id':uuid.uuid5(uuid.NAMESPACE_URL,str(p)).hex[:16], 'path':str(p),
                           'human_name':e.name, 'kind':'directory' if stat.S_ISDIR(info.st_mode) else 'file',
                           'bytes':None, 'file_count':0, 'sample_names':[], 'status':'protected',
                           'reason':'配置、历史、项目或用途未核实的内容，默认保留；不读取内容。',
                           'selectable':False, 'partial':False}
                    if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & BLOCK_FLAGS:
                        row['reason'] = '链接、云端占位或离线内容，未深入检查。'
                    elif stat.S_ISDIR(info.st_mode) and e.name in PURPOSES:
                        row['human_name'], row['reason'] = PURPOSES[e.name]
                        row['bytes'], row['file_count'], row['sample_names'], row['partial'] = count_tree(p)
                        row['status'] = 'review' if e.name == 'cache' else 'protected'
                        row['reason'] += ' 仅统计，不代表可释放空间；用途未确认，不清理。'
                        cover['files'] += row['file_count']
                        cover['logical_bytes'] += row['bytes']
                    elif stat.S_ISREG(info.st_mode):
                        row['bytes'] = info.st_size
                        row['file_count'] = 1
                        total_files += 1
                        cover['files'] += 1
                        cover['logical_bytes'] += info.st_size
                        if p.suffix.lower() in ('.sqlite','.db') or '.sqlite-' in e.name:
                            row['reason'] = 'Codex 数据库或配套文件；仅查看大小，不打开、不整理、不删除。'
                    else:
                        row['reason'] = '受保护或用途未知的目录，不深入扫描；大小未统计。'
                    items.append(row)
            if cover['status'] == 'complete' and any(x['partial'] for x in items):
                cover['status'] = 'partial_protected_or_limit'
        except SafetyError as e:
            if cancel is not None and cancel.is_set():
                raise
            complete = False
            cover.update(status='skipped', reason=str(e))
        except OSError as e:
            complete = False
            cover.update(status='skipped', reason=type(e).__name__)
    items.sort(key=lambda x:(x['status']!='review', -(x['bytes'] or 0)))
    import project_inventory
    ctx=context(state) if ctx is None else ctx
    projects=project_inventory.scan_projects(state,ctx,roots(),progress,cancel,project,deep)
    generated=project_inventory.discover_generated(projects,progress,cancel,artifact_entries(state))
    discovery_file=state/'discovery-index.json'
    remembered=read_json(discovery_file).get('artifacts',[]) if discovery_file.exists() else []
    discovered={x['path']:x for x in remembered}
    discovered.update({x['path']:x for x in generated})
    if discovered:write_json(discovery_file,{'version':1,'artifacts':list(discovered.values())})
    artifacts=[]
    for entry in artifact_entries(state):
        if project and not within(absolute(entry['path']),absolute(project)):continue
        try:
            row=inspect_artifact(entry,state=state)
            artifacts.append(row)
            if row['selectable']:candidates.append(row)
        except (OSError,SafetyError,ValueError) as e:
            skipped.append({'path':entry.get('path','未登记路径'),'reason':str(e)})
    complete=complete and projects['complete'] and not projects.get('evidence_limits')
    candidates=list({row['id']:row for row in candidates}.values())
    report = {'mode':'inventory_only', 'scope_version':SCOPE_VERSION, 'id':uuid.uuid4().hex,
              'created':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()), 'complete':complete,
              'elapsed_seconds':round(time.perf_counter()-started,4), 'coverage':coverage, 'skipped':skipped,
              'file_count':total_files, 'logical_bytes':sum(x['bytes'] or 0 for x in items),
              'items':items, 'counts':{s:sum(x['status']==s for x in items) for s in ('review','protected')},
              'eligible':0, 'candidates':candidates, 'duplicate_groups':[], 'hash_bytes':0, 'review_summary':[],
              'scanned_logical_bytes':sum(x['bytes'] or 0 for x in items)+projects['logical_bytes'],
              'candidate_bytes':sum(row['bytes'] for row in candidates),'actual_released_bytes':None,
              'project_items':projects['items'],'project_coverage':projects['coverage'],
              'project_complete':projects['complete'],'project_file_count':projects['file_count'],
              'selected_project':project,'project_scope_omissions':projects['scope_omissions'],
              'project_skipped':projects['skipped'],
              'scan_profile':projects['scan_profile'],'scan_limits':projects['scan_limits'],
              'evidence_limits':projects.get('evidence_limits',[]),
              'project_index_captured_at':ctx.get('captured_at'),'project_count':len(ctx.get('projects',[])),
              'thread_count':len(ctx.get('threads',[])),'task_artifacts':artifacts,
              'metadata_signature':metadata_signature(state,ctx),'record_state':str(state),
              'visible_findings':list({x['id']:x for x in [*candidates,*artifacts]}.values()),
              'size_note':'仅已统计文件的逻辑大小；保护目录未统计，不是整个 .codex 占用或可释放空间。',
              'exclusions':['未扫描 Windows、整盘、常用文件夹或其他软件',
                            '配置、指令、Skills、插件和历史保护；未核实项目文件不获删除许可',
                            '运行数据只读元数据；项目生成物限量核对生成脚本或格式，不执行代码、不做内容重复判断']}
    folder = state/'codex-reports'
    report['preview'] = str(folder/(report['id']+'.md'))
    write_json(folder/(report['id']+'.json'), report)
    lines = ['# Codex 专用只读检查', '', report['size_note'], '',
             f"耗时 {report['elapsed_seconds']:.3f} 秒；确认可清理项：0。", '']
    for row in items:
        size = '未统计' if row['bytes'] is None else f"{row['bytes']/1024**2:.2f} MiB"
        lines += [f"- {row['human_name']}：{size}；{row['reason']}", f"  路径：{row['path']}"]
    lines+=['','## Codex 项目覆盖',f"登记项目 {len(ctx.get('projects',[]))} 个，聊天 {len(ctx.get('threads',[]))} 个；项目元数据文件 {projects['file_count']} 个。"]
    for cover in projects['coverage']:lines.append('- '+cover['path']+'：'+cover['status'])
    for row in projects['items']:lines+=['- '+row['path']+'：'+str(row['bytes'])+' 字节；'+row['reason']]
    lines+=['','## 已核实关联的具体任务产物']
    for row in artifacts:lines+=['- '+row['name']+'：'+str(row['bytes'])+' 字节；'+row['purpose']+'；'+row['reason'],'  路径：'+row['path']]
    lines += ['', *['- '+x for x in report['exclusions']]]
    for x in skipped:
        lines.append('- 跳过 '+x['path']+'：'+x['reason'])
    Path(report['preview']).write_text('\n'.join(lines), encoding='utf-8')
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', default=str(Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'CodexSafeCleanup'))
    p.add_argument('--scopes', action='store_true')
    p.add_argument('--project', help='只检查已发现项目的完整路径')
    p.add_argument('--deep', action='store_true',help='用户主动深度检查选定项目，最多100万文件/180秒')
    p.add_argument('--relocate-from',help='明确重新关联已移动的旧项目路径，配合 --register-project')
    p.add_argument('--register-project', help='手动登记具体项目，仅授予只读范围')
    a = p.parse_args()
    c = Cleaner(a.state)
    if a.register_project:
        from project_discovery import register
        with c.locked():result=register(c.state,a.register_project,roots(),a.relocate_from)
    elif a.scopes:
        from project_discovery import discover as discover_projects
        projects,omitted=discover_projects(context(c.state),roots())
        result = {'scopes':discover(c.state),'projects':projects,'omissions':omitted}
    else:
        with c.locked():
            result = run_inventory(c.state,project=a.project,deep=a.deep)
    print(json.dumps(result,ensure_ascii=False))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
