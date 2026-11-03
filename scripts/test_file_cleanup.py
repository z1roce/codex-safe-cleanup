import os
from pathlib import Path
import tempfile
import subprocess
import sys
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

 def test_restore_ignores_index_keep_git_and_current_scope_after_restart(self):
  with patch('windows_recycle.native_operation',return_value={'ok':False}):r=self.engine.execute(self.preview()['id'],ACK)
  (self.file.parent/'.codex-keep').write_text('preserve future files')
  write_json(self.c.state/'discovery-index.json',{'artifacts':[]})
  with patch.object(inventory,'candidate_info',return_value=None),patch.object(inventory,'git_tracked_status',return_value=None):
   FileCleanup(self.c.state).restore(r['id'])
  self.assertEqual(self.file.read_bytes(),self.data)

 def test_restart_after_native_move_without_coordinator_result(self):
  r=self.engine.execute(self.preview()['id'],ACK);self.assertFalse(r['failed'],r)
  folder,m=self.engine.get(r['id']);e=m['entries'][0]
  self.assertTrue((folder/(e['id']+'.receipt')).exists())
  e.pop('recycle_result');e['status']='recycling';self.engine.save(folder,m)
  FileCleanup(self.c.state).restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)

 def test_restart_can_find_exact_recycle_item_without_callback_receipt(self):
  for variant in ('missing','corrupt'):
   with self.subTest(receipt=variant):
    r=self.engine.execute(self.preview()['id'],ACK);self.assertFalse(r['failed'],r)
    folder,m=self.engine.get(r['id']);e=m['entries'][0];receipt=folder/(e['id']+'.receipt')
    if variant=='missing':receipt.unlink()
    else:receipt.write_bytes(b'partial native receipt fixture')
    e.pop('recycle_result');e['status']='recycling';self.engine.save(folder,m)
    FileCleanup(self.c.state).restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)

 def test_restart_after_restore_move_is_idempotent(self):
  with patch('windows_recycle.native_operation',return_value={'ok':False}):r=self.engine.execute(self.preview()['id'],ACK)
  folder,m=self.engine.get(r['id']);e=m['entries'][0]
  e['status']='restoring';self.engine.save(folder,m)
  os.rename(e['stage'],e['original'])
  FileCleanup(self.c.state).restore(r['id']);self.assertEqual(self.file.read_bytes(),self.data)

 def test_actual_process_exit_releases_lock_and_recovers_staging_and_shell_receipt(self):
  code='''
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import inventory,windows_recycle
from file_cleanup import FileCleanup,ACK
inventory.roots=lambda:[Path(sys.argv[3])]
native=windows_recycle.native_operation
def interrupted(*args,**kwargs):
    if sys.argv[5]=='after-shell':native(*args,**kwargs)
    os._exit(77)
windows_recycle.native_operation=interrupted
FileCleanup(sys.argv[2]).execute(sys.argv[4],ACK)
'''
  for boundary in ('before-shell','after-shell'):
   with self.subTest(boundary=boundary):
    preview=self.preview()
    result=subprocess.run([sys.executable,'-B','-c',code,str(Path(__file__).parent),str(self.c.state),str(self.root),preview['id'],boundary],timeout=30)
    self.assertEqual(result.returncode,77)
    engine=FileCleanup(self.c.state)
    record=next(r for r in engine.history() if r['recoverable'])
    engine.restore(record['id']);self.assertEqual(self.file.read_bytes(),self.data)

 def test_partial_failure_reports_unprocessed_selection(self):
  other=self.file.with_name('user-assist.json');other.write_bytes(b'disposable second cache')
  report=inventory.run_inventory(self.c.state)
  preview=self.engine.preview(report['id'],[r['id'] for r in report['candidates']])
  with patch('windows_recycle.native_operation',return_value={'ok':False}):r=self.engine.execute(preview['id'],ACK)
  self.assertEqual(r['selected_count'],2);self.assertEqual(r['unprocessed_count'],1)
  FileCleanup(self.c.state).restore(r['id'])
  self.assertEqual(self.file.read_bytes(),self.data);self.assertEqual(other.read_bytes(),b'disposable second cache')

 def test_restore_rejects_changed_staged_content(self):
  with patch('windows_recycle.native_operation',return_value={'ok':False}):r=self.engine.execute(self.preview()['id'],ACK)
  folder,m=self.engine.get(r['id']);stage=Path(m['entries'][0]['stage']);stage.write_bytes(b'changed test bytes')
  with self.assertRaises(SafetyError):FileCleanup(self.c.state).restore(r['id'])
  self.assertFalse(self.file.exists());self.assertEqual(stage.read_bytes(),b'changed test bytes')

 def test_volume_unavailable_after_first_move_keeps_partial_journal(self):
  other=self.file.with_name('user-assist.json');other.write_bytes(b'disposable second cache')
  report=inventory.run_inventory(self.c.state)
  rows={row['path']:row['id'] for row in report['candidates']}
  preview=self.engine.preview(report['id'],[rows[str(self.file)],rows[str(other)]])
  import windows_recycle
  native=windows_recycle.native_operation;original_stat=Path.stat;offline=False
  def recycle(*args,**kwargs):
   nonlocal offline
   result=native(*args,**kwargs);offline=True;return result
  def unavailable(path,*args,**kwargs):
   if offline and path==other:raise OSError('synthetic source volume unavailable')
   return original_stat(path,*args,**kwargs)
  with patch('windows_recycle.native_operation',side_effect=recycle),patch.object(Path,'stat',unavailable):
   result=self.engine.execute(preview['id'],ACK)
  self.assertTrue(result['failed']);self.assertTrue(result['recoverable'])
  FileCleanup(self.c.state).restore(result['id'])
  self.assertEqual(self.file.read_bytes(),self.data);self.assertEqual(other.read_bytes(),b'disposable second cache')

 def test_offline_staging_does_not_lose_record_or_allow_other_moves(self):
  with patch('windows_recycle.native_operation',return_value={'ok':False}):r=self.engine.execute(self.preview()['id'],ACK)
  folder,m=self.engine.get(r['id']);stage=Path(m['entries'][0]['stage']);hidden=stage.with_suffix('.offline')
  stage.rename(hidden)
  with self.assertRaises((OSError,SafetyError)):FileCleanup(self.c.state).restore(r['id'])
  self.assertTrue((folder/'manifest.json').is_file())
  hidden.rename(stage);FileCleanup(self.c.state).restore(r['id'])
  self.assertEqual(self.file.read_bytes(),self.data)

 def test_history_paginates_and_reports_corrupt_or_missing_manifests(self):
  import uuid
  for index in range(1,36):
   folder=self.engine.records/uuid.UUID(int=index).hex;folder.mkdir(parents=True)
   if index==1:(folder/'manifest.json').write_bytes(b'corrupt test record')
  first=self.engine.history_page()
  self.assertEqual(len(first['records']),30);self.assertEqual(first['next_offset'],30)
  second=self.engine.history_page(30)
  self.assertEqual(len(second['records']),5);self.assertIsNone(second['next_offset'])
  self.assertTrue(all(row['corrupt'] for row in first['records']+second['records']))
  self.assertEqual(self.file.read_bytes(),self.data)

 def test_stage_permission_failure_does_not_move_source(self):
  preview=self.preview()
  real_mkdir=Path.mkdir
  def denied(path,*args,**kwargs):
   if self.engine.records in path.parents and path.parent!=self.engine.records and path.name!='entry-records':raise PermissionError('synthetic staging denial')
   return real_mkdir(path,*args,**kwargs)
  with patch.object(Path,'mkdir',denied):r=self.engine.execute(preview['id'],ACK)
  self.assertTrue(r['failed']);self.assertEqual(self.file.read_bytes(),self.data)

 def test_streaming_time_budget_keeps_source_and_creates_no_authorization(self):
  with patch('file_cleanup.HASH_SECONDS',-1):
   with self.assertRaises(SafetyError):self.preview()
  self.assertEqual(self.file.read_bytes(),self.data)
  self.assertEqual(list(self.engine.plans.glob('*.json')),[])

 def make_catalogs(self,count):
  import uuid
  files=[self.file]
  for index in range(2,count+1):
   file=self.file.parent.parent/str(uuid.UUID(int=index))/self.file.name
   file.parent.mkdir();file.write_bytes(b'disposable batch fixture');files.append(file)
  return files

 def simulated_recycle(self,mode,path,expected_id,**kwargs):
  destination=self.base/'simulated-bin'/str(expected_id)/path.name
  destination.parent.mkdir(parents=True);os.rename(path,destination)
  return {'ok':True,'recycle_path':str(destination)}

 def simulated_restore(self,record,destination,expected_id):
  os.rename(record['recycle_path'],destination)
  return {'ok':True}

 def test_over_100_files_one_fixed_authorization_and_restore(self):
  files=self.make_catalogs(105);report=inventory.run_inventory(self.c.state)
  preview=self.engine.preview(report['id'],[r['id'] for r in report['candidates']])
  self.assertEqual(preview['internal_batches'],2)
  with patch('windows_recycle.native_operation',side_effect=self.simulated_recycle):
   result=self.engine.execute(preview['id'],ACK)
  self.assertEqual(result['success_count'],105);self.assertFalse(result['failed'])
  self.assertTrue(all(not file.exists() for file in files))
  with patch('windows_recycle.restore',side_effect=self.simulated_restore):self.engine.restore(result['id'])
  self.assertTrue(all(file.exists() for file in files))

 def test_resume_preserves_original_scope_after_partial_failure(self):
  files=self.make_catalogs(4);report=inventory.run_inventory(self.c.state)
  preview=self.engine.preview(report['id'],[r['id'] for r in report['candidates']])
  calls=0
  def failure(*args,**kwargs):
   nonlocal calls
   calls+=1
   return {'ok':False} if calls==2 else self.simulated_recycle(*args,**kwargs)
  with patch('windows_recycle.native_operation',side_effect=failure):result=self.engine.execute(preview['id'],ACK)
  self.assertEqual(result['success_count'],1);self.assertEqual(result['unprocessed_count'],2)
  self.assertEqual(len(result['completed_ids']),1)
  new=files[-1].with_name('user-assist.json');new.write_bytes(b'new scan must never extend authorization')
  with patch('windows_recycle.native_operation',side_effect=self.simulated_recycle):
   resumed=FileCleanup(self.c.state).resume(result['id'],ACK)
  self.assertEqual(resumed['id'],result['id']);self.assertEqual(resumed['success_count'],3)
  self.assertEqual(new.read_bytes(),b'new scan must never extend authorization')
  with patch('windows_recycle.restore',side_effect=self.simulated_restore):self.engine.restore(result['id'])
  self.assertTrue(all(file.exists() for file in files))

 def test_resume_rejects_appended_files_in_saved_plan(self):
  self.make_catalogs(2);report=inventory.run_inventory(self.c.state)
  preview=self.engine.preview(report['id'],[r['id'] for r in report['candidates']])
  with patch('windows_recycle.native_operation',return_value={'ok':False}):result=self.engine.execute(preview['id'],ACK)
  from safe_cleanup import read_json
  file=self.engine.plans/(preview['id']+'.json');plan=read_json(file)
  plan['entries'].append(dict(plan['entries'][-1],path=str(self.base/'unconfirmed.bin')))
  write_json(file,plan)
  with self.assertRaises(SafetyError):self.engine.resume(result['id'],ACK)
  self.engine.restore(result['id'])

 def test_missing_per_file_journal_is_reported_without_moving_anything(self):
  with patch('windows_recycle.native_operation',return_value={'ok':False}):result=self.engine.execute(self.preview()['id'],ACK)
  folder,manifest=self.engine.get(result['id'])
  path=folder/'entry-records'/(manifest['entries'][0]['id']+'.json');missing=path.with_suffix('.missing')
  path.rename(missing)
  with self.assertRaises(SafetyError):self.engine.restore(result['id'])
  self.assertTrue(self.engine.history()[0]['corrupt'])
  missing.rename(path);self.engine.restore(result['id']);self.assertEqual(self.file.read_bytes(),self.data)

 def test_v2_manifest_still_restores(self):
  with patch('windows_recycle.native_operation',return_value={'ok':False}):result=self.engine.execute(self.preview()['id'],ACK)
  folder,manifest=self.engine.get(result['id']);manifest['version']=2
  self.engine.save(folder,manifest)
  FileCleanup(self.c.state).restore(result['id']);self.assertEqual(self.file.read_bytes(),self.data)

 @unittest.skipUnless(os.environ.get('CLEANUP_LARGE_FILE_TEST')=='1','Explicit disposable 1GiB integration test')
 def test_real_gib_file_streaming_recycle_restore_bounded_memory(self):
  import tracemalloc
  file=self.base/'large generated fixture.bin'
  with file.open('wb') as stream:stream.seek(1024**3);stream.write(b'x')
  stamp=file.stat()
  entry={'path':str(file),'scope_root':str(self.base),'task_id':'test','task_title':'GB disposable fixture',
         'origin':'this test creates a disposable generated file','purpose':'streaming regression','reason':'test only',
         'impact':'test fixture only','category':'test_output','keep':False,
         'stamp':{'ino':stamp.st_ino,'dev':stamp.st_dev,'size':stamp.st_size,'mtime_ns':stamp.st_mtime_ns}}
  write_json(self.c.state/'artifact-registry.json',{'artifacts':[entry]})
  report=inventory.run_inventory(self.c.state);row=next(r for r in report['candidates'] if r['path']==str(file))
  events=[]
  tracemalloc.start()
  try:
   preview=self.engine.preview(report['id'],[row['id']],lambda *args,**kwargs:events.append(kwargs))
   result=self.engine.execute(preview['id'],ACK);self.assertFalse(result['failed'],result)
   self.engine.restore(result['id'])
   peak=tracemalloc.get_traced_memory()[1]
  finally:tracemalloc.stop()
  self.assertLess(peak,12*1024**2)
  self.assertTrue(any(event.get('hash_bytes',0)>128*1024**2 for event in events))
  self.assertEqual(file.stat().st_size,1024**3+1)
  with file.open('rb') as stream:stream.seek(-1,2);self.assertEqual(stream.read(),b'x')
  print('1GiB streaming Python peak bytes',peak)

 @unittest.skipUnless(os.environ.get('CLEANUP_CROSS_VOLUME_ROOT'),'Dedicated second-volume fixture root required')
 def test_real_multi_volume_batch_recycle_restart_restore(self):
  with tempfile.TemporaryDirectory(prefix='清理 cross volume ',dir=os.environ['CLEANUP_CROSS_VOLUME_ROOT']) as folder:
   root=Path(folder)/'.codex';other=root/self.file.relative_to(self.root)
   other.parent.mkdir(parents=True);other.write_bytes(b'disposable second volume')
   self.assertNotEqual(root.stat().st_dev,self.c.state.stat().st_dev)
   with patch.object(inventory,'roots',return_value=[self.root,root]):
    report=inventory.run_inventory(self.c.state)
    preview=self.engine.preview(report['id'],[r['id'] for r in report['candidates']])
    result=self.engine.execute(preview['id'],ACK);self.assertFalse(result['failed'],result)
    FileCleanup(self.c.state).restore(result['id'])
   self.assertEqual(self.file.read_bytes(),self.data)
   self.assertEqual(other.read_bytes(),b'disposable second volume')

if __name__=='__main__':unittest.main(verbosity=2)
