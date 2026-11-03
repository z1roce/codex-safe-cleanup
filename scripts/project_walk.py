"""A resumable, scope-checked metadata walk. Cursors never authorize cleanup."""
import os
from pathlib import Path

from safe_cleanup import SafetyError,absolute,no_links,within


def identity(path):
    info=no_links(path).stat()
    return {'ino':info.st_ino,'dev':info.st_dev,'mtime_ns':info.st_mtime_ns}


class ProjectWalk:
    def __init__(self,root,blocked,flags,saved=None):
        self.root=no_links(absolute(root));self.blocked=blocked;self.flags=flags
        current=identity(self.root)
        if saved and saved['identity']!=current:raise SafetyError('项目根目录在续查前发生变化，请重新检查')
        self.root_identity=current
        self.stack=list(saved['stack']) if saved else [{'path':'.','after':None}]
        self.current=None

    def walk(self,onerror):
        while self.stack:
            frame=self.stack.pop();relative=Path(frame['path'])
            if relative.is_absolute() or '..' in relative.parts or any(part.lower() in self.blocked for part in relative.parts):
                raise SafetyError('续查路径超出允许的目录范围')
            base=no_links(absolute(self.root/relative))
            if not within(base,self.root):raise SafetyError('续查路径越界')
            try:
                stamp=identity(base)
                if frame.get('stamp') and frame['stamp']!=stamp:
                    raise SafetyError('上次未完成的目录已变化，请重新检查')
                if getattr(base.stat(),'st_file_attributes',0)&self.flags:raise SafetyError('续查目录是云端或链接内容，保留')
                with os.scandir(base) as iterator:
                    entries=list(iterator)
                directories={entry.name for entry in entries if entry.is_dir(follow_symlinks=False) or entry.is_symlink() or getattr(entry.stat(follow_symlinks=False),'st_file_attributes',0)&0x400}
                dirs=sorted(directories)
                dirs=[name for name in dirs if frame.get('after_dir') is None or name>frame['after_dir']]
                names=sorted(entry.name for entry in entries if entry.name not in directories)
                names=[name for name in names if frame.get('after') is None or name>frame['after']]
            except OSError as error:
                onerror(error);continue
            self.current={'path':str(relative),'after':frame.get('after'),'stamp':stamp,
                          'after_dir':frame.get('after_dir'),'safe_dirs':list(frame.get('safe_dirs',[]))}
            yield str(base),dirs,names
            self.stack.extend({'path':str(relative/name),'after':None} for name in reversed(self.current['safe_dirs']))
            self.current=None

    def advance(self,name):self.current['after']=name

    def directory(self,name,allowed):
        self.current['after_dir']=name
        if allowed:self.current['safe_dirs'].append(name)

    def checkpoint(self):
        pending=list(self.stack)
        if self.current:
            base=self.root/self.current['path']
            if identity(base)!=self.current['stamp']:raise SafetyError('目录在扫描期间变化，请重新检查')
            pending.append(dict(self.current))
        return {'identity':self.root_identity,'stack':pending}
