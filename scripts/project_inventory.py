"""Scoped project inventory and bounded static evidence for generated artifacts."""
import collections,hashlib,json,os,re,stat,time,uuid
from pathlib import Path
from safe_cleanup import Cleaner,SafetyError,absolute,no_links,within,is_sync,read_json
MAX_FILES=200000
MAX_SECONDS=30
MAX_HASH_BYTES=0
SOURCE={'.java','.py','.js','.ts','.tsx','.cs','.cpp','.c','.h','.rs','.go','.sql','.ps1'}
DELIVERY={'.doc','.docx','.ppt','.pptx','.xls','.xlsx','.pdf','.apk','.exe','.zip','.7z','.rar','.mp4'}
HASH_TYPES=set()
SECRET_NAMES={'.ssh','.aws','.gnupg','credentials','secrets'}
SECRET_SUFFIXES={'.pem','.key','.keystore','.pfx','.sqlite','.db'}
CLOUD_FLAGS=0x1000|0x40000|0x400000
CPYTHON_MAGIC={'312':3531,'313':3571,'314':3627}
UUID=re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$',re.I)
HINTS={'tmp','temp','scratch','render','renders','preview','screenshots','browser-qa','gradle-home','java-user','toolchain','sdk','build','dist','__pycache__'}
def context(state):return {}
def is_hint(name):
    return name.lower() in HINTS or name.lower().endswith(('-inspect', '_inspect', '-inspection', '_inspection'))


def group_path(p, root):
    rel = p.relative_to(root)
    parts = rel.parts[:-1]
    if root.name == 'visualizations':
        for i, part in enumerate(parts):
            if UUID.fullmatch(part):
                # Split a task into inspectable subdirectories, never a whole-project deletion.
                depth = min(len(parts), i + 4)
                return root.joinpath(*parts[:depth])
    if parts and re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[0]):
        return root.joinpath(*parts[:min(3, len(parts))])
    if 'ChatGPT' in parts:
        i = parts.index('ChatGPT')
        for j in range(i + 2, len(parts)):
            if is_hint(parts[j]):
                return root.joinpath(*parts[:j + 1])
        return root.joinpath(*parts[:min(len(parts), i + 3)])
    if root.name == 'ChatGPT':
        for j in range(1, len(parts)):
            if is_hint(parts[j]):
                return root.joinpath(*parts[:j + 1])
        return root.joinpath(*parts[:min(2, len(parts))])
    return root.joinpath(*parts[:min(2, len(parts))]) if parts else root


