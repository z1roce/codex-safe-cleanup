"""HTTP and browser tests use only a newly created fake Codex home."""
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from safe_cleanup import Cleaner,write_json
from scan_panel import Panel, start
import inventory


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='codex-panel-test-',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.base=Path(self.tmp.name);self.root=self.base/'.codex';self.root.mkdir()
        (self.root/'cache').mkdir();(self.root/'cache/test.log').write_bytes(b'unconfirmed cache fixture')
        (self.root/'skills').mkdir();(self.root/'skills/important.txt').write_bytes(b'keep')
        self.patch=patch.object(inventory,'roots',return_value=[self.root]);self.patch.start()
        self.env=patch.dict(os.environ,CODEX_HOME=str(self.root));self.env.start()
        c=Cleaner(self.base/'state')
        with c.locked():c.init([])
        self.panel=Panel(c.state);self.server=self.panel.server()
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/{self.panel.token}/'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=3)
        for _ in range(300):
            if not self.panel.scan_lock.locked():break
            time.sleep(.05)
        if self.panel.scan_lock.locked():raise RuntimeError('Test operation still running; fixture retained for recovery')
        for record in self.panel.file_cleanup.history():
            if record['recoverable']:self.panel.file_cleanup.restore(record['id'])
        self.env.stop();self.patch.stop();self.tmp.cleanup()

    def request(self,path,post=False,data=None,headers=None):
        h={'X-Codex-Panel':self.panel.token,'Content-Type':'application/json'} if post else {}
        h.update(headers or {})
        try:
            return urlopen(Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,
                                  method='POST' if post else 'GET',headers=h),timeout=15)
        except HTTPError as error:
            error.close()
            raise

    def operation(self,route,data):
        with self.request('api/'+route,True,data) as r:jid=json.load(r)['job']
        for _ in range(100):
            with self.request('api/jobs/'+jid) as r:j=json.load(r)
            if j['state']!='running':return j
            time.sleep(.02)
        self.fail('Fixture operation timeout')

    def test_scan_only_and_no_broad_legacy_report_on_boot(self):
        write_json(self.panel.state/'inventory-reports/legacy.json',{'items':[{'path':'C:\\unrelated'}]})
        with self.request('api/bootstrap') as r:b=json.load(r)
        self.assertIsNone(b['latest']);self.assertEqual(b['candidates'],[])
        self.assertTrue(b['cleanup_enabled']);self.assertEqual(b['roots'],[str(self.root)])
        done=self.operation('inventory-start',{})
        self.assertEqual(done['state'],'done');self.assertEqual(done['result']['scope_version'],inventory.SCOPE_VERSION)
        self.assertEqual(done['result']['hash_bytes'],0)
        self.assertEqual((self.root/'skills/important.txt').read_bytes(),b'keep')

    def test_new_empty_association_report_cannot_hide_valid_project_and_artifact_results(self):
        import uuid
        project=self.base/'project';project.mkdir()
        output=project/'test-report.json';output.write_bytes(b'disposable generated fixture')
        s=output.stat()
        write_json(self.panel.state/'inventory-context.json',{'projects':[{'path':str(project),'label':'测试项目'}],
                   'threads':[{'id':'test-thread','cwd':str(project),'title':'测试任务','status':'notLoaded'}]})
        write_json(self.panel.state/'artifact-registry.json',{'artifacts':[{'path':str(output),'scope_root':str(project),
            'task_id':'test-thread','task_title':'测试任务','origin':'测试创建步骤','name':'测试报告','purpose':'生成报告测试',
            'reason':'待用户确认','impact':'仅测试文件','category':'test_output','keep':False,
            'stamp':{'ino':s.st_ino,'dev':s.st_dev,'size':s.st_size,'mtime_ns':s.st_mtime_ns}}]})
        good=self.panel.inventory()
        self.assertEqual(good['project_count'],1);self.assertEqual(good['thread_count'],1)
        self.assertEqual(len(good['task_artifacts']),1)
        bad=dict(good,id=uuid.uuid4().hex,project_count=0,thread_count=0,task_artifacts=[],metadata_signature='missing metadata')
        write_json(self.panel.state/'codex-reports'/(bad['id']+'.json'),bad)
        with self.request('api/bootstrap') as r:boot=json.load(r)
        self.assertEqual(boot['latest']['id'],good['id'])
        self.assertEqual(len(boot['latest']['task_artifacts']),1)

    def test_old_cleanup_apis_and_scope_expansion_are_rejected(self):
        for route in ('cleanup-preview','cleanup-execute','cleanup-recycle','cleanup-stored-recycle','cleanup-compress','cleanup-pause'):
            with self.assertRaises(HTTPError) as e:self.request('api/'+route,True,{})
            self.assertEqual(e.exception.code,403)
        with self.assertRaises(HTTPError) as e:self.request('api/inventory-start',True,{'root':'C:\\'})
        self.assertEqual(e.exception.code,400)
        with self.assertRaises(HTTPError) as e:self.request('api/cleanup-restore',True,{'batch':'x'})
        self.assertEqual(e.exception.code,403)

    def test_cross_origin_and_missing_token_rejected(self):
        for headers in ({'Origin':'https://example.com'},{'X-Codex-Panel':''},{'Host':'example.com'}):
            with self.assertRaises(HTTPError) as e:self.request('api/inventory-start',True,{},headers)
            self.assertEqual(e.exception.code,403)

    def test_project_filter_rejects_unregistered_paths(self):
        result=self.operation('inventory-start',{'project':str(self.base)})
        self.assertEqual(result['state'],'error')
        self.assertEqual((self.root/'skills/important.txt').read_bytes(),b'keep')

    def test_scan_cancellation_preserves_sources_and_releases_lock(self):
        original=self.panel.inventory
        def slow(scopes=False,progress=None,cancel=None,project=None,deep=False,resume=None):
            if scopes:return original(True)
            progress('测试只读扫描')
            if cancel.wait(3):raise inventory.SafetyError('本次扫描已取消')
            return original()
        self.panel.inventory=slow
        with self.request('api/inventory-start',True,{}) as r:jid=json.load(r)['job']
        with self.request('api/inventory-cancel',True,{'job':jid}) as r:self.assertTrue(json.load(r)['cancel_requested'])
        for _ in range(100):
            with self.request('api/jobs/'+jid) as r:j=json.load(r)
            if j['state']!='running':break
            time.sleep(.02)
        self.assertEqual(j['state'],'error');self.assertFalse(self.panel.scan_lock.locked())
        self.assertEqual((self.root/'cache/test.log').read_bytes(),b'unconfirmed cache fixture')

    def test_stop_does_not_change_authorization(self):
        before=self.panel.cleaner.config_file.read_bytes()
        with self.request('api/stop',True,{}) as r:self.assertTrue(json.load(r)['stopped'])
        self.assertEqual(before,self.panel.cleaner.config_file.read_bytes())

    def test_first_start_initializes_only_unapproved_tool_configuration(self):
        state=self.base/'fresh-state'
        url=start(state)
        try:
            config=json.loads((state/'config.json').read_text(encoding='utf-8'))
            self.assertTrue(config['paused']);self.assertIsNone(config['approved_policy'])
            self.assertEqual(config['roots'],[]);self.assertEqual(config['receipts'],{})
        finally:
            record=json.loads((state/'panel.json').read_text(encoding='utf-8'))
            with urlopen(Request(url+'api/stop',data=b'{}',headers={'X-Codex-Panel':record['token']},method='POST'),timeout=5) as r:
                self.assertTrue(json.load(r)['stopped'])
            time.sleep(.4)  # Let the owned child release its log handle before fixture teardown.

    @unittest.skipUnless(os.environ.get('CLEANUP_BROWSER_TEST')=='1','Optional installed Chrome test')
    def test_responsive_browser_and_repeat_scan_and_recovery(self):
        from reviewed_cleanup import ReviewedCleanup,ACK
        tid='00000000-0000-0000-0000-000000000001'
        folder=self.root/'visualizations/2026/10/01'/tid/'test-build/sdk';folder.mkdir(parents=True)
        (folder/'tool.bin').write_bytes(b'only disposable fixture')
        write_json(self.panel.state/'inventory-context.json',{'threads':[{'id':tid,'status':'notLoaded'}],
            'projects':[],'directory_explanations':[{'path':str(folder),'name':'测试旧记录'+('长内容'*40),
            'purpose':'仅用于恢复测试','task_title':'测试任务','task_id':tid,'cleanup_kind':'task_dependency_bundle'}]})
        engine=ReviewedCleanup(self.panel.state)
        plan=engine.preview([engine.candidates()[0]['id']]);engine.execute(plan['id'],ACK)
        cache=self.root/'cache/computer-use/computer-use-app-catalog'/tid;cache.mkdir(parents=True)
        (cache/'shell-apps.json').write_bytes(b'only disposable app catalog')
        (cache/'user-assist.json').write_bytes(b'only disposable usage catalog')
        cfg=self.panel.cleaner.cfg();cfg['sync_roots']=[str(cache)];self.panel.cleaner.save(cfg)
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        node=os.environ.get('CLEANUP_NODE') or (str(bundled) if bundled.is_file() else shutil.which('node'))
        self.assertTrue(node,'Browser tests require Node.js; set CLEANUP_NODE if it is not on PATH')
        r=subprocess.run([str(node),str(Path(__file__).with_name('test_panel_ui.cjs'))],
                         env=dict(os.environ,PANEL_TEST_URL=self.url),capture_output=True,text=True,encoding='utf-8',timeout=90,
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(r.returncode,0,r.stdout+'\n'+r.stderr)
        self.assertEqual((folder/'tool.bin').read_bytes(),b'only disposable fixture')
        self.assertEqual((self.root/'skills/important.txt').read_bytes(),b'keep')
        self.assertEqual((cache/'shell-apps.json').read_bytes(),b'only disposable app catalog')

    @unittest.skipUnless(os.environ.get('CLEANUP_BULK_BROWSER_TEST')=='1','Explicit 105-file native browser integration')
    def test_bulk_browser_partial_failure_resume_and_restore(self):
        import uuid
        import windows_recycle
        files=[]
        for index in range(1,106):
            file=self.root/'cache/computer-use/computer-use-app-catalog'/str(uuid.UUID(int=index))/'shell-apps.json'
            file.parent.mkdir(parents=True);file.write_bytes(b'disposable browser bulk fixture');files.append(file)
        native=windows_recycle.native_operation;calls=0
        def fail_second(mode,*args,**kwargs):
            nonlocal calls
            if mode=='recycle':
                calls+=1
                if calls==2:return {'ok':False,'error':'synthetic second-file bridge refusal'}
            return native(mode,*args,**kwargs)
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        node=os.environ.get('CLEANUP_NODE') or (str(bundled) if bundled.is_file() else shutil.which('node'))
        self.assertTrue(node,'Browser tests require Node.js; set CLEANUP_NODE if it is not on PATH')
        with patch('windows_recycle.native_operation',side_effect=fail_second):
            result=subprocess.run([str(node),str(Path(__file__).with_name('test_panel_ui.cjs'))],
                env=dict(os.environ,PANEL_TEST_URL=self.url,PANEL_TEST_BULK='1'),capture_output=True,text=True,encoding='utf-8',timeout=420)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
        self.assertTrue(all(file.read_bytes()==b'disposable browser bulk fixture' for file in files))

    @unittest.skipUnless(os.environ.get('CLEANUP_BROWSER_TEST')=='1','Optional installed Chrome test')
    def test_browser_persistent_scan_cursor_after_page_reload(self):
        import project_inventory
        project=self.base/'cursor project';project.mkdir()
        for index in range(7):(project/f'{index}.txt').write_bytes(b'preserve original')
        write_json(self.panel.state/'inventory-context.json',{'projects':[{'path':str(project)}],'threads':[]})
        bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
        node=os.environ.get('CLEANUP_NODE') or (str(bundled) if bundled.is_file() else shutil.which('node'))
        self.assertTrue(node,'Browser tests require Node.js')
        with patch.object(project_inventory,'MAX_FILES',2):
            result=subprocess.run([str(node),str(Path(__file__).with_name('test_panel_ui.cjs'))],
                env=dict(os.environ,PANEL_TEST_URL=self.url,PANEL_TEST_CURSOR='1',PANEL_TEST_PROJECT=str(project)),
                capture_output=True,text=True,encoding='utf-8',timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)


if __name__=='__main__':unittest.main(verbosity=2)
