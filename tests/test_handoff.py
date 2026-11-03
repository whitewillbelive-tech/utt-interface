"""Synthetic filenames and temporary code copies only; never inspect real data."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from tools.verify_handoff import ROOT, verify_original, verify_worktree, forbidden_filename, audit

class HandoffTests(unittest.TestCase):
    def test_original_complete_and_current_changes_explained(self):
        original,manifest=verify_original(ROOT/'provenance/handoff-v1')
        self.assertTrue(original['ok']);self.assertEqual(original['verified'],len(manifest['files']))
        policy=json.loads((ROOT/'docs/HANDOFF_WORKTREE_POLICY.json').read_text())
        current=verify_worktree(ROOT,manifest,policy);self.assertTrue(current['ok'])
        self.assertEqual(current['counts'].get('intentional_change'),len(policy['intentional_changes']))
        self.assertEqual((ROOT/'.env.example').read_bytes(),(ROOT/'provenance/handoff-v1/.env.example').read_bytes())
    def test_missing_or_unexplained_worktree_changes_fail(self):
        _,manifest=verify_original(ROOT/'provenance/handoff-v1')
        policy=json.loads((ROOT/'docs/HANDOFF_WORKTREE_POLICY.json').read_text())
        with tempfile.TemporaryDirectory(prefix='utt-handoff-test-') as tmp:
            root=Path(tmp);shutil.copytree(ROOT/'provenance/handoff-v1',root,dirs_exist_ok=True)
            for p in root.rglob('*'):
                if p.is_file():p.chmod(0o644)
            self.assertTrue(verify_worktree(root,manifest,policy)['ok'])
            (root/'.env.example').unlink()
            (root/'AGENTS.md').write_text('synthetic tamper')
            report=verify_worktree(root,manifest,policy);self.assertFalse(report['ok'])
            self.assertEqual(report['counts']['missing'],1);self.assertEqual(report['counts']['unexplained_change'],1)
    def test_original_manifest_or_file_tamper_never_silently_passes(self):
        with tempfile.TemporaryDirectory(prefix='utt-handoff-test-') as tmp:
            root=Path(tmp);shutil.copytree(ROOT/'provenance/handoff-v1',root,dirs_exist_ok=True)
            p=root/'.gitignore';p.chmod(0o644);p.write_text('synthetic tamper')
            self.assertFalse(verify_original(root)[0]['ok'])
            p=root/'HANDOFF_MANIFEST.json';p.chmod(0o644);p.write_text('{}')
            self.assertIsNone(verify_original(root)[1])
    def test_ignore_rules_use_fictional_filenames_including_tracked_bypass_check(self):
        blocked=['fiction.sqlite','fiction.sqlite3','fiction.db','fiction.db-wal','fiction.zip','fiction.tar','fiction.gz',
                 'fiction.bak','fiction.log','fiction.pem','fiction.key','.env','.env.synthetic-test',
                 'cache/fiction.json','backups/fiction.json','exports/fiction.json','local-data/fiction.json',
                 'artifacts/fiction.png','.venv/fiction.py','node_modules/fiction.js','__pycache__/fiction.pyc']
        result=subprocess.run(['git','-C',str(ROOT),'check-ignore','--no-index','--stdin'],input='\n'.join(blocked)+'\n',text=True,capture_output=True,check=True)
        self.assertEqual(set(result.stdout.splitlines()),set(blocked))
        # Filename inspection still rejects tracked files, independent of Git ignore behavior.
        self.assertTrue(all(forbidden_filename(n) for n in blocked))
        self.assertFalse(forbidden_filename('.env.example'))
        self.assertEqual(subprocess.run(['git','-C',str(ROOT),'check-ignore','--no-index','.env.example'],capture_output=True).returncode,1)
        self.assertEqual(audit()['tracked_filename_check']['failures'],[])
    def test_reference_excluded_from_export_and_test_discovery(self):
        attr=subprocess.run(['git','-C',str(ROOT),'check-attr','export-ignore','--','provenance'],capture_output=True,text=True,check=True)
        self.assertIn('export-ignore: set',attr.stdout)
        self.assertIn('provenance/',(ROOT/'.dockerignore').read_text().splitlines())
        # Supported commands restrict discovery to active tests, not the repo root.
        python_tests=list((ROOT/'tests').glob('test*.py'))
        node_tests=list((ROOT/'source/tests').glob('*.test.mjs'))
        self.assertTrue(python_tests and node_tests)
        self.assertTrue(all('provenance' not in p.parts for p in python_tests+node_tests))
        from app.server import STATIC
        self.assertTrue(all('provenance' not in name for name,_ in STATIC.values()))

if __name__=='__main__':unittest.main()
