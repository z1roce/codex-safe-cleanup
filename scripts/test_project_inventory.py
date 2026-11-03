import os,json,tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import inventory,project_inventory
from file_cleanup import FileCleanup,ACK
from safe_cleanup import Cleaner,write_json,SafetyError

class ProjectInventoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='all-projects-test-',dir=os.environ['CLEANUP_TEST_ROOT'])
  self.base=Path(self.tmp.name);self.root=self.base/'.codex';self.root.mkdir()
  self.state=self.base/'state';c=Cleaner(self.state)
  with c.locked():c.init([])
  self.patch=patch.object(inventory,'roots',return_value=[self.root]);self.patch.start()
 def tearDown(self):self.patch.stop();self.tmp.cleanup()
 def entry(self,file,keep=False):
  s=file.stat()
  return {'path':str(file),'scope_root':str(file.parent),'task_id':'00000000-0000-0000-0000-000000000001',
   'task_title':'测试任务','name':'测试生成报告','origin':'专门测试创建记录','purpose':'验证项目产物登记',
   'reason':'测试阶段结束','impact':'仅此测试文件','category':'test_output','keep':keep,
   'stamp':{'ino':s.st_ino,'dev':s.st_dev,'size':s.st_size,'mtime_ns':s.st_mtime_ns}}
 def test_all_registered_projects_and_tasks_in_scope_without_common_folders(self):
  a=self.base/'project-a';b=self.base/'project-b';a.mkdir();b.mkdir()
  (a/'Main.java').write_bytes(b'original source');(b/'notes.txt').write_bytes(b'original notes')
  ctx={'projects':[{'path':str(a),'label':'A'}],'threads':[{'id':'x','cwd':str(b),'title':'B','status':'notLoaded'}]}
  r=inventory.run_inventory(self.state,ctx)
  paths={x['path'] for x in r['project_coverage']}
  self.assertIn(str(a),paths);self.assertIn(str(b),paths)
  self.assertNotIn(str(Path.home()/'Downloads'),paths)
  self.assertTrue(all(not x['selectable'] for x in r['project_items']))
  self.assertEqual((a/'Main.java').read_bytes(),b'original source')
 def test_source_contents_not_read_and_generic_user_roots_rejected(self):
  a=self.base/'project';a.mkdir();(a/'Main.java').write_bytes(b'original')
  original=Path.open
  def guard(p,*args,**kwargs):
   if a in p.parents:raise AssertionError('Project contents opened')
   return original(p,*args,**kwargs)
  with patch.object(Path,'open',guard):r=inventory.run_inventory(self.state,{'projects':[{'path':str(a)}],'threads':[]})
  scopes,omitted=project_inventory.discover_projects({'projects':[{'path':str(Path.home())},{'path':str(Path.home().anchor)}]},[self.root])
  self.assertTrue(omitted)
  self.assertNotIn(str(Path.home()),{s['path'] for s in scopes})
 def test_registered_ended_test_output_detected_and_recovers_from_recycle_bin(self):
  a=self.base/'project';a.mkdir();file=a/'benchmark.json';data=b'{"test":"report"}';file.write_bytes(data)
  write_json(self.state/'artifact-registry.json',{'artifacts':[self.entry(file)]})
  r=inventory.run_inventory(self.state,{'projects':[{'path':str(a)}],'threads':[]})
  self.assertEqual(len(r['task_artifacts']),1);self.assertTrue(r['task_artifacts'][0]['selectable'])
  e=FileCleanup(self.state);p=e.preview(r['id'],[r['task_artifacts'][0]['id']]);done=e.execute(p['id'],ACK)
  self.assertFalse(done['failed'],done);self.assertFalse(file.exists())
  e.restore(done['id']);self.assertEqual(file.read_bytes(),data)
 def test_final_output_source_code_and_changed_identity_not_selectable(self):
  a=self.base/'project';a.mkdir();f=a/'final.json';f.write_bytes(b'final')
  self.assertFalse(inventory.inspect_artifact(self.entry(f,True))['selectable'])
  entry=self.entry(f);f.write_bytes(b'changed after declaration')
  self.assertFalse(inventory.inspect_artifact(entry)['selectable'])
  code=a/'Main.java';code.write_bytes(b'original code')
  self.assertFalse(inventory.inspect_artifact(self.entry(code))['selectable'])
  write_json(self.state/'inventory-context.json',{'preserve_paths':[{'path':str(a),'reason':'explicit keep fixture'}]})
  self.assertFalse(inventory.inspect_artifact(self.entry(f),state=self.state)['selectable'])

 def test_automatic_generated_artifacts_require_format_source_and_producer_evidence(self):
  import importlib.util,struct,zipfile
  project=self.base/'generated-project';tools=project/'tools';src=project/'src';tools.mkdir(parents=True);src.mkdir()
  source=tools/'helper.py';source.write_text('print("source fixture")',encoding='utf-8')
  cache=tools/'__pycache__/helper.cpython-312.pyc';cache.parent.mkdir();s=source.stat()
  cache.write_bytes(importlib.util.MAGIC_NUMBER+struct.pack('<III',0,int(s.st_mtime),s.st_size)+b'disposable cache fixture')
  script=tools/'package_release.py'
  script.write_text("from pathlib import Path\nroot=Path(__file__).resolve().parents[1]\ntest_output_names={'self-test.txt','input.png'}\nfor path in []:\n if path.name in test_output_names: continue\n",encoding='utf-8')
  (tools/'self-test.txt').write_bytes(b'PASS fixture');(tools/'input.png').write_bytes(b'important final preview fixture')
  (src/'Check.cs').write_text('File.WriteAllText(Path.Combine(Storage.Root,"tools","self-test.txt"),"PASS");',encoding='utf-8')
  current=project/'current.zip';old=project/'prior.zip'
  for file in (current,old):
   with zipfile.ZipFile(file,'w') as z:z.writestr('test-skill/SKILL.md','---\nname: test-skill\ndescription: fixture\n---\n')
  result={'bytecode_paths':[str(cache)],'package_scripts':[str(script)],'package_paths':[str(current),str(old)]}
  generated=project_inventory.discover_generated(result,known_artifacts=[dict(self.entry(current,True),category='superseded_package')])
  paths={x['path'] for x in generated}
  self.assertIn(str(cache),paths);self.assertIn(str(tools/'self-test.txt'),paths);self.assertIn(str(old),paths)
  self.assertNotIn(str(current),paths);self.assertNotIn(str(tools/'input.png'),paths);self.assertNotIn(str(source),paths)

if __name__=='__main__':unittest.main(verbosity=2)
