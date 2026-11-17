"""Synthetic HTTP coverage of the separate workspace inspection boundary."""
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from app.provider import ReadOnlyAdapter, request
from app.publisher_workspace import snapshot_workspace
from app.publisher_workspace_reader import WorkspaceSnapshotProvider
from app.server import make_server
from tests.test_publisher_workspace import make_fixture


class WorkspaceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'fictional.sqlite'
        self.writer = make_fixture(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("UPDATE work_records SET archived_at='fictional-time' WHERE record_id='fictional-record'")
        body = 'fictional merged child'
        self.writer.execute('''INSERT INTO work_records(record_id,title,body,body_sha256,
             document_role,review_status,adoption_status,merged_into)
             VALUES(?,?,?,?,?,?,?,?)''', ('fictional-child', 'child', body,
             hashlib.sha256(body.encode()).hexdigest(), 'NOTE', 'PENDING', 'UNREVIEWED', 'fictional-record'))
        self.writer.execute("INSERT INTO work_projects(project_id,name,state) VALUES('fictional-project','Project',NULL)")
        self.writer.execute("INSERT INTO work_record_projects(link_id,record_id,project_id,role,scope,basis) VALUES('fictional-link','fictional-record','fictional-project','PRIMARY',NULL,'CANDIDATE')")
        self.writer.executemany("INSERT INTO work_sources VALUES(?,?,?,?,?,?)", [
            ('fictional-evidence-1', 'fictional-record', 'fictional-source', 'p.1', 'PAGE', 0),
            ('fictional-evidence-2', 'fictional-record', 'fictional-source', 'p.2', 'PAGE', 1),
        ])
        self.writer.commit()
        with snapshot_workspace(self.path, directory=self.directory) as snapshot:
            self.provider = WorkspaceSnapshotProvider(snapshot.path.read_bytes(), lifecycle_enabled=True)
        self.addCleanup(self.provider.close)
        self.snapshot = self.provider.read(request('capabilities'))['snapshot']
        self.scope = {key: self.snapshot[key] for key in ('dataset_id', 'snapshot_id')}
        self.server = make_server(0, mode='readonly', provider=self.provider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, payload=None, *, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        headers = {}
        if origin is not None:
            headers['Origin'] = origin
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            payload = json.dumps(payload).encode()
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read().decode())

    def query(self, **overrides):
        return {'contract_version': 'UTT-WORKSPACE-READ/0.1', 'operation': 'detail',
                **self.scope, 'record_id': 'fictional-record', **overrides}

    def lifecycle(self, **overrides):
        return {'contract_version': 'UTT-WORKSPACE-LIFECYCLE/0.1', **self.scope,
                'record_ids': ['fictional-record', 'fictional-child'], **overrides}

    def test_features_and_full_detail_preserve_stored_metadata(self):
        status, features = self.call('GET', '/api/workspace/features')
        self.assertEqual((status, features['contract_version'], features['read_detail'],
                          features['writes_supported']), (200, 'UTT-WORKSPACE-FEATURES/0.1', True, False))
        status, body = self.call('POST', '/api/workspace/read', self.query())
        self.assertEqual((status, body['status']), (200, 'OK'))
        self.assertEqual(body['snapshot_id'], self.scope['snapshot_id'])
        self.assertEqual(body['record']['archived_at'], 'fictional-time')
        self.assertEqual(body['merged_children'][0]['record_id'], 'fictional-child')
        self.assertEqual(body['projects'][0]['link']['basis'], 'CANDIDATE')
        self.assertIsNone(body['projects'][0]['link']['scope'])
        self.assertEqual([source['locator'] for source in body['sources']], ['p.1', 'p.2'])
        self.assertEqual(body['events'][0]['actor'], 'ASSISTANT')
        self.assertEqual(body['revisions'][0]['version'], 1)

    def test_lifecycle_and_detail_reject_stale_or_forged_snapshot(self):
        status, body = self.call('POST', '/api/workspace/lifecycle', self.lifecycle())
        self.assertEqual(status, 200)
        self.assertEqual(body['records'], [
            {'record_id': 'fictional-record', 'archived_at': 'fictional-time', 'merged_into': None},
            {'record_id': 'fictional-child', 'archived_at': None, 'merged_into': 'fictional-record'},
        ])
        for path, payload in [('/api/workspace/read', self.query(snapshot_id='stale-snapshot')),
                              ('/api/workspace/lifecycle', self.lifecycle(dataset_id='forged-dataset'))]:
            status, body = self.call('POST', path, payload)
            self.assertEqual((status, body['error']['code']), (200, 'VERSION_MISMATCH'))
        status, body = self.call('POST', '/api/workspace/read', self.query(unexpected='forged'))
        self.assertEqual((status, body['error']['code']), (200, 'INVALID_REQUEST'))
        status, body = self.call('POST', '/api/workspace/lifecycle', self.lifecycle(record_ids=['fictional-record'] * 2))
        self.assertEqual((status, body['error']['code']), (200, 'INVALID_REQUEST'))

    def test_foreign_origin_is_rejected_on_every_workspace_route(self):
        for method, path, payload in [('GET', '/api/workspace/features', None),
                                      ('POST', '/api/workspace/read', self.query()),
                                      ('POST', '/api/workspace/lifecycle', self.lifecycle())]:
            with self.subTest(path=path):
                status, body = self.call(method, path, payload, origin='https://fictional.invalid')
                self.assertEqual((status, body['error']), (403, 'ORIGIN_DENIED'))

    def test_actual_readonly_adapter_exposes_same_snapshot_features(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.server = make_server(0, mode='readonly', provider=ReadOnlyAdapter(self.provider.read))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, body = self.call('GET', '/api/workspace/features')
        self.assertEqual((status, body['read_detail']), (200, True))
        status, body = self.call('POST', '/api/workspace/read', self.query())
        self.assertEqual((status, body['status']), (200, 'OK'))
        status, body = self.call('POST', '/api/workspace/lifecycle', self.lifecycle())
        self.assertEqual((status, len(body['records'])), (200, 2))

    def test_forged_adapter_and_unrelated_provider_cannot_expose_workspace(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        # A wrapper that delegates to the right method is still not the exact
        # bound method accepted by the server's explicit workspace boundary.
        forged = ReadOnlyAdapter(lambda *args: self.provider.read(*args))
        self.server = make_server(0, mode='readonly', provider=forged)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        for method, path, payload in [('GET', '/api/workspace/features', None),
                                      ('POST', '/api/workspace/read', self.query()),
                                      ('POST', '/api/workspace/lifecycle', self.lifecycle())]:
            status, body = self.call(method, path, payload)
            self.assertEqual((status, body['error']), (404, 'NOT_SUPPORTED'))

    def test_lifecycle_opt_out_is_reported_and_cannot_be_bypassed(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        with snapshot_workspace(self.path, directory=self.directory) as snapshot:
            disabled = WorkspaceSnapshotProvider(snapshot.path.read_bytes())
        self.addCleanup(disabled.close)
        self.server = make_server(0, mode='readonly', provider=ReadOnlyAdapter(disabled.read))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, features = self.call('GET', '/api/workspace/features')
        self.assertEqual((status, features['read_detail']), (200, False))
        for path, payload in [('/api/workspace/read', self.query()),
                              ('/api/workspace/lifecycle', self.lifecycle())]:
            status, body = self.call('POST', path, payload)
            self.assertEqual((status, body['error']['code']), (200, 'UNSUPPORTED_CONTRACT'))


if __name__ == '__main__':
    unittest.main()
