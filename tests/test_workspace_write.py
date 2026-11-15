"""Synthetic transaction tests; these codecs are NOT the production protocol."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from app.workspace_write import EncodedEvent, WorkspaceWriteError, WorkspaceWriter
from tests.test_publisher_workspace import make_fixture


class SyntheticCodec:
    def verify_history(self, events):
        if not events or events[0][-1] != 'fictional-event-hash':
            return False
        for previous, current in zip(events, events[1:]):
            if (previous[0] >= current[0] or current[-2] != previous[-1]
                    or current[-1] != hashlib.sha256(current[8].encode()).hexdigest()):
                return False
        return True

    def encode(self, *, sequence, previous_hash, current_data_revision, operation, record_id,
               record_version, before, after, detail, reason):
        raw = json.dumps({'sequence': sequence, 'op': operation, 'detail': detail}, sort_keys=True)
        return EncodedEvent('fictional-event-' + str(sequence), operation, 'fictional-authorization',
                            reason, raw, 'fictional-time', previous_hash,
                            hashlib.sha256(raw.encode()).hexdigest(), 'fictional-revision-' + str(sequence))


class BrokenCodec(SyntheticCodec):
    def encode(self, **kwargs):
        raise ValueError('fictional-secret')


class RejectCodec(SyntheticCodec):
    def verify_history(self, events):
        return False


class WrongHashCodec(SyntheticCodec):
    def encode(self, **kwargs):
        return replace(super().encode(**kwargs), event_hash='fictional-wrong-hash')


class CommitFaultConnection(sqlite3.Connection):
    fault = None
    def commit(self):
        if self.fault == 'before':
            raise sqlite3.OperationalError('fictional-secret')
        result = super().commit()
        if self.fault == 'after':
            raise sqlite3.OperationalError('fictional-secret')
        return result


class WorkspaceWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'synthetic.sqlite'
        self.conn = make_fixture(self.path)
        self.addCleanup(self.conn.close)
        self.writer = WorkspaceWriter(self.conn, codec=SyntheticCodec())

    def command(self, op, version=1, **kw):
        return {'operation': op, 'record_id': 'fictional-record', 'expected_version': version,
                'reason': 'fictional-user-reason', **kw}

    def assert_code(self, code, action):
        with self.assertRaises(WorkspaceWriteError) as ctx:
            action()
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn('fictional-secret', str(ctx.exception))

    def test_no_original_codec_no_write(self):
        self.assert_code('VERIFIED_EVENT_CODEC_REQUIRED', lambda: WorkspaceWriter(self.conn, codec=None))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 1)

    def test_edit_revision_and_event_atomic(self):
        result = self.writer.execute(self.command('update', changes={'title': 'new fictional title', 'body': 'new fictional body'}))
        self.assertEqual((result.version, result.status), (2, 'LOCAL_SAVED'))
        row = self.conn.execute('SELECT title,body,body_sha256,decision_status,version FROM work_records').fetchone()
        self.assertEqual(row, ('new fictional title', 'new fictional body', hashlib.sha256(b'new fictional body').hexdigest(), 'UNREVIEWED', 2))
        revs = self.conn.execute('SELECT version,title FROM work_revisions ORDER BY version').fetchall()
        self.assertEqual(revs, [(1, 'fictional title'), (2, 'new fictional title')])
        events = self.conn.execute('SELECT preceding_hash,actor FROM work_events ORDER BY sequence').fetchall()
        self.assertEqual(events[-1], ('fictional-event-hash', 'USER'))
        self.assert_code('VERSION_CONFLICT', lambda: self.writer.execute(self.command('update', changes={'title': 'stale'})))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_encoder_failure_rolls_back_everything_and_redacts(self):
        bad = WorkspaceWriter(self.conn, codec=BrokenCodec())
        self.assert_code('EVENT_ENCODING_FAILED', lambda: bad.execute(self.command('update', changes={'body': 'changed'})))
        self.assertEqual(self.conn.execute('SELECT body,version FROM work_records').fetchone(), ('fictional body 🧭', 1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_revisions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 1)

    def test_archive_restore_and_new_record(self):
        self.writer.execute(self.command('archive'))
        self.assertIsNotNone(self.conn.execute('SELECT archived_at FROM work_records').fetchone()[0])
        self.writer.execute(self.command('restore', 2, restore_archive=True, restore_merge=False))
        self.assertIsNone(self.conn.execute('SELECT archived_at FROM work_records').fetchone()[0])
        self.assertIsNotNone(self.conn.execute("SELECT archived_at FROM work_revisions WHERE record_id='fictional-record' AND version=2").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT version FROM work_revisions WHERE record_id='fictional-record' ORDER BY version").fetchall(), [(1,), (2,), (3,)])
        values = {'title': 'second title', 'body': 'second body', 'summary': None, 'document_role': 'NOTE',
                  'review_status': 'PENDING', 'adoption_status': 'UNREVIEWED', 'source_kind': 'USER_CREATED'}
        self.writer.execute({'operation': 'create', 'record_id': 'second-id', 'reason': 'create', 'record': values})
        self.assertEqual(self.conn.execute("SELECT decision_status,version FROM work_records WHERE record_id='second-id'").fetchone(), ('UNREVIEWED', 1))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM work_revisions WHERE record_id='second-id'").fetchone()[0], 1)

    def test_merge_and_explicit_unmerge_keep_prior_revisions(self):
        values = {'title': 'target', 'body': 'b', 'summary': None, 'document_role': 'NOTE',
                  'review_status': 'PENDING', 'adoption_status': 'UNREVIEWED', 'source_kind': 'USER_CREATED'}
        self.writer.execute({'operation': 'create', 'record_id': 'target', 'reason': 'create', 'record': values})
        self.assert_code('TARGET_VERSION_CONFLICT', lambda: self.writer.execute(self.command('merge', target_record_id='target', target_expected_version=2)))
        self.writer.execute(self.command('merge', target_record_id='target', target_expected_version=1))
        self.assertEqual(self.conn.execute("SELECT merged_into FROM work_records WHERE record_id='fictional-record'").fetchone()[0], 'target')
        self.writer.execute(self.command('restore', 2, restore_archive=False, restore_merge=True))
        self.assertEqual(self.conn.execute("SELECT version,merged_into FROM work_records WHERE record_id='fictional-record'").fetchone(), (3, None))
        self.assertEqual(self.conn.execute("SELECT merged_into FROM work_revisions WHERE record_id='fictional-record' AND version=2").fetchone()[0], 'target')

    def test_project_link_is_preserved_on_archive_and_restore(self):
        self.conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-id','fictional project')")
        self.conn.commit()
        self.writer.execute(self.command('project_add', link_id='link-id', project_id='project-id', role='related', scope=None, basis='USER_CONFIRMED'))
        self.writer.execute(self.command('project_archive', 2, link_id='link-id'))
        self.assertIsNotNone(self.conn.execute('SELECT archived_at FROM work_record_projects').fetchone()[0])
        event = json.loads(self.conn.execute('SELECT detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0])
        self.assertIsNone(event['detail']['relation_before']['archived_at'])
        self.assertIsNotNone(event['detail']['relation_after']['archived_at'])
        self.writer.execute(self.command('project_restore', 3, link_id='link-id'))
        self.assertIsNone(self.conn.execute('SELECT archived_at FROM work_record_projects').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 4)
        self.assertEqual(self.conn.execute("SELECT version FROM work_revisions WHERE record_id='fictional-record' ORDER BY version").fetchall(), [(1,), (2,), (3,), (4,)])
        self.assert_code('RELATION_EXISTS', lambda: self.writer.execute(self.command('project_add', 4, link_id='other', project_id='project-id', role='related', scope=None, basis='USER_CONFIRMED')))

    def test_missing_current_revision_and_invalid_history_fail_closed(self):
        self.conn.execute('DELETE FROM work_revisions')
        self.conn.commit()
        self.assert_code('CURRENT_REVISION_MISSING_OR_MISMATCHED', lambda: self.writer.execute(self.command('archive')))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (1, None))

    def test_invalid_history_fails_before_change(self):
        bad = WorkspaceWriter(self.conn, codec=RejectCodec())
        self.assert_code('EVENT_HISTORY_UNVERIFIED', lambda: bad.execute(self.command('archive')))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (1, None))

    def test_new_invalid_event_cannot_commit(self):
        writer = WorkspaceWriter(self.conn, codec=WrongHashCodec())
        self.assert_code('EVENT_HISTORY_UNVERIFIED', lambda: writer.execute(self.command('archive')))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (1, None))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_revisions').fetchone()[0], 1)

    def test_commit_error_before_and_after_commit_requires_reread(self):
        conn = sqlite3.connect(self.path, factory=CommitFaultConnection)
        self.addCleanup(conn.close)
        writer = WorkspaceWriter(conn, codec=SyntheticCodec())
        conn.fault = 'before'
        self.assert_code('COMMIT_UNKNOWN', lambda: writer.execute(self.command('archive')))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (1, None))
        conn.fault = 'after'
        self.assert_code('COMMIT_UNKNOWN', lambda: writer.execute(self.command('archive')))
        saved = self.conn.execute('SELECT version,archived_at FROM work_records').fetchone()
        self.assertEqual(saved[0], 2)
        self.assertIsNotNone(saved[1])
        conn.fault = None
        self.assert_code('VERSION_CONFLICT', lambda: writer.execute(self.command('archive')))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_new_records_and_links_cannot_fabricate_inheritance_or_approval(self):
        values = {'title': 'second', 'body': 'body', 'summary': None, 'document_role': 'NOTE',
                  'review_status': 'PENDING', 'adoption_status': 'UNREVIEWED', 'source_kind': 'USER_CREATED'}
        for key, value in [('review_status', 'CONFIRMED'), ('adoption_status', 'APPROVED'),
                           ('source_kind', 'B_LOCAL_REORGANIZED_PREVIEW')]:
            record = {**values, key: value}
            self.assert_code('NEW_RECORD_PROVENANCE_INVALID', lambda: self.writer.execute(
                {'operation': 'create', 'record_id': 'second', 'reason': 'create', 'record': record}))
        self.conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-id','project')")
        self.conn.commit()
        self.assert_code('INVALID_REQUEST', lambda: self.writer.execute(self.command(
            'project_add', link_id='link-id', project_id='project-id', role='related', scope=None,
            basis='INHERITED_CLASSIFICATION_ONLY')))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_record_projects').fetchone()[0], 0)

    def test_unexpected_trigger_on_workspace_table_blocks_write(self):
        self.conn.execute('CREATE TRIGGER sneaky AFTER UPDATE ON work_records BEGIN SELECT 1; END')
        self.conn.commit()
        self.assert_code('SCHEMA_MISMATCH', lambda: self.writer.execute(self.command('archive')))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (1, None))

    def test_two_connections_enforce_optimistic_version(self):
        other = sqlite3.connect(self.path)
        self.addCleanup(other.close)
        other_writer = WorkspaceWriter(other, codec=SyntheticCodec())
        self.writer.execute(self.command('update', changes={'title': 'first change'}))
        self.assert_code('VERSION_CONFLICT', lambda: other_writer.execute(self.command('update', changes={'title': 'stale change'})))
        self.assertEqual(self.conn.execute('SELECT title,version FROM work_records').fetchone(), ('first change', 2))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_changed_event_chain_blocks_next_write(self):
        self.writer.execute(self.command('update', changes={'title': 'first change'}))
        self.conn.execute("UPDATE work_events SET detail_json='{}' WHERE sequence=2")
        self.conn.commit()
        self.assert_code('EVENT_HISTORY_UNVERIFIED', lambda: self.writer.execute(self.command('archive', 2)))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (2, None))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_simultaneous_same_version_one_wins(self):
        connections = [sqlite3.connect(self.path, check_same_thread=False, timeout=5) for _ in range(2)]
        for conn in connections:
            self.addCleanup(conn.close)
        writers = [WorkspaceWriter(conn, codec=SyntheticCodec()) for conn in connections]
        barrier = threading.Barrier(2)
        results = []
        def change(index):
            barrier.wait()
            try:
                writers[index].execute(self.command('update', changes={'title': 'update-' + str(index)}))
                results.append('LOCAL_SAVED')
            except WorkspaceWriteError as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=change, args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ['LOCAL_SAVED', 'VERSION_CONFLICT'])
        self.assertEqual(self.conn.execute('SELECT version FROM work_records').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_unrecognized_parameters_do_not_silently_change_approval(self):
        self.assert_code('INVALID_REQUEST', lambda: self.writer.execute(self.command('archive', decision_status='CONFIRMED')))
        self.assertEqual(self.conn.execute('SELECT decision_status FROM work_records').fetchone()[0], 'UNREVIEWED')


if __name__ == '__main__':
    unittest.main()
