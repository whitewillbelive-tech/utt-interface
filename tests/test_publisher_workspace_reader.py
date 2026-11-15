"""Workspace reader checks using fictional, in-memory SQLite images only."""
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.provider import ReadOnlyAdapter, request
from app.features import features_for
from app.publisher_workspace_reader import WorkspaceSnapshotError, WorkspaceSnapshotProvider
from app.server import make_server
from app.validation import validate_response


def image(rows=None, *, format_version='UTT-INTEGRATED-WORKSPACE/0.1', dataset='fictional-workspace'):
    db = sqlite3.connect(':memory:')
    db.executescript('''
        CREATE TABLE work_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE work_records(record_id TEXT PRIMARY KEY, title TEXT NOT NULL,
          body TEXT NOT NULL, body_sha256 TEXT NOT NULL, summary TEXT,
          document_role TEXT NOT NULL, review_status TEXT NOT NULL,
          archived_at TEXT, merged_into TEXT);
    ''')
    db.executemany('INSERT INTO work_meta VALUES (?,?)', [('format_version', format_version),
                   ('dataset_id', dataset), ('data_revision', 'fictional-revision')])
    if rows is None:
        rows = [
            ('fictional-001', '첫 노트', '앞 🧭 동쪽', None, 'NOTE', 'UNREVIEWED', None, None),
            ('fictional-002', '100%_경로', '본문 두번째', '요약', 'NOTE', 'HELD', None, None),
            ('fictional-003', '셋째', '공백   문자', None, 'NOTE', 'UNREVIEWED', None, None),
            ('fictional-004', '넷째', '본문 네번째', None, 'NOTE', 'UNREVIEWED', None, None),
        ]
    for ident, title, body, summary, role, review, archived, merged in rows:
        digest = hashlib.sha256(body.encode()).hexdigest()
        db.execute('INSERT INTO work_records VALUES (?,?,?,?,?,?,?,?,?)',
                   (ident, title, body, digest, summary, role, review, archived, merged))
    value = db.serialize()
    db.close()
    return value


