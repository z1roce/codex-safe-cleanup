#!/usr/bin/env python3
"""Conservative Windows cleanup. Standard library only, Python 3.12+.

No recursive directory removal, shell commands, network access or permanent purge.
Only exact approved file snapshots or closed registered scratch snapshots can move.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
import zipfile

VERSION = 1
MAX_ENTRIES = 5000
MAX_FILE_BYTES = 128 * 1024**2
MAX_HASH_BYTES = 512 * 1024**2
COOLDOWN = 24 * 3600
AUTO_SUFFIXES = {'.tmp', '.log', '.cache', '.pyc', '.part'}
PROTECTED_SUFFIXES = {
    '.java', '.py', '.js', '.ts', '.tsx', '.jsx', '.c', '.h', '.cpp', '.cs',
    '.go', '.rs', '.sql', '.doc', '.docx', '.ppt', '.pptx', '.xls', '.xlsx',
    '.pdf', '.zip', '.7z', '.rar', '.psd', '.blend', '.pem', '.key', '.sqlite', '.db', '.lock', '.pid',
}
PROTECTED_NAMES = {'.git', '.hg', '.svn', '.env', '.ssh', '.aws', '.agents',
                   'agents.md', 'agents.override.md', 'skill.md', '.codex-keep'}
SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '.venv', 'venv'}
SYNC_NAMES = {'onedrive', 'dropbox', 'googledrive', 'google drive', 'icloudrive', 'iclouddrive'}
SCOPE_ACK = 'APPROVE_SCOPE_AND_RULES'
ITEM_ACK = 'CONFIRMED_UNUSED_AND_UNREFERENCED'


class SafetyError(Exception):
    pass


def absolute(value):
    p = Path(value).expanduser()
    if not p.is_absolute() or '..' in p.parts:
        raise SafetyError('必须使用绝对路径，且不能包含 ..')
    s = str(p)
    if os.name == 'nt' and (s.startswith('\\\\') or ':' in s[2:]):
        raise SafetyError('拒绝网络路径、设备路径和备用数据流路径')
    return Path(os.path.abspath(p))


def within(p, parent):
    return p == parent or parent in p.parents


def no_links(p):
    """Check lexically, BEFORE resolve; do not traverse junctions or cloud links."""
    p = absolute(p)
    for part in reversed([p, *p.parents]):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise SafetyError('符号链接、目录联接或重解析点：' + str(part))
    return p


def sync_roots():
    roots = []
    for k, v in os.environ.items():
        if k.lower().startswith('onedrive') and v:
            roots.append(absolute(v))
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r'Software\Microsoft\OneDrive\Accounts') as accounts:
                for i in range(winreg.QueryInfoKey(accounts)[0]):
                    with winreg.OpenKey(accounts, winreg.EnumKey(accounts, i)) as account:
                        try:
                            roots.append(absolute(winreg.QueryValueEx(account, 'UserFolder')[0]))
                        except FileNotFoundError:
                            pass
        except FileNotFoundError:
            pass
    return roots


def is_sync(p, extra=()):
    if any(part.lower() in SYNC_NAMES or part.lower().startswith('onedrive - ')
           for part in p.parts):
        return True
    return any(within(p, r) for r in [*sync_roots(), *(absolute(x) for x in extra)])


def utc_stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def key():
    return uuid.uuid4().hex


def canonical(data):
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def digest(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def read_json(path):
    no_links(path)
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    no_links(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + key() + '.new')
    with temp.open('xb') as f:
        f.write(canonical(value))
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def hash_stream(f):
    h = hashlib.sha256()
    while block := f.read(1024 * 1024):
        h.update(block)
    return h.hexdigest()


def has_ads(p):
    """ZIP cannot preserve NTFS alternate streams. Fail closed on API errors."""
    if os.name != 'nt':
        return False
    from ctypes import wintypes as w
    class StreamData(ctypes.Structure):
        _fields_ = [('size', ctypes.c_longlong), ('name', w.WCHAR * 296)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.FindFirstStreamW.argtypes = [w.LPCWSTR, ctypes.c_int, ctypes.POINTER(StreamData), w.DWORD]
    kernel.FindFirstStreamW.restype = w.HANDLE
    kernel.FindNextStreamW.argtypes = [w.HANDLE, ctypes.POINTER(StreamData)]
    kernel.FindNextStreamW.restype = w.BOOL
    kernel.FindClose.argtypes = [w.HANDLE]
    data = StreamData()
    handle = kernel.FindFirstStreamW(str(p), 0, ctypes.byref(data), 0)
    if handle == w.HANDLE(-1).value:
        if ctypes.get_last_error() == 38:  # ERROR_HANDLE_EOF, no streams
            return False
        raise SafetyError('无法检查 NTFS 附加数据流，保留文件')
    try:
        if data.name != '::$DATA':
            return True
        while kernel.FindNextStreamW(handle, ctypes.byref(data)):
            if data.name != '::$DATA':
                return True
        if ctypes.get_last_error() != 38:
            raise SafetyError('无法完整检查 NTFS 附加数据流')
        return False
    finally:
        kernel.FindClose(handle)


@contextlib.contextmanager
def locked_source(p):
    """Windows: deny writers for the ENTIRE transaction, including the final unlink.

    Desired DELETE access lets us dispose of this exact handle, avoiding a final
    path lookup. Sharing READ|DELETE lets rename work but excludes every writer.
    Any existing writer (even one allowing shared deletion) makes CreateFile fail.
    """
    no_links(p)
    if os.name != 'nt':
        raise SafetyError('文件隔离仅支持 Windows；其他系统只允许扫描')
    from ctypes import wintypes as w
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                  w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateFileW(str(p), 0x80000000 | 0x10000, 1 | 4, None, 3, 0x80, None)
    if handle == w.HANDLE(-1).value:
        raise SafetyError('无法锁定文件（可能正被使用、写入或只读），保留：' + str(p))
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(fd, 'rb') as f:
        yield f


def dispose_locked(f):
    """Delete the isolated duplicate through its locked identity, not its path."""
    from ctypes import wintypes as w
    import msvcrt
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.SetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetFileInformationByHandle.restype = w.BOOL
    delete = ctypes.c_ubyte(1)  # FILE_DISPOSITION_INFO.DeleteFile is BOOLEAN.
    if not kernel.SetFileInformationByHandle(msvcrt.get_osfhandle(f.fileno()), 4,
                                            ctypes.byref(delete), ctypes.sizeof(delete)):
        raise SafetyError('隔离副本无法安全移除，ZIP 与暂存文件均保留')


def snapshot(p, source=None, max_bytes=None):
    no_links(p)
    before = p.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise SafetyError('不是普通独立文件（可能是硬链接）')
    if getattr(before, 'st_file_attributes', 0) & (0x1000 | 0x4000 | 0x40000 | 0x400000):
        raise SafetyError('离线、加密或云端文件，保留')
    if has_ads(p):
        raise SafetyError('有 NTFS 附加数据流，保留')
    limit = MAX_FILE_BYTES if max_bytes is None else max_bytes
    if before.st_size > limit:
        raise SafetyError('超过单文件校验上限，保留并人工检查')
    if source is None:
        with p.open('rb') as f:
            h = hash_stream(f)
    else:
        identity = os.fstat(source.fileno())
        if (identity.st_ino, identity.st_dev) != (before.st_ino, before.st_dev):
            raise SafetyError('路径不再指向已锁定的文件')
        source.seek(0)
        h = hash_stream(source)
    after = p.stat()
    fields = ('st_size', 'st_mtime_ns', 'st_ino', 'st_dev')
    if any(getattr(before, x) != getattr(after, x) for x in fields):
        raise SafetyError('扫描期间内容发生变化')
    return {'sha256': h, 'size': before.st_size, 'mtime_ns': before.st_mtime_ns,
            'ino': before.st_ino, 'dev': before.st_dev,
            'mode': stat.S_IMODE(before.st_mode), 'atime_ns': before.st_atime_ns}


def same(a, b):
    return all(a.get(k) == b.get(k) for k in ('sha256', 'size', 'mtime_ns', 'ino', 'dev'))


class Cleaner:
    def __init__(self, state):
        self.state = no_links(absolute(state))
        if is_sync(self.state):
            raise SafetyError('运行记录与隔离区必须位于非同步目录')
        self.config_file = self.state / 'config.json'

    @contextlib.contextmanager
    def locked(self):
        no_links(self.state)
        self.state.mkdir(parents=True, exist_ok=True)
        lock_path = no_links(self.state / 'operation.lock')
        with lock_path.open('a+b') as f:
            f.seek(0)
            f.write(b'0')
            f.flush()
            f.seek(0)
            if os.name == 'nt':
                import msvcrt
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise SafetyError('另一个清理操作正在运行；不要删除锁文件')
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                if os.name == 'nt':
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    def cfg(self):
        c = read_json(self.config_file)
        if c['version'] != VERSION:
            raise SafetyError('配置版本不兼容')
        return c

    def policy_hash(self, c):
        return digest({k: c[k] for k in ('version', 'roots', 'protect', 'sync_roots', 'managed_rule')})

    def save(self, c):
        write_json(self.config_file, c)

    def init(self, roots):
        if self.config_file.exists():
            raise SafetyError('已有配置，拒绝覆盖')
        checked = self.validate_roots(roots)
        c = dict(version=VERSION, roots=checked, protect=[], sync_roots=[], managed_rule=False,
                 approved_policy=None, paused=True, receipts={}, tasks={})
        self.save(c)
        return c

    def validate_roots(self, roots):
        result = []
        for root in roots:
            p = no_links(absolute(root))
            codex = absolute(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
            if not within(p, codex):
                raise SafetyError('仅接受 Codex 数据目录内的范围；不扫描其他软件、用户文件夹或普通项目')
            if p == Path(p.anchor) or p == Path.home() or within(self.state, p) or within(p, self.state):
                raise SafetyError('拒绝整盘、整个用户目录或包含运行记录的扫描范围')
            if any(within(p, absolute(x)) or within(absolute(x), p) for x in result):
                raise SafetyError('扫描范围相互重叠')
            result.append(str(p))
        return result

    def protected_reason(self, p, c):
        no_links(p)
        if is_sync(p, c['sync_roots']):
            return '同步目录默认保护，避免云端连带删除'
        if within(Path.cwd(), p) or within(p, Path.cwd()):
            return '当前正在使用的工作目录'
        if any(within(p, absolute(x)) for x in c['protect']):
            return '用户指定保护'
        # Ancestors beyond the scan root matter: a cache inside a repository is still Git-owned.
        for ancestor in [p, *p.parents]:
            if ancestor.name.lower() in PROTECTED_NAMES or ancestor.name.lower().startswith('.env'):
                return '原始资料、配置、密钥或技能默认保护'
            if ancestor.is_dir():
                if (ancestor / '.codex-keep').exists():
                    return '.codex-keep 标记保护'
                if (ancestor / '.git').exists() or (ancestor / '.git').is_symlink():
                    return 'Git 项目整体保护（包含未提交、未推送和忽略文件）；使用官方工作副本流程'
        if p.suffix.lower() in PROTECTED_SUFFIXES:
            return '源码、文档、最终包或数据库默认保护'
        codex = absolute(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        if within(p, codex):
            if within(p, codex / '.tmp') or any(within(p, codex / 'tmp' / x)
                                               for x in ('arg0', 'arg0-windows')):
                return 'Codex 插件同步、锁与命令代理等内部运行资源保护'
            permitted = [codex / 'tmp', codex / 'visualizations']
            if not any(within(p, x) for x in permitted):
                return 'Codex 内部状态、历史、运行库或工作副本默认保护'
        return None

    def task_files(self, folder, c):
        files = {}
        count = 0
        size = 0
        def walk_error(e):
            raise SafetyError('无法完整读取临时任务，整个任务保留：' + str(e))
        for base, dirs, names in os.walk(folder, followlinks=False, onerror=walk_error):
            no_links(Path(base))
            for name in dirs:
                p = Path(base) / name
                no_links(p)
                if name.lower() in SKIP_DIRS or self.protected_reason(p, c):
                    raise SafetyError('临时任务目录内出现受保护的目录')
            for name in names:
                count += 1
                if count > MAX_ENTRIES:
                    raise SafetyError('临时任务过大，人工检查')
                p = Path(base) / name
                reason = self.protected_reason(p, c)
                if reason or p.suffix.lower() not in AUTO_SUFFIXES:
                    raise SafetyError(reason or '包含不在临时文件白名单内的文件，整个任务保留')
                s = snapshot(p)
                size += s['size']
                if size > MAX_HASH_BYTES:
                    raise SafetyError('临时任务超过校验总量上限')
                files[str(p)] = s
        return files

    def scan(self):
        c = self.cfg()
        self.validate_roots(c['roots'])
        report = dict(id=key(), created=utc_stamp(), policy_hash=self.policy_hash(c),
                      complete=True, roots=c['roots'], protections=c['protect'],
                      managed_rule=c['managed_rule'], items=[], notes=[])
        count = 0
        hashed = 0
        def add(p, kind='file', force=None, managed=None):
            nonlocal count, hashed
            count += 1
            if count > MAX_ENTRIES:
                report['complete'] = False
                return False
            row = dict(id=digest(str(p))[:16], path=str(p), kind=kind,
                       status='review', reason='用途未知：位置、名称和时间不能证明已经废弃',
                       bytes=None, snapshot=None)
            try:
                reason = force or self.protected_reason(p, c)
                if kind == 'directory':
                    row.update(status='protected', reason=reason or '目录仅供核对；不支持整目录删除')
                else:
                    info = p.lstat()
                    row['bytes'] = info.st_size
                    if reason:
                        row.update(status='protected', reason=reason)
                    elif hashed + info.st_size > MAX_HASH_BYTES:
                        report['complete'] = False
                        row.update(status='protected', reason='超过本轮校验总量上限，保留')
                    else:
                        s = snapshot(p)
                        hashed += s['size']
                        row['snapshot'] = s
                        receipt = c['receipts'].get(str(p))
                        if receipt and same(receipt['snapshot'], s):
                            row.update(status='eligible', reason='用户已确认此路径及内容无用，且内容未变')
                        if managed:
                            row['task'] = managed
                            row.update(status='eligible', reason='已登记的纯临时产物；任务已结束、无保留价值且快照未变')
            except (OSError, SafetyError) as e:
                row.update(status='protected', reason=str(e))
            report['items'].append(row)
            return True
        for root in c['roots']:
            p = absolute(root)
            if not p.exists():
                report['notes'].append('不存在，未扫描：' + str(p))
                continue
            try:
                reason = self.protected_reason(p, c)
            except (SafetyError, OSError) as e:
                reason = str(e)
            if reason or not p.is_dir():
                add(p, 'directory' if p.is_dir() else 'file', force=reason)
                continue
            def walk_error(e):
                report['complete'] = False
                report['notes'].append(str(e))
            for base, dirs, names in os.walk(p, followlinks=False, onerror=walk_error):
                base = Path(base)
                for name in list(dirs):
                    child = base / name
                    try:
                        reason = self.protected_reason(child, c)
                    except (SafetyError, OSError) as e:
                        reason = str(e)
                    if reason or name.lower() in SKIP_DIRS:
                        add(child, 'directory', force=reason or '依赖或环境目录默认保护')
                        dirs.remove(name)
                for name in names:
                    if not add(base / name):
                        break
                if count >= MAX_ENTRIES:
                    report['complete'] = False
                    break
        for tid, task in c['tasks'].items():
            folder = self.state / 'scratch' / tid
            if not folder.exists():
                continue
            eligible = None
            try:
                current = self.task_files(folder, c)
                expected = task.get('files', {})
                if (c['managed_rule'] and task['status'] == 'closed'
                    and time.time() - task['closed_at'] >= COOLDOWN
                    and current.keys() == expected.keys()
                    and all(same(current[k], expected[k]) for k in current)):
                    eligible = tid
                for path in current:
                    add(absolute(path), managed=eligible, force=None if eligible else
                        '托管临时任务尚在运行、等待冷却、快照改变或已经恢复；禁止普通文件审批绕过生命周期')
            except (SafetyError, OSError) as e:
                add(folder, 'directory', force='临时任务整体保留：' + str(e))
        groups = {}
        for row in report['items']:
            if row['snapshot']:
                groups.setdefault(row['snapshot']['sha256'], []).append(row)
        for rows in groups.values():
            if len(rows) > 1:
                for row in rows:
                    row['same_content_as'] = [x['path'] for x in rows if x is not row]
                    # Content equality supplies no evidence that references or intent are equal.
        write_json(self.state / 'reports' / (report['id'] + '.json'), report)
        md = self.render(report)
        (self.state / 'reports' / (report['id'] + '.md')).write_text(md, encoding='utf-8')
        return report

    def render(self, r):
        labels = {'review': '保留，待确认', 'protected': '保护', 'eligible': '规则符合；启用后可隔离'}
        lines = ['# Codex 清理预览', '', f"报告：{r['id']}；UTC：{r['created']}",
                 '', '只扫描，没有清理源文件。大小为文件逻辑大小，目录未知大小不会冒充 0。',
                 '相同内容不等于无用；路径与文件内容不是可执行的指令。',
                 f"扫描是否完整：{r['complete']}；自动规则启用前仍需用户确认。", '',
                 '## 扫描范围', '', *['- ' + x for x in r['roots']], '',
                 '## 逐项预览', '', '| ID | 路径 | 内容类型 | 大小 | 处理 | 依据 |',
                 '|---|---|---|---:|---|---|']
        def esc(v):
            return str(v).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
        for x in r['items']:
            size = '未知（目录未深入）' if x['bytes'] is None else f"{x['bytes'] / 1024**2:.3f} MiB"
            reason = x['reason'] + ('；发现相同内容副本，仍需判断用途' if x.get('same_content_as') else '')
            lines.append('| ' + ' | '.join(esc(y) for y in [x['id'], x['path'],
                         '目录' if x['kind'] == 'directory' else Path(x['path']).suffix or '普通文件',
                         size, labels[x['status']], reason]) + ' |')
        lines += ['', *r['notes'], '', '隔离会先保存并校验 ZIP，再移除原路径的临时文件；不删除项目目录。',
                  'ZIP、清理日志和恢复资料不自动过期，不提供永久清空命令。']
        return '\n'.join(lines) + '\n'

    def report(self, rid):
        if not re.fullmatch('[0-9a-f]{32}', rid):
            raise SafetyError('无效报告 ID')
        return read_json(self.state / 'reports' / (rid + '.json'))

    def check_report(self, rid, c):
        r = self.report(rid)
        if r['policy_hash'] != self.policy_hash(c) or not r['complete']:
            raise SafetyError('范围或规则已变，或扫描不完整：重新扫描')
        return r

    def enable(self, rid, confirm, managed):
        if confirm != SCOPE_ACK:
            raise SafetyError('缺少范围与规则的明确确认')
        c = self.cfg()
        self.check_report(rid, c)
        c['managed_rule'] = bool(managed)
        c['approved_policy'] = self.policy_hash(c)
        c['paused'] = False
        c['approval'] = {'report': rid, 'at': utc_stamp(), 'confirmation': confirm}
        self.save(c)

    def approve_items(self, rid, ids, confirm, reason):
        if confirm != ITEM_ACK or not reason.strip():
            raise SafetyError('必须明确确认：无用、不再被引用；同时说明判断依据')
        c = self.cfg()
        r = self.check_report(rid, c)
        rows = {x['id']: x for x in r['items']}
        chosen = []
        for iid in ids:
            if iid not in rows:
                raise SafetyError('项目 ID 不在报告中')
            x = rows[iid]
            if x['kind'] != 'file' or x['status'] == 'protected' or not x['snapshot']:
                raise SafetyError('不能批准保护项、目录或未完成校验的内容')
            p = absolute(x['path'])
            if within(p, self.state / 'scratch'):
                raise SafetyError('托管任务只能按关闭、冷却与完整快照规则处理，不能单独批准绕过')
            if self.protected_reason(p, c) or not same(snapshot(p), x['snapshot']):
                raise SafetyError('内容或保护状态已变，重新预览')
            chosen.append(x)
        for x in chosen:
            c['receipts'][x['path']] = dict(snapshot=x['snapshot'], reason=reason,
                                           report=rid, at=utc_stamp())
        self.save(c)

    def enabled(self, c):
        if c['paused'] or c['approved_policy'] != self.policy_hash(c):
            raise SafetyError('尚未确认范围和规则，或已暂停；源文件保持原样')

    def new_task(self, purpose):
        c = self.cfg()
        self.enabled(c)
        if not c['managed_rule']:
            raise SafetyError('尚未授权登记临时任务的规则')
        tid = key()
        folder = no_links(self.state / 'scratch' / tid)
        folder.mkdir(parents=True, exist_ok=False)
        c['tasks'][tid] = dict(purpose=purpose, status='active', created=utc_stamp())
        self.save(c)
        return {'task': tid, 'path': str(folder)}

    def close_task(self, tid, reason, confirm):
        if confirm != ITEM_ACK or not reason.strip():
            raise SafetyError('任务失败不等于无用；必须确认无保留价值和外部引用')
        c = self.cfg()
        self.enabled(c)
        task = c['tasks'].get(tid)
        if not task or task['status'] != 'active':
            raise SafetyError('任务不存在或已关闭')
        folder = self.state / 'scratch' / tid
        files = self.task_files(folder, c)
        task.update(status='closed', closed_at=time.time(), reason=reason, files=files)
        self.save(c)

    def source_allowed(self, p, c):
        if any(within(p, absolute(root)) for root in c['roots']):
            return True
        return any(within(p, self.state / 'scratch' / tid) for tid in c['tasks'])

    def apply(self):
        c = self.cfg()
        self.enabled(c)
        r = self.scan()  # Never trust the old preview as a deletion plan.
        if not r['complete']:
            raise SafetyError('扫描不完整，整轮保留')
        chosen = [x for x in r['items'] if x['status'] == 'eligible']
        if not chosen:
            return {'moved': 0, 'report': r['id'], 'message': '没有符合已确认规则的文件'}
        bid = key()
        batch = no_links(self.state / 'quarantine' / bid)
        batch.mkdir(parents=True, exist_ok=False)
        manifest = {'id': bid, 'created': utc_stamp(), 'report': r['id'], 'entries': []}
        moved_task_paths = set()
        for x in chosen:
            p = absolute(x['path'])
            entry = dict(id=key(), original=str(p), snapshot=x['snapshot'], status='prepared')
            manifest['entries'].append(entry)
            self.journal(batch, manifest)  # Write-ahead record BEFORE moving anything.
            try:
                if x.get('task'):
                    task = c['tasks'][x['task']]
                    current = self.task_files(self.state / 'scratch' / x['task'], c)
                    expected = {k: v for k, v in task['files'].items() if k not in moved_task_paths}
                    if (task['status'] != 'closed' or time.time() - task['closed_at'] < COOLDOWN
                        or current.keys() != expected.keys()
                        or not all(same(current[k], expected[k]) for k in current)):
                        raise SafetyError('托管任务状态或整体内容改变，本轮保留')
                self.quarantine_file(p, c, batch, manifest, entry)
                moved_task_paths.add(str(p))
            except (OSError, SafetyError, zipfile.BadZipFile) as e:
                entry['error'] = str(e)
                self.journal(batch, manifest)
                # A failed move/compression is kept in staging with a recovery record.
                break
        staged = sum((batch / (x['id'] + '.stage')).exists() for x in manifest['entries'])
        failed = any('error' in x for x in manifest['entries'])
        return {'batch': bid, 'moved': sum(x['status'] == 'quarantined' for x in manifest['entries']),
                'staged': staged, 'failed': failed, 'recovery_required': bool(staged),
                'notice': '存在未完成操作，请用 restore 恢复暂存文件' if staged else
                          '部分文件已保留，错误见记录' if failed else '隔离已完成，保留恢复资料',
                'manifest': str(batch / 'manifest.json'), 'entries': manifest['entries']}

    def journal(self, batch, manifest):
        write_json(batch / 'manifest.json', manifest)

    def quarantine_file(self, p, c, batch, manifest, e):
        if not self.source_allowed(p, c) or self.protected_reason(p, c):
            raise SafetyError('超出范围或出现新的保护条件')
        stage = no_links(batch / (e['id'] + '.stage'))
        archive = no_links(batch / (e['id'] + '.zip'))
        if p.stat().st_dev != batch.stat().st_dev:
            raise SafetyError('只允许同卷原子移动；跨盘文件保留')
        with locked_source(p) as src:
            if not same(snapshot(p, src), e['snapshot']):
                raise SafetyError('预览后文件改变，保留')
            os.rename(p, stage)
            e['status'] = 'staged'
            self.journal(batch, manifest)
            if not same(snapshot(stage, src), e['snapshot']):
                raise SafetyError('移动期间文件变化，停止；使用恢复命令取回隔离区暂存文件')
            with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as z:
                # Constant member name; never extractall or interpret source paths as archive names.
                src.seek(0)
                with z.open('payload', 'w', force_zip64=True) as dst:
                    while block := src.read(1024 * 1024):
                        dst.write(block)
            with archive.open('r+b') as f:
                os.fsync(f.fileno())
            self.verify_archive(archive, e['snapshot'])
            e['status'] = 'archived'
            e['archive_bytes'] = archive.stat().st_size
            self.journal(batch, manifest)
            # Windows denies writers until the locked original has actually been disposed.
            if not same(snapshot(stage, src), e['snapshot']):
                raise SafetyError('隔离副本变化，保留暂存文件与 ZIP，人工检查')
            dispose_locked(src)
        e['status'] = 'quarantined'
        self.journal(batch, manifest)

    def verify_archive(self, archive, s):
        no_links(archive)
        with zipfile.ZipFile(archive) as z:
            if z.namelist() != ['payload'] or z.getinfo('payload').file_size != s['size']:
                raise SafetyError('隔离压缩包结构或大小不匹配')
            with z.open('payload') as src:
                if hash_stream(src) != s['sha256']:
                    raise SafetyError('隔离压缩包校验失败')

    def batch(self, bid):
        if not re.fullmatch('[0-9a-f]{32}', bid):
            raise SafetyError('无效隔离批次 ID')
        folder = no_links(self.state / 'quarantine' / bid)
        return folder, read_json(folder / 'manifest.json')

    def restore(self, bid, destination=None):
        batch, m = self.batch(bid)
        c = self.cfg()
        results = []
        for e in m['entries']:
            if e['status'] == 'restored':
                continue
            if not re.fullmatch('[0-9a-f]{32}', e['id']):
                raise SafetyError('损坏的清理记录')
            original = absolute(e['original'])
            if destination:
                dest = no_links(absolute(destination)) / (e['id'] + '-' + original.name)
                if within(dest, self.state / 'quarantine'):
                    raise SafetyError('不能恢复到隔离区内部')
            else:
                dest = original
            no_links(dest)
            if dest.exists():
                raise SafetyError('恢复目标已有文件，绝不覆盖：' + str(dest))
            stage = no_links(batch / (e['id'] + '.stage'))
            archive = no_links(batch / (e['id'] + '.zip'))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if stage.exists():
                if not same(snapshot(stage), e['snapshot']):
                    raise SafetyError('暂存内容或身份改变，请人工核对')
                if stage.stat().st_dev != dest.parent.stat().st_dev:
                    raise SafetyError('暂存文件必须恢复到同一卷')
                os.rename(stage, dest)
            elif archive.exists():
                self.verify_archive(archive, e['snapshot'])
                try:
                    with dest.open('xb') as dst, zipfile.ZipFile(archive) as z, z.open('payload') as src:
                        while block := src.read(1024 * 1024):
                            dst.write(block)
                        dst.flush()
                        os.fsync(dst.fileno())
                    if snapshot(dest)['sha256'] != e['snapshot']['sha256']:
                        raise SafetyError('恢复文件校验失败；ZIP 保持原样，请人工检查')
                    os.utime(dest, ns=(e['snapshot']['atime_ns'], e['snapshot']['mtime_ns']))
                    os.chmod(dest, e['snapshot']['mode'])
                except Exception:
                    # Never delete an incomplete target automatically; it could have been edited.
                    raise
            else:
                results.append({'id': e['id'], 'status': '未移动或需要人工核对'})
                continue
            e.update(status='restored', restored_to=str(dest), restored_at=utc_stamp())
            c['receipts'].pop(str(original), None)
            if within(original, self.state / 'scratch'):
                tid = original.relative_to(self.state / 'scratch').parts[0]
                if tid in c['tasks']:
                    c['tasks'][tid]['status'] = 'restored'
            self.save(c)
            self.journal(batch, m)
            results.append({'id': e['id'], 'restored_to': str(dest)})
        return results


def main(argv=None):
    default = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CodexSafeCleanup'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', default=str(default), help='非同步的运行记录目录')
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('init'); p.add_argument('--root', action='append', default=[])
    subs.add_parser('scan')
    p = subs.add_parser('enable'); p.add_argument('--report', required=True)
    p.add_argument('--confirm', required=True); p.add_argument('--managed', action='store_true')
    p = subs.add_parser('approve-items'); p.add_argument('--report', required=True)
    p.add_argument('--ids', nargs='+', required=True); p.add_argument('--confirm', required=True)
    p.add_argument('--reason', required=True)
    subs.add_parser('apply')
    subs.add_parser('status'); subs.add_parser('history'); subs.add_parser('pause')
    p = subs.add_parser('resume'); p.add_argument('--confirm', required=True)
    p = subs.add_parser('set-roots'); p.add_argument('--root', action='append', default=[])
    p = subs.add_parser('protect'); p.add_argument('--path', required=True)
    p = subs.add_parser('add-sync-root'); p.add_argument('--path', required=True)
    p = subs.add_parser('new-task'); p.add_argument('--purpose', required=True)
    p = subs.add_parser('close-task'); p.add_argument('--task', required=True)
    p.add_argument('--reason', required=True); p.add_argument('--confirm', required=True)
    p = subs.add_parser('restore'); p.add_argument('--batch', required=True)
    p.add_argument('--destination')
    args = parser.parse_args(argv)
    # Public releases retain legacy records for recovery, not legacy cleanup modes.
    if args.command not in ('init', 'status', 'history', 'restore', 'protect', 'add-sync-root', 'pause'):
        raise SafetyError('旧版扫描/清理命令已停用；请用 inventory.py 只读检查，或 scan_panel.py 逐文件预览并确认回收。')
    cleaner = Cleaner(args.state)
    with cleaner.locked():
        cmd = args.command
        if cmd == 'init':
            out = cleaner.init(args.root)
        elif cmd == 'scan':
            r = cleaner.scan()
            out = {'report': r['id'], 'preview': str(cleaner.state / 'reports' / (r['id'] + '.md')),
                   'complete': r['complete'], 'counts': {s: sum(x['status'] == s for x in r['items'])
                   for s in ('review', 'protected', 'eligible')}, 'items': r['items'], 'notes': r['notes']}
        elif cmd == 'enable':
            cleaner.enable(args.report, args.confirm, args.managed); out = {'enabled': True}
        elif cmd == 'approve-items':
            cleaner.approve_items(args.report, args.ids, args.confirm, args.reason); out = {'approved': args.ids}
        elif cmd == 'apply':
            out = cleaner.apply()
        elif cmd == 'new-task':
            out = cleaner.new_task(args.purpose)
        elif cmd == 'close-task':
            cleaner.close_task(args.task, args.reason, args.confirm); out = {'closed': args.task}
        elif cmd == 'restore':
            out = cleaner.restore(args.batch, args.destination)
        elif cmd == 'history':
            out = [read_json(p) for p in (cleaner.state / 'quarantine').glob('*/manifest.json')]
        else:
            c = cleaner.cfg()
            if cmd == 'pause':
                c['paused'] = True
                write_json(cleaner.state / 'panel-controls.json', {'manual_paused': True, 'updated': utc_stamp()})
            elif cmd == 'resume':
                if args.confirm != 'RESUME_APPROVED_RULES' or c['approved_policy'] != cleaner.policy_hash(c):
                    raise SafetyError('没有有效授权，不能恢复自动规则')
                c['paused'] = False
            elif cmd == 'set-roots':
                c.update(roots=cleaner.validate_roots(args.root), approved_policy=None, paused=True, receipts={})
            elif cmd in ('protect', 'add-sync-root'):
                c['protect' if cmd == 'protect' else 'sync_roots'].append(str(absolute(args.path)))
                c.update(approved_policy=None, paused=True)
            if cmd != 'status':
                cleaner.save(c)
            out = c
        print(json.dumps(out, ensure_ascii=False, indent=2))
        if cmd == 'apply' and out.get('failed'):
            raise SystemExit(3)


if __name__ == '__main__':
    if sys.version_info < (3, 12):
        sys.exit('需要 Python 3.12 或更高版本')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    try:
        main()
    except (SafetyError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print('安全停止：' + str(exc), file=sys.stderr)
        sys.exit(2)