def _scan(state, ctx=None, scopes=None, progress=None, cancel=None):
    state = Cleaner(state).state
    ctx = ctx or context(state)
    scopes = scopes or []
    started = time.monotonic()
    groups, files, coverage = {}, [], []
    bytecode_paths, package_scripts, package_paths = [], [], []
    pytest_paths,java_paths=[],[]
    skipped = []
    count = 0
    budget_hit = False
    known = {t['id']: t for t in ctx.get('threads', []) if isinstance(t.get('id'),str)}
    explanations = ctx.get('task_explanations', {})
    codex = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    def problem(path, why):
        if len(skipped) < 1000:
            skipped.append({'path': str(path), 'reason': why})
    for scope in scopes:
        root = absolute(scope['path'])
        if progress:progress('检查 Codex 项目：'+root.name,files=count)
        cover = dict(scope, status='complete', files=0, logical_bytes=0)
        coverage.append(cover)
        if not root.exists():
            cover['status'] = 'missing'
            continue
        if budget_hit:
            cover['status'] = 'not_scanned_limit'
            continue
        try:
            no_links(root)
        except (OSError, ValueError, SafetyError) as e:
            cover.update(status='skipped', reason=str(e))
            continue
        if within(root, codex / '.tmp') or within(root, codex / 'tmp'):
            cover.update(status='protected_runtime', reason='Codex 内部运行资源；不当作垃圾')
            continue
        def walk_error(e):
            cover['status'] = 'partial'
            problem(getattr(e, 'filename', root), '无法读取：' + str(e))
        for base, dirs, names in os.walk(root, followlinks=False, onerror=walk_error):
            if time.monotonic()-started>=MAX_SECONDS:
                budget_hit=True;cover['status']='partial_limit';break
            if cancel is not None and cancel.is_set():raise SafetyError('本次项目检查已取消；未清理文件')
            base = Path(base)
            safe_dirs = []
            for name in dirs:
                if time.monotonic()-started>=MAX_SECONDS:
                    budget_hit=True;cover['status']='partial_limit';break
                p = base / name
                try:
                    s = p.lstat()
                    if name.lower() in SECRET_NAMES or name.lower() in ('.git', '.hg', '.svn', '.agents', 'skills', 'plugins', 'sessions', '.codex-cleanup-staging'):
                        problem(p, '凭据或版本历史保护，未深入统计')
                        continue
                    if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & (0x400|CLOUD_FLAGS):
                        cover['status'] = 'partial'
                        problem(p, '链接、目录联接或云端占位目录，未深入')
                        continue
                    safe_dirs.append(name)
                except OSError as e:
                    cover['status'] = 'partial'
                    problem(p, str(e))
            dirs[:] = safe_dirs
            for name in names:
                if cancel is not None and cancel.is_set():raise SafetyError('本次项目检查已取消；未清理文件')
                if count >= MAX_FILES or time.monotonic() - started >= MAX_SECONDS:
                    budget_hit = True
                    cover['status'] = 'partial_limit'
                    break
                p = base / name
                try:
                    s = p.lstat()
                    if not stat.S_ISREG(s.st_mode) or stat.S_ISLNK(s.st_mode):
                        continue
                    if p.name.lower().startswith('.env') or p.suffix.lower() in SECRET_SUFFIXES:
                        problem(p, '密钥、环境配置或数据库保护；不读取内容')
                        continue
                    count += 1
                    if progress and count % 1000 == 0:progress('检查 Codex 项目产物',files=count)
                    cover['files'] += 1
                    cover['logical_bytes'] += s.st_size
                    if p.suffix.lower()=='.zip':package_paths.append(str(p))
                    if p.suffix=='.pyc' and p.parent.name=='__pycache__':bytecode_paths.append(str(p))
                    if p.parent.name=='cache' and p.parent.parent.name=='v' and p.parent.parent.parent.name=='.pytest_cache' and p.name in ('nodeids','lastfailed'):
                        pytest_paths.append(str(p))
                    if p.suffix=='.class':java_paths.append(str(p))
                    if p.name=='package_release.py' and p.parent.name=='tools':package_scripts.append(str(p))
                    gp = group_path(p, root)
                    g = groups.setdefault(str(gp), dict(path=str(gp), bytes=0, file_count=0,
                        samples=[], source_files=0, delivery_files=0, hints=set(), tasks=set(),
                        cloud_files=0, newest_mtime=0))
                    g['bytes'] += s.st_size
                    g['file_count'] += 1
                    g['newest_mtime'] = max(g['newest_mtime'], s.st_mtime)
                    if len(g['samples']) < 5:
                        g['samples'].append(name)
                    g['source_files'] += p.suffix.lower() in SOURCE
                    g['delivery_files'] += p.suffix.lower() in DELIVERY
                    g['cloud_files'] += bool(getattr(s, 'st_file_attributes', 0) & CLOUD_FLAGS)
                    for part in p.relative_to(root).parts[:-1]:
                        if UUID.fullmatch(part) and part in known:
                            g['tasks'].add(part)
                        if is_hint(part):
                            g['hints'].add(part)
                    if p.suffix.lower() in HASH_TYPES and 64 * 1024 <= s.st_size <= 32 * 1024**2:
                        files.append((p, s))
                except OSError as e:
                    cover['status'] = 'partial'
                    problem(p, str(e))
            if budget_hit:
                break
    items = []
    for g in groups.values():
        reasons = []
        gp = Path(g['path'])
        task_rows = [known[tid] for tid in g['tasks']]
        origin = next((explanations[t['id']] for t in task_rows if t['id'] in explanations), {})
        purpose = next((x for x in ctx.get('directory_explanations', [])
                        if within(gp, Path(x['path']))), {})
        if task_rows:
            reasons.append('关联聊天：' + '；'.join(t['title'] for t in task_rows))
        if origin.get('origin'):
            reasons.append('来源：' + origin['origin'])
        if purpose.get('purpose'):
            reasons.append('原用途（已核对）：' + purpose['purpose'])
        else:
            reasons.append('原用途尚未核实；不把名称或位置线索当作废弃证据，继续保留')
        if g['hints']:
            reasons.append('存在中间产物/依赖线索：' + '、'.join(sorted(g['hints'])[:4]) + '；线索不能证明无用')
        if g['source_files'] or g['delivery_files']:
            reasons.append(f"含 {g['source_files']} 个源码/脚本、{g['delivery_files']} 个文档/安装包/压缩包或视频，保留价值未排除")
        if g['cloud_files']:
            reasons.append('有云端占位内容，逻辑大小不等于本地占用；未下载内容')
        repo = any((parent / '.git').exists() for parent in [gp, *gp.parents])
        active = within(gp, Path.cwd()) or any(t.get('status') in ('active', 'running', 'inProgress') for t in task_rows)
        if active:
            reasons.append('属于当前工作目录或已知活动任务')
        if repo:
            reasons.append('Git 项目保护，未提交/未推送/忽略文件均未排除')
        if is_sync(gp):
            reasons.append('同步目录；任何后续处理都需考虑云端影响')
        if not reasons:
            reasons.append('尚无证据证明是 Codex 废弃内容，按原始资料保留')
        # Review is a question queue, NOT a deletion class or authorization.
        in_task_area = True  # Every scope was tied to a Codex project/task; hints still grant no deletion permission.
        suspect = bool(task_rows or (g['hints'] and in_task_area))
        mixed_project = not task_rows and (repo or g['source_files'] or g['delivery_files'])
        retained_delivery = bool(g['delivery_files'] and not g['hints'])
        preserve_reason = next((x['reason'] for x in ctx.get('preserve_paths', [])
                                if within(gp, Path(x['path'])) or within(Path(x['path']), gp)), None)
        if preserve_reason:
            reasons.append('明确保留依据：' + preserve_reason)
        status = 'review' if suspect and not (active or mixed_project or retained_delivery or preserve_reason) else 'protected'
        reasons.append('整个目录未获清理许可；需核对用途、备份和外部引用')
        g.update(id=hashlib.sha256(g['path'].encode()).hexdigest()[:16], kind='directory',
                 status=status, reason='；'.join(reasons), tasks=[t['id'] for t in task_rows],
                 hints=sorted(g['hints']), sample_names=g.pop('samples'),
                 human_name=purpose.get('name', ''), purpose=purpose.get('purpose', '原用途尚未核实，继续保留'),
                 origin_tasks=[{'id':t['id'],'title':t['title']} for t in task_rows])
        items.append(g)
    hashed = 0
    duplicate_groups = []
    items.sort(key=lambda x: (x['status'] != 'review', -x['bytes']))
    report = dict(mode='inventory_only', id=uuid.uuid4().hex, created=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        complete=not budget_hit and all(x['status'] in ('complete', 'missing', 'protected_runtime') for x in coverage),
        coverage=coverage, skipped=skipped, file_count=count, logical_bytes=sum(x['logical_bytes'] for x in coverage),
        size_note='逻辑大小，仅供定位；不是本地实际占用，更不是可释放空间。保护/跳过内容不计入。',
        counts={s: sum(x['status'] == s for x in items) for s in ('protected', 'review')},
        eligible=0, items=items, duplicate_groups=duplicate_groups, hash_bytes=hashed,
        context_captured_at=ctx.get('captured_at'), thread_metadata_count=len(known),
        exclusions=['系统和程序目录未整盘遍历', '凭据和版本历史未读取', '链接/云端占位目录未穿透',
                    '未保证发现未登记、离线或其他电脑上的目录', '回收站未清空，也未统计其可释放空间'])
    report['counts']['eligible'] = 0
    report['review_summary'] = []
    for x in ctx.get('directory_explanations', []):
        members = [g for g in items if within(Path(g['path']), Path(x['path']))]
        if members and any(g['status'] == 'review' for g in members):
            report['review_summary'].append(dict(x, bytes=sum(g['bytes'] for g in members),
                file_count=sum(g['file_count'] for g in members), decision='用途已说明，是否仍需使用待用户判断；继续保留'))
    report['bytecode_paths']=bytecode_paths
    report['package_scripts']=package_scripts
    report['package_paths']=package_paths
    report['pytest_paths']=pytest_paths
    report['java_paths']=java_paths
    report['evidence_limits']=[{'kind':kind,'discovered':len(paths),'checked_limit':limit}
        for kind,paths,limit in [('Python 缓存',bytecode_paths,500),('发布脚本',package_scripts,20),
                                ('pytest 缓存',pytest_paths,500),('Java 编译缓存',java_paths,500)] if len(paths)>limit]
    return report


