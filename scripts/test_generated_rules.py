"""Synthetic evidence and false-positive protection, no cleanup operations."""
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import inventory
import project_inventory
from safe_cleanup import Cleaner


class GeneratedRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='生成证据 rules ',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.state=self.root/'state';Cleaner(self.state).init([])

    def write(self,path,data):
        path=self.root/path;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(data),encoding='utf-8');return path

    def discover(self,file):
        return project_inventory.discover_generated({'rule_inputs':[(str(file),str(self.root))]})

    def protected(self,rows):
        for row in rows:
            with patch.object(inventory,'git_tracked_status',return_value=True):
                self.assertFalse(inventory.inspect_artifact(row,state=self.state)['selectable'])
            with patch.object(inventory,'git_tracked_status',return_value=False):
                self.assertFalse(inventory.inspect_artifact(dict(row,keep=True),state=self.state)['selectable'])

    def test_typescript_requires_config_version_structure_and_source(self):
        source=self.root/'main.ts';source.write_text('export const value=1;')
        config=self.write('tsconfig.json',{'compilerOptions':{'incremental':True}})
        cache=self.write('tsconfig.tsbuildinfo',{'version':'5.9.3','fileNames':['./main.ts'],'fileInfos':['fixture-version']})
        rows=self.discover(config);self.assertEqual([row['path'] for row in rows],[str(cache)]);self.protected(rows)
        self.write('tsconfig.json',{'compilerOptions':{}});self.assertEqual(self.discover(config),[])
        self.write('tsconfig.json',{'compilerOptions':{'incremental':True}})
        source.unlink();self.assertEqual(self.discover(config),[])

    def test_mypy_requires_matching_metadata_ast_and_source(self):
        source=self.root/'main.py';source.write_text('value=1\n');stamp=source.stat()
        meta=self.write('.mypy_cache/3.12/main.meta.json',{'id':'main','path':'main.py','version_id':'1.18.2',
                       'mtime':int(stamp.st_mtime),'size':stamp.st_size,'dependencies':[]})
        data=self.write('.mypy_cache/3.12/main.data.json',{'.class':'MypyFile','_fullname':'main','path':'main.py'})
        rows=self.discover(meta);self.assertEqual(len(rows),2);self.protected(rows)
        self.write('.mypy_cache/3.12/main.data.json',{'.class':'MypyFile','_fullname':'other','path':'main.py'})
        self.assertEqual(self.discover(meta),[])
        data.write_text('{}');self.assertEqual(self.discover(meta),[])

    def test_cpp_requires_exact_compile_input_output_and_object_format(self):
        source=self.root/'main.c';source.write_text('int value(void){return 1;}')
        obj=self.root/'main.obj';obj.write_bytes(struct.pack('<HHIIIIHH',0x8664,1,0,0,0,0,0,0))
        entry={'directory':str(self.root),'file':'main.c','arguments':['gcc','-c','main.c','-o','main.obj']}
        database=self.write('compile_commands.json',[entry])
        rows=self.discover(database);self.assertEqual(len(rows),1);self.protected(rows)
        obj.write_bytes(b'original user document');self.assertEqual(self.discover(database),[])
        source.unlink();self.assertEqual(self.discover(database),[])

    def test_real_gcc_object_and_compilation_database(self):
        gcc=os.environ.get('CLEANUP_GCC') or shutil.which('gcc')
        if not gcc:self.skipTest('Optional installed GCC')
        source=self.root/'main.c';source.write_text('int value(void){return 1;}')
        result=subprocess.run([gcc,'-c','main.c','-o','main.obj'],cwd=self.root,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        database=self.write('compile_commands.json',[{'directory':str(self.root),'file':'main.c',
                            'arguments':[gcc,'-c','main.c','-o','main.obj']}])
        self.assertEqual(len(self.discover(database)),1)

    def test_gradle_class_requires_build_file_and_matching_source(self):
        source=self.root/'src/main/java/demo/Hello.java';source.parent.mkdir(parents=True);source.write_text('package demo; class Hello {}')
        build=self.root/'build.gradle';build.write_text("plugins { id 'java' }")
        obj=self.root/'build/classes/java/main/demo/Hello.class';obj.parent.mkdir(parents=True)
        obj.write_bytes(b'\xca\xfe\xba\xbe'+struct.pack('>HHH',0,61,2)+b'fixture')
        result={'java_paths':[str(obj)]}
        rows=project_inventory.discover_generated(result);self.assertEqual(len(rows),1);self.protected(rows)
        build.unlink();self.assertEqual(project_inventory.discover_generated(result),[])


if __name__=='__main__':unittest.main()
