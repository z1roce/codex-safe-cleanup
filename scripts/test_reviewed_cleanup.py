import ctypes
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from safe_cleanup import Cleaner, SafetyError, read_json, write_json
import reviewed_cleanup as rc


class ReviewedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='reviewed-cleanup-test-', dir=os.environ['CLEANUP_TEST_ROOT'])
        self.base = Path(self.tmp.name)
        self.codex = self.base / 'codex'
        self.tid = '00000000-0000-0000-0000-000000000001'
        self.folder = self.codex / 'visualizations/2026/10/01' / self.tid / 'task-build/sdk'
        self.folder.mkdir(parents=True)
        (self.folder / 'empty').mkdir()
        (self.folder / 'tool.bin').write_bytes(b'test dependency bytes\n' * 1000)
        (self.folder / 'headers.java').write_bytes(b'distributed tool header, not a user project')
        self.patch = patch.dict(os.environ, CODEX_HOME=str(self.codex))
        self.patch.start()
        self.state = self.base / 'state'
        self.cleaner = Cleaner(self.state)
        with self.cleaner.locked():
            self.cleaner.init([])
        self.description = {'path':str(self.folder), 'name':'测试任务的编译工具', 'purpose':'仅用于本测试的依赖工具',
            'question':'测试数据，无保留价值', 'task_id':self.tid, 'task_title':'隔离测试任务',
            'cleanup_kind':'task_dependency_bundle'}
        write_json(self.state / 'inventory-context.json', {'directory_explanations':[self.description],
            'threads':[{'id':self.tid,'status':'notLoaded'}], 'projects':[], 'preserve_paths':[]})
        self.engine = rc.ReviewedCleanup(self.state)
        self.iid = self.engine.candidates()[0]['id']

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def preview(self):
        return self.engine.preview([self.iid])

    def test_native_recycle_and_restore_only_disposable_fixture(self):
        p = self.preview()
        with self.assertRaises(SafetyError):
            self.engine.execute(p['id'], rc.ACK, recycle=True)
        result = self.engine.execute(p['id'], rc.RECYCLE_ACK, recycle=True)
        self.assertFalse(result['failed'], result)
        self.assertEqual(result['entries'][0]['status'], 'recycled')
        self.assertFalse(self.folder.exists())
        self.assertTrue(result['recycled'])
        _, record = self.engine.get_batch(result['id'])
        recycled = Path(record['entries'][0]['recycle_result']['recycle_path'])
        self.assertTrue(recycled.exists())
        self.assertEqual((recycled / 'tool.bin').read_bytes(), b'test dependency bytes\n' * 1000)
        restored = self.engine.restore(result['id'])
        self.assertEqual(restored['entries'][0]['status'], 'restored')
        self.assertEqual((self.folder / 'tool.bin').read_bytes(), b'test dependency bytes\n' * 1000)
        self.assertTrue((self.folder / 'empty').is_dir())
        self.assertFalse(recycled.exists())

    def test_recycle_refusal_rolls_back_without_deletion(self):
        p = self.preview()
        with patch('windows_recycle.send', return_value={'ok':False, 'error':'test refusal'}):
            result = self.engine.execute(p['id'], rc.RECYCLE_ACK, recycle=True)
        self.assertTrue(result['failed'])
        self.assertEqual(result['entries'][0]['status'], 'rolled_back')
        self.assertTrue((self.folder / 'tool.bin').is_file())

    def test_existing_quarantine_preview_recycle_and_restore(self):
        moved = self.engine.execute(self.preview()['id'], rc.ACK)
        p = self.engine.preview_stored(moved['id'])
        with self.assertRaises(SafetyError):
            self.engine.recycle_stored(p['id'], rc.ACK)
        with self.assertRaises(SafetyError):
            self.engine.execute(p['id'], rc.RECYCLE_ACK, recycle=True)
        result = self.engine.recycle_stored(p['id'], rc.RECYCLE_ACK)
        self.assertFalse(result['failed'], result)
        self.assertEqual(result['entries'][0]['status'], 'recycled')
        self.engine.restore(result['id'])
        self.assertEqual((self.folder / 'tool.bin').read_bytes(), b'test dependency bytes\n' * 1000)

    def test_existing_quarantine_changed_or_protected_is_retained(self):
        moved = self.engine.execute(self.preview()['id'], rc.ACK)
        p = self.engine.preview_stored(moved['id'])
        config = self.cleaner.cfg()
        config['protect'].append(str(self.folder))
        self.cleaner.save(config)
        result = self.engine.recycle_stored(p['id'], rc.RECYCLE_ACK)
        self.assertTrue(result['failed'])
        self.assertEqual(result['entries'][0]['status'], 'quarantined')

    def test_native_identity_guard_rejects_wrong_directory(self):
        from windows_recycle import send
        result = send(self.folder, 0)
        self.assertFalse(result['ok'])
        self.assertTrue((self.folder / 'tool.bin').is_file())

    def test_preview_preserves_files_and_global_authorization(self):
        before = self.cleaner.config_file.read_bytes()
        p = self.preview()
        self.assertEqual(p['items'][0]['files'], 2)
        self.assertTrue(self.folder.exists())
        self.assertEqual(before, self.cleaner.config_file.read_bytes())
        self.assertTrue((self.folder/'empty').exists())

    def test_consent_required_and_plan_single_use(self):
        p = self.preview()
        with self.assertRaises(SafetyError):
            self.engine.execute(p['id'], 'not-authorized')
        self.assertTrue(self.folder.exists())
        result = self.engine.execute(p['id'], rc.ACK)
        self.assertTrue(result['recoverable'])
        self.assertFalse(self.folder.exists())
        with self.assertRaises(SafetyError):
            self.engine.execute(p['id'], rc.ACK)
        self.engine.restore(result['id'])

    def test_move_restore_preserves_every_byte_and_empty_directory(self):
        original = {str(p.relative_to(self.folder)):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        p = self.preview()
        result = self.engine.execute(p['id'], rc.ACK)
        self.assertFalse(self.folder.exists())
        restored = self.engine.restore(result['id'])
        self.assertEqual(restored['entries'][0]['status'], 'restored')
        self.assertEqual({str(p.relative_to(self.folder)):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}, original)
        self.assertTrue((self.folder/'empty').is_dir())

    def test_new_or_modified_content_invalidates_preview(self):
        for added in (False, True):
            p = self.preview()
            target = self.folder / ('extra.tmp' if added else 'tool.bin')
            target.write_bytes(b'valuable new content')
            result = self.engine.execute(p['id'], rc.ACK)
            self.assertTrue(result['failed'])
            self.assertTrue(self.folder.exists())
            self.assertEqual(target.read_bytes(), b'valuable new content')

    def test_secrets_git_markers_and_hardlinks_block_preview(self):
        for name in ('signing.keystore', '.env', 'important.key', '.codex-keep'):
            p = self.folder / name
            p.write_bytes(b'keep')
            with self.assertRaises(SafetyError):
                self.preview()
            p.unlink()  # Only this test's fixture.
        (self.folder/'.git').mkdir()
        with self.assertRaises(SafetyError):
            self.preview()
        (self.folder/'.git').rmdir()
        os.link(self.folder/'tool.bin', self.base/'outside-hardlink.bin')
        with self.assertRaises(SafetyError):
            self.preview()

    def test_uncatalogued_path_and_whole_task_not_eligible(self):
        with self.assertRaises(SafetyError):
            self.engine.preview(['anything'])
        self.description['path'] = str(self.folder.parent.parent)
        c = read_json(self.state/'inventory-context.json')
        c['directory_explanations'] = [self.description]
        write_json(self.state/'inventory-context.json',c)
        self.assertFalse(self.engine.candidates()[0]['selectable'])

    def test_existing_user_protection_and_custom_sync_roots_honored(self):
        for field in ('protect','sync_roots'):
            config=self.cleaner.cfg()
            config[field]=[str(self.folder/'empty')]
            self.cleaner.save(config)
            self.assertFalse(self.engine.candidates()[0]['selectable'])
            with self.assertRaises(SafetyError):
                self.preview()
            config[field]=[]
            self.cleaner.save(config)

    def test_new_protection_after_preview_blocks_execution(self):
        p=self.preview()
        config=self.cleaner.cfg();config['protect']=[str(self.folder)]
        self.cleaner.save(config)
        with self.assertRaises(SafetyError):
            self.engine.execute(p['id'],rc.ACK)
        self.assertTrue(self.folder.exists())

    def test_pause_blocks_mutation_but_allows_restore(self):
        p = self.preview()
        self.engine.set_paused(True)
        with self.assertRaises(SafetyError):
            self.engine.execute(p['id'], rc.ACK)
        self.engine.set_paused(False)
        result = self.engine.execute(p['id'], rc.ACK)
        self.engine.set_paused(True)
        self.engine.restore(result['id'])
        self.assertTrue(self.folder.exists())

    def test_restore_conflict_never_overwrites(self):
        result = self.engine.execute(self.preview()['id'], rc.ACK)
        self.folder.mkdir()
        (self.folder/'new.txt').write_bytes(b'new user content')
        with self.assertRaises(SafetyError):
            self.engine.restore(result['id'])
        self.assertEqual((self.folder/'new.txt').read_bytes(),b'new user content')
        self.assertTrue(self.engine.history()[0]['recoverable'])

    def test_interrupted_postmove_validation_rolls_back(self):
        p = self.preview()
        real = rc.tree
        def fail_after_move(root,*args,**kwargs):
            if root.name == 'payload':
                raise SafetyError('模拟移动后检查失败')
            return real(root,*args,**kwargs)
        with patch.object(rc,'tree',side_effect=fail_after_move):
            result = self.engine.execute(p['id'],rc.ACK)
        self.assertTrue(result['failed'])
        self.assertEqual(result['entries'][0]['status'],'rolled_back')
        self.assertTrue((self.folder/'tool.bin').exists())

    @unittest.skipUnless(os.name=='nt','Windows write sharing')
    def test_existing_writer_blocks_directory_move(self):
        from ctypes import wintypes as w
        p = self.preview()
        k = ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateFileW.argtypes=[w.LPCWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,w.DWORD,w.DWORD,w.HANDLE]
        k.CreateFileW.restype=w.HANDLE
        k.CloseHandle.argtypes=[w.HANDLE]
        h=k.CreateFileW(str(self.folder/'tool.bin'),0x40000000,1|2|4,None,3,0x80,None)
        self.assertNotEqual(h,w.HANDLE(-1).value)
        try:
            result=self.engine.execute(p['id'],rc.ACK)
            self.assertTrue(result['failed'])
            self.assertTrue(self.folder.exists())
        finally:
            k.CloseHandle(h)

    @unittest.skipUnless(os.name=='nt','Windows native handle scalability')
    def test_more_than_8192_locks_do_not_exhaust_python_file_descriptors(self):
        p=self.folder/'tool.bin'
        expected=p.read_bytes()
        with contextlib.ExitStack() as stack:
            leases=[stack.enter_context(rc.NativeLease(p)) for _ in range(8300)]
            with leases[-1].reader() as f:
                self.assertEqual(f.read(),expected)

    @unittest.skipUnless(os.name=='nt','Windows NTFS compression')
    def test_lossless_compression_and_restore(self):
        original=(self.folder/'tool.bin').read_bytes()
        result=self.engine.execute(self.preview()['id'],rc.ACK,compress=True)
        self.assertTrue(result['recoverable'])
        self.assertEqual(result['entries'][0]['status'],'quarantined')
        self.assertTrue(result['entries'][0].get('compressed'))
        self.assertLess(result['entries'][0]['allocated_after'],result['entries'][0]['allocated_before'])
        self.engine.restore(result['id'])
        self.assertEqual((self.folder/'tool.bin').read_bytes(),original)


if __name__=='__main__':
    unittest.main(verbosity=2)
