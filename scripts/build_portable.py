"""Build an offline Windows x64 bundle from a pinned official Python archive."""
import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import zipfile

from install_skill import distribution
from safe_cleanup import SafetyError,absolute,no_links,within,hash_stream

PYTHON_VERSION='3.14.10'
PYTHON_URL='https://www.python.org/ftp/python/3.14.10/python-3.14.10-embed-amd64.zip'
PYTHON_SHA256='90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'


def build(archive,destination):
    archive=no_links(absolute(archive));destination=no_links(absolute(destination))
    source=Path(__file__).resolve().parent.parent
    if destination.exists() or Path(str(destination)+'.zip').exists():raise SafetyError('便携包目标已存在，保留原文件')
    if within(source,destination) or within(destination,source):raise SafetyError('便携输出目录必须与源码分离')
    with archive.open('rb') as stream:
        if hash_stream(stream)!=PYTHON_SHA256:raise SafetyError('官方Python包SHA256不符，停止构建')
    with zipfile.ZipFile(archive) as runtime:
        members=runtime.infolist()
        if len(members)>200 or sum(item.file_size for item in members)>100*1024**2:raise SafetyError('运行库归档结构超出预期')
        for item in members:
            path=Path(item.filename)
            if path.is_absolute() or '..' in path.parts or ':' in item.filename or stat.S_ISLNK(item.external_attr>>16):raise SafetyError('运行库包包含不安全路径')
        destination.mkdir(parents=True)
        runtime.extractall(destination/'runtime')
    for relative,path in distribution(source).items():
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
    (destination/'runtime/python314._pth').write_text('python314.zip\n.\n..\\scripts\n',encoding='ascii')
    (destination/'Start.cmd').write_text('@echo off\r\nsetlocal\r\nset "CODEX_CLEANUP_PYTHON=%~dp0runtime\\python.exe"\r\ncall "%~dp0scripts\\open-scan-panel.cmd" %*\r\nexit /b %errorlevel%\r\n',encoding='ascii',newline='')
    (destination/'Install-Skill.cmd').write_text('@echo off\r\nsetlocal\r\nset "CODEX_CLEANUP_PYTHON=%~dp0runtime\\python.exe"\r\n"%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\\install.ps1" %*\r\nexit /b %errorlevel%\r\n',encoding='ascii',newline='')
    (destination/'THIRD-PARTY-NOTICES.txt').write_text(
        f'Bundled CPython {PYTHON_VERSION}, Windows x64 embeddable distribution.\n'
        f'Official source {PYTHON_URL}\nSHA256 {PYTHON_SHA256}\n'
        'Python license and bundled component notices are retained in runtime/LICENSE.txt.\n'
        'Codex Safe Cleanup is distributed under the root LICENSE.\n',encoding='utf-8')
    (destination/'portable-build.json').write_text(json.dumps({'python':PYTHON_VERSION,'source':PYTHON_URL,
        'sha256':PYTHON_SHA256,'architecture':'Windows x64','app_revision':os.environ.get('GITHUB_SHA')},indent=2)+'\n',encoding='utf-8')
    output=Path(shutil.make_archive(str(destination),'zip',destination.parent,destination.name))
    with output.open('rb') as stream:checksum=hash_stream(stream)
    return {'folder':str(destination),'zip':str(output),'bytes':output.stat().st_size,'sha256':checksum}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-zip',required=True)
    parser.add_argument('--destination',required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.python_zip,args.destination),ensure_ascii=False))
