#!/usr/bin/env python3
"""Read-only Codex-scoped shortcut; independent of legacy cleanup authorization."""
import os
from pathlib import Path
import sys
sys.dont_write_bytecode = True
from inventory import run_inventory
from safe_cleanup import Cleaner


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    state=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'CodexSafeCleanup'
    cleaner=Cleaner(state)
    with cleaner.locked():report=run_inventory(state)
    print('Codex 专用只读检查')
    print('范围：'+'、'.join(x['path'] for x in report['coverage']))
    print('已统计 {} 个文件，耗时 {:.3f} 秒。'.format(report['file_count'],report['elapsed_seconds']))
    print('保护目录未统计；大小不是可释放空间。')
    print('关联项目 {} 个，历史聊天 {} 个，项目元数据文件 {} 个。'.format(report['project_count'],report['thread_count'],report['project_file_count']))
    print('待核对文件 {} 项；没有取得清理授权，没有移动或删除文件。'.format(len(report['candidates'])))
    print('报告：'+report['preview'])


if __name__=='__main__':main()
