"""Additional static evidence adapters. Never execute project or build commands."""
import ctypes
import json
import os
from pathlib import Path
import re

from safe_cleanup import SafetyError, absolute, no_links, within


def project_path(value,base,root):
    if not isinstance(value,str) or not value:raise SafetyError('生成路径无效')
    path=no_links(absolute(os.path.abspath(base/value)))
    if not within(path,root):raise SafetyError('生成证据指向项目范围外')
    return path


def typescript(config,root,read,add):
    data=json.loads(read(config).decode('utf-8-sig'))
    if not isinstance(data,dict) or data.get('extends'):return
    options=data.get('compilerOptions',{})
    if not isinstance(options,dict) or not (options.get('incremental') is True or options.get('composite') is True):return
    output=options.get('tsBuildInfoFile')
    if output is None:
        if options.get('outDir') or options.get('outFile'):return
        output=config.stem+'.tsbuildinfo'
    path=project_path(output,config.parent,root)
    info=json.loads(read(path).decode('utf-8-sig'))
    if not isinstance(info,dict) or not re.fullmatch(r'5\.9\.\d+',str(info.get('version',''))):return
    names=info.get('fileNames');versions=info.get('fileInfos')
    if not isinstance(names,list) or not isinstance(versions,list) or len(names)!=len(versions) or not names:return
    if not all(isinstance(name,str) for name in names):return
    sources=[]
    for name in names:
        # Compiler libraries are not evidence for this project's own source.
        if 'node_modules' in Path(name).parts:continue
        source=project_path(name,path.parent,root)
        if source.suffix in ('.ts','.tsx') and source.is_file():sources.append(source)
    if sources:add(path,root,'TypeScript 增量编译状态；删除后需完整重新编译。',
                   'tsconfig 启用增量编译，精确输出位置、5.9 文件版本结构和项目源码已核对','bytecode_cache')


def mypy(meta,root,read,add):
    value=json.loads(read(meta).decode('utf-8'))
    if not isinstance(value,dict) or not re.fullmatch(r'1\.18\.\d+',str(value.get('version_id',''))):return
    if not isinstance(value.get('id'),str) or not isinstance(value.get('dependencies'),list):return
    source=project_path(value.get('path'),root,root)
    if source.suffix not in ('.py','.pyi') or not source.is_file():return
    stat=source.stat()
    if value.get('size')!=stat.st_size or int(value.get('mtime',-1))!=int(stat.st_mtime):return
    data=meta.with_name(meta.name.removesuffix('.meta.json')+'.data.json')
    tree=json.loads(read(data).decode('utf-8'))
    if not isinstance(tree,dict) or tree.get('.class')!='MypyFile' or tree.get('_fullname')!=value['id']:return
    if project_path(tree.get('path'),root,root)!=source:return
    for path in (meta,data):
        add(path,root,'mypy 类型检查增量缓存；对应 Python 源码仍存在，可重新检查生成。',
            'mypy 1.18 元数据、MypyFile 缓存身份及源码时间/大小已核对','bytecode_cache')


def windows_arguments(command):
    if not isinstance(command,str) or os.name!='nt':return []
    shell=ctypes.WinDLL('shell32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    shell.CommandLineToArgvW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    count=ctypes.c_int();args=shell.CommandLineToArgvW(command,ctypes.byref(count))
    if not args:raise SafetyError('编译命令无法只读解析')
    try:return [args[index] for index in range(count.value)]
    finally:kernel.LocalFree(args)


def cpp_commands(database,root,read,add):
    entries=json.loads(read(database).decode('utf-8-sig'))
    if not isinstance(entries,list):return
    for entry in entries[:500]:
        try:
            if not isinstance(entry,dict):continue
            directory=project_path(entry.get('directory'),database.parent,root)
            source=project_path(entry.get('file'),directory,root)
            if source.suffix.lower() not in ('.c','.cc','.cpp','.cxx') or not source.is_file():continue
            args=entry.get('arguments') or windows_arguments(entry.get('command'))
            if not isinstance(args,list) or not args or not all(isinstance(arg,str) for arg in args):continue
            sources=[project_path(arg,directory,root) for arg in args[1:] if Path(arg).suffix.lower() in ('.c','.cc','.cpp','.cxx')]
            if source not in sources:continue
            compiler=Path(args[0]).stem.lower()
            if compiler not in ('cl','gcc','g++','clang','clang++') and not compiler.endswith(('-gcc','-g++')):continue
            if '-c' not in args and '/c' not in [arg.lower() for arg in args]:continue
            output=entry.get('output')
            if not output:
                for index,arg in enumerate(args[:-1]):
                    if arg in ('-o','/Fo'):output=args[index+1]
                    elif arg.startswith('/Fo') and len(arg)>3:output=arg[3:]
            path=project_path(output,directory,root)
            if path.suffix.lower() not in ('.o','.obj'):continue
            # Header reads are bounded too. Reading only known object outputs is required.
            no_links(path)
            header=read(path,header_only=True)
            coff=len(header)>=20 and header[:2] in (b'\x64\x86',b'\x4c\x01') and int.from_bytes(header[2:4],'little')>0
            elf=len(header)>=20 and header[:4]==b'\x7fELF' and header[4] in (1,2) and header[5] in (1,2) and int.from_bytes(header[16:18],'little' if header[5]==1 else 'big')==1
            if coff or elf:add(path,root,'C/C++ 仅编译生成的目标文件；仍需源码、头文件和工具链才能重建。',
                              '编译数据库明确列出输入与输出，源码存在，目标文件 COFF/ELF 格式已核对','bytecode_cache')
        except (OSError,SafetyError,ValueError,TypeError):continue


def discover(inputs,read,add,cancel=None):
    for name,scope in inputs[:500]:
        if cancel is not None and cancel.is_set():raise SafetyError('生成证据检查已取消')
        path=Path(name);root=Path(scope)
        try:
            if path.name=='compile_commands.json':cpp_commands(path,root,read,add)
            elif path.name.startswith('tsconfig') and path.suffix=='.json':typescript(path,root,read,add)
            elif path.name.endswith('.meta.json') and '.mypy_cache' in path.parts:mypy(path,root,read,add)
        except (OSError,SafetyError,ValueError,TypeError):continue
