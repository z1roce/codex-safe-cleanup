"""Transactional local installation. State and recovery records are never migrated."""
import argparse
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import shutil

from safe_cleanup import SafetyError, absolute, key, no_links, read_json, write_json, within


def distribution(source):
    paths=[source/name for name in ('LICENSE','README.md','SKILL.md','CONTRIBUTING.md','SECURITY.md')]
    for folder in ('scripts','agents','assets','references'):
        paths.extend(p for p in (source/folder).rglob('*') if p.is_file() and
                     '__pycache__' not in p.parts and p.suffix!='.pyc')
    return {str(p.relative_to(source)).replace('\\','/'):p for p in paths}


def file_hash(path):
    no_links(path)
    return hashlib.sha256(path.read_bytes().replace(b'\r\n',b'\n')).hexdigest()


@contextlib.contextmanager
def installation_lock(path):
    import msvcrt
    with path.open('a+b') as stream:
        stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
        try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError as error:raise SafetyError('另一个安装操作正在进行') from error
        try:yield
        finally:
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


def recover_install(target):
    target=no_links(absolute(target))
    journal=no_links(target.with_name(target.name+'.install-transaction.json'))
    with installation_lock(no_links(target.with_name(target.name+'.install-lock'))):
        record=read_json(journal)
        if record.get('target')!=str(target):raise SafetyError('安装事务目标不一致')
        backup=no_links(absolute(record['backup']))
        if backup.parent!=target.parent or not backup.name.startswith(target.name+'.install-backup-'):
            raise SafetyError('安装备份路径无效')
        if not target.exists() and backup.exists():os.rename(backup,target)
        if target.exists() and not target.is_dir():raise SafetyError('安装目标已被替换，保留记录')
        journal.unlink()
        return {'recovered':str(target),'retained_transaction':record}


def install(source,target,upgrade=False):
    source=no_links(absolute(source));target=no_links(absolute(target))
    if within(source,target) or within(target,source) or target==Path(target.anchor):
        raise SafetyError('安装源和目标不能重叠，也不能指向整盘')
    files=distribution(source)
    for path in files.values():
        if not no_links(path).is_file():raise SafetyError('分发文件缺失：'+str(path))
    target.parent.mkdir(parents=True,exist_ok=True)
    lock=no_links(target.with_name(target.name+'.install-lock'))
    # The transaction log and backup are intentionally retained on interruption.
    journal=no_links(target.with_name(target.name+'.install-transaction.json'))
    if journal.exists():
        raise SafetyError('存在未完成安装，请先按事务记录恢复或核对：'+str(journal))
    with installation_lock(lock):
        if target.exists() and not upgrade:raise SafetyError('已有安装，请使用 -Upgrade')
        if target.exists():
            identity=no_links(target/'SKILL.md')
            if not identity.is_file() or identity.stat().st_size>256*1024 or not re.search(r'^name:\s*codex-safe-cleanup\s*$',identity.read_text(encoding='utf-8-sig'),re.M):
                raise SafetyError('目标无法识别为已有 codex-safe-cleanup 安装，保留原目录')
            manifest=target/'installation-manifest.json'
            previous=read_json(manifest)['files'] if manifest.exists() else read_json(source/'scripts/legacy-install-hashes.json')
            modified=[name for name,expected in previous.items() if (target/name).exists() and file_hash(target/name)!=expected]
            collisions=[name for name in files if (target/name).exists() and name not in previous]
            if modified or collisions:
                raise SafetyError('检测到用户修改或未知同名文件，保留安装：'+', '.join(modified+collisions))
        stage=target.with_name(target.name+'.install-stage-'+key())
        backup=target.with_name(target.name+'.install-backup-'+key())
        write_json(journal,{'target':str(target),'stage':str(stage),'backup':str(backup),'phase':'preparing'})
        if target.exists():
            # Validate every existing path before copying, preserving unknown user files.
            for base,dirs,names in os.walk(target,followlinks=False):
                for name in dirs+names:no_links(Path(base)/name)
            shutil.copytree(target,stage)
        else:stage.mkdir()
        for name,path in files.items():
            dest=stage/name;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,dest)
        write_json(stage/'installation-manifest.json',{'version':1,'files':{name:file_hash(path) for name,path in files.items()}})
        write_json(journal,{'target':str(target),'stage':str(stage),'backup':str(backup),'phase':'switching'})
        try:
            if target.exists():os.rename(target,backup)
            os.rename(stage,target)
        except BaseException:
            if backup.exists() and not target.exists():os.rename(backup,target)
            raise
        write_json(journal,{'target':str(target),'backup':str(backup),'phase':'complete'})
        journal.unlink()
        return {'installed':str(target),'previous_installation':str(backup) if backup.exists() else None}



def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True)
    parser.add_argument('--upgrade',action='store_true')
    parser.add_argument('--recover',action='store_true')
    args=parser.parse_args()
    result=recover_install(args.destination) if args.recover else install(Path(__file__).resolve().parent.parent,args.destination,args.upgrade)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