def discover_projects(ctx,codex_roots):
    from project_discovery import discover
    return discover(ctx,codex_roots)


def scan_projects(state,ctx,codex_roots,progress=None,cancel=None,project=None):
    scopes,omitted=discover_projects(ctx,codex_roots)
    if project is not None:
        scopes=[scope for scope in scopes if absolute(scope['path'])==absolute(project)]
        if not scopes:raise SafetyError('只能按已发现或手动登记的项目检查')
    safe_ctx=dict(ctx,threads=[dict(t,title=t.get('title') or '任务标题未核实') for t in ctx.get('threads',[])])
    result=_scan(state,safe_ctx,scopes,progress,cancel)
    result['complete']=result['complete'] and not omitted and all(x['status']=='complete' for x in result['coverage'])
    result['scope_omissions']=omitted
    for row in result['items']:
        row['selectable']=False
        row['reason']+='；项目/聊天关联及名称线索不是废弃证明，未加载的聊天也可能仍有用途。'
    return result


def discover_generated(result,progress=None,cancel=None,known_artifacts=()):
    # Static evidence only: never execute project scripts or infer uselessness from age.
    import ast,struct
    found=[]
    evidence_started=time.monotonic();read_bytes=0
    def bounded_read(path,limit=512*1024):
        nonlocal read_bytes
        no_links(path)
        info=path.stat()
        if getattr(info,'st_file_attributes',0)&(CLOUD_FLAGS|0x400):raise SafetyError('云端或链接证据文件，继续保留')
        size=info.st_size
        if size>limit or read_bytes+size>8*1024**2 or time.monotonic()-evidence_started>5:
            result.setdefault('evidence_limits',[]).append({'kind':'生成证据','path':str(path),'reason':'单文件/总读取8MiB/5秒预算达到上限，未识别'})
            raise SafetyError('生成证据读取预算达到上限')
        with path.open('rb') as stream:data=stream.read(limit+1)
        read_bytes+=len(data)
        if len(data)>limit:raise SafetyError('证据读取时文件变化')
        return data
    def add(path,scope,purpose,proof,category=None):
        try:
            no_links(path);s=path.stat()
            if not path.is_file() or s.st_nlink!=1 or getattr(s,'st_file_attributes',0)&(0x400|CLOUD_FLAGS):return
            found.append({'path':str(path),'scope_root':str(scope),'task_id':'',
                'task_title':'任务标题未核实；项目：'+(scope.parent.name if scope.name in ('scripts','tools') else scope.name),'origin':proof,'name':(scope.parent.name if scope.name in ('scripts','tools') else scope.name)+' · '+path.name,
                'purpose':purpose,'reason':'生成或打包用途有具体证据；是否已不再需要仍由用户确认。',
                'impact':'只处理此生成文件，不动源码、原始资料和最终成果；需要诊断或重新打包时请保留。',
                'category':category or ('bytecode_cache' if path.suffix=='.pyc' else 'test_output'),'keep':False,
                'stamp':{'ino':s.st_ino,'dev':s.st_dev,'size':s.st_size,'mtime_ns':s.st_mtime_ns}})
        except (OSError,SafetyError):pass
    for name in result.get('bytecode_paths',[])[:500]:
        if cancel is not None and cancel.is_set():raise SafetyError('本次项目检查已取消')
        p=Path(name)
        match=re.fullmatch(r'(.+)\.cpython-(312|313|314)(?:\.opt-[12])?\.pyc',p.name)
        if not match:continue
        source=p.parent.parent/(match[1]+'.py')
        try:
            no_links(p);no_links(source)
            if not source.is_file():continue
            if getattr(p.stat(),'st_file_attributes',0)&(CLOUD_FLAGS|0x400):continue
            with p.open('rb') as f:header=f.read(16)
            magic=CPYTHON_MAGIC[match[2]].to_bytes(2,'little')+b'\r\n'
            if len(header)!=16 or header[:4]!=magic or p.stat().st_size<=16:continue
            flags=struct.unpack('<I',header[4:8])[0]
            if flags not in (0,1,3):continue
            if flags==0:
                stamp,size=struct.unpack('<II',header[8:16]);info=source.stat()
                if stamp!=(int(info.st_mtime)&0xffffffff) or size!=(info.st_size&0xffffffff):continue
            add(p,p.parent.parent,'Python 根据仍存在的源码自动生成的字节码缓存；不是源码或唯一编译产物。',
                '已核对 Python 缓存头及对应源码位置：'+str(source))
        except (OSError,SafetyError):continue
    for name in result.get('pytest_paths',[])[:500]:
        if cancel is not None and cancel.is_set():raise SafetyError('本次项目检查已取消')
        p=Path(name);cache=p.parent.parent.parent
        try:
            if p.name not in ('nodeids','lastfailed') or cache.name!='.pytest_cache':continue
            for marker in (p,cache/'README.md',cache/'CACHEDIR.TAG',cache/'.gitignore'):no_links(marker)
            if any(marker.stat().st_size>256*1024 for marker in (p,cache/'README.md',cache/'CACHEDIR.TAG',cache/'.gitignore')):continue
            if not bounded_read(cache/'README.md').decode('utf-8').startswith('# pytest cache directory #'):continue
            if not bounded_read(cache/'CACHEDIR.TAG').startswith(b'Signature: 8a477f597d28d172789f06886806bc55'):continue
            if bounded_read(cache/'.gitignore').decode('utf-8').replace('\r\n','\n')!='# Created by pytest automatically.\n*\n':continue
            data=json.loads(bounded_read(p).decode('utf-8'))
            valid=(isinstance(data,list) and all(isinstance(value,str) for value in data)) if p.name=='nodeids' else (
                isinstance(data,dict) and all(isinstance(k,str) and isinstance(v,bool) for k,v in data.items()))
            if valid:add(p,cache.parent,'pytest 生成的测试节点/失败记录；移除后会失去上次测试选择信息。',
                         'pytest 缓存目录标记、自动生成 ignore 标记及 JSON 格式均已核对','test_output')
        except (OSError,SafetyError,ValueError,UnicodeError):continue
    for name in result.get('java_paths',[])[:500]:
        if cancel is not None and cancel.is_set():raise SafetyError('本次项目检查已取消')
        p=Path(name)
        output=next((parent for parent in p.parents if parent.name in ('classes','test-classes') and parent.parent.name=='target'),None)
        if output is None:continue
        scope=output.parent.parent
        relative=p.relative_to(output)
        source=scope/'src'/('main' if output.name=='classes' else 'test')/'java'/relative.parent/(p.stem.split('$')[0]+'.java')
        try:
            no_links(p);no_links(source);no_links(scope/'pom.xml')
            if not source.is_file() or not (scope/'pom.xml').is_file():continue
            if getattr(p.stat(),'st_file_attributes',0)&(CLOUD_FLAGS|0x400):continue
            with p.open('rb') as stream:header=stream.read(10)
            if len(header)!=10 or header[:4]!=b'\xca\xfe\xba\xbe':continue
            minor,major,pool=struct.unpack('>HHH',header[4:10])
            if minor not in (0,65535) or not 52<=major<=69 or pool<1:continue
            add(p,scope,'Maven 标准输出位置中的 Java 编译缓存；对应源码仍存在。需要运行或交付时请保留。',
                '已核对 class 格式（Java 8–25）、pom.xml 与对应源码位置：'+str(source),'bytecode_cache')
        except (OSError,SafetyError):continue
    for name in result.get('package_scripts',[])[:20]:
        if cancel is not None and cancel.is_set():raise SafetyError('本次项目检查已取消')
        script=Path(name);scope=script.parent.parent
        try:
            no_links(script)
            if script.stat().st_size>256*1024:continue
            code=bounded_read(script,256*1024).decode('utf-8-sig');tree=ast.parse(code)
            if not any(isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='root' for t in n.targets)
                       and ast.unparse(n.value)=='Path(__file__).resolve().parents[1]' for n in tree.body):continue
            names=set()
            for n in tree.body:
                if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='test_output_names' for t in n.targets):
                    if isinstance(n.value,(ast.Set,ast.List,ast.Tuple)) and all(isinstance(v,ast.Constant) and isinstance(v.value,str) for v in n.value.elts):names={v.value for v in n.value.elts}
            excluded=any(isinstance(n,ast.If) and any(isinstance(b,ast.Continue) for b in n.body)
                         and any(isinstance(c,ast.Compare) and any(isinstance(v,ast.Name) and v.id=='test_output_names' for v in c.comparators) for c in ast.walk(n.test)) for n in ast.walk(tree))
            if not names or not excluded:continue
            producers=[]
            src=scope/'src'
            no_links(src)
            if src.is_dir():
                for file in list(src.glob('*.cs'))[:40]:
                    no_links(file)
                    if file.stat().st_size<=512*1024:producers.append((file,bounded_read(file).decode('utf-8-sig')))
            for output in names:
                if Path(output).name!=output or Path(output).suffix not in ('.txt','.log'):continue
                p=script.parent/output
                if not p.is_file():continue
                writer=None
                target=r'Path\.Combine\(\s*Storage\.Root\s*,\s*"tools"\s*,\s*"'+re.escape(output)+r'"\s*\)'
                for file,text in producers:
                    direct=re.search(r'File\.WriteAll(?:Text|Lines)\s*\(\s*'+target,text)
                    aliases=re.findall(r'(?:string|var)\s+(\w+)\s*=\s*'+target,text)
                    if direct or any(re.search(r'File\.WriteAll(?:Text|Lines)\s*\(\s*'+re.escape(alias)+r'\s*,',text) for alias in aliases):
                        writer=file;break
                if writer:
                    add(p,scope,'项目自检生成的结果文件；打包脚本明确将其排除，不是正式运行资源。',
                        '生成代码：'+str(writer)+'；发布排除清单：'+str(script))
            if progress:progress('核对项目生成文件与发布清单',files=len(found))
        except (OSError,SafetyError,SyntaxError,ValueError,UnicodeError):continue
    # A current, explicitly retained package is the comparison anchor, not mtime/name.
    import zipfile
    def package_identity(path):
        no_links(path)
        if getattr(path.stat(),'st_file_attributes',0)&(CLOUD_FLAGS|0x400):return None
        if path.stat().st_size>32*1024**2 or time.monotonic()-evidence_started>5:return None
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist())>2000:return None
            for member in archive.infolist():
                if member.filename.endswith('/SKILL.md') and member.file_size<65536:
                    text=archive.read(member).decode('utf-8-sig')
                    match=re.search(r'^name:\s*[\"\']?([a-z0-9-]+)',text,re.M)
                    if match:return match.group(1)
    for entry in known_artifacts:
        if not entry.get('keep') or entry.get('category')!='superseded_package':continue
        current=Path(entry['path'])
        try:
            identity=package_identity(current)
            if not identity:continue
            for name in result.get('package_paths',[])[:500]:
                old=Path(name)
                if old==current or old.parent!=current.parent:continue
                if package_identity(old)==identity:
                    add(old,old.parent,'与当前已保留安装包属于同一技能的历史包；可能仍有回滚价值，默认不替用户认定无用。',
                        '已读取包内 SKILL 身份 '+identity+'；当前保留包：'+str(current),'superseded_package')
        except (OSError,SafetyError,ValueError,zipfile.BadZipFile,UnicodeError):continue
    return found