class WorkspaceReaderTests(unittest.TestCase):
    def setUp(self):
        self.raw = image()
        self.reader = WorkspaceSnapshotProvider(self.raw)
        self.addCleanup(self.reader.close)
        self.adapter = ReadOnlyAdapter(self.reader.read)
        cap = self.adapter.read(request('capabilities'))
        self.assertEqual(cap['status'], 'OK')
        self.snapshot = cap['snapshot']
        self.scope = {'dataset_id': self.snapshot['dataset_id'], 'snapshot_id': self.snapshot['snapshot_id']}

    def fetch(self, operation, *, sort='id_asc', **args):
        q = request(operation, **self.scope, **args)
        result = self.adapter.read(q, sort)
        validate_response(result, q, sort)
        return result

    def test_read_contract_end_to_end_list_pages_detail_and_unknown_fields(self):
        page = self.fetch('search', page_size=2)
        self.assertEqual(page['status'], 'OK')
        records = page['result']['records']
        self.assertEqual([x['record_id'] for x in records], ['fictional-001', 'fictional-002'])
        self.assertTrue(all(x['raw_text'] is None and x['content_availability'] == 'NOT_REQUESTED' for x in records))
        cursor = page['result']['page']['next_cursor']
        page2 = self.fetch('search', page_size=2, cursor=cursor)
        self.assertEqual([x['record_id'] for x in page2['result']['records']], ['fictional-003', 'fictional-004'])
        self.assertIsNone(page2['result']['page']['next_cursor'])
        detail = self.fetch('detail', record_id='fictional-001')['result']['record']
        self.assertEqual(detail['raw_text'], '앞 🧭 동쪽')
        self.assertEqual(detail['raw_text_sha256'], hashlib.sha256('앞 🧭 동쪽'.encode()).hexdigest())
        self.assertEqual(detail['source_status'], 'UNKNOWN')
        self.assertEqual(detail['project_relations_status'], 'UNKNOWN')
        self.assertIsNone(detail['state']['adoption'])
        self.assertEqual(detail['source_refs'], [])
        self.assertEqual(detail['original_ids'], [])
        self.assertEqual(self.fetch('projects')['error']['code'], 'UNSUPPORTED_CONTRACT')
        self.assertEqual(self.fetch('related_history', record_id='fictional-001')['error']['code'], 'UNSUPPORTED_CONTRACT')

    def test_search_preserves_sqlite_like_wildcard_and_whitespace_semantics(self):
        def ids(text):
            return [r['record_id'] for r in self.fetch('search', query=text)['result']['records']]
        self.assertEqual(ids('100%_'), ['fictional-002'])
        self.assertEqual(ids('%'), ['fictional-002'])
        self.assertEqual(ids('   '), ['fictional-003'])
        self.assertEqual(ids('fictional-004'), ['fictional-004'])
        self.assertEqual(len(ids('')), 3)
        self.assertEqual(ids('100__'), [])

    def test_cursors_are_opaque_context_bound_and_snapshot_scoped(self):
        first = self.fetch('search', page_size=2, query='fictional')
        token = first['result']['page']['next_cursor']
        self.assertNotIn('fictional', token)
        self.assertEqual(self.fetch('search', page_size=2, query='different', cursor=token)['error']['code'], 'VERSION_MISMATCH')
        self.assertEqual(self.fetch('search', page_size=2, query='fictional', cursor='x' * 43)['error']['code'], 'VERSION_MISMATCH')
        self.assertEqual(self.fetch('search', page_size=2, query='fictional', cursor=token, sort='id_desc')['error']['code'], 'VERSION_MISMATCH')
        other = WorkspaceSnapshotProvider(self.raw)
        try:
            q = request('search', **self.scope, page_size=2, query='fictional', cursor=token)
            self.assertEqual(other.read(q)['error']['code'], 'VERSION_MISMATCH')
        finally:
            other.close()
        stale = request('search', dataset_id=self.scope['dataset_id'], snapshot_id='fictional-other')
        self.assertEqual(self.reader.read(stale)['error']['code'], 'VERSION_MISMATCH')

    def test_unsupported_filters_are_never_silently_ignored(self):
        for field in ('project_id', 'document_role', 'review_status'):
            with self.subTest(field=field):
                self.assertEqual(self.fetch('search', **{field: 'fictional-value'})['error']['code'], 'INVALID_REQUEST')

    def test_hash_mismatch_and_archived_or_merged_records_fail_closed(self):
        bad = sqlite3.connect(':memory:')
        bad.deserialize(self.raw)
        bad.execute("UPDATE work_records SET body='fictional altered' WHERE record_id='fictional-001'")
        bad.commit()
        altered = WorkspaceSnapshotProvider(bad.serialize())
        try:
            cap = altered.read(request('capabilities'))['snapshot']
            q = request('detail', dataset_id=cap['dataset_id'], snapshot_id=cap['snapshot_id'], record_id='fictional-001')
            self.assertEqual(altered.read(q)['error']['code'], 'VERSION_MISMATCH')
        finally:
            altered.close()
            bad.close()
        for archived, merged in [('fictional-date', None), (None, 'fictional-other')]:
            with self.subTest(archived=archived, merged=merged):
                fixture = image([('fictional-archived', '제목', '본문', None, 'NOTE', 'UNREVIEWED', archived, merged)])
                r = WorkspaceSnapshotProvider(fixture)
                try:
                    cap = r.read(request('capabilities'))['snapshot']
                    q = request('search', dataset_id=cap['dataset_id'], snapshot_id=cap['snapshot_id'])
                    self.assertEqual(r.read(q)['error']['code'], 'UNSUPPORTED_CONTRACT')
                finally:
                    r.close()

    def test_opt_in_lifecycle_preserves_archive_and_merge_state_on_same_snapshot(self):
        raw = image([
            ('fictional-active', '제목', '본문', None, 'NOTE', 'UNREVIEWED', None, None),
            ('fictional-archived', '보관', '보관 본문', None, 'NOTE', 'UNREVIEWED', 'fictional-time', None),
            ('fictional-merged', '합병', '합병 본문', None, 'NOTE', 'UNREVIEWED', None, 'fictional-active'),
        ])
        reader = WorkspaceSnapshotProvider(raw, lifecycle_enabled=True)
        try:
            snapshot = reader.read(request('capabilities'))['snapshot']
            scope = {key: snapshot[key] for key in ('dataset_id', 'snapshot_id')}
            page = reader.read(request('search', **scope, page_size=3))
            self.assertEqual(page['status'], 'OK')
            ids = [row['record_id'] for row in page['result']['records']]
            self.assertEqual(ids, ['fictional-active', 'fictional-archived', 'fictional-merged'])
            for ident in ids:
                self.assertEqual(reader.read(request('detail', **scope, record_id=ident))['status'], 'OK')
            lifecycle = reader.lifecycle(**scope, record_ids=ids)
            self.assertEqual(lifecycle['contract_version'], 'UTT-WORKSPACE-LIFECYCLE/0.1')
            self.assertEqual(lifecycle['snapshot'], snapshot)
            self.assertEqual(lifecycle['records'], [
                {'record_id': 'fictional-active', 'archived_at': None, 'merged_into': None},
                {'record_id': 'fictional-archived', 'archived_at': 'fictional-time', 'merged_into': None},
                {'record_id': 'fictional-merged', 'archived_at': None, 'merged_into': 'fictional-active'},
            ])
            for ids, scope_override, code in [
                (['fictional-active'], {'snapshot_id': 'different'}, 'VERSION_MISMATCH'),
                (['missing'], {}, 'ITEM_NOT_FOUND'),
                (['fictional-active', 'fictional-active'], {}, 'INVALID_REQUEST'),
                ([''] , {}, 'INVALID_REQUEST'),
                (['fictional-active'] * 101, {}, 'INVALID_REQUEST'),
            ]:
                with self.subTest(code=code):
                    with self.assertRaises(WorkspaceSnapshotError) as exc:
                        reader.lifecycle(**{**scope, **scope_override}, record_ids=ids)
                    self.assertEqual(exc.exception.code, code)
        finally:
            reader.close()

    def test_lifecycle_is_never_implicitly_enabled(self):
        with self.assertRaises(WorkspaceSnapshotError) as exc:
            self.reader.lifecycle(**self.scope, record_ids=['fictional-001'])
        self.assertEqual(exc.exception.code, 'UNSUPPORTED_CONTRACT')
        with self.assertRaises(WorkspaceSnapshotError):
            WorkspaceSnapshotProvider(self.raw, lifecycle_enabled='yes')

    def test_extended_detail_uses_the_same_snapshot_and_preserves_record_state(self):
        from app.publisher_workspace import snapshot_workspace
        from tests.test_publisher_workspace import make_fixture

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'fictional.sqlite'
            writer = make_fixture(path)
            try:
                writer.execute("UPDATE work_records SET archived_at='fictional-time'")
                writer.executemany('INSERT INTO work_projects(project_id,name,state) VALUES (?,?,?)',
                                   [('fictional-p1', '가상 프로젝트', 'OPEN'),
                                    ('fictional-p2', '가상 프로젝트 2', None)])
                writer.execute("INSERT INTO work_topics(topic_id,name) VALUES ('fictional-topic','가상 주제')")
                writer.execute("INSERT INTO work_record_projects(link_id,record_id,project_id,role,basis,archived_at) "
                               "VALUES ('fictional-link','fictional-record','fictional-p1','MEMBER','CANDIDATE','fictional-date')")
                writer.execute("INSERT INTO work_record_topics(record_id,topic_id,status) "
                               "VALUES ('fictional-record','fictional-topic','INHERITED')")
                writer.commit()
                with snapshot_workspace(path, directory=temp) as snapshot:
                    reader = WorkspaceSnapshotProvider(snapshot.path.read_bytes(), lifecycle_enabled=True)
                try:
                    self.assertTrue(reader.workspace_features()['read_detail'])
                    scope = reader.read(request('capabilities'))['snapshot']
                    q = {'contract_version': 'UTT-WORKSPACE-READ/0.1', 'operation': 'detail',
                         'dataset_id': scope['dataset_id'], 'snapshot_id': scope['snapshot_id'],
                         'record_id': 'fictional-record'}
                    actual = reader.read_workspace_detail(q)
                    self.assertEqual(actual['status'], 'OK')
                    self.assertEqual(actual['contract_version'], 'UTT-WORKSPACE-DETAIL/0.1')
                    self.assertEqual(actual['record']['archived_at'], 'fictional-time')
                    self.assertIsNone(actual['record']['merged_into'])
                    self.assertEqual(actual['snapshot_id'], scope['snapshot_id'])
                    self.assertEqual(len(actual['revisions']), 1)
                    self.assertEqual(len(actual['events']), 1)
                    stale = reader.read_workspace_detail({**q, 'snapshot_id': 'fictional-stale'})
                    self.assertEqual(stale['error']['code'], 'VERSION_MISMATCH')
                    self.assertEqual(reader.read_workspace_detail({**q, 'other': 1})['error']['code'],
                                     'INVALID_REQUEST')
                    self.assertEqual(reader.read_workspace_detail({**q, 'record_id': 'fictional-missing'})
                                     ['error']['code'], 'ITEM_NOT_FOUND')
                    catalog = {'contract_version': 'UTT-WORKSPACE-READ/0.1', 'operation': 'projects',
                               'dataset_id': scope['dataset_id'], 'snapshot_id': scope['snapshot_id'],
                               'limit': 1, 'offset': 0}
                    first = reader.workspace_catalog(catalog)
                    self.assertEqual(first['status'], 'OK')
                    self.assertEqual(first['items'][0]['project_id'], 'fictional-p1')
                    self.assertEqual(first['next_offset'], 1)
                    second = reader.workspace_catalog({**catalog, 'offset': first['next_offset']})
                    self.assertEqual(second['items'][0]['project_id'], 'fictional-p2')
                    self.assertIsNone(second['next_offset'])
                    topics = reader.workspace_catalog({**catalog, 'operation': 'topics', 'limit': 10})
                    self.assertEqual(topics['items'][0]['topic_id'], 'fictional-topic')
                    members = reader.workspace_catalog({**catalog, 'operation': 'project_members',
                                                        'identifier': 'fictional-p1'})
                    self.assertEqual(members['items'][0]['link']['basis'], 'CANDIDATE')
                    self.assertEqual(members['items'][0]['link']['archived_at'], 'fictional-date')
                    self.assertEqual(members['items'][0]['record']['archived_at'], 'fictional-time')
                    members = reader.workspace_catalog({**catalog, 'operation': 'topic_members',
                                                        'identifier': 'fictional-topic'})
                    self.assertEqual(members['items'][0]['link']['status'], 'INHERITED')
                    self.assertEqual(members['items'][0]['record']['archived_at'], 'fictional-time')
                    self.assertEqual(reader.workspace_catalog({**catalog, 'snapshot_id': 'wrong'})
                                     ['error']['code'], 'VERSION_MISMATCH')
                finally:
                    reader.close()
                self.assertFalse(reader.workspace_features()['read_detail'])
            finally:
                writer.close()

    def test_standalone_capture_never_falls_back_to_wal_or_files(self):
        with patch('builtins.open', side_effect=AssertionError('filesystem forbidden')):
            copy = WorkspaceSnapshotProvider(self.raw)
            try:
                self.assertEqual(copy.read(request('capabilities'))['status'], 'OK')
            finally:
                copy.close()
        wal = bytearray(self.raw)
        wal[18:20] = b'\x02\x02'
        for bad in (bytes(wal), b'not sqlite', image(format_version='fictional-unknown')):
            with self.subTest(length=len(bad)):
                with self.assertRaises(WorkspaceSnapshotError):
                    WorkspaceSnapshotProvider(bad)
        with self.assertRaises(WorkspaceSnapshotError):
            WorkspaceSnapshotProvider(self.raw, max_bytes=1024)

    def test_close_is_idempotent_and_refuses_further_reads(self):
        self.reader.close()
        self.reader.close()
        self.assertEqual(self.reader.read(request('capabilities'))['error']['code'], 'CONNECTION_UNAVAILABLE')

    def test_workspace_handoff_schema_is_readable(self):
        sql = (Path(__file__).resolve().parents[1] / 'docs/publisher-reference/WORKSPACE_SCHEMA.sql').read_text(encoding='utf-8')
        db = sqlite3.connect(':memory:')
        try:
            db.executescript(sql)
            db.executemany('INSERT INTO work_meta VALUES (?,?)', [
                ('format_version', 'UTT-INTEGRATED-WORKSPACE/0.1'), ('dataset_id', 'fictional')])
            body = '가상 본문'
            db.execute('INSERT INTO work_records(record_id,title,body,body_sha256,document_role,review_status,adoption_status) VALUES (?,?,?,?,?,?,?)',
                       ('fictional-id', '가상 제목', body, hashlib.sha256(body.encode()).hexdigest(), 'NOTE', 'UNREVIEWED', 'UNKNOWN'))
            db.commit()
            reader = WorkspaceSnapshotProvider(db.serialize())
            try:
                cap = reader.read(request('capabilities'))['snapshot']
                response = reader.read(request('detail', dataset_id=cap['dataset_id'],
                                               snapshot_id=cap['snapshot_id'], record_id='fictional-id'))
                self.assertEqual(response['status'], 'OK')
                self.assertEqual(response['result']['record']['raw_text'], body)
            finally:
                reader.close()
        finally:
            db.close()

    def test_primary_key_is_required_for_stable_cursor_order(self):
        db = sqlite3.connect(':memory:')
        try:
            db.executescript('''
                CREATE TABLE work_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE work_records(record_id TEXT, title TEXT, body TEXT, body_sha256 TEXT,
                  summary TEXT, document_role TEXT, review_status TEXT, archived_at TEXT, merged_into TEXT);
                INSERT INTO work_meta VALUES ('format_version', 'UTT-INTEGRATED-WORKSPACE/0.1');
                INSERT INTO work_meta VALUES ('dataset_id', 'fictional');
            ''')
            with self.assertRaises(WorkspaceSnapshotError):
                WorkspaceSnapshotProvider(db.serialize())
        finally:
            db.close()

    def test_feature_declaration_only_trusts_exact_bound_provider(self):
        expected = {'contract_version': 'UTT-READ-FEATURES/0.1.0', 'filters': {
            'query': 'SUPPORTED', 'project': 'UNSUPPORTED', 'document_role': 'UNSUPPORTED',
            'review_status': 'UNSUPPORTED', 'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'}}
        self.assertEqual(features_for(self.reader), expected)
        self.assertEqual(features_for(self.adapter), expected)
        # Wrappers and subclasses must not claim support using attributes alone.
        wrapped = ReadOnlyAdapter(lambda q, sort: self.reader.read(q, sort))
        self.assertEqual(features_for(wrapped)['filters']['query'], 'UNKNOWN')
        class Derived(WorkspaceSnapshotProvider):
            pass
        derived = Derived(self.raw)
        try:
            self.assertEqual(features_for(derived)['filters']['query'], 'UNKNOWN')
            self.assertEqual(features_for(ReadOnlyAdapter(derived.read))['filters']['query'], 'UNKNOWN')
        finally:
            derived.close()

    def test_loopback_server_ui_read_flow(self):
        server = make_server(0, mode='readonly', provider=self.adapter)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def send(method, path, payload=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            try:
                body = json.dumps(payload).encode('utf-8') if payload is not None else None
                conn.request(method, path, body=body, headers={'Content-Type': 'application/json', **(headers or {})})
                reply = conn.getresponse()
                return reply.status, reply.read(), dict(reply.getheaders())
            finally:
                conn.close()
        def read(q):
            status, raw, _ = send('POST', '/api/read', {'request': q})
            result = json.loads(raw)
            self.assertEqual(status, 200)
            validate_response(result, q, 'id_asc')
            return result
        try:
            status, html, headers = send('GET', '/')
            self.assertEqual(status, 200)
            self.assertIn(b'<html', html.lower())
            self.assertEqual(headers.get('Cache-Control'), 'no-store')
            status, raw, _ = send('GET', '/api/features')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw), features_for(self.adapter))
            status, raw, _ = send('GET', '/api/mode')
            self.assertEqual(json.loads(raw), {'mode': 'readonly', 'simulation_namespace': None})
            cap = read(request('capabilities'))
            scope = {key: cap['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
            first = read(request('search', **scope, query='fictional', page_size=2))
            second = read(request('search', **scope, query='fictional', page_size=2,
                                  cursor=first['result']['page']['next_cursor']))
            self.assertEqual([r['record_id'] for r in first['result']['records'] + second['result']['records']],
                             ['fictional-001', 'fictional-002', 'fictional-003', 'fictional-004'])
            detail = read(request('detail', **scope, record_id='fictional-001'))
            self.assertEqual(detail['result']['record']['raw_text'], '앞 🧭 동쪽')
            self.assertEqual(detail['result']['record']['source_status'], 'UNKNOWN')
            denied = read(request('search', **scope, review_status='fictional'))
            self.assertEqual(denied['error']['code'], 'INVALID_REQUEST')
            status, _, _ = send('GET', '/api/features', headers={'Origin': 'https://untrusted.invalid'})
            self.assertEqual(status, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
