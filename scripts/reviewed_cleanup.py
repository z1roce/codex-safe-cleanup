"""Explicitly reviewed directory quarantine. Never recursively deletes anything.

Only individually declared, explained task dependency bundles can be selected.
A preview binds exact tree bytes/identities; execution requires separate consent.
"""
import contextlib
import ctypes
import os
from pathlib import Path
import re
import subprocess
import time

from safe_cleanup import (Cleaner, SafetyError, absolute, within, no_links, is_sync,
                          read_json, write_json, digest, key, utc_stamp, snapshot, locked_source)

ACK = 'UNUSED_PROCESSES_STOPPED_RECOVERABLE'
RECYCLE_ACK = 'UNUSED_PROCESSES_STOPPED_RECYCLE_BIN'
LIMIT_FILES = 50000
LIMIT_BYTES = 16 * 1024**3
LIMIT_FILE_BYTES = 2 * 1024**3
PLAN_SECONDS = 20 * 60
BLOCKED_NAMES = {'.git', '.hg', '.svn', '.codex-keep', 'agents.md', 'agents.override.md',
                 'skill.md', '.ssh', '.aws', '.gnupg'}
BLOCKED_SUFFIXES = {'.pem', '.key', '.keystore', '.pfx', '.p12', '.sqlite', '.db'}


def identity(p):
    no_links(p)
    s = p.stat()
    return {'ino': s.st_ino, 'dev': s.st_dev, 'mtime_ns': s.st_mtime_ns}


def allocated(p):
    """NTFS allocated/compressed file bytes, not a global free-space measurement."""
    if os.name != 'nt':
        return None
    from ctypes import wintypes as w
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.GetCompressedFileSizeW.argtypes = [w.LPCWSTR, ctypes.POINTER(w.DWORD)]
    k.GetCompressedFileSizeW.restype = w.DWORD
    high = w.DWORD()
    ctypes.set_last_error(0)
    low = k.GetCompressedFileSizeW(str(p), ctypes.byref(high))
    if low == 0xffffffff and ctypes.get_last_error():
        return None
    return (high.value << 32) | low


def safe_component(p):
    no_links(p)
    if getattr(p.stat(), 'st_file_attributes', 0) & (0x1000 | 0x4000 | 0x40000 | 0x400000):
        raise SafetyError('离线、加密或云端内容，继续保留')
    name = p.name.lower()
    if name in BLOCKED_NAMES or name.startswith('.env') or p.suffix.lower() in BLOCKED_SUFFIXES:
        raise SafetyError('包含项目标记、密钥、数据库或明确保护内容，整个所选目录保留')
    if is_sync(p):
        raise SafetyError('同步目录不进入此清理入口，避免连带影响云端')


