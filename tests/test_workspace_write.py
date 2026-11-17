"""Synthetic transaction tests; these codecs are NOT the production protocol."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from app.workspace_write import EncodedEvent, WorkspaceWriteError, WorkspaceWriter, OriginalWorkspaceEventCodec
from tests.test_publisher_workspace import make_fixture


class SyntheticCodec:
    def verify_history(self, events):
        if len(events) < 2 or not OriginalWorkspaceEventCodec().verify_history(events[:2]):
            return False
        for previous, current in zip(events[1:], events[2:]):
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
                'expected_data_revision': self.conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0],
                'reason': 'fictional-user-reason', **kw}

    def assert_code(self, code, action):
        with self.assertRaises(WorkspaceWriteError) as ctx:
            action()
        self.assertEqual(ctx.exception.code, code)
        self.assertNotIn('fictional-secret', str(ctx.exception))

    def test_no_original_codec_no_write(self):
        self.assert_code('VERIFIED_EVENT_CODEC_REQUIRED', lambda: WorkspaceWriter(self.conn, codec=None))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_edit_revision_and_event_atomic(self):
        previous_hash = self.conn.execute('SELECT event_hash FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0]
        result = self.writer.execute(self.command('update', changes={'title': 'new fictional title', 'body': 'new fictional body'}))
        self.assertEqual((result.version, result.status), (2, 'LOCAL_SAVED'))
        row = self.conn.execute('SELECT title,body,body_sha256,decision_status,version FROM work_records').fetchone()
        self.assertEqual(row, ('new fictional title', 'new fictional body', hashlib.sha256(b'new fictional body').hexdigest(), 'UNREVIEWED', 2))
        revs = self.conn.execute('SELECT version,title FROM work_revisions ORDER BY version').fetchall()
        self.assertEqual(revs, [(1, 'fictional title'), (2, 'new fictional title')])
        events = self.conn.execute('SELECT preceding_hash,actor FROM work_events ORDER BY sequence').fetchall()
        self.assertEqual(events[-1], (previous_hash, 'USER'))
        self.assert_code('VERSION_CONFLICT', lambda: self.writer.execute(self.command('update', changes={'title': 'stale'})))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 3)

    def test_encoder_failure_rolls_back_everything_and_redacts(self):
        bad = WorkspaceWriter(self.conn, codec=BrokenCodec())
        self.assert_code('EVENT_ENCODING_FAILED', lambda: bad.execute(self.command('update', changes={'body': 'changed'})))
        self.assertEqual(self.conn.execute('SELECT body,version FROM work_records').fetchone(), ('fictional body 🧭', 1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_revisions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)

    def test_archive_restore_and_new_record(self):
        self.writer.execute(self.command('archive'))
        self.assertIsNotNone(self.conn.execute('SELECT archived_at FROM work_records').fetchone()[0])
        self.writer.execute(self.command('restore', 2, restore_archive=True, restore_merge=False))
        self.assertIsNone(self.conn.execute('SELECT archived_at FROM work_records').fetchone()[0])
        self.assertIsNotNone(self.conn.execute("SELECT archived_at FROM work_revisions WHERE record_id='fictional-record' AND version=2").fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT version FROM work_revisions WHERE record_id='fictional-record' ORDER BY version").fetchall(), [(1,), (2,), (3,)])
        values = {'title': 'second title', 'body': 'second body', 'summary': None, 'document_role': 'NOTE',
                  'review_status': 'PENDING_USER_REVIEW', 'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}
        new_id = 'usr-' + 'a' * 32
        self.writer.execute({'operation': 'create', 'record_id': new_id, 'reason': 'create', 'record': values,
                             'expected_data_revision': self.conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0]})
        self.assertEqual(self.conn.execute('SELECT decision_status,version FROM work_records WHERE record_id=?', (new_id,)).fetchone(), ('UNREVIEWED', 1))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_revisions WHERE record_id=?', (new_id,)).fetchone()[0], 1)

    def test_merge_and_explicit_unmerge_keep_prior_revisions(self):
        values = {'title': 'target', 'body': 'b', 'summary': None, 'document_role': 'NOTE',
                  'review_status': 'PENDING_USER_REVIEW', 'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}
        target = 'usr-' + 'b' * 32
        self.writer.execute({'operation': 'create', 'record_id': target, 'reason': 'create', 'record': values, 'expected_data_revision': '1'})
        self.assert_code('TARGET_VERSION_CONFLICT', lambda: self.writer.execute(self.command('merge', target_record_id=target, target_expected_version=2)))
        self.writer.execute(self.command('merge', target_record_id=target, target_expected_version=1))
        self.assertEqual(self.conn.execute("SELECT merged_into FROM work_records WHERE record_id='fictional-record'").fetchone()[0], target)
        self.writer.execute(self.command('restore', 2, restore_archive=False, restore_merge=True))
        self.assertEqual(self.conn.execute("SELECT version,merged_into FROM work_records WHERE record_id='fictional-record'").fetchone(), (3, None))
        self.assertEqual(self.conn.execute("SELECT merged_into FROM work_revisions WHERE record_id='fictional-record' AND version=2").fetchone()[0], target)

    def test_project_link_is_preserved_on_archive_and_restore(self):
        self.conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-id','fictional project')")
        self.conn.commit()
        self.writer.execute(self.command('project_add', link_id='link-id', project_id='project-id', role='related', scope=None, basis='USER_CONFIRMED'))
        self.writer.execute(self.command('project_archive', 1, link_id='link-id'))
        self.assertIsNotNone(self.conn.execute('SELECT archived_at FROM work_record_projects').fetchone()[0])
        event = json.loads(self.conn.execute('SELECT detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0])
        self.assertIsNone(event['detail']['relation_before']['archived_at'])
        self.assertIsNotNone(event['detail']['relation_after']['archived_at'])
        self.writer.execute(self.command('project_restore', 1, link_id='link-id'))
        self.assertIsNone(self.conn.execute('SELECT archived_at FROM work_record_projects').fetchone()[0])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT version FROM work_revisions WHERE record_id='fictional-record' ORDER BY version").fetchall(), [(1,)])
        self.assert_code('RELATION_EXISTS', lambda: self.writer.execute(self.command('project_add', 1, link_id='other', project_id='project-id', role='related', scope=None, basis='USER_CONFIRMED')))

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
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 2)
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
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 3)

    def test_new_records_and_links_cannot_fabricate_inheritance_or_approval(self):
        values = {'title': 'second', 'body': 'body', 'summary': None, 'document_role': 'NOTE',
                  'review_status': 'PENDING_USER_REVIEW', 'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}
        for key, value in [('review_status', 'CONFIRMED'), ('adoption_status', 'APPROVED'),
                           ('source_kind', 'B_LOCAL_REORGANIZED_PREVIEW')]:
            record = {**values, key: value}
            self.assert_code('NEW_RECORD_PROVENANCE_INVALID', lambda: self.writer.execute(
                {'operation': 'create', 'record_id': 'usr-' + 'c' * 32, 'reason': 'create', 'record': record, 'expected_data_revision': '1'}))
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
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 3)

    def test_changed_event_chain_blocks_next_write(self):
        self.writer.execute(self.command('update', changes={'title': 'first change'}))
        self.conn.execute("UPDATE work_events SET detail_json='{}' WHERE sequence=3")
        self.conn.commit()
        self.assert_code('EVENT_HISTORY_UNVERIFIED', lambda: self.writer.execute(self.command('archive', 2)))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (2, None))
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 3)

    def test_simultaneous_same_version_one_wins(self):
        connections = [sqlite3.connect(self.path, check_same_thread=False, timeout=5) for _ in range(2)]
        for conn in connections:
            self.addCleanup(conn.close)
        writers = [WorkspaceWriter(conn, codec=SyntheticCodec()) for conn in connections]
        barrier = threading.Barrier(2)
        results = []
        commands = [self.command('update', changes={'title': 'update-' + str(i)}) for i in range(2)]
        def change(index):
            barrier.wait()
            try:
                writers[index].execute(commands[index])
                results.append('LOCAL_SAVED')
            except WorkspaceWriteError as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=change, args=(i,)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(results, ['LOCAL_SAVED', 'DATA_REVISION_CONFLICT'])
        self.assertEqual(self.conn.execute('SELECT version FROM work_records').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], 3)

    def test_unrecognized_parameters_do_not_silently_change_approval(self):
        self.assert_code('INVALID_REQUEST', lambda: self.writer.execute(self.command('archive', decision_status='CONFIRMED')))
        self.assertEqual(self.conn.execute('SELECT decision_status FROM work_records').fetchone()[0], 'UNREVIEWED')

    def test_expected_global_revision_is_required_even_when_record_version_matches(self):
        command = self.command('archive')
        del command['expected_data_revision']
        self.assert_code('EXPECTED_DATA_REVISION_REQUIRED', lambda: self.writer.execute(command))
        self.assertEqual(self.conn.execute('SELECT version,archived_at FROM work_records').fetchone(), (1, None))

class OriginalProtocolCompatibilityTests(unittest.TestCase):
    """Fictional rows only; compare generated events with the provided source."""
    def setUp(self):
        import importlib.util
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = make_fixture(Path(self.tmp.name) / 'synthetic.sqlite')
        self.addCleanup(self.conn.close)
        source_path = Path(__file__).resolve().parents[1] / 'docs/publisher-reference/utt_workspace.py'
        spec = importlib.util.spec_from_file_location('fictional_original_workspace', source_path)
        self.original = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.original)
        self.conn.row_factory = sqlite3.Row
        self.writer = WorkspaceWriter(self.conn, codec=OriginalWorkspaceEventCodec())

    def check_original_verify(self):
        result = self.original.verify(self.conn)
        self.assertEqual(result['result'], 'PASS', result['failures'])
        self.assertEqual(result['event_chain_errors'], 0)

    def test_original_seed_null_details_unicode_and_auth_not_hash_domain(self):
        from app.workspace_write import _EVENT_FIELDS, _native_json
        self.original.event(self.conn, 'NOTE', None, None, 'SYSTEM', '가상 🧭 이유', None)
        self.conn.commit()
        self.check_original_verify()
        events = tuple(tuple(row) for row in self.conn.execute('SELECT ' + ','.join(_EVENT_FIELDS) + ' FROM work_events ORDER BY sequence'))
        self.assertTrue(OriginalWorkspaceEventCodec().verify_history(events))
        payload = {'z': None, 'a': ['한글 🧭', None, {'가': 3}]}
        self.assertEqual(_native_json(payload).encode('utf-8'), self.original.textjson(payload).encode('utf-8'))
        self.conn.execute("UPDATE work_events SET authorization_basis='changed outside hash domain' WHERE sequence=3")
        self.conn.commit()
        self.check_original_verify()
        events = tuple(tuple(row) for row in self.conn.execute('SELECT ' + ','.join(_EVENT_FIELDS) + ' FROM work_events ORDER BY sequence'))
        self.assertTrue(OriginalWorkspaceEventCodec().verify_history(events))
        self.conn.execute("UPDATE work_events SET detail_json='{}' WHERE sequence=3")
        self.conn.commit()
        events = tuple(tuple(row) for row in self.conn.execute('SELECT ' + ','.join(_EVENT_FIELDS) + ' FROM work_events ORDER BY sequence'))
        self.assertFalse(OriginalWorkspaceEventCodec().verify_history(events))

    def test_generated_edit_exact_original_payload_and_undo(self):
        result = self.writer.execute({'operation': 'update', 'record_id': 'fictional-record',
            'expected_version': 1, 'expected_data_revision': '1', 'reason': '가상 편집',
            'changes': {'title': '가상 제목', 'body': '새 🧭 본문'}})
        self.assertEqual(result.version, 2)
        self.assertEqual(self.conn.row_factory, sqlite3.Row)
        event = self.conn.execute('SELECT * FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
        self.assertEqual((event['event_kind'], event['record_version']), ('EDIT', 2))
        detail = {'fields': ['body', 'title']}
        self.assertEqual(event['detail_json'], self.original.textjson(detail))
        payload = {'id': event['event_id'], 'kind': event['event_kind'], 'record_id': event['record_id'],
                   'version': event['record_version'], 'actor': event['actor'], 'reason': event['reason'],
                   'details': detail, 'predecessor': event['preceding_hash'], 'at': event['occurred_at']}
        self.assertEqual(event['event_hash'], self.original.sha(self.original.textjson(payload)))
        self.assertEqual(self.conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0], '2')
        self.writer.execute({'operation': 'restore_revision', 'record_id': 'fictional-record',
            'expected_version': 2, 'expected_data_revision': '2', 'reason': '가상 복원', 'restore_version': 1})
        restored = self.conn.execute("SELECT body,version FROM work_records WHERE record_id='fictional-record'").fetchone()
        self.assertEqual(tuple(restored), ('fictional body 🧭', 3))
        self.assertEqual(self.conn.execute('SELECT event_kind FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0], 'RESTORE_REVISION')
        self.check_original_verify()

    def test_project_link_original_version_and_global_conflict(self):
        self.conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-id','project')")
        self.conn.commit()
        command = {'operation': 'project_add', 'record_id': 'fictional-record', 'expected_version': 1,
                   'expected_data_revision': '1', 'reason': 'direct user action', 'link_id': 'link-fictional',
                   'project_id': 'project-id', 'role': 'RELATED', 'scope': None, 'basis': 'USER_CONFIRMED'}
        result = self.writer.execute(command)
        self.assertEqual(result.version, 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM work_revisions WHERE record_id='fictional-record'").fetchone()[0], 1)
        event = self.conn.execute('SELECT event_kind,record_version,detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
        self.assertEqual((event['event_kind'], event['record_version']), ('PROJECT_LINK', None))
        self.assertEqual(json.loads(event['detail_json']), {'project_id': 'project-id', 'link_id': 'link-fictional', 'basis': 'USER_CONFIRMED'})
        self.assertEqual(self.conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0], '2')
        with self.assertRaises(WorkspaceWriteError) as ctx:
            self.writer.execute({'operation': 'archive', 'record_id': 'fictional-record', 'expected_version': 1,
                                 'expected_data_revision': '1', 'reason': 'stale snapshot'})
        self.assertEqual(ctx.exception.code, 'DATA_REVISION_CONFLICT')
        self.check_original_verify()

    def test_merge_unmerge_and_extended_project_events_verify_in_original(self):
        target = 'usr-' + 'd' * 32
        record = {'title': 'merge target', 'body': 'fictional target', 'summary': None, 'document_role': 'USER_NOTE',
                  'review_status': 'PENDING_USER_REVIEW', 'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}
        self.writer.execute({'operation': 'create', 'record_id': target, 'reason': 'create', 'record': record,
                             'expected_data_revision': '1'})
        self.writer.execute({'operation': 'merge', 'record_id': 'fictional-record', 'reason': 'merge',
                             'expected_version': 1, 'target_record_id': target, 'target_expected_version': 1,
                             'expected_data_revision': '2'})
        merged = self.conn.execute("SELECT merged_into,archived_at FROM work_records WHERE record_id='fictional-record'").fetchone()
        self.assertEqual(merged['merged_into'], target)
        self.assertIsNotNone(merged['archived_at'])
        self.writer.execute({'operation': 'restore', 'record_id': 'fictional-record', 'reason': 'unmerge',
                             'expected_version': 2, 'expected_data_revision': '3', 'restore_merge': True,
                             'restore_archive': False})
        restored = self.conn.execute("SELECT merged_into,archived_at FROM work_records WHERE record_id='fictional-record'").fetchone()
        self.assertEqual(tuple(restored), (None, None))
        self.assertEqual(self.conn.execute('SELECT event_kind FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0], 'UNMERGE')
        self.check_original_verify()
        self.conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-id','project')")
        self.conn.commit()
        base = {'record_id': 'fictional-record', 'reason': 'direct action', 'expected_version': 3}
        self.writer.execute({**base, 'operation': 'project_add', 'expected_data_revision': '4', 'link_id': 'link-fixture',
                             'project_id': 'project-id', 'role': 'RELATED', 'scope': None, 'basis': 'USER_CONFIRMED'})
        self.writer.execute({**base, 'operation': 'project_archive', 'expected_data_revision': '5', 'link_id': 'link-fixture'})
        row = self.conn.execute('SELECT event_kind,record_version,detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
        self.assertEqual((row['event_kind'], row['record_version']), ('PROJECT_ARCHIVE', None))
        details = json.loads(row['detail_json'])
        self.assertEqual(details['before']['link_id'], 'link-fixture')
        self.assertIsNone(details['before']['archived_at'])
        self.assertIsNotNone(details['after']['archived_at'])
        self.assertEqual(self.conn.execute("SELECT version FROM work_records WHERE record_id='fictional-record'").fetchone()[0], 3)
        self.check_original_verify()

    def test_topic_and_tag_add_remove_preserve_event_rows(self):
        self.conn.execute("INSERT INTO work_topics(topic_id,name) VALUES ('topic-fictional','topic')")
        self.conn.commit()
        base = {'record_id': 'fictional-record', 'reason': 'direct action', 'expected_version': 1}
        self.writer.execute({**base, 'operation': 'tag_add', 'expected_data_revision': '1',
                             'tag_id': 'tag-fictional', 'tag_name': '가상 태그'})
        event = self.conn.execute('SELECT event_kind,record_version,detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
        self.assertEqual((event['event_kind'], event['record_version'], json.loads(event['detail_json'])),
                         ('TAG', None, {'tag_id': 'tag-fictional'}))
        self.writer.execute({**base, 'operation': 'tag_remove', 'expected_data_revision': '2', 'tag_id': 'tag-fictional'})
        detail = json.loads(self.conn.execute('SELECT detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0])
        self.assertEqual(detail['before'], {'record_id': 'fictional-record', 'tag_id': 'tag-fictional'})
        self.assertIsNone(detail['after'])
        self.writer.execute({**base, 'operation': 'topic_add', 'expected_data_revision': '3',
                             'topic_id': 'topic-fictional', 'status': 'CANDIDATE'})
        self.writer.execute({**base, 'operation': 'topic_remove', 'expected_data_revision': '4', 'topic_id': 'topic-fictional'})
        detail = json.loads(self.conn.execute('SELECT detail_json FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()[0])
        self.assertEqual(detail['before'], {'record_id': 'fictional-record', 'topic_id': 'topic-fictional', 'status': 'CANDIDATE'})
        self.assertIsNone(detail['after'])
        self.assertEqual(self.conn.execute("SELECT version FROM work_records WHERE record_id='fictional-record'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0], '5')
        self.check_original_verify()

    def test_restore_old_merge_refuses_current_target_cycle(self):
        target = 'usr-' + 'e' * 32
        record = {'title': 'merge target', 'body': 'fictional target', 'summary': None, 'document_role': 'USER_NOTE',
                  'review_status': 'PENDING_USER_REVIEW', 'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}
        self.writer.execute({'operation': 'create', 'record_id': target, 'reason': 'create', 'record': record,
                             'expected_data_revision': '1'})
        self.writer.execute({'operation': 'merge', 'record_id': 'fictional-record', 'reason': 'merge A to B',
                             'expected_version': 1, 'target_record_id': target, 'target_expected_version': 1,
                             'expected_data_revision': '2'})
        self.writer.execute({'operation': 'restore', 'record_id': 'fictional-record', 'reason': 'unmerge',
                             'expected_version': 2, 'expected_data_revision': '3', 'restore_merge': True,
                             'restore_archive': False})
        self.writer.execute({'operation': 'merge', 'record_id': target, 'reason': 'merge B to A',
                             'expected_version': 1, 'target_record_id': 'fictional-record', 'target_expected_version': 3,
                             'expected_data_revision': '4'})
        before = self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0]
        with self.assertRaises(WorkspaceWriteError) as ctx:
            self.writer.execute({'operation': 'restore_revision', 'record_id': 'fictional-record',
                                 'expected_version': 3, 'expected_data_revision': '5',
                                 'reason': 'restore old revision', 'restore_version': 2})
        self.assertEqual(ctx.exception.code, 'INVALID_MERGE_TARGET')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM work_events').fetchone()[0], before)
        self.check_original_verify()


if __name__ == '__main__':
    unittest.main()
