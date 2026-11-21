"""Tests for Windows/Linux filesystem defaults (without a PostgreSQL server)."""
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import platform_paths
import audit
import viewdeps
from types import SimpleNamespace


class PlatformTests(unittest.TestCase):
    def test_timestamp_both_platforms(self):
        t = datetime.datetime(2026, 11, 21, 16, 45)
        with patch.object(platform_paths, 'WINDOWS', True):
            self.assertEqual(platform_paths.timestamp_part(t), '21-11-2026_16-45')
        with patch.object(platform_paths, 'WINDOWS', False):
            self.assertEqual(platform_paths.timestamp_part(t), '21-11-2026_16:45')

    def test_windows_state_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(platform_paths.os.environ, {'LOCALAPPDATA':tmp}, clear=True):
                with patch.object(platform_paths, 'WINDOWS', True):
                    self.assertEqual(platform_paths.state_root(), Path(tmp) / 'PGDDL')
                with patch.dict(platform_paths.os.environ, {'PGDDL_STATE_HOME':str(Path(tmp) / 'work')}, clear=False):
                    self.assertEqual(platform_paths.state_root(), Path(tmp) / 'work')

    def test_linux_default(self):
        with patch.dict(platform_paths.os.environ, {'XDG_STATE_HOME':'/tmp/pgddl-demo'}, clear=True):
            with patch.object(platform_paths,'WINDOWS',False):
                self.assertEqual(platform_paths.state_root(),Path('/tmp/pgddl-demo')/'pgddl')

    def test_windows_directory_sync_is_skipped(self):
        with patch.object(platform_paths, 'WINDOWS', True), patch.object(platform_paths.os, 'open') as op:
            platform_paths.sync_directory('anything')
            op.assert_not_called()

    def test_windows_report_name_and_content(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_paths, 'WINDOWS', True):
            p=Path(audit.write_report('conteúdo',tmp))
            self.assertNotIn(':',p.name)
            self.assertIn('_pgddl.html',p.name)
            self.assertEqual(p.read_text(encoding='utf-8'),'conteúdo')

    def test_windows_view_backup_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(platform_paths, 'WINDOWS', True):
            args=SimpleNamespace(backup_dir=tmp,dbname='teste',schema='public')
            step=SimpleNamespace(number=20,sql='ALTER TABLE t ALTER COLUMN c TYPE VARCHAR(40)')
            manifest=SimpleNamespace(digest='sample')
            nodes=[{'kind':'m','issues':['MATERIALIZED VIEW'],'definition':'SELECT 1','indexes':[],'populated':True}]
            path=Path(viewdeps.write_backup(None,nodes,[],args,step,manifest))
            self.assertNotIn(':',path.name)
            self.assertTrue((path/'views.json').is_file())
            self.assertFalse((path/'restore-views.sql').exists())
            self.assertTrue((path/'LEIA-ME.txt').is_file())


if __name__ == '__main__':
    unittest.main()