class NativeLease:
    """Keep a Windows kernel handle, avoiding the CRT's ~8K descriptor limit.

    A short-lived duplicate is wrapped as a Python stream only for its hash read.
    The kernel handle continues to deny writers until the whole tree is checked.
    """
    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        if os.name != 'nt':
            raise SafetyError('目录隔离仅支持 Windows')
        from ctypes import wintypes as w
        no_links(self.path)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateFileW.argtypes = [w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
        self.kernel.CreateFileW.restype = w.HANDLE
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.GetCurrentProcess.restype = w.HANDLE
        self.kernel.DuplicateHandle.argtypes = [w.HANDLE,w.HANDLE,w.HANDLE,ctypes.POINTER(w.HANDLE),w.DWORD,w.BOOL,w.DWORD]
        self.kernel.DuplicateHandle.restype = w.BOOL
        self.handle = self.kernel.CreateFileW(str(self.path),0x80000000|0x10000,1|4,None,3,0x80,None)
        if self.handle == w.HANDLE(-1).value:
            self.handle = None
            raise SafetyError('文件正在使用或无法锁定，整个目录继续保留')
        return self

    @contextlib.contextmanager
    def reader(self):
        from ctypes import wintypes as w
        import msvcrt
        duplicate = w.HANDLE()
        process = self.kernel.GetCurrentProcess()
        if not self.kernel.DuplicateHandle(process,self.handle,process,ctypes.byref(duplicate),0,False,2):
            raise SafetyError('无法安全读取已锁定的文件')
        try:
            fd = msvcrt.open_osfhandle(duplicate.value,os.O_RDONLY|os.O_BINARY)
        except Exception:
            self.kernel.CloseHandle(duplicate.value)
            raise
        with os.fdopen(fd,'rb') as stream:
            yield stream

    def __exit__(self, *args):
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def tree(root, progress=None, locks=None, held=None):
    """Record every file/empty directory, rejecting omissions and unsafe metadata."""
    safe_component(root)
    if not root.is_dir():
        raise SafetyError('目录已不存在，或已经被替换')
    result = {'root': identity(root), 'dirs': {}, 'files': {}, 'bytes': 0, 'allocated': 0}
    held = held if held is not None else {}
    alloc_known = True
    def error(e):
        raise SafetyError('无法完整读取目录，保留全部内容：' + str(e))
    for base, dirs, names in os.walk(root, followlinks=False, onerror=error):
        base = Path(base)
        safe_component(base)
        for name in sorted(dirs):
            p = base / name
            safe_component(p)
            result['dirs'][str(p.relative_to(root))] = identity(p)
        for name in sorted(names):
            p = base / name
            safe_component(p)
            rel = str(p.relative_to(root))
            if len(result['files']) >= LIMIT_FILES:
                raise SafetyError('目录文件数超过本次安全校验上限，未移动任何内容')
            lease = held.get(rel)
            if locks is not None and lease is None:
                lease = locks.enter_context(NativeLease(p))
                held[rel] = lease
            with lease.reader() if lease else contextlib.nullcontext(None) as stream:
                s = snapshot(p, stream, max_bytes=LIMIT_FILE_BYTES)
            result['bytes'] += s['size']
            if result['bytes'] > LIMIT_BYTES:
                raise SafetyError('所选目录超过校验上限，继续保留')
            result['files'][rel] = {k: s[k] for k in ('sha256', 'size', 'mtime_ns', 'ino', 'dev')}
            a = allocated(p)
            if a is None:
                alloc_known = False
            else:
                result['allocated'] += a
            if progress and len(result['files']) % 200 == 0:
                progress('正在校验文件', files=len(result['files']), bytes=result['bytes'])
    if not alloc_known:
        result['allocated'] = None
    return result


def tree_hash(t):
    return digest({k: t[k] for k in ('root', 'dirs', 'files')})


class ReviewedCleanup:
    def __init__(self, state):
        self.cleaner = Cleaner(state)
        self.state = self.cleaner.state
        self.records = self.state / 'reviewed-quarantine'
        self.plans = self.state / 'reviewed-plans'
        self.controls = self.state / 'panel-controls.json'

    def paused(self):
        return read_json(self.controls).get('manual_paused', False) if self.controls.exists() else False

    def set_paused(self, value):
        if type(value) is not bool:
            raise SafetyError('无效的暂停设置')
        with self.cleaner.locked():
            write_json(self.controls, {'manual_paused': value, 'updated': utc_stamp()})
            if value:
                c = self.cleaner.cfg()
                c['paused'] = True
                self.cleaner.save(c)

    def ctx(self):
        p = self.state / 'inventory-context.json'
        return read_json(p) if p.exists() else {}

    def definitions(self):
        return {digest(x['path'])[:16]: x for x in self.ctx().get('directory_explanations', [])
                if x.get('cleanup_kind') == 'task_dependency_bundle'}

    def check_target(self, item, allow_missing=False):
        p = absolute(item['path'])
        codex = absolute(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        area = codex / 'visualizations'
        tid = item.get('task_id', '')
        if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', tid):
            raise SafetyError('缺少已核对的任务身份，继续保留')
        if not within(p, area) or tid not in p.parts or p.name == tid:
            raise SafetyError('不是该任务下单独确认的依赖目录，不能清理整个任务或项目')
        if len(p.relative_to(area).parts) < 6:
            raise SafetyError('所选范围过大，继续保留')
        if not item.get('purpose') or not item.get('task_title'):
            raise SafetyError('尚未说明原任务与用途，继续保留')
        if within(p, Path.cwd()) or within(Path.cwd(), p):
            raise SafetyError('当前工作目录保护')
        ctx = self.ctx()
        cfg = self.cleaner.cfg()
        for value in cfg.get('protect', []):
            keep = absolute(value)
            if within(p, keep) or within(keep, p):
                raise SafetyError('与用户指定保护的文件或目录重叠，继续保留')
        for value in cfg.get('sync_roots', []):
            sync = absolute(value)
            if within(p, sync) or within(sync, p):
                raise SafetyError('与用户指定同步目录重叠，继续保留')
        task = next((t for t in ctx.get('threads', []) if t.get('id') == tid), None)
        if not task or task.get('status') not in ('notLoaded', 'idle', 'completed', 'archived'):
            raise SafetyError('任务状态未核实或仍在活动，继续保留')
        for x in ctx.get('projects', []):
            project = absolute(x['path'])
            if within(p, project) or within(project, p):
                raise SafetyError('仍属于已登记项目，继续保护')
        for x in ctx.get('preserve_paths', []):
            keep = absolute(x['path'])
            if within(p, keep) or within(keep, p):
                raise SafetyError('与明确保留的资料重叠')
        for parent in [p, *p.parents]:
            no_links(parent)
            if (parent / '.git').exists() or (parent / '.codex-keep').exists():
                raise SafetyError('Git 项目或保护标记，继续保留')
        if allow_missing and not p.exists():
            no_links(p)
            return p
        safe_component(p)
        if not p.is_dir():
            raise SafetyError('目录已不存在或已移入隔离区')
        if p.stat().st_dev != self.state.stat().st_dev:
            raise SafetyError('此入口只允许同一磁盘卷的可恢复移动')
        return p

    def candidates(self):
        out = []
        for iid, x in self.definitions().items():
            detail = None
            try:
                self.check_target(x)
                reason = None
            except FileNotFoundError:
                reason = '原位置已无此目录，请查看下方隔离与恢复记录'
            except (OSError, SafetyError) as e:
                detail = str(e)
                reason = detail if len(detail) < 160 and not re.search(r'[A-Za-z]:[\\/]', detail) else '安全检查未通过，继续保留；具体原因可展开查看'
            out.append(dict(x, id=iid, selectable=reason is None, blocked_reason=reason, blocked_detail=detail))
        return out

    def preview(self, ids, progress=None):
        with self.cleaner.locked():
            if self.paused():
                raise SafetyError('手动清理已暂停，先恢复操作')
            if not isinstance(ids, list) or not ids or len(ids) > 100 or not all(isinstance(x, str) for x in ids) or len(set(ids)) != len(ids):
                raise SafetyError('请明确勾选需要预览的项目')
            definitions = self.definitions()
            items = []
            paths = []
            for iid in ids:
                if iid not in definitions:
                    raise SafetyError('不能把任意路径或未核实项目交给清理入口')
                x = definitions[iid]
                p = self.check_target(x)
                if any(within(p, old) or within(old, p) for old in paths):
                    raise SafetyError('所选目录重叠，继续保留')
                paths.append(p)
                if progress:
                    progress('正在预览：' + x['name'])
                t = tree(p, progress)
                if sum(len(x['tree']['files']) for x in items) + len(t['files']) > LIMIT_FILES or sum(x['tree']['bytes'] for x in items) + t['bytes'] > LIMIT_BYTES:
                    raise SafetyError('全选内容超过单批安全校验上限，请分批预览；未移动任何内容')
                items.append(dict(x, id=iid, tree=t, fingerprint=tree_hash(t)))
            plan = {'id': key(), 'created': utc_stamp(), 'created_at': time.time(),
                    'expires_at': time.time() + PLAN_SECONDS, 'status': 'preview', 'items': items}
            write_json(self.plans / (plan['id'] + '.json'), plan)
            return self.plan_view(plan)

    def plan_view(self, plan):
        return {'id': plan['id'], 'expires_at': plan['expires_at'], 'items': [
            {k: x[k] for k in ('id', 'name', 'path', 'purpose', 'question', 'task_title', 'task_id') if k in x}
            | {'bytes': x['tree']['bytes'], 'files': len(x['tree']['files'])} for x in plan['items']],
            'note': '尚未移动。执行前会重新校验全部内容、拒绝写入中的文件，只移入同盘隔离区；不会启用自动清理。'}

    def plan(self, pid):
        if not isinstance(pid, str) or not re.fullmatch('[0-9a-f]{32}', pid):
            raise SafetyError('无效的预览编号')
        return read_json(self.plans / (pid + '.json'))

    def stored_target(self, batch, entry, progress=None):
        definition = self.definitions().get(digest(entry['original'])[:16])
        if not definition:
            raise SafetyError('原任务与用途声明已改变，隔离内容继续保留')
        original = self.check_target(definition, allow_missing=True)
        if original.exists():
            raise SafetyError('原位置已有新内容，先保留隔离副本并核对用途')
        target = self.payload(batch, entry)
        if tree_hash(tree(target, progress)) != entry['fingerprint']:
            raise SafetyError('隔离内容已变化，继续保留并核对')
        return definition, target

    def preview_stored(self, bid, progress=None):
        with self.cleaner.locked():
            if self.paused():
                raise SafetyError('手动清理已暂停')
            batch, m = self.get_batch(bid)
            items = []
            for e in m['entries']:
                if e['status'] != 'quarantined':
                    continue
                if progress:
                    progress('校验已隔离内容：' + e['name'])
                definition, target = self.stored_target(batch, e, progress)
                items.append(dict(definition, id=e['id'], entry_id=e['id'], tree=e['tree'], fingerprint=e['fingerprint']))
            if not items:
                raise SafetyError('没有可移入回收站的已隔离内容')
            plan = {'id':key(), 'mode':'stored_recycle', 'source_batch':bid, 'created':utc_stamp(),
                    'created_at':time.time(), 'expires_at':time.time()+PLAN_SECONDS, 'status':'preview', 'items':items}
            write_json(self.plans / (plan['id'] + '.json'), plan)
            return self.plan_view(plan)

    def recycle_stored(self, pid, ack, progress=None):
        if ack != RECYCLE_ACK:
            raise SafetyError('需要在预览后明确同意移入回收站')
        with self.cleaner.locked():
            if self.paused():
                raise SafetyError('手动清理已暂停')
            plan = self.plan(pid)
            if plan.get('mode') != 'stored_recycle' or plan['status'] != 'preview' or time.time() > plan['expires_at']:
                raise SafetyError('预览已失效，请重新预览')
            batch, m = self.get_batch(plan['source_batch'])
            plan['status'] = 'executing'
            write_json(self.plans / (pid + '.json'), plan)
            from windows_recycle import send
            for x in plan['items']:
                e = next((v for v in m['entries'] if v['id'] == x['entry_id']), None)
                if not e or e['status'] != 'quarantined' or e['fingerprint'] != x['fingerprint']:
                    raise SafetyError('隔离记录已改变，停止处理')
                try:
                    _, target = self.stored_target(batch, e, progress)
                    with contextlib.ExitStack() as leases:
                        if tree_hash(tree(target, progress, leases, {})) != x['fingerprint']:
                            raise SafetyError('隔离内容变化或正在使用，继续保留')
                    e['status'] = 'recycle_pending'
                    e['recycle_confirmation'] = ack
                    self.journal(batch, m)
                    result = send(target, e['tree']['root']['ino'])
                    e['recycle_result'] = result
                    if not result.get('ok'):
                        raise SafetyError('Windows 未确认进入回收站，请核对记录；不永久删除')
                    e['status'] = 'recycled'
                    self.journal(batch, m)
                except (SafetyError, OSError) as err:
                    e['error'] = str(err)
                    if self.payload(batch, e).exists():
                        e['status'] = 'quarantined'
                    self.journal(batch, m)
                    break
            plan['status'] = 'finished'
            write_json(self.plans / (pid + '.json'), plan)
            return self.batch_view(m)

    def execute(self, pid, ack, compress=False, progress=None, recycle=False):
        if type(recycle) is not bool or ack != (RECYCLE_ACK if recycle else ACK) or type(compress) is not bool or (recycle and compress):
            raise SafetyError('需要在预览后明确确认所选内容无用、相关程序已停止，并选择可恢复隔离')
        with self.cleaner.locked():
            if self.paused():
                raise SafetyError('手动清理已暂停')
            plan = self.plan(pid)
            if plan.get('mode') == 'stored_recycle':
                raise SafetyError('隔离记录预览只能通过专用回收站入口执行')
            if plan['status'] != 'preview' or time.time() > plan['expires_at']:
                raise SafetyError('预览已使用或过期，请重新预览')
            definitions = self.definitions()
            for x in plan['items']:
                if x['id'] not in definitions or definitions[x['id']]['path'] != x['path']:
                    raise SafetyError('范围已改变，重新预览')
                self.check_target(definitions[x['id']])
            bid = key()
            batch = no_links(self.records / bid)
            batch.mkdir(parents=True, exist_ok=False)
            manifest = {'id': bid, 'plan': pid, 'created': utc_stamp(), 'entries': [],
                        'confirmation': ack, 'mode': 'reviewed_directory_move'}
            plan['status'] = 'executing'
            write_json(self.plans / (pid + '.json'), plan)
            self.journal(batch, manifest)
            for x in plan['items']:
                src = absolute(x['path'])
                entry = {'id': key(), 'original': str(src), 'name': x['name'], 'purpose': x['purpose'],
                         'task_title': x['task_title'], 'status': 'prepared',
                         'payload_name': src.name if recycle else 'payload',
                         'tree': x['tree'], 'fingerprint': x['fingerprint']}
                manifest['entries'].append(entry)
                self.journal(batch, manifest)  # Write BEFORE the atomic rename.
                payload = self.payload(batch, entry)
                payload.parent.mkdir(exist_ok=False)
                try:
                    if progress:
                        progress('重新校验：' + x['name'])
                    if tree_hash(tree(src, progress)) != x['fingerprint']:
                        raise SafetyError('内容或文件身份在预览后改变，整个目录保留')
                    self.check_target(definitions[x['id']])
                    # Windows refuses a directory rename while our own child handles are open.
                    # Rename atomically first, then lock and verify; failed validation rolls back.
                    # No content is ever erased in this workflow.
                    os.rename(src, payload)
                    entry['status'] = 'staged'
                    self.journal(batch, manifest)
                    with contextlib.ExitStack() as leases:
                        held = {}
                        if progress:
                            progress('锁定并核对隔离内容：' + x['name'])
                        current = tree(payload, progress, leases, held)
                        if tree_hash(current) != x['fingerprint']:
                            raise SafetyError('移动期间目录内容变化，保留隔离内容以便恢复')
                        entry['status'] = 'quarantined'
                        self.journal(batch, manifest)
                    if recycle:
                        # First detach and verify using the existing recoverable move.
                        # The native sink refuses a permanent-delete operation.
                        from windows_recycle import send
                        entry['status'] = 'recycle_pending'
                        self.journal(batch, manifest)
                        result = send(payload, entry['tree']['root']['ino'])
                        entry['recycle_result'] = result
                        if not result.get('ok'):
                            raise SafetyError('Windows 未确认进入回收站，停止后续处理；查看记录恢复或核对回收站')
                        entry['status'] = 'recycled'
                        self.journal(batch, manifest)
                except (OSError, SafetyError) as e:
                    entry['error'] = str(e)
                    # Never overwrite a recreated source. Staging is recoverable through history.
                    if payload.exists() and not src.exists():
                        try:
                            no_links(src)
                            os.rename(payload, src)
                            entry['status'] = 'rolled_back'
                        except OSError:
                            entry['status'] = 'staged'
                    self.journal(batch, manifest)
                    break
                if compress:
                    self.compress_entry(batch, manifest, entry, progress)
            plan['status'] = 'finished'
            write_json(self.plans / (pid + '.json'), plan)
            self.journal(batch, manifest)
            return self.batch_view(manifest)

    def journal(self, batch, m):
        write_json(batch / 'manifest.json', m)

    def get_batch(self, bid):
        if not isinstance(bid, str) or not re.fullmatch('[0-9a-f]{32}', bid):
            raise SafetyError('无效的清理记录')
        batch = no_links(self.records / bid)
        return batch, read_json(batch / 'manifest.json')

    def payload(self, batch, entry):
        if not re.fullmatch('[0-9a-f]{32}', entry['id']):
            raise SafetyError('损坏的恢复记录')
        name = entry.get('payload_name', 'payload')
        if name not in ('payload', Path(entry['original']).name) or Path(name).name != name:
            raise SafetyError('损坏的隔离路径记录')
        return no_links(batch / entry['id'] / name)

    def compress_entry(self, batch, m, e, progress=None):
        target = self.payload(batch, e)
        try:
            if tree_hash(tree(target, progress)) != e['fingerprint']:
                raise SafetyError('隔离内容发生变化，保留原样，不压缩')
            if progress:
                progress('正在尝试 Windows 无损压缩：' + e['name'])
            executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/compact.exe'
            result = subprocess.run([str(executable), '/C', '/S:' + str(target), '/I', '/Q'],
                capture_output=True, timeout=300, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            after = tree(target, progress)
            if tree_hash(after) != e['fingerprint']:
                raise SafetyError('压缩后校验不一致，所有内容仍在隔离区，请人工核对')
            e['allocated_before'] = e['tree']['allocated']
            e['allocated_after'] = after['allocated']
            e['compressed'] = result.returncode == 0
            if result.returncode:
                e['compression_warning'] = 'Windows 未完成全部压缩；文件仍完整保留在隔离区'
        except (OSError, SafetyError, subprocess.TimeoutExpired) as err:
            e['compression_warning'] = str(err)
        self.journal(batch, m)

    def compress(self, bid, progress=None):
        with self.cleaner.locked():
            if self.paused():
                raise SafetyError('手动清理已暂停')
            batch, m = self.get_batch(bid)
            for e in m['entries']:
                if e['status'] == 'quarantined':
                    self.compress_entry(batch, m, e, progress)
            return self.batch_view(m)

    def restore(self, bid, progress=None):
        with self.cleaner.locked():
            batch, m = self.get_batch(bid)
            for e in m['entries']:
                if e['status'] not in ('quarantined', 'staged', 'recycled', 'recycle_pending'):
                    continue
                original = no_links(absolute(e['original']))
                cfg = self.cleaner.cfg()
                for value in cfg.get('protect', []):
                    keep = absolute(value)
                    if within(original, keep) or within(keep, original):
                        raise SafetyError('原位置已有新的用户保护规则，暂停恢复，请人工核对')
                if original.exists():
                    raise SafetyError('原位置已有内容，未覆盖。请让 Codex 帮你选择另一个恢复位置')
                if is_sync(original, cfg.get('sync_roots', [])):
                    raise SafetyError('原位置现在属于同步目录，暂停恢复，请选择非同步位置')
                for parent in original.parents:
                    if (parent / '.git').exists() or (parent / '.codex-keep').exists():
                        raise SafetyError('原位置的保护状态已改变，暂停恢复')
                if e['status'] in ('recycled', 'recycle_pending') and e.get('recycle_result', {}).get('ok'):
                    payload = no_links(absolute(e['recycle_result']['recycle_path']))
                else:
                    payload = self.payload(batch, e)
                if tree_hash(tree(payload, progress)) != e['fingerprint']:
                    raise SafetyError('隔离内容改变，保留现状，请人工核对恢复')
                original.parent.mkdir(parents=True, exist_ok=True)
                if original.parent.stat().st_dev != payload.stat().st_dev:
                    raise SafetyError('恢复位置不在同一卷，未移动任何内容')
                if progress:
                    progress('恢复原位置：' + e['name'])
                if e['status'] in ('recycled', 'recycle_pending') and e.get('recycle_result', {}).get('ok'):
                    from windows_recycle import restore
                    result = restore(e['recycle_result'], original, e['tree']['root']['ino'])
                    e['restore_result'] = result
                    self.journal(batch, m)
                    if not result.get('ok'):
                        raise SafetyError('Windows 未确认恢复完成，请核对记录中的目标和回收站；未覆盖已有文件')
                else:
                    os.rename(payload, original)
                e.update(status='restored', restored_at=utc_stamp())
                self.journal(batch, m)
            return self.batch_view(m)

    def batch_view(self, m):
        return {'id': m['id'], 'created': m['created'], 'entries': [
            {k: e[k] for k in ('id', 'original', 'name', 'purpose', 'task_title', 'status', 'error',
                              'compressed', 'compression_warning', 'allocated_before', 'allocated_after') if k in e}
            | {'bytes': e['tree']['bytes'], 'files': len(e['tree']['files'])} for e in m['entries']],
            'recoverable': any(e['status'] in ('quarantined', 'staged', 'recycled', 'recycle_pending') for e in m['entries']),
            'recycled': any(e['status'] in ('recycled', 'recycle_pending') for e in m['entries']),
            'failed': any('error' in e for e in m['entries'])}

    def history(self):
        return [self.batch_view(read_json(p)) for p in sorted(self.records.glob('*/manifest.json'),
                key=lambda p: p.stat().st_mtime, reverse=True)][:30]
