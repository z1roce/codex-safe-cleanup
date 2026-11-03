import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import inventory
from file_cleanup import FileCleanup,ACK,SYNC_ACK
from safe_cleanup import Cleaner,SafetyError
from safe_cleanup import write_json

class FileCleanupTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='codex-file-test-',dir=os.environ['CLEANUP_TEST_ROOT'])
  self.base=Path(self.tmp.name);self.root=self.base/'.codex';self.root.mkdir()
  self.scope=patch.object(inventory,'roots',return_value=[self.root]);self.scope.start()
  self.tid='00000000-0000-0000-0000-000000000001'
  self.file=self.root/'cache/computer-use/computer-use-app-catalog'/self.tid/'shell-apps.json'
  self.file.parent.mkdir(parents=True);self.data=b'{"test":"disposable app catalog"}'*100;self.file.write_bytes(self.data)
  self.c=Cleaner(self.base/'state')
  with self.c.locked():self.c.init([])
  self.engine=FileCleanup(self.c.state)
 def tearDown(self):self.scope.stop();self.tmp.cleanup()
 def preview(self):
  r=inventory.run_inventory(self.c.state)
  return self.engine.preview(r['id'],[r['candidates'][0]['id']])
 def test_scan_does_not_authorize_and_protected_files_cannot_enter(self):
  r=inventory.run_inventory(self.c.state)
  self.assertEqual(len(r['candidates']),1);self.assertFalse(r['candidates'][0]['confirmed'])
  for p in (self.root/'config.toml',self.root/'plugins/cache/test.json',self.root/'cache/unknown.json'):
   with self.assertRaises(SafetyError):self.engine.check(str(p),exists=False)
  with patch.dict(os.environ,CODEX_THREAD_ID=self.tid):self.assertIsNone(inventory.candidate_info(self.file))
 def test_real_recycle_restore_and_plan_single_use(self):
  p=self.preview()
  with self.assertRaises(SafetyError):self.engine.execute(p['id'],'wrong')
  self.assertTrue(self.file.exists())
  r=self.engine.execute(p['id'],ACK);self.assertFalse(r['failed'],r)
  self.assertEqual(r['entries'][0]['status'],'recycled');self.assertFalse(self.file.exists())
  with self.assertRaises(SafetyError):self.engine.execute(p['id'],ACK)
  self.engine.restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)
 def test_changed_file_and_new_protection_block_processing(self):
  p=self.preview();self.file.write_bytes(b'new useful data')
  with self.assertRaises(SafetyError):self.engine.execute(p['id'],ACK)
  self.assertEqual(self.file.read_bytes(),b'new useful data')
  p=self.preview();c=self.c.cfg();c['protect'].append(str(self.file));self.c.save(c)
  with self.assertRaises(SafetyError):self.engine.execute(p['id'],ACK)
 def test_refused_recycle_preserves_staged_file_and_can_restore(self):
  p=self.preview()
  with patch('windows_recycle.native_operation',return_value={'ok':False,'error':'test refusal'}):
   r=self.engine.execute(p['id'],ACK)
  self.assertTrue(r['failed']);self.assertTrue(r['recoverable'])
  self.engine.restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)
 def test_restore_refuses_overwrite(self):
  r=self.engine.execute(self.preview()['id'],ACK);self.assertFalse(r['failed'],r)
  self.file.write_bytes(b'new original location data')
  with self.assertRaises(SafetyError):self.engine.restore(r['id'])
  self.assertEqual(self.file.read_bytes(),b'new original location data')
  self.file.rename(self.file.with_suffix('.conflict'))
  self.engine.restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)
 def test_pause_blocks_cleanup_but_not_recovery(self):
  p=self.preview();write_json(self.c.state/'panel-controls.json',{'manual_paused':True})
  with self.assertRaises(SafetyError):self.engine.execute(p['id'],ACK)
  self.assertTrue(self.file.exists())
 def test_sync_impact_requires_separate_confirmation_and_restores(self):
  # The fixture is local; custom sync_roots only simulates the sync policy.
  c=self.c.cfg();c['sync_roots']=[str(self.root)];self.c.save(c)
  p=self.preview();self.assertEqual(p['sync_count'],1)
  with self.assertRaises(SafetyError):self.engine.execute(p['id'],ACK)
  self.assertEqual(self.file.read_bytes(),self.data)
  r=self.engine.execute(p['id'],ACK,sync_ack=SYNC_ACK)
  self.assertFalse(r['failed'],r);self.assertFalse(self.file.exists())
  self.engine.restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)
 def test_new_git_tracking_blocks_even_with_sync_confirmation(self):
  p=self.preview()
  with patch.object(inventory,'git_tracked_status',return_value=True):
   with self.assertRaises(SafetyError):self.engine.execute(p['id'],ACK,sync_ack=SYNC_ACK)
  self.assertEqual(self.file.read_bytes(),self.data)
 def test_system_powershell_works_without_codex_bundled_runtime(self):
  p=self.preview()
  # Verify the public distribution's native fallback, not only this machine's pwsh.
  with patch('windows_recycle.Path.home',return_value=self.base/'no-bundled-runtime'):
   r=self.engine.execute(p['id'],ACK)
   self.assertFalse(r['failed'],r)
   self.engine.restore(r['id'])
  self.assertEqual(self.file.read_bytes(),self.data)
 def test_prepared_journal_with_staged_file_can_recover(self):
  p=self.preview()
  with patch('windows_recycle.native_operation',return_value={'ok':False}):r=self.engine.execute(p['id'],ACK)
  folder,m=self.engine.get(r['id']);m['entries'][0]['status']='prepared';self.engine.save(folder,m)
  self.assertTrue(self.engine.history()[0]['recoverable'])
  self.engine.restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)

if __name__=='__main__':unittest.main(verbosity=2)
