"""Portable packaging guards plus opt-in official-runtime integration."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import build_portable
import install_skill
from safe_cleanup import SafetyError


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='便携 smoke ',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.source=Path(__file__).resolve().parent.parent

    def test_unverified_runtime_is_rejected_before_output_creation(self):
        archive=self.base/'unverified.zip';archive.write_bytes(b'not official Python')
        output=self.base/'output'
        with self.assertRaises(SafetyError):build_portable.build(archive,output)
        self.assertFalse(output.exists())

    def test_portable_runtime_survives_source_and_portable_upgrades(self):
        package=self.base/'portable-source';package.mkdir()
        for name,path in install_skill.distribution(self.source).items():
            target=package/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
        (package/'runtime').mkdir();(package/'runtime/python.exe').write_bytes(b'disposable binary fixture\r\n')
        for name in ('Start.cmd','Install-Skill.cmd','THIRD-PARTY-NOTICES.txt','portable-build.json'):
            (package/name).write_bytes(b'disposable package marker')
        private=package/'scripts/state/private.py';private.parent.mkdir();private.write_bytes(b'private fixture, do not distribute')
        (package/'scripts/artifact-registry.json').write_bytes(b'private fixture')
        files=install_skill.distribution(package)
        self.assertNotIn('scripts/state/private.py',files)
        self.assertNotIn('scripts/artifact-registry.json',files)
        target=self.base/'installed-skill'
        install_skill.install(package,target)
        original=(target/'runtime/python.exe').read_bytes()
        install_skill.install(self.source,target,True)
        install_skill.install(package,target,True)
        self.assertEqual((target/'runtime/python.exe').read_bytes(),original)
        (target/'runtime/python.exe').write_bytes(original.replace(b'\r\n',b'\n'))
        with self.assertRaises(SafetyError):install_skill.install(package,target,True)

    @unittest.skipUnless(os.environ.get('CLEANUP_EMBED_ZIP'),'Pinned official embedded Python archive required')
    def test_official_bundle_without_host_python_recycle_restore_and_skill_install(self):
        package=self.base/'bundle 中文 with spaces'
        result=build_portable.build(os.environ['CLEANUP_EMBED_ZIP'],package)
        self.assertTrue(Path(result['zip']).is_file())
        profile=self.base/'empty-user';profile.mkdir()
        local=self.base/'empty-local-appdata';local.mkdir()
        env=dict(os.environ,USERPROFILE=str(profile),LOCALAPPDATA=str(local),CODEX_HOME=str(profile/'.codex'),
                 PATH=str(Path(os.environ['SystemRoot'])/'System32'),PYTHONHOME=str(self.base/'not-installed'),PYTHONPATH=str(self.base/'untrusted-path'))
        env.pop('CODEX_CLEANUP_PYTHON',None)
        command=[os.environ.get('COMSPEC',r'C:\Windows\System32\cmd.exe'),'/d','/c']
        diagnose=subprocess.run(command+['Start.cmd','--diagnose','--state',str(local/'state')],cwd=package,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(diagnose.returncode,0,diagnose.stdout+diagnose.stderr)
        self.assertIn(str(package/'runtime/python.exe'),diagnose.stdout)
        code=r'''
import json,os,shutil,sys,time
from pathlib import Path
from urllib.request import Request,urlopen
import inventory,scan_panel
from file_cleanup import FileCleanup,ACK
from safe_cleanup import Cleaner,read_json
assert sys.flags.isolated==1
assert shutil.which('python') is None
assert Path(inventory.__file__).parent==Path(sys.argv[1])/'scripts'
codex=Path(os.environ['CODEX_HOME']);codex.mkdir()
file=codex/'cache/computer-use/computer-use-app-catalog/00000000-0000-0000-0000-000000000001/shell-apps.json'
file.parent.mkdir(parents=True);file.write_bytes(b'disposable embedded-runtime recycle fixture')
state=Path(os.environ['LOCALAPPDATA'])/'operation-state'
Cleaner(state).init([])
report=inventory.run_inventory(state)
engine=FileCleanup(state)
preview=engine.preview(report['id'],[report['candidates'][0]['id']])
done=engine.execute(preview['id'],ACK)
assert not done['failed'],done
engine.restore(done['id'])
assert file.read_bytes()==b'disposable embedded-runtime recycle fixture'
(codex/'.git').mkdir()
try:
    protected=inventory.run_inventory(state)
    assert not protected['candidates'],protected['candidates']
finally:(codex/'.git').rmdir()
url=scan_panel.start(state)
record=read_json(state/'panel.json')
try:
    with urlopen(url+'api/bootstrap',timeout=30) as response:data=json.load(response)
    assert not next(row for row in data['diagnostics'] if row['feature']=='Git')['available']
finally:
    with urlopen(Request(url+'api/stop',data=b'{}',headers={'X-Codex-Panel':record['token']},method='POST'),timeout=10) as response:response.read()
    time.sleep(1)
print('isolated portable runtime, no host Python, native recycle/restore and panel child passed')
'''
        runtime=package/'runtime/python.exe'
        operation=subprocess.run([str(runtime),'-X','utf8','-B','-c',code,str(package)],env=env,capture_output=True,text=True,encoding='utf-8',timeout=90)
        self.assertEqual(operation.returncode,0,operation.stdout+operation.stderr)
        installed=profile/'.agents/skills/codex-safe-cleanup'
        install=subprocess.run(command+['Install-Skill.cmd','-Destination',str(installed)],cwd=package,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(install.returncode,0,install.stdout+install.stderr)
        launch=subprocess.run(command+['Start.cmd','--diagnose','--state',str(local/'installed-state')],cwd=installed,env=env,capture_output=True,text=True,encoding='utf-8',timeout=60)
        self.assertEqual(launch.returncode,0,launch.stdout+launch.stderr)
        self.assertIn(str(installed/'runtime/python.exe'),launch.stdout)


if __name__=='__main__':unittest.main()
