"""Synthetic-only checks of the Windows local workspace verifier."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from app.publisher_workspace import snapshot_workspace
from app.workspace_verify_windows import main, verify_snapshot
from tests.test_publisher_workspace import make_fixture


class WorkspaceVerifyWindowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-private-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'fictional-sensitive-path.sqlite'
        conn = make_fixture(self.path)
        self.addCleanup(conn.close)
        self.conn = conn

    def add_associations(self):
        c = self.conn
        c.execute("INSERT INTO work_projects(project_id,name) VALUES ('fictional-project','fictional project')")
        c.execute("INSERT INTO work_topics(topic_id,name) VALUES ('fictional-topic','fictional topic')")
        c.execute("INSERT INTO work_record_projects(link_id,record_id,project_id,role,basis) VALUES ('link','fictional-record','fictional-project','PRIMARY','CANDIDATE')")
        c.execute("INSERT INTO work_record_topics VALUES ('fictional-record','fictional-topic','INHERITED')")
        c.executemany("INSERT INTO work_sources(evidence_id,record_id,source_id,locator) VALUES (?, 'fictional-record','fictional-source',?)", [('e1','one'),('e2','two')])
        c.execute("INSERT INTO work_decisions(decision_id,record_id,decision,actor,user_authorized) VALUES ('d','fictional-record','UNRESOLVED','ASSISTANT',0)")
        body = 'fictional merged child'
        c.execute('''INSERT INTO work_records(record_id,title,body,body_sha256,document_role,review_status,adoption_status,archived_at,merged_into)
                     VALUES (?,?,?,?,?,?,?,?,?)''', ('fictional-child','fictional title',body,hashlib.sha256(body.encode()).hexdigest(),'NOTE','PENDING','UNREVIEWED','fictional-time','fictional-record'))
        c.commit()

    def test_all_records_relations_sources_and_record_history_checked_offline(self):
        self.add_associations()
        before = self.conn.execute('SELECT COUNT(*) FROM work_records').fetchone()[0]
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
            with snapshot_workspace(self.path, directory=self.root) as snapshot:
                counts = verify_snapshot(snapshot)
        self.assertEqual(counts['records'], 2)
        self.assertEqual(counts['projects'], 1)
        self.assertEqual(counts['topics'], 1)
        self.assertEqual(counts['record_project_links'], 1)
        self.assertEqual(counts['record_topic_links'], 1)
        self.assertEqual(counts['source_evidence'], 2)
        self.assertEqual(counts['record_decisions'], 1)
        self.assertEqual(counts['record_revisions'], 1)
        self.assertEqual(counts['record_events'], 1)
        self.assertEqual(counts['merged_children'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_records').fetchone()[0], before)

    def test_windows_main_prints_only_fixed_labels_and_counts(self):
        self.add_associations()
        out, err = io.StringIO(), io.StringIO()
        with patch('app.workspace_verify_windows.sys.platform', 'win32'), \
             patch('app.workspace_verify_windows._secret', side_effect=[str(self.path), str(self.root)]), \
             patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
             redirect_stdout(out), redirect_stderr(err):
            result = main()
        self.assertEqual(result, 0)
        self.assertEqual(err.getvalue(), '')
        lines = out.getvalue().splitlines()
        self.assertIn('PASS records 2', lines)
        self.assertTrue(all(line.startswith('PASS ') and line.rsplit(' ',1)[1].isdigit() for line in lines))
        for secret in ('fictional-', str(self.path), str(self.root), 'fictional-source', 'fictional project'):
            self.assertNotIn(secret, out.getvalue())

    def test_failure_never_prints_secrets_or_traceback(self):
        out, err = io.StringIO(), io.StringIO()
        with patch('app.workspace_verify_windows.sys.platform', 'win32'), \
             patch('app.workspace_verify_windows._secret', side_effect=[str(self.path), str(self.root)]), \
             patch('app.workspace_verify_windows._preflight_reader', side_effect=RuntimeError('fictional-secret-body')), \
             redirect_stdout(out), redirect_stderr(err):
            result = main()
        self.assertEqual(result, 2)
        self.assertEqual(out.getvalue(), 'FAIL\n')
        self.assertEqual(err.getvalue(), '')

    def test_non_windows_never_reads_input(self):
        out = io.StringIO()
        with patch('app.workspace_verify_windows.sys.platform', 'linux'), \
             patch('app.workspace_verify_windows._secret', side_effect=AssertionError('must not read')), \
             redirect_stdout(out):
            self.assertEqual(main(), 2)
        self.assertEqual(out.getvalue(), 'FAIL\n')


if __name__ == '__main__':
    unittest.main()
