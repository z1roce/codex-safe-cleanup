"""Synthetic installer, interpreter selection and capability regression tests."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import diagnostics
import install_skill
from safe_cleanup import SafetyError


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='安装 upgrade 中文 ',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.source=Path(__file__).resolve().parent.parent

    def test_install_upgrade_preserves_config_and_records_license(self):
        target=self.base/'skill with spaces'
        install_skill.install(self.source,target)
        (target/'user-config.json').write_bytes(b'preserve custom config')
        (target/'recovery-record.json').write_bytes(b'preserve recovery records')
        result=install_skill.install(self.source,target,True)
        self.assertTrue((target/'LICENSE').is_file())
        self.assertEqual((target/'user-config.json').read_bytes(),b'preserve custom config')
        self.assertEqual((target/'recovery-record.json').read_bytes(),b'preserve recovery records')
        self.assertTrue(Path(result['previous_installation']).is_dir())

    def test_install_rejects_modified_managed_files(self):
        target=self.base/'skill'
        install_skill.install(self.source,target)
        (target/'SKILL.md').write_bytes(b'user edited instructions')
        with self.assertRaises(SafetyError):install_skill.install(self.source,target,True)
        self.assertEqual((target/'SKILL.md').read_bytes(),b'user edited instructions')

    def test_legacy_install_is_recognized_and_unknown_files_preserved(self):
        import shutil
        target=self.base/'legacy';target.mkdir()
        shutil.copy2(self.source/'LICENSE',target/'LICENSE')
        legacy=(self.source/'SKILL.md').read_text(encoding='utf-8').split('## Windows 可移植性与升级')[0].rstrip()+'\n'
        (target/'SKILL.md').write_text(legacy,encoding='utf-8')
        (target/'user-config.json').write_bytes(b'legacy user config')
        install_skill.install(self.source,target,True)
        self.assertEqual((target/'user-config.json').read_bytes(),b'legacy user config')
        unknown=self.base/'unrelated-directory';unknown.mkdir()
        (unknown/'original.txt').write_bytes(b'original user data')
        with self.assertRaises(SafetyError):install_skill.install(self.source,unknown,True)
        self.assertEqual((unknown/'original.txt').read_bytes(),b'original user data')

    def test_failed_upgrade_switch_rolls_back(self):
        target=self.base/'skill'
        install_skill.install(self.source,target)
        original=(target/'SKILL.md').read_bytes()
        rename=os.rename
        def fail_stage(source,destination):
            if '.install-stage-' in str(source):raise PermissionError('synthetic switch failure')
            return rename(source,destination)
        with patch('install_skill.os.rename',side_effect=fail_stage):
            with self.assertRaises(PermissionError):install_skill.install(self.source,target,True)
        self.assertEqual((target/'SKILL.md').read_bytes(),original)
        self.assertTrue(target.with_name('skill.install-transaction.json').exists())
        install_skill.recover_install(target)
        self.assertFalse(target.with_name('skill.install-transaction.json').exists())
        install_skill.install(self.source,target,True)

    def test_interrupted_install_recovers_missing_target(self):
        from safe_cleanup import write_json
        target=self.base/'skill'
        install_skill.install(self.source,target)
        backup=target.with_name('skill.install-backup-'+'0'*32)
        os.rename(target,backup)
        write_json(target.with_name('skill.install-transaction.json'),
                   {'target':str(target),'backup':str(backup),'phase':'switching'})
        install_skill.recover_install(target)
        self.assertTrue((target/'SKILL.md').is_file())

    def powershell(self,script,env=None):
        shell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        preamble="$ErrorActionPreference='Stop'; . '"+str(self.source/'scripts/python-launcher.ps1').replace("'","''")+"'; "
        return subprocess.run([str(shell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-Command',preamble+script],
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30,env=env)

    def test_launcher_skips_broken_and_old_candidates(self):
        broken=self.base/'broken.exe';broken.write_bytes(b'not an executable')
        candidates=[str(broken)]
        old=Path(r'C:\Program Files (x86)\Python35-32\python.exe')
        if old.exists():candidates.append(str(old))
        candidates.append(sys.executable)
        args=','.join("'"+p.replace("'","''")+"'" for p in candidates)
        env=dict(os.environ);env.pop('CODEX_CLEANUP_PYTHON',None)
        result=self.powershell('Find-CleanupPython -Candidates @('+args+')',env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn(sys.executable,result.stdout)

    def test_explicit_invalid_interpreter_is_clear(self):
        result=self.powershell('Find-CleanupPython',dict(os.environ,CODEX_CLEANUP_PYTHON=str(self.base/'missing.exe')))
        self.assertNotEqual(result.returncode,0)
        self.assertIn('CODEX_CLEANUP_PYTHON',result.stderr)

    def test_git_missing_diagnostic_keeps_readonly_capabilities(self):
        with patch('diagnostics.shutil.which',return_value=None),patch('windows_recycle.diagnose',return_value=False):
            result={r['feature']:r for r in diagnostics.diagnose(self.base/'state')}
        self.assertFalse(result['Git']['available'])
        self.assertTrue(result['状态目录']['available'])
        self.assertFalse(result['Windows 回收站桥接']['available'])


if __name__=='__main__':unittest.main()
