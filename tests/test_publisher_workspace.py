"""Fictional SQLite files only; never open actual workspace databases."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.publisher_workspace import (WorkspaceError, _HISTORY_TABLES,
                                     _REFERENCE, _REQUIRED_META, snapshot_workspace)


def make_fixture(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA wal_autocheckpoint=0')
    conn.executescript(_REFERENCE.read_text(encoding='utf-8'))
    for name in _HISTORY_TABLES:
        conn.execute('CREATE TABLE "' + name + '" (id TEXT PRIMARY KEY)')
    meta = {k: 'fictional-value' for k in _REQUIRED_META}
    meta['format_version'] = 'UTT-INTEGRATED-WORKSPACE/0.1'
    meta['data_revision'] = '1'
    conn.executemany('INSERT INTO work_meta(key,value) VALUES (?,?)', meta.items())
    body = 'fictional body 🧭'
    digest = hashlib.sha256(body.encode('utf-8')).hexdigest()
    conn.execute('''INSERT INTO work_records(record_id,title,body,body_sha256,
                 document_role,review_status,adoption_status,decision_status)
                 VALUES (?,?,?,?,?,?,?,?)''',
                 ('fictional-record', 'fictional title', body, digest,
                  'NOTE', 'PENDING', 'UNREVIEWED', 'UNREVIEWED'))
    conn.execute('''INSERT INTO work_revisions(record_id,version,title,body,body_sha256,
                 review_status,adoption_status,decision_status)
                 VALUES (?,?,?,?,?,?,?,?)''',
                 ('fictional-record', 1, 'fictional title', body, digest,
                  'PENDING', 'UNREVIEWED', 'UNREVIEWED'))
    # The original workspace migration uses a fixed IMPORT genesis digest;
    # later events hash a canonical JSON object containing its predecessor.
    seed = hashlib.sha256(b'initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1').hexdigest()
    conn.execute('''INSERT INTO work_events(event_id,event_kind,record_id,record_version,
                 actor,authorization_basis,reason,detail_json,preceding_hash,event_hash)
                 VALUES (?,?,?,?,?,?,?,?,?,?)''',
                 ('initial-historical-b-import', 'IMPORT', None, None, 'SYSTEM',
                  'USER_DIRECTED_DB_UPGRADE',
                  'Initialize writable DB from actual historical B, not operating Dropbox approval',
                  json.dumps({'records': 1, 'legacy_source': 'B_LOCAL_REORGANIZED_PREVIEW'}, ensure_ascii=False),
                  None, seed))
    payload = {'id': 'fictional-event', 'kind': 'PROPOSAL', 'record_id': 'fictional-record',
               'version': 1, 'actor': 'ASSISTANT', 'reason': 'fictional reason', 'details': {},
               'predecessor': seed, 'at': '2026-01-01T00:00:00+00:00'}
    event_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode('utf-8')).hexdigest()
    conn.execute('''INSERT INTO work_events(event_id,event_kind,record_id,record_version,
                 actor,authorization_basis,reason,detail_json,occurred_at,preceding_hash,event_hash)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                 (payload['id'], payload['kind'], payload['record_id'], payload['version'], payload['actor'],
                  'EXPLICIT_WORKSPACE_WRITE', payload['reason'], '{}', payload['at'], seed, event_hash))
    conn.commit()
    return conn


class WorkspaceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-workspace-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'fictional-local.sqlite'
        self.writer = make_fixture(self.path)
        self.addCleanup(self.writer.close)

    def assert_code(self, expected, action):
        with self.assertRaises(WorkspaceError) as error:
            action()
        self.assertEqual(error.exception.code, expected)
        self.assertNotIn('fictional-local', str(error.exception))

    def test_backup_includes_committed_wal_and_preserves_work_history(self):
        self.assertTrue(Path(str(self.path) + '-wal').exists())
        with snapshot_workspace(self.path, directory=self.root) as snapshot:
            self.assertNotIn(str(self.path), repr(snapshot))
            self.assertTrue(snapshot.path.exists())
            for suffix in ('-wal', '-shm', '-journal'):
                self.assertFalse(Path(str(snapshot.path) + suffix).exists())
            with sqlite3.connect(snapshot.path) as conn:
                self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone(), ('delete',))
                self.assertEqual(conn.execute('SELECT body FROM work_records').fetchone()[0], 'fictional body 🧭')
                self.assertEqual(conn.execute('SELECT decision_status FROM work_revisions').fetchone()[0], 'UNREVIEWED')
                self.assertEqual(conn.execute('SELECT actor,event_kind FROM work_events ORDER BY sequence').fetchall(),
                                 [('SYSTEM', 'IMPORT'), ('ASSISTANT', 'PROPOSAL')])
            snapshot.assert_source_unchanged()
            snapshot.assert_snapshot_unchanged()
            size, digest = snapshot.describe_snapshot()
            self.assertEqual(size, snapshot.path.stat().st_size)
            self.assertEqual(digest, hashlib.sha256(snapshot.path.read_bytes()).hexdigest())
            connection = snapshot.open_readonly()
            try:
                self.assertTrue(connection.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("UPDATE work_records SET title='fictional-write'")
            finally:
                connection.close()
            snapshot_path = snapshot.path
        self.assertFalse(snapshot_path.exists())
        self.assert_code('SNAPSHOT_CLOSED', snapshot.assert_source_unchanged)
        self.assertEqual(self.writer.execute('SELECT decision_status FROM work_records').fetchone()[0], 'UNREVIEWED')

    def test_fixture_events_follow_original_import_seed_and_chain(self):
        from app.workspace_write import OriginalWorkspaceEventCodec, _EVENT_FIELDS
        rows = tuple(self.writer.execute('SELECT ' + ','.join(_EVENT_FIELDS) +
                                         ' FROM work_events ORDER BY sequence'))
        codec = OriginalWorkspaceEventCodec()
        self.assertTrue(codec.verify_history(rows))
        altered = [list(row) for row in rows]
        altered[-1][_EVENT_FIELDS.index('reason')] = 'fictional altered reason'
        self.assertFalse(codec.verify_history(tuple(tuple(row) for row in altered)))

    def test_missing_or_bad_source_does_not_create_it(self):
        absent = self.root / 'fictional-missing.sqlite'
        self.assert_code('SOURCE_MISSING', lambda: next_snapshot(absent))
        self.assertFalse(absent.exists())
        self.assert_code('INVALID_SOURCE', lambda: next_snapshot(self.root))

    def test_explicit_protected_temp_directory_is_required(self):
        self.assert_code('PROTECTED_TEMP_REQUIRED', lambda: snapshot_workspace(self.path).__enter__())

    def test_missing_history_table_stops(self):
        self.writer.execute('DROP TABLE utt_event')
        self.writer.commit()
        self.assert_code('HISTORY_TABLES_MISSING', lambda: next_snapshot(self.path))

    def test_changed_schema_or_metadata_cannot_be_accepted(self):
        self.writer.execute('DROP TRIGGER work_fts_update')
        self.writer.commit()
        self.assert_code('SCHEMA_MISMATCH', lambda: next_snapshot(self.path))
        # A separate fresh fixture tests format identity without changing schema.
        other = self.root / 'fictional-other.sqlite'
        conn = make_fixture(other)
        try:
            conn.execute("UPDATE work_meta SET value='fictional-unknown' WHERE key='format_version'")
            conn.commit()
            self.assert_code('WORKSPACE_FORMAT_MISMATCH', lambda: next_snapshot(other))
        finally:
            conn.close()

    def test_legacy_workspace_table_must_not_be_treated_as_work_schema(self):
        # The real historical UTT B dataset contains a legacy table named
        # "workspace"; LIKE 'work_%' incorrectly treats '_' as a wildcard.
        # Only literal work_ prefixed schema belongs to this check.
        self.writer.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
        self.writer.execute('CREATE TABLE workz_legacy(id TEXT PRIMARY KEY)')
        self.writer.commit()
        with snapshot_workspace(self.path, directory=self.root) as snapshot:
            snapshot.assert_source_unchanged()
            snapshot.assert_snapshot_unchanged()
            with sqlite3.connect(snapshot.path) as conn:
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='workspace'"
                ).fetchone()[0], 1)

    def test_unexpected_work_schema_object_is_rejected(self):
        self.writer.execute('CREATE TABLE work_fictional_extra(id TEXT)')
        self.writer.commit()
        self.assert_code('SCHEMA_MISMATCH', lambda: next_snapshot(self.path))

    def test_foreign_key_violation_stops_without_reporting_id(self):
        self.writer.execute('PRAGMA foreign_keys=OFF')
        self.writer.execute("INSERT INTO work_sources(evidence_id,record_id) VALUES ('fictional-evidence','fictional-absent')")
        self.writer.commit()
        self.assert_code('FOREIGN_KEY_FAILED', lambda: next_snapshot(self.path))

    def test_work_and_revision_body_hashes_must_match(self):
        self.writer.execute("UPDATE work_records SET body_sha256=?", ('0' * 64,))
        self.writer.commit()
        self.assert_code('WORK_BODY_HASH_MISMATCH', lambda: next_snapshot(self.path))
        self.writer.execute("UPDATE work_records SET body_sha256=?", (hashlib.sha256('fictional body 🧭'.encode()).hexdigest(),))
        self.writer.execute("UPDATE work_revisions SET body_sha256=?", ('1' * 64,))
        self.writer.commit()
        self.assert_code('WORK_BODY_HASH_MISMATCH', lambda: next_snapshot(self.path))

    def test_concurrent_source_change_invalidates_snapshot(self):
        with snapshot_workspace(self.path, directory=self.root) as snapshot:
            self.writer.execute("UPDATE work_records SET summary='fictional changed'")
            self.writer.commit()
            self.assert_code('SOURCE_CHANGED', snapshot.assert_source_unchanged)

    def test_changing_temporary_backup_invalidates_snapshot(self):
        with snapshot_workspace(self.path, directory=self.root) as snapshot:
            with sqlite3.connect(snapshot.path) as conn:
                conn.execute("UPDATE work_records SET summary='fictional change'")
                conn.commit()
            self.assert_code('SNAPSHOT_CHANGED', snapshot.assert_snapshot_unchanged)
            self.assert_code('SNAPSHOT_CHANGED', snapshot.describe_snapshot)
            self.assert_code('SNAPSHOT_CHANGED', snapshot.open_readonly)

    def test_source_error_never_displays_contents_or_path(self):
        self.writer.execute("UPDATE work_records SET body_sha256=?", ('0' * 64,))
        self.writer.commit()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            self.assert_code('WORK_BODY_HASH_MISMATCH', lambda: next_snapshot(self.path))
        for value in (str(self.path), 'fictional body', 'fictional-record'):
            self.assertNotIn(value, output.getvalue())


def next_snapshot(path):
    with snapshot_workspace(path, directory=Path(path).parent):
        pass


if __name__ == '__main__':
    unittest.main()
