"""Persistent metadata continuation on dedicated synthetic project trees."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import inventory,project_inventory,project_discovery
from safe_cleanup import Cleaner,SafetyError,read_json,write_json
from scan_panel import Panel


class ScanCursorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='中文 cursor ',dir=os.environ['CLEANUP_TEST_ROOT'])
        self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.project=self.base/'project with spaces';self.project.mkdir()
        self.codex=self.base/'codex';self.codex.mkdir()
        self.state=self.base/'state';Cleaner(self.state).init([])
        self.roots=patch.object(inventory,'roots',return_value=[self.codex]);self.roots.start();self.addCleanup(self.roots.stop)
        project_discovery.register(self.state,str(self.project),[self.codex])
        for index in range(5):(self.project/f'{index}.txt').write_bytes(b'x')
        child=self.project/'子目录';child.mkdir()
        for index in range(2):(child/f'{index}.txt').write_bytes(b'x')

    def first(self):return inventory.run_inventory(self.state,project=str(self.project))

    def test_restart_continuation_finishes_without_recounting(self):
        with patch.object(project_inventory,'MAX_FILES',2):
            report=self.first();total=report['project_file_count'];segments=1
            self.assertIsNotNone(report['scan_cursor'])
            self.assertEqual(Panel(self.state).bootstrap()['latest']['id'],report['id'])
            while report['scan_cursor']:
                previous=report['scan_cursor']
                report=inventory.run_inventory(self.state,resume=previous)
                self.assertFalse((self.state/'scan-cursors'/(previous+'.json')).exists())
                total+=report['project_file_count'];segments+=1
                self.assertLess(segments,10)
            self.assertEqual(total,7);self.assertEqual(report['project_cumulative_files'],7)
            self.assertEqual(report['candidates'],[])
            self.assertEqual(len(list(self.project.rglob('*.txt'))),7)
            self.assertEqual(list((self.state/'scan-cursors').glob('*.json')),[])

    def test_newly_registered_project_does_not_expand_existing_cursor(self):
        with patch.object(project_inventory,'MAX_FILES',2):
            report=self.first()
            other=self.base/'other';other.mkdir();(other/'original.txt').write_bytes(b'keep')
            project_discovery.register(self.state,str(other),[self.codex])
            continued=inventory.run_inventory(self.state,resume=report['scan_cursor'])
        self.assertEqual({row['path'] for row in continued['project_coverage']},{str(self.project)})

    def test_changed_directory_requires_fresh_scan(self):
        with patch.object(project_inventory,'MAX_FILES',2):report=self.first()
        (self.project/'new.txt').write_bytes(b'new original')
        with self.assertRaises(SafetyError):inventory.run_inventory(self.state,resume=report['scan_cursor'])

    def test_time_budget_during_directory_filtering_makes_forward_progress(self):
        from project_walk import ProjectWalk
        clock=0.;seen=[];original=ProjectWalk.directory
        for index in range(3):
            folder=self.project/f'dir-{index}';folder.mkdir();(folder/'source.txt').write_bytes(b'x')
        def stop_after_first(walker,name,allowed):
            nonlocal clock
            original(walker,name,allowed);seen.append(name);clock=31.
        with patch.object(project_inventory.time,'monotonic',side_effect=lambda:clock),patch.object(ProjectWalk,'directory',stop_after_first):
            report=self.first()
        self.assertIsNotNone(report['scan_cursor']);self.assertEqual(report['project_file_count'],0)
        continued=inventory.run_inventory(self.state,resume=report['scan_cursor'])
        self.assertIsNone(continued['scan_cursor']);self.assertEqual(continued['project_cumulative_files'],10)

    def test_cursor_path_tampering_and_removed_scope_are_rejected(self):
        with patch.object(project_inventory,'MAX_FILES',2):report=self.first()
        path=self.state/'scan-cursors'/(report['scan_cursor']+'.json');cursor=read_json(path)
        original=read_json(path)
        cursor['pending'][str(self.project)]['stack'][-1]['path']='../outside'
        write_json(path,cursor)
        with self.assertRaises(SafetyError):inventory.run_inventory(self.state,resume=report['scan_cursor'])
        write_json(path,original);write_json(self.state/'manual-projects.json',{'projects':[]})
        with self.assertRaises(SafetyError):inventory.run_inventory(self.state,resume=report['scan_cursor'])


if __name__=='__main__':unittest.main()
