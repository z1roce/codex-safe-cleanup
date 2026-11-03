import os
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import patch

import inventory
from safe_cleanup import Cleaner, SafetyError


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='codex-only-test-',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.base=Path(self.tmp.name)
        self.root=self.base/'.codex'
        self.root.mkdir()
        self.scope=patch.object(inventory,'roots',return_value=[self.root]);self.scope.start()
        self.cleaner=Cleaner(self.base/'state')
        with self.cleaner.locked():self.cleaner.init([])

    def tearDown(self):
        self.scope.stop();self.tmp.cleanup()

    def scan(self,**kwargs):return inventory.run_inventory(self.cleaner.state,**kwargs)

    def test_default_scope_ignores_projects_history_and_extra_drives(self):
        ctx={'projects':[{'path':str(self.base/'unrelated')}],'threads':[{'cwd':str(self.base/'docs')} ]}
        self.assertEqual([x['path'] for x in inventory.discover(self.cleaner.state,ctx)],[str(self.root)])
        with self.assertRaises(SafetyError):self.scan(extra=[str(self.base)])
        with self.assertRaises(SafetyError):self.scan(scopes=[{'path':str(self.base),'sources':['untrusted']}])

    def test_only_metadata_no_hashing_or_source_contents(self):
        (self.root/'cache').mkdir();(self.root/'cache/test.log').write_bytes(b'not known to be useless')
        (self.root/'auth.json').write_bytes(b'credential fixture')
        before=self.cleaner.config_file.read_bytes()
        original=Path.open
        root=self.root
        def guard(p,*args,**kwargs):
            if p==root or root in p.parents:raise AssertionError('Source content must not be opened: '+str(p))
            return original(p,*args,**kwargs)
        with patch.object(Path,'open',guard):r=self.scan()
        self.assertEqual(r['hash_bytes'],0);self.assertEqual(r['eligible'],0)
        self.assertFalse(any(x['selectable'] for x in r['items']))
        self.assertEqual(r['scope_version'],inventory.SCOPE_VERSION)
        self.assertEqual(before,self.cleaner.config_file.read_bytes())

    def test_protected_directories_never_traversed(self):
        protected=['skills','plugins','sessions','unknown-project']
        for n in protected:
            (self.root/n).mkdir();(self.root/n/'original.txt').write_bytes(b'important fixture')
        real_scandir=os.scandir
        def guard(p):
            if Path(p)!=self.root:raise AssertionError('Protected directory was traversed: '+str(p))
            return real_scandir(p)
        with patch.object(inventory.os,'scandir',side_effect=guard):r=self.scan()
        self.assertEqual(len(r['items']),len(protected))
        self.assertTrue(all(x['bytes'] is None and x['status']=='protected' for x in r['items']))

    def test_nested_plugin_cache_and_runtime_are_preserved(self):
        (self.root/'.tmp/plugins').mkdir(parents=True)
        (self.root/'.tmp/plugins/plugin.bin').write_bytes(b'installed plugin fixture')
        (self.root/'.tmp/plugins.sync.lock').write_bytes(b'lock')
        r=self.scan()
        self.assertEqual(r['file_count'],1)
        self.assertFalse(r['complete'])
        self.assertEqual(r['items'][0]['status'],'protected')
        self.assertTrue(any('plugins' in x['path'] for x in r['skipped']))
        self.assertEqual((self.root/'.tmp/plugins/plugin.bin').read_bytes(),b'installed plugin fixture')

    def test_limit_and_cancellation_do_not_change_sources(self):
        (self.root/'cache').mkdir()
        for n in range(4):(self.root/'cache'/f'{n}.log').write_bytes(b'keep')
        with patch.object(inventory,'MAX_FILES',2):r=self.scan()
        self.assertFalse(r['complete']);self.assertEqual(r['file_count'],2)
        cancel=threading.Event();cancel.set()
        with self.assertRaises(SafetyError):self.scan(cancel=cancel)
        self.assertEqual(len(list((self.root/'cache').iterdir())),4)

    def test_missing_root_reports_missing(self):
        absent=self.root/'missing'
        with patch.object(inventory,'roots',return_value=[absent]):r=self.scan()
        self.assertEqual(r['coverage'][0]['status'],'missing')

    def test_repeated_scan_has_new_id_and_updated_metadata(self):
        (self.root/'cache').mkdir();f=self.root/'cache/test.log';f.write_bytes(b'one')
        first=self.scan();f.write_bytes(b'longer fixture')
        second=self.scan()
        self.assertNotEqual(first['id'],second['id'])
        self.assertGreater(second['logical_bytes'],first['logical_bytes'])
        self.assertEqual(second['duplicate_groups'],[])

    def test_real_git_index_change_and_worktree_tracked_files_are_protected(self):
        repo=self.base/'git-fixture';repo.mkdir()
        def git(*args):
            return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,
                env=dict(os.environ,GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_OPTIONAL_LOCKS='0'))
        git('init');f=repo/'generated.log';f.write_bytes(b'fixture')
        self.assertIs(inventory.git_tracked_status(f),False)
        git('add','generated.log')
        self.assertIs(inventory.git_tracked_status(f),True)
        git('-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-m','fixture')
        work=self.base/'worktree-fixture';git('worktree','add','--detach',str(work))
        self.assertIs(inventory.git_tracked_status(work/'generated.log'),True)
        new=work/'new.log';new.write_bytes(b'fixture')
        self.assertIs(inventory.git_tracked_status(new),False)
        subprocess.run(['git','-C',str(work),'add','new.log'],check=True,capture_output=True)
        self.assertIs(inventory.git_tracked_status(new),True)


if __name__=='__main__':unittest.main(verbosity=2)
