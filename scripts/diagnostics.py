"""Startup capabilities, without scanning projects or moving user files."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from safe_cleanup import Cleaner, SafetyError, key, no_links
import inventory


def diagnose(state):
    checks=[{'feature':'Python','available':sys.version_info>=(3,12),
             'detail':sys.executable+' '+sys.version.split()[0]}]
    try:
        cleaner=Cleaner(state)
        with cleaner.locked():
            probe=no_links(cleaner.state/('diagnostic-'+key()))
            with probe.open('xb') as stream:
                stream.write(b'diagnostic');stream.flush();os.fsync(stream.fileno())
            probe.unlink()
        checks.append({'feature':'状态目录','available':True,'detail':str(cleaner.state)})
    except (OSError,SafetyError) as error:
        checks.append({'feature':'状态目录','available':False,'detail':str(error)})
    git=shutil.which('git')
    try:
        result=subprocess.run([git,'--version'],capture_output=True,text=True,timeout=5) if git else None
        available=result is not None and result.returncode==0
        detail=result.stdout.strip() if available else 'Git 不可用，Git 项目中状态未知的文件继续保护'
    except (OSError,subprocess.TimeoutExpired):
        available=False;detail='Git 无法运行，相关项目文件继续保护'
    checks.append({'feature':'Git','available':available,'detail':detail})
    try:
        import windows_recycle
        # Load/compile the shipped bridge and instantiate the Shell COM object only.
        result=windows_recycle.diagnose()
        checks.append({'feature':'Windows 回收站桥接','available':result,'detail':
                       '桥接加载成功，各卷回收能力在执行时检查' if result else '回收功能受限，仍可只读检查'})
    except (OSError,ValueError,subprocess.SubprocessError,SafetyError) as error:
        checks.append({'feature':'Windows 回收站桥接','available':False,'detail':str(error)})
    try:
        ctx=inventory.context(Path(state))
        checks.append({'feature':'项目索引','available':True,'detail':
            f"登记项目 {len(ctx.get('projects',[]))}，聊天元数据 {len(ctx.get('threads',[]))}，索引时间 {ctx.get('captured_at','未记录')}；本机发现不保证覆盖完整历史"})
    except (OSError,ValueError,SafetyError,TypeError) as error:
        checks.append({'feature':'项目索引','available':False,'detail':str(error)})
    return checks
