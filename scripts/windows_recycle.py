"""Windows native Recycle Bin bridge. No permanent-delete fallback."""
import json
import os
from pathlib import Path
import subprocess

from safe_cleanup import SafetyError, absolute, no_links

POWERSHELL = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
Add-Type -LiteralPath $env:CODEX_RECYCLE_CS
$request=[Console]::In.ReadToEnd() | ConvertFrom-Json
$result=[CodexSafeRecycle.Operation]::Run([string]$request.mode,[string]$request.path,[string]$request.destination,[string]$request.expected_id)
$result | ConvertTo-Json -Compress
'''


def native_operation(mode, path, expected_id, destination=''):
    if os.name != 'nt' or mode not in ('recycle', 'restore'):
        raise SafetyError('此入口仅使用 Windows 原生回收站')
    shell = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native/powershell/pwsh.exe'
    if not shell.is_file():
        shell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    request = {'mode':mode,'path':str(path),'destination':str(destination),'expected_id':str(expected_id)}
    result = subprocess.run([str(shell),'-STA','-NoProfile','-NonInteractive','-Command',POWERSHELL],
        input=json.dumps(request),text=True,encoding='utf-8',capture_output=True,timeout=180,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
        env=dict(os.environ,CODEX_RECYCLE_CS=str(Path(__file__).with_name('RecycleBridge.cs'))))
    if result.returncode:
        raise SafetyError('Windows 回收站操作未完成；不尝试永久删除。' + result.stderr[-1200:])
    try:
        data = json.loads(result.stdout)
    except ValueError as e:
        raise SafetyError('无法确认回收站结果，请保留现状并核对 Windows 回收站') from e
    return data


def send(path, expected_id):
    path=no_links(absolute(path))
    data=native_operation('recycle',path,expected_id)
    if data.get('ok'):
        recycled=no_links(absolute(data['recycle_path']))
        if '\\$Recycle.Bin\\' not in str(recycled) or not recycled.exists() or path.exists():
            raise SafetyError('回收站结果需要人工核对，未继续处理其他项目')
    return data


def restore(record, destination, expected_id):
    destination=no_links(absolute(destination))
    if destination.exists():
        raise SafetyError('恢复位置已有内容，绝不覆盖')
    recycled=no_links(absolute(record['recycle_path']))
    if '\\$Recycle.Bin\\' not in str(recycled):
        raise SafetyError('不是本次记录中的回收站内容')
    destination.parent.mkdir(parents=True,exist_ok=True)
    return native_operation('restore',record.get('recycle_parsing') or recycled,expected_id,destination)
