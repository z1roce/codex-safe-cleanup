"""Read-only portability fixtures, never invoke a cleanup operation."""
import os
from pathlib import Path
import struct
import subprocess
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

import inventory
import project_discovery as discovery
import project_inventory as projects
from safe_cleanup import Cleaner,SafetyError,write_json


class DiscoveryRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='中文 project rules ',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name);self.codex=self.base/'codex';self.codex.mkdir()
        self.state=self.base/'state';Cleaner(self.state).init([])
        self.project=self.base/'项目 with spaces';self.project.mkdir()
        self.scope=patch.object(inventory,'roots',return_value=[self.codex]);self.scope.start();self.addCleanup(self.scope.stop)

    def test_known_codex_shapes_missing_fields_and_corrupt_index(self):
        file=self.codex/'.codex-global-state.json'
        write_json(file,{'local-projects':{'id':{'id':'fixture','name':'fixture','rootPaths':[str(self.project)]}},
                         'thread-workspace-root-hints':{'thread':str(self.project)},'thread-projectless-output-directories':None})
        scopes,omitted=discovery.discover({},[self.codex])
        project=next(row for row in scopes if row['path']==str(self.project))
        self.assertIsNotNone(project['index_time']);self.assertEqual(len(project['sources']),2)
        file.write_text('{broken',encoding='utf-8')
        scopes,omitted=discovery.discover({},[self.codex]);self.assertTrue(omitted)
        write_json(self.state/'inventory-context.json',{'projects':None,'threads':[{},None],'preserve_paths':'unknown'})
        report=inventory.run_inventory(self.state)
        self.assertTrue(report['project_scope_omissions'])
        self.assertEqual(report['candidates'],[])

    def test_manual_registration_is_only_readonly_scope_and_rejects_broad_roots(self):
        unknown=self.project/'unknown.log';unknown.write_bytes(b'original unknown file')
        discovery.register(self.state,str(self.project),[self.codex])
        report=inventory.run_inventory(self.state)
        self.assertIn(str(self.project),[row['path'] for row in report['project_coverage']])
        self.assertEqual(report['candidates'],[])
        self.assertEqual(unknown.read_bytes(),b'original unknown file')
        for path in (str(Path.home()),str(Path.home().anchor),str(Path.home()/'Documents')):
            with self.assertRaises(SafetyError):discovery.register(self.state,path,[self.codex])

    def test_project_filter_and_budget_show_unscanned_scope(self):
        second=self.base/'second';second.mkdir()
        for root in (self.project,second):(root/'source.java').write_bytes(b'original')
        ctx={'projects':[{'path':str(self.project)},{'path':str(second)}],'threads':[]}
        report=inventory.run_inventory(self.state,ctx,project=str(second))
        self.assertEqual([row['path'] for row in report['project_coverage']],[str(second)])
        with self.assertRaises(SafetyError):inventory.run_inventory(self.state,ctx,project=str(self.base))
        with patch.object(projects,'MAX_FILES',0):report=inventory.run_inventory(self.state,ctx)
        self.assertFalse(report['project_complete'])
        self.assertIn('not_scanned_limit',[row['status'] for row in report['project_coverage']])

    def test_python_stable_version_headers_and_unknown_versions(self):
        source=self.project/'helper.py';source.write_text('value=1\n',encoding='utf-8')
        folder=self.project/'__pycache__';folder.mkdir()
        stamp=source.stat();paths=[]
        for tag,magic in projects.CPYTHON_MAGIC.items():
            path=folder/('helper.cpython-'+tag+'.pyc')
            path.write_bytes(magic.to_bytes(2,'little')+b'\r\n'+struct.pack('<III',0,int(stamp.st_mtime),stamp.st_size)+b'fixture')
            paths.append(str(path))
        unknown=folder/'helper.cpython-315.pyc';unknown.write_bytes(Path(paths[0]).read_bytes())
        wrong=folder/'helper.cpython-314.opt-1.pyc';wrong.write_bytes(Path(paths[0]).read_bytes())
        generated=projects.discover_generated({'bytecode_paths':paths+[str(unknown),str(wrong)]})
        self.assertEqual({row['path'] for row in generated},set(paths))
        source.unlink();self.assertEqual(projects.discover_generated({'bytecode_paths':paths}),[])

    def test_real_python_compilers_generate_recognized_caches(self):
        source=self.project/'helper.py';source.write_text('value=1\n',encoding='utf-8')
        versions=[sys.executable]+[value for value in os.environ.get('CLEANUP_PYTHON_VERSIONS','').split(os.pathsep) if value]
        for executable in dict.fromkeys(versions):
            subprocess.run([executable,'-I','-c','import py_compile,sys; py_compile.compile(sys.argv[1],doraise=True)',str(source)],check=True)
        paths=[str(path) for path in (self.project/'__pycache__').glob('*.pyc')]
        self.assertEqual(len(projects.discover_generated({'bytecode_paths':paths})),len(paths))

    def pytest_cache(self):
        folder=self.project/'.pytest_cache';(folder/'v/cache').mkdir(parents=True)
        (folder/'README.md').write_text('# pytest cache directory #\nfixture',encoding='utf-8')
        (folder/'CACHEDIR.TAG').write_text('Signature: 8a477f597d28d172789f06886806bc55\n',encoding='utf-8')
        (folder/'.gitignore').write_text('# Created by pytest automatically.\n*\n',encoding='utf-8')
        file=folder/'v/cache/nodeids';file.write_text('["test_example.py::test_ok"]',encoding='utf-8')
        return folder,file

    def test_cloud_cache_is_not_opened_for_generation_evidence(self):
        folder,file=self.pytest_cache()
        original_stat=Path.stat;original_open=Path.open
        class CloudStat:
            st_file_attributes=0x1000
            def __getattr__(self,name):return getattr(original_stat(file),name)
        def cloud_stat(path,*args,**kwargs):
            return CloudStat() if path==file else original_stat(path,*args,**kwargs)
        def guard(path,*args,**kwargs):
            if path==file:raise AssertionError('Cloud cache must not be hydrated')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'stat',cloud_stat),patch.object(Path,'open',guard):
            self.assertEqual(projects.discover_generated({'pytest_paths':[str(file)]}),[])

    def test_pytest_cache_requires_markers_shape_and_all_protections(self):
        folder,file=self.pytest_cache()
        generated=projects.discover_generated({'pytest_paths':[str(file)]});self.assertEqual(len(generated),1)
        self.assertTrue(inventory.inspect_artifact(generated[0],state=self.state)['selectable'])
        (self.project/'.codex-keep').touch()
        self.assertFalse(inventory.inspect_artifact(generated[0],state=self.state)['selectable'])
        (self.project/'.codex-keep').unlink()
        with patch.object(inventory,'git_tracked_status',return_value=None):
            self.assertFalse(inventory.inspect_artifact(generated[0],state=self.state)['selectable'])
        file.write_text('[12]',encoding='utf-8');self.assertEqual(projects.discover_generated({'pytest_paths':[str(file)]}),[])
        file.write_text('[]',encoding='utf-8');(folder/'CACHEDIR.TAG').unlink()
        self.assertEqual(projects.discover_generated({'pytest_paths':[str(file)]}),[])

    def test_java_cache_requires_standard_output_source_build_and_protection(self):
        source=self.project/'src/main/java/demo/Hello.java';source.parent.mkdir(parents=True);source.write_text('class Hello {}')
        (self.project/'pom.xml').write_text('<project/>')
        file=self.project/'target/classes/demo/Hello.class';file.parent.mkdir(parents=True)
        file.write_bytes(b'\xca\xfe\xba\xbe'+struct.pack('>HHH',0,61,2)+b'fixture')
        result=projects.discover_generated({'java_paths':[str(file)]});self.assertEqual(len(result),1)
        with patch.object(inventory,'git_tracked_status',return_value=True):
            self.assertFalse(inventory.inspect_artifact(result[0],state=self.state)['selectable'])
        source.unlink();self.assertEqual(projects.discover_generated({'java_paths':[str(file)]}),[])

    def test_real_java_compiler_output(self):
        javac=os.environ.get('CLEANUP_JAVAC') or shutil.which('javac')
        if not javac:self.skipTest('Set CLEANUP_JAVAC to an installed Java 8–25 compiler')
        source=self.project/'src/main/java/demo/Hello.java';source.parent.mkdir(parents=True)
        source.write_text('package demo; public class Hello { public static void main(String[] args) {} }',encoding='utf-8')
        (self.project/'pom.xml').write_text('<project/>')
        output=self.project/'target/classes';output.mkdir(parents=True)
        result=subprocess.run([javac,'-encoding','UTF-8','-d',str(output),str(source)],capture_output=True)
        if result.returncode:
            version=subprocess.run([javac,'-J-XshowSettings:properties','-version'],capture_output=True)
            self.fail(f'javac exit {result.returncode}\nstdout={result.stdout!r}\nstderr={result.stderr!r}\nJDK={version.stderr!r}')
        file=output/'demo/Hello.class'
        major=struct.unpack('>H',file.read_bytes()[6:8])[0]
        self.assertEqual(len(projects.discover_generated({'java_paths':[str(file)]})),int(52<=major<=69))


if __name__=='__main__':unittest.main()
