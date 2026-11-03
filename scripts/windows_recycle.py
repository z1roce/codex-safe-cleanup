"""Windows native Recycle Bin bridge. No permanent-delete fallback."""
import json
import os
from pathlib import Path
import subprocess
import struct

from safe_cleanup import SafetyError, absolute, no_links

POWERSHELL = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Add-Type -LiteralPath $env:CODEX_RECYCLE_CS
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
$result=[CodexSafeRecycle.Operation]::Run([string]$request.mode,[string]$request.path,[string]$request.destination,[string]$request.expected_id)
$result | ConvertTo-Json -Compress
'''


def diagnose():
    if os.name!='nt':return False
    shell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    code="Add-Type -LiteralPath $env:CODEX_RECYCLE_CS; $o=New-Object -ComObject Shell.Application; [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($o)"
    result=subprocess.run([str(shell),'-STA','-NoProfile','-NonInteractive','-Command',code],
        capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
        env=dict(os.environ,CODEX_RECYCLE_CS=str(Path(__file__).with_name('RecycleBridge.cs'))))
    return result.returncode==0


def native_operation(mode, path, expected_id, destination='', receipt=None):
    if os.name != 'nt' or mode not in ('recycle', 'restore'):
        raise SafetyError('此入口仅使用 Windows 原生回收站')
    shell = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
    if not shell.is_file():
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    request = {'mode':mode,'path':str(path),'destination':str(destination),'expected_id':str(expected_id)}
    result = subprocess.run([str(shell),'-STA','-NoProfile','-NonInteractive','-Command',POWERSHELL],
        input=json.dumps(request),text=True,encoding='utf-8',capture_output=True,timeout=180,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
        env=dict(os.environ,CODEX_RECYCLE_CS=str(Path(__file__).with_name('RecycleBridge.cs')),
                 CODEX_RECYCLE_RECEIPT=str(no_links(absolute(receipt))) if receipt else ''))
    if result.returncode:
        raise SafetyError('Windows 回收站操作未完成；不尝试永久删除。' + result.stderr[-1200:])
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        raise SafetyError('无法确认回收站结果，请保留现状并核对 Windows 回收站') from e
    return data


def read_receipt(path):
    lines=no_links(absolute(path)).read_text(encoding='utf-8-sig').splitlines()
    if len(lines)!=2 or '\\$recycle.bin\\' not in lines[0].lower():
        raise SafetyError('原生回收凭据损坏，请保留记录')
    return {'ok':True,'recycle_path':lines[0],'recycle_parsing':lines[1]}


def locate_recycled(stage):
    """Reconcile a Shell/process interruption using the exact unique staging path.

    Only read this volume's Recycle Bin metadata. The caller must validate the
    returned file identity and full content before any restore operation.
    """
    stage=absolute(stage)
    recycle=Path(stage.anchor)/'$Recycle.Bin'
    count=0
    for user in recycle.iterdir():
        no_links(user)
        if not user.is_dir():continue
        try:
            for info in user.glob('$I*'):
                count+=1
                if count>10000:raise SafetyError('回收站查找达到预算，请人工核对恢复记录')
                no_links(info)
                with info.open('rb') as stream:data=stream.read(65536)
                if len(data)<28:continue
                version=struct.unpack_from('<Q',data)[0]
                if version==1:original=data[24:544].decode('utf-16-le').rstrip('\0')
                elif version==2:
                    length=struct.unpack_from('<I',data,24)[0]
                    if length>32000 or len(data)<28+length*2:continue
                    original=data[28:28+length*2].decode('utf-16-le').rstrip('\0')
                else:continue
                if os.path.normcase(original)==os.path.normcase(str(stage)):
                    source=info.with_name('$R'+info.name[2:])
                    return {'ok':True,'recycle_path':str(source)}
        except PermissionError:
            continue  # Other users' bins are deliberately inaccessible.
    raise SafetyError('未找到本次精确暂存路径的回收内容；卷可能离线或回收站已清空')


def send(path, expected_id):
    path=no_links(absolute(path))
    data=native_operation('recycle',path,expected_id)
    if data.get('ok'):
        recycled=no_links(absolute(data['recycle_path']))
        if '\\$recycle.bin\\' not in str(recycled).lower() or not recycled.exists() or path.exists():
            raise SafetyError('回收站结果需要人工核对，未继续处理其他项目')
    return data


def restore(record, destination, expected_id):
    destination=no_links(absolute(destination))
    if destination.exists():
        raise SafetyError('恢复位置已有内容，绝不覆盖')
    recycled=no_links(absolute(record['recycle_path']))
    if '\\$recycle.bin\\' not in str(recycled).lower():
        raise SafetyError('不是本次记录中的回收站内容')
    destination.parent.mkdir(parents=True,exist_ok=True)
    return native_operation('restore',record.get('recycle_parsing') or recycled,expected_id,destination)
