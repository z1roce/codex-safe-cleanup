"""Isolated safety tests; never use real project files as deletion fixtures."""
import importlib.util
import ctypes
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('cleanup', Path(__file__).with_name('safe_cleanup.py'))
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)


class SafetyTests(unittest.TestCase):
    def setUp(self):
        # Base supplied by caller must be a disposable non-synced test area.
        self.temp = tempfile.TemporaryDirectory(prefix='codex-cleanup-test-',
                                                dir=os.environ['CLEANUP_TEST_ROOT'])
        self.base = Path(self.temp.name)
        self.codex_env=patch.dict(os.environ,CODEX_HOME=str(self.base/'codex'))
        self.codex_env.start()
        self.addCleanup(self.codex_env.stop)
        self.root = self.base / 'codex/visualizations/candidates'
        self.root.mkdir(parents=True)
        self.c = sc.Cleaner(self.base / 'state')
        with self.c.locked():
            self.c.init([str(self.root)])

    def tearDown(self):
        self.temp.cleanup()

    def test_legacy_mutating_cli_stops_before_creating_state(self):
        state=self.base/'must-not-be-created'
        with self.assertRaises(sc.SafetyError):sc.main(['--state',str(state),'apply'])
        self.assertFalse(state.exists())

    def file(self, name, data=b'disposable log\n' * 1000):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def test_legacy_restore_survives_scope_change(self):
        p=self.file('recovery.tmp');self.enable();self.approve(p)
        result=self.c.apply()
        cfg=self.c.cfg();cfg['roots']=[];cfg['protect']=[str(p)];self.c.save(cfg)
        self.c.restore(result['batch'])
        self.assertEqual(p.read_bytes(),b'disposable log\n'*1000)

    def row(self, report, p):
        return next(x for x in report['items'] if x['path'] == str(p))

    def enable(self, managed=False):
        r = self.c.scan()
        self.c.enable(r['id'], sc.SCOPE_ACK, managed)

    def approve(self, p):
        r = self.c.scan()
        self.c.approve_items(r['id'], [self.row(r, p)['id']], sc.ITEM_ACK,
                             '测试数据：已确认独立生成，无引用，无保留价值')

    def test_first_scan_never_moves_and_apply_disabled(self):
        p = self.file('old.tmp')
        r = self.c.scan()
        self.assertEqual(self.row(r, p)['status'], 'review')
        self.assertTrue(p.exists())
        with self.assertRaises(sc.SafetyError):
            self.c.apply()
        self.assertFalse((self.c.state / 'quarantine').exists())

    def test_effective_original_and_final_protected(self):
        for name in ('Main.java', 'report.docx', 'final.pdf', 'submit.zip', '.env', 'state.sqlite'):
            p = self.file(name)
            r = self.c.scan()
            self.assertEqual(self.row(r, p)['status'], 'protected')
            with self.assertRaises(sc.SafetyError):
                self.c.approve_items(r['id'], [self.row(r, p)['id']], sc.ITEM_ACK, 'test')

    def test_git_dirty_ignored_untracked_and_clean_all_preserved(self):
        repo = self.root / 'repo'
        repo.mkdir()
        (repo / '.git').mkdir()
        (repo / 'valuable.tmp').write_bytes(b'untracked original')
        r = self.c.scan()
        self.assertEqual(self.row(r, repo)['status'], 'protected')
        self.assertFalse(any(x['path'].endswith('valuable.tmp') for x in r['items']))

    def test_git_ancestor_outside_scan_root(self):
        (self.base / '.git').mkdir()
        p = self.file('ignored.tmp')
        r = self.c.scan()
        self.assertEqual(self.row(r, self.root)['status'], 'protected')
        self.assertTrue(p.exists())

    def test_keep_marker_protects_entire_directory(self):
        p = self.file('mixed/task.tmp')
        (p.parent / '.codex-keep').touch()
        r = self.c.scan()
        self.assertEqual(self.row(r, p.parent)['status'], 'protected')

    def test_current_project_child_root_protected(self):
        p = self.file('active/temp/running.tmp')
        old_cwd = Path.cwd()
        try:
            os.chdir(self.root / 'active')
            config = self.c.cfg()
            config['roots'] = [str(p.parent)]
            self.c.save(config)
            r = self.c.scan()
            self.assertEqual(self.row(r, p.parent)['status'], 'protected')
        finally:
            os.chdir(old_cwd)

    def test_mixed_project_never_deleted(self):
        p = self.file('project/render.tmp')
        final = self.file('project/final.docx', b'important')
        self.enable()
        self.approve(p)
        result = self.c.apply()
        self.assertEqual(result['moved'], 1)
        self.assertTrue(final.exists())
        self.assertTrue(p.parent.exists())

    def test_duplicate_files_are_review_not_auto(self):
        a = self.file('first.tmp')
        b = self.file('second.tmp')
        self.enable()
        r = self.c.scan()
        self.assertEqual(self.row(r, a)['same_content_as'], [str(b)])
        self.assertEqual(self.row(r, a)['status'], 'review')
        self.assertEqual(self.c.apply()['moved'], 0)

    def test_changed_snapshot_and_new_file_retained(self):
        p = self.file('changed.tmp')
        self.enable()
        self.approve(p)
        p.write_bytes(b'new valuable text')
        q = self.file('new.tmp')
        self.assertEqual(self.c.apply()['moved'], 0)
        self.assertTrue(p.exists() and q.exists())

    def test_approved_file_restore_hash_and_revoke(self):
        p = self.file('gone.tmp')
        original = p.read_bytes()
        self.enable()
        self.approve(p)
        result = self.c.apply()
        self.assertFalse(p.exists())
        self.assertEqual(result['moved'], 1)
        self.assertLess(result['entries'][0]['archive_bytes'], len(original))
        self.c.restore(result['batch'])
        self.assertEqual(p.read_bytes(), original)
        self.assertEqual(self.c.apply()['moved'], 0)

    def test_conflict_restore_does_not_overwrite(self):
        p = self.file('conflict.tmp')
        self.enable()
        self.approve(p)
        result = self.c.apply()
        p.write_bytes(b'new file after cleanup')
        with self.assertRaises(sc.SafetyError):
            self.c.restore(result['batch'])
        self.assertEqual(p.read_bytes(), b'new file after cleanup')
        dest = self.base / 'recovered'
        out = self.c.restore(result['batch'], str(dest))
        self.assertEqual(Path(out[0]['restored_to']).read_bytes(), b'disposable log\n' * 1000)

    def test_corrupted_archive_rejected(self):
        p = self.file('broken.tmp')
        self.enable()
        self.approve(p)
        result = self.c.apply()
        folder, m = self.c.batch(result['batch'])
        archive = folder / (m['entries'][0]['id'] + '.zip')
        archive.write_bytes(b'not a zip')
        with self.assertRaises((sc.SafetyError, zipfile.BadZipFile)):
            self.c.restore(result['batch'])
        self.assertFalse(p.exists())

    def test_interrupted_compression_recovers_staging(self):
        p = self.file('interrupted.tmp')
        self.enable()
        self.approve(p)
        with patch.object(self.c, 'verify_archive', side_effect=sc.SafetyError('模拟校验中断')):
            result = self.c.apply()
        self.assertEqual(result['moved'], 0)
        self.assertTrue(result['failed'])
        self.assertTrue(result['recovery_required'])
        self.assertEqual(result['staged'], 1)
        self.assertFalse(p.exists())
        self.c.restore(result['batch'])
        self.assertTrue(p.exists())

    def test_protection_added_after_receipt_blocks(self):
        p = self.file('protected-later.tmp')
        self.enable()
        self.approve(p)
        (self.root / '.codex-keep').touch()
        self.assertEqual(self.c.apply()['moved'], 0)
        self.assertTrue(p.exists())

    def test_scope_change_invalidates_approval(self):
        self.enable()
        c = self.c.cfg()
        c['roots'].append(str(self.base / 'other'))
        self.c.save(c)
        with self.assertRaises(sc.SafetyError):
            self.c.apply()

    def test_sync_root_protected(self):
        p = self.file('OneDrive/personal.tmp')
        self.enable()
        r = self.c.scan()
        self.assertEqual(self.row(r, p.parent)['status'], 'protected')

    def test_custom_sync_root_protected(self):
        c = self.c.cfg()
        c['sync_roots'] = [str(self.root)]
        self.c.save(c)
        p = self.file('original.tmp')
        r = self.c.scan()
        self.assertEqual(self.row(r, self.root)['status'], 'protected')
        self.assertTrue(p.exists())

    def test_state_in_sync_rejected(self):
        with self.assertRaises(sc.SafetyError):
            sc.Cleaner(self.base / 'OneDrive' / 'state')

    def test_hardlink_protected(self):
        p = self.file('hard.tmp')
        os.link(p, self.base / 'other-hard.tmp')
        r = self.c.scan()
        self.assertEqual(self.row(r, p)['status'], 'protected')

    @unittest.skipUnless(os.name == 'nt', 'Windows ADS check')
    def test_ntfs_alternate_stream_protected(self):
        p = self.file('with-stream.tmp')
        Path(str(p) + ':important').write_bytes(b'preserve alternate stream')
        r = self.c.scan()
        self.assertEqual(self.row(r, p)['status'], 'protected')
        self.assertIn('附加数据流', self.row(r, p)['reason'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction check')
    def test_junction_never_traversed(self):
        target = self.base / 'outside'
        target.mkdir()
        (target / 'valuable.tmp').write_bytes(b'important')
        junction = self.root / 'linked'
        command = 'New-Item -ItemType Junction -Path $env:CLEANUP_LINK_PATH -Target $env:CLEANUP_LINK_TARGET | Out-Null'
        env = dict(os.environ, CLEANUP_LINK_PATH=str(junction), CLEANUP_LINK_TARGET=str(target))
        subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], env=env, check=True,
                       capture_output=True)
        r = self.c.scan()
        self.assertEqual(self.row(r, junction)['status'], 'protected')
        self.assertFalse(any(x['path'].endswith('valuable.tmp') for x in r['items']))
        # Delete junction itself, never recurse into its target.
        junction.rmdir()

    def test_managed_active_and_cooldown_retained(self):
        self.enable(managed=True)
        task = self.c.new_task('test render')
        p = Path(task['path']) / 'render.log'
        p.write_bytes(b'generated' * 1000)
        self.assertEqual(self.row(self.c.scan(), p)['status'], 'protected')
        self.c.close_task(task['task'], '进程停止且无任何保留价值和引用', sc.ITEM_ACK)
        self.assertEqual(self.row(self.c.scan(), p)['status'], 'protected')
        self.assertEqual(self.c.apply()['moved'], 0)

    def test_managed_closed_snapshot_automatic_after_cooldown(self):
        self.enable(managed=True)
        task = self.c.new_task('test render')
        p = Path(task['path']) / 'render.log'
        p.write_bytes(b'generated' * 1000)
        self.c.close_task(task['task'], '无保留价值且无引用', sc.ITEM_ACK)
        with patch.object(sc.time, 'time', return_value=sc.time.time() + sc.COOLDOWN + 1):
            result = self.c.apply()
        self.assertEqual(result['moved'], 1)
        self.assertFalse(p.exists())
        self.c.restore(result['batch'])
        with patch.object(sc.time, 'time', return_value=sc.time.time() + sc.COOLDOWN + 1):
            self.assertEqual(self.c.apply()['moved'], 0)

    def test_managed_lifecycle_cannot_be_bypassed_with_receipt(self):
        self.enable(managed=True)
        task = self.c.new_task('test')
        p = Path(task['path']) / 'active.tmp'
        p.write_bytes(b'active temporary data')
        for closed in (False, True):
            if closed:
                self.c.close_task(task['task'], 'no references, disposable', sc.ITEM_ACK)
            r = self.c.scan()
            with self.assertRaises(sc.SafetyError):
                self.c.approve_items(r['id'], [self.row(r, p)['id']], sc.ITEM_ACK, 'test approval')
            self.assertEqual(self.c.apply()['moved'], 0)
            self.assertTrue(p.exists())

    def test_managed_extra_file_protects_whole_task(self):
        self.enable(managed=True)
        task = self.c.new_task('test')
        p = Path(task['path']) / 'temp.log'
        p.write_bytes(b'disposable')
        self.c.close_task(task['task'], '无保留价值且无引用', sc.ITEM_ACK)
        (p.parent / 'Main.java').write_bytes(b'valuable source')
        with patch.object(sc.time, 'time', return_value=sc.time.time() + sc.COOLDOWN + 1):
            self.assertEqual(self.c.apply()['moved'], 0)
        self.assertTrue(p.exists())

    def test_managed_changed_task_retained(self):
        self.enable(managed=True)
        task = self.c.new_task('test')
        p = Path(task['path']) / 'temp.log'
        p.write_bytes(b'disposable')
        self.c.close_task(task['task'], '无保留价值且无引用', sc.ITEM_ACK)
        p.write_bytes(b'new important diagnostic')
        with patch.object(sc.time, 'time', return_value=sc.time.time() + sc.COOLDOWN + 1):
            self.assertEqual(self.c.apply()['moved'], 0)

    def test_managed_walk_permission_error_not_silently_ignored(self):
        self.enable(managed=True)
        task = self.c.new_task('test')
        real_walk = sc.os.walk
        def inaccessible(top, *args, **kwargs):
            if str(top) == task['path']:
                kwargs['onerror'](PermissionError('test access denied'))
                return iter(())
            return real_walk(top, *args, **kwargs)
        with patch.object(sc.os, 'walk', side_effect=inaccessible):
            with self.assertRaises(sc.SafetyError):
                self.c.close_task(task['task'], 'disposable', sc.ITEM_ACK)
            r = self.c.scan()
            self.assertEqual(self.row(r, Path(task['path']))['status'], 'protected')

    def test_pause_blocks_apply_and_task_registration(self):
        self.enable(managed=True)
        c = self.c.cfg()
        c['paused'] = True
        self.c.save(c)
        with self.assertRaises(sc.SafetyError):
            self.c.apply()
        with self.assertRaises(sc.SafetyError):
            self.c.new_task('should not run')

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing check')
    def test_existing_writer_even_with_share_delete_preserved(self):
        from ctypes import wintypes as w
        p = self.file('busy.tmp')
        self.enable()
        self.approve(p)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p,
                                      w.DWORD, w.DWORD, w.HANDLE]
        kernel.CreateFileW.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        writer = kernel.CreateFileW(str(p), 0x40000000, 1 | 2 | 4, None, 3, 0x80, None)
        self.assertNotEqual(writer, w.HANDLE(-1).value)
        try:
            result = self.c.apply()
            self.assertEqual(result['moved'], 0)
            self.assertTrue(p.exists())
            self.assertIn('锁定', result['entries'][0]['error'])
        finally:
            kernel.CloseHandle(writer)

    @unittest.skipUnless(os.name == 'nt', 'Windows late write check')
    def test_late_writer_denied_until_disposition(self):
        p = self.file('late.tmp')
        self.enable()
        self.approve(p)
        real_snapshot = sc.snapshot
        attempts = []
        def try_late_write(path, source=None):
            s = real_snapshot(path, source)
            if str(path).endswith('.stage'):
                with self.assertRaises(PermissionError):
                    with path.open('ab') as f:
                        f.write(b'late important data')
                attempts.append(True)
            return s
        with patch.object(sc, 'snapshot', side_effect=try_late_write):
            result = self.c.apply()
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(result['moved'], 1)
        self.c.restore(result['batch'])
        self.assertEqual(p.read_bytes(), b'disposable log\n' * 1000)

    def test_incomplete_scan_cannot_enable(self):
        for i in range(5):
            self.file(str(i) + '.tmp')
        with patch.object(sc, 'MAX_ENTRIES', 2):
            r = self.c.scan()
        self.assertFalse(r['complete'])
        with self.assertRaises(sc.SafetyError):
            self.c.enable(r['id'], sc.SCOPE_ACK, False)

    def test_bad_ids_and_paths_rejected(self):
        with self.assertRaises(sc.SafetyError):
            self.c.restore('../elsewhere')
        with self.assertRaises(sc.SafetyError):
            self.c.report('../config')
        for p in ('relative', str(self.base / '..' / 'unexpected')):
            with self.assertRaises(sc.SafetyError):
                sc.absolute(p)


if __name__ == '__main__':
    unittest.main(verbosity=2)
