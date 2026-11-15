"""Workspace reader checks using fictional, in-memory SQLite images only."""
import hashlib
import http.client
import json
import sqlite3
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
