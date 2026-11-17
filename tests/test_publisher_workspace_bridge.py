"""Fiction-only checks of the native workspace download and reader bridge."""
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from app.provider import request
from app.publisher_read_transport import (_ReadOperation, _dispatch,
                                          PublisherReadTransportError)
from app.publisher_reader import ACTIVE
from app.publisher_workspace_reader import WorkspaceSnapshotError
from app.publisher_workspace_server import load_verified_workspace


def fixture():
    db = sqlite3.connect(':memory:')
    db.executescript('''
      CREATE TABLE work_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE work_records(record_id TEXT PRIMARY KEY,title TEXT NOT NULL,
        body TEXT NOT NULL,body_sha256 TEXT NOT NULL,summary TEXT,
        document_role TEXT NOT NULL,review_status TEXT NOT NULL,
        archived_at TEXT,merged_into TEXT);
    ''')
    db.executemany('INSERT INTO work_meta VALUES (?,?)', [('format_version', 'UTT-INTEGRATED-WORKSPACE/0.1'),
                    ('dataset_id', 'fictional-dataset'), ('data_revision', 'fictional-version')])
    text = 'fictional text'
    db.execute('INSERT INTO work_records VALUES (?,?,?,?,?,?,?,?,?)',
               ('fictional-record', 'title', text, hashlib.sha256(text.encode()).hexdigest(),
                None, 'NOTE', 'UNREVIEWED', None, None))
    raw = db.serialize()
    db.close()
    sha = hashlib.sha256(raw).hexdigest()
    path = 'workspace/snapshot-' + sha + '.sqlite'
    encode = lambda obj: (json.dumps(obj, separators=(',', ':')) + '\n').encode()
    manifest = encode({'format': 'utt-baseline/1', 'baseline_id': 'fictional',
                       'files': [{'path': path, 'bytes': len(raw), 'sha256': sha}],
                       'utt_workspace': {'format': 'UTT-WORKSPACE-OVERLAY/0.1',
                                         'path': path, 'sha256': sha}})
    generation = hashlib.sha256(manifest).hexdigest()
    commit = encode({'format': 'utt-baseline/1', 'baseline_id': 'fictional',
                     'manifest_sha256': generation, 'state': 'complete'})
    root = '/snapshots/baselines/' + generation + '/'
    active = encode({'format': 'utt-active-baseline/1', 'generation_id': generation,
                     'manifest_sha256': generation, 'baseline_id': 'fictional', 'root': root,
                     'commit_sha256': hashlib.sha256(commit).hexdigest(), 'files': 3,
                     'bytes': len(raw) + len(manifest) + len(commit)})
    return raw, path, {ACTIVE: (active, 'fictional-rev'), root + 'system/manifest.json': (manifest, 'manifest-rev'),
                       root + 'system/COMMIT.json': (commit, 'commit-rev')}


class FakeTransport:
    def __init__(self):
        self.raw, self.path, self.controls = fixture()
        self.active_reads = 0
        self.change_on_final_read = False
    def control(self, path):
        if path == ACTIVE:
            self.active_reads += 1
            if self.change_on_final_read and self.active_reads == 3:
                return self.controls[path][0], 'changed-rev'
        return self.controls.get(path)
    def workspace(self, path, *, expected_size):
        if not path.endswith('/' + self.path) or expected_size != len(self.raw):
            raise AssertionError('wrong binding')
        return self.raw, 'workspace-rev'


class BridgeTests(unittest.TestCase):
    def test_verified_workspace_is_readable_and_controls_rechecked(self):
        transport = FakeTransport()
        provider = load_verified_workspace(transport)
        self.addCleanup(provider.close)
        self.assertEqual(transport.active_reads, 3)
        cap = provider.read(request('capabilities'))
        self.assertEqual(cap['status'], 'OK')
        result = provider.read(request('search', dataset_id=cap['snapshot']['dataset_id'],
                                       snapshot_id=cap['snapshot']['snapshot_id']))
        self.assertEqual(result['result']['records'][0]['record_id'], 'fictional-record')

    def test_changed_active_revision_or_workspace_bytes_fail(self):
        transport = FakeTransport()
        transport.change_on_final_read = True
        with self.assertRaises(WorkspaceSnapshotError) as caught:
            load_verified_workspace(transport)
        self.assertEqual(caught.exception.code, 'VERSION_MISMATCH')
        transport = FakeTransport()
        original = transport.raw
        transport.raw = original[:-1] + bytes([original[-1] ^ 1])
        with self.assertRaises(WorkspaceSnapshotError) as caught:
            load_verified_workspace(transport)
        self.assertEqual(caught.exception.code, 'VERSION_MISMATCH')

    def test_fixed_https_endpoint_metadata_and_redaction(self):
        body = b'fictional-content'
        path = '/snapshots/baselines/' + 'a' * 64 + '/system/manifest.json'
        class Response:
            status = 200
            def __init__(self): self.body = body
            def getheader(self, name):
                return json.dumps({'.tag': 'file', 'path_lower': path.lower(), 'size': len(body), 'rev': 'rev'})
            def read(self, size):
                result, self.body = self.body[:size], self.body[size:]
                return result
        class Connection:
            def __init__(self, host, port, **kwargs):
                self.host, self.port = host, port
                connections.append(self)
            def set_debuglevel(self, value): self.debug = value
            def request(self, method, route, body, headers): self.method, self.route = method, route
            def getresponse(self): return Response()
            def close(self): pass
        connections = []
        operation = _ReadOperation(path, 100)
        self.assertNotIn(path, repr(operation))
        with patch('app.publisher_read_transport.http.client.HTTPSConnection', Connection):
            self.assertEqual(_dispatch('fictional-token', operation), (body, 'rev'))
        self.assertEqual((connections[0].host, connections[0].port, connections[0].route),
                         ('content.dropboxapi.com', 443, '/2/files/download'))
        self.assertEqual(connections[0].debug, 0)

    def test_disallowed_remote_path_fails_before_network(self):
        with patch('app.publisher_read_transport.http.client.HTTPSConnection', side_effect=AssertionError('network')):
            with self.assertRaises(PublisherReadTransportError) as caught:
                _dispatch('fictional-token', _ReadOperation('/fictional-secret', 100))
        self.assertEqual(caught.exception.code, 'INVALID_REQUEST')
        self.assertNotIn('fictional', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
