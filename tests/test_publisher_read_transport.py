"""Synthetic read-only Dropbox and workspace bridge integration checks."""
import hashlib
import http.client
import io
import json
import socket
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from app.dropbox_auth import AuthAttempt, OAuthError, token_transport
from app.provider import ReadOnlyAdapter, request
from app.publisher_candidate import build_candidate
from app.publisher_read_transport import PublisherReadTransport, PublisherReadTransportError
from app.publisher_workspace_server import load_verified_workspace
from app.publisher_workspace_reader import WorkspaceSnapshotError
from app.server import make_server
from tests.test_publisher_candidate import parent_fixture
from tests.test_publisher_workspace_reader import image


class FakeResponse:
    def __init__(self, status, payload=b'', *, header=None):
        self.status = status
        self.payload = io.BytesIO(payload)
        self.header = header

    def read(self, size=-1):
        return self.payload.read(size)

    def getheader(self, name):
        return self.header if name == 'Dropbox-API-Result' else None


class FakeConnection:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    def set_debuglevel(self, level):
        self.calls.append(('debug', level))

    def request(self, method, path, body=None, headers=None):
        self.calls.append(('request', method, path, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        pass


def trusted_read_session():
    attempt = AuthAttempt('fictional-app', 'http://127.0.0.1:54321/callback')
    state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
    attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
    body = json.dumps({'access_token': 'fictional-read-token', 'token_type': 'bearer',
                       'scope': 'files.metadata.read files.content.read', 'expires_in': 3600}).encode()
    response = FakeResponse(200, body)
    with patch('app.dropbox_auth.http.client.HTTPSConnection', return_value=FakeConnection(response, [])):
        return attempt.exchange(token_transport)


def header(path, size, rev='fictional-revision', **extra):
    return json.dumps({'path_lower': path.lower(), 'size': size, 'rev': rev, **extra})


def snapshot_transport(raw=None):
    parent, parent_raw, _ = parent_fixture()
    workspace = image() if raw is None else raw
    digest = hashlib.sha256(workspace).hexdigest()
    candidate = build_candidate(parent, parent_raw, workspace_size=len(workspace), workspace_sha256=digest)
    objects = {'/system/active-baseline.json': candidate.active_bytes,
               candidate.root + 'system/manifest.json': candidate.manifest_bytes,
               candidate.root + 'system/COMMIT.json': candidate.commit_bytes,
               candidate.root + candidate.workspace_path: workspace}
    class Fixture:
        def __init__(self):
            self.objects = objects
            self.active_reads = 0
        def control(self, path):
            if path == '/system/active-baseline.json':
                self.active_reads += 1
            return (self.objects[path], 'fictional-revision')
        def workspace(self, path, *, expected_size):
            return (self.objects[path], 'fictional-revision')
    return Fixture(), candidate


class ReadTransportTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.session = trusted_read_session()
        self.addCleanup(self.session.close)
        self.transport = PublisherReadTransport(self.session, confirmed_app_key='fictional-app',
                                                app_folder_confirmation='USER_CONFIRMED')
        self.path = '/system/active-baseline.json'

    def request(self, response):
        calls = []
        def factory(host, port, *, timeout, context):
            self.assertEqual((host, port), ('content.dropboxapi.com', 443))
            self.assertTrue(context.check_hostname)
            return FakeConnection(response, calls)
        with patch('app.publisher_read_transport.http.client.HTTPSConnection', side_effect=factory):
            result = self.transport.control(self.path)
        return result, calls

    def test_fixed_download_returns_exact_bytes_and_same_response_revision(self):
        body = b'fictional-control'
        result, calls = self.request(FakeResponse(200, body, header=header(self.path, len(body))))
        self.assertEqual(result, (body, 'fictional-revision'))
        req = next(row for row in calls if row[0] == 'request')
        self.assertEqual(req[1:4], ('POST', '/2/files/download', b''))
        self.assertEqual(json.loads(req[4]['Dropbox-API-Arg']), {'path': self.path})
        self.assertNotIn('fictional-read-token', repr(self.transport))

    def test_long_read_oauth_tokens_are_valid_without_changing_revision_limit(self):
        from app.publisher_read_transport import _dispatch, _ReadOperation
        body = b'fictional-control'
        for token_length in (1024, 1025, 2048, 8192):
            with self.subTest(token_length=token_length):
                calls = []
                response = FakeResponse(200, body, header=header(self.path, len(body)))
                with patch('app.publisher_read_transport.http.client.HTTPSConnection',
                           return_value=FakeConnection(response, calls)):
                    result = _dispatch('T' * token_length,
                                       _ReadOperation(self.path, 16 * 1024 * 1024))
                self.assertEqual(result[0], body)
                self.assertEqual(sum(call[0] == 'request' for call in calls), 1)

    def test_invalid_read_oauth_tokens_are_rejected_before_https(self):
        from app.publisher_read_transport import _dispatch, _ReadOperation
        for token in ('', 'T' * 8193, 'bad\\nvalue', 'bad\\x00value'):
            with self.subTest(length=len(token)):
                with patch('app.publisher_read_transport.http.client.HTTPSConnection',
                           side_effect=AssertionError('network called')):
                    with self.assertRaises(PublisherReadTransportError) as exc:
                        _dispatch(token, _ReadOperation(self.path, 16 * 1024 * 1024))
                self.assertEqual(exc.exception.code, 'INVALID_REQUEST')

    def test_one_read_timeout_retries_with_new_connection_and_discards_partial_bytes(self):
        class TimeoutResponse(FakeResponse):
            def read(self, size=-1):
                raise TimeoutError('fictional timeout private content')
        calls = []
        answers = [
            FakeConnection(TimeoutResponse(200, header=header(self.path, 8)), calls),
            FakeConnection(FakeResponse(200, b'complete',
                                        header=header(self.path, 8)), calls),
        ]
        with patch('app.publisher_read_transport.http.client.HTTPSConnection',
                   side_effect=answers):
            result = self.transport.control(self.path)
        self.assertEqual(result, (b'complete', 'fictional-revision'))
        self.assertEqual(sum(call[0] == 'request' for call in calls), 2)

    def test_repeated_read_timeouts_stop_bounded_without_leaking_details(self):
        class TimeoutResponse(FakeResponse):
            def read(self, size=-1):
                raise TimeoutError('fictional private content')
        calls = []
        def factory(*_args, **_kwargs):
            return FakeConnection(TimeoutResponse(200, header=header(self.path, 8)), calls)
        with patch('app.publisher_read_transport.http.client.HTTPSConnection',
                   side_effect=factory):
            with self.assertRaises(PublisherReadTransportError) as exc:
                self.transport.control(self.path)
        self.assertEqual(exc.exception.code, 'READ_TIMEOUT')
        self.assertEqual(sum(call[0] == 'request' for call in calls), 2)
        self.assertNotIn('fictional private content', str(exc.exception))

    def test_read_socket_timeout_is_sixty_seconds(self):
        body = b'fictional-control'
        observed = []
        def factory(host, port, *, timeout, context):
            observed.append(timeout)
            return FakeConnection(FakeResponse(
                200, body, header=header(self.path, len(body))), [])
        with patch('app.publisher_read_transport.http.client.HTTPSConnection',
                   side_effect=factory):
            self.assertEqual(self.transport.control(self.path)[0], body)
        self.assertEqual(observed, [60])

    def test_no_arbitrary_paths_or_fake_authorization(self):
        with patch('app.publisher_read_transport.http.client.HTTPSConnection', side_effect=AssertionError('network')):
            with self.assertRaises(PublisherReadTransportError) as exc:
                self.transport.control('/fictional-other')
            self.assertEqual(exc.exception.code, 'INVALID_REQUEST')
            with self.assertRaises(PublisherReadTransportError):
                self.transport.workspace('/snapshots/baselines/' + 'a'*64 + '/workspace/snapshot-' + 'b'*64 + '.sqlite', expected_size=None)
        self.session.close()
        with self.assertRaises(OAuthError) as exc:
            self.transport.control(self.path)
        self.assertEqual(exc.exception.code, 'AUTH_EXPIRED')

    def test_grant_key_scope_and_folder_are_required_before_connection(self):
        for key, folder in [('fictional-other', 'USER_CONFIRMED'), ('fictional-app', 'unknown')]:
            transport = PublisherReadTransport(self.session, confirmed_app_key=key, app_folder_confirmation=folder)
            with self.subTest(folder=folder), patch('app.publisher_read_transport.http.client.HTTPSConnection', side_effect=AssertionError('network')):
                with self.assertRaises(OAuthError):
                    transport.control(self.path)
        self.session.scopes = frozenset({'files.metadata.read', 'files.content.read', 'files.content.write'})
        with self.assertRaises(OAuthError) as exc:
            self.transport.control(self.path)
        self.assertEqual(exc.exception.code, 'READ_AUTH_UNVERIFIED')

    def test_mismatched_or_truncated_metadata_and_symlink_are_rejected(self):
        for response in [FakeResponse(200, b'abc', header=header('/fictional-other', 3)),
                         FakeResponse(200, b'ab', header=header(self.path, 3)),
                         FakeResponse(200, b'abc', header=header(self.path, 3, symlink_info={'target':'fictional'})),
                         FakeResponse(200, b'abc', header='{"size":3,"size":3}')]:
            with self.subTest(header=response.header):
                with self.assertRaises(PublisherReadTransportError):
                    self.request(response)

    def test_denial_revocation_and_missing_are_distinct(self):
        missing = b'{"error":{".tag":"path","path":{".tag":"not_found"}}}'
        self.assertIsNone(self.request(FakeResponse(409, missing))[0])
        with self.assertRaises(PublisherReadTransportError) as exc:
            self.request(FakeResponse(403))
        self.assertEqual(exc.exception.code, 'ACCESS_DENIED')
        with self.assertRaises(PublisherReadTransportError) as exc:
            self.request(FakeResponse(401))
        self.assertEqual(exc.exception.code, 'AUTH_REQUIRED')
        self.assertFalse(self.session.is_valid())


class WorkspaceBridgeTests(unittest.TestCase):
    def test_overlay_is_resolved_hashed_and_active_rechecked_before_reading(self):
        transport, _ = snapshot_transport()
        provider = load_verified_workspace(transport)
        self.addCleanup(provider.close)
        self.assertEqual(transport.active_reads, 3)
        snapshot = provider.read(request('capabilities'))['snapshot']
        q = request('search', dataset_id=snapshot['dataset_id'], snapshot_id=snapshot['snapshot_id'])
        self.assertEqual(provider.read(q)['status'], 'OK')

    def test_changed_active_or_bad_workspace_does_not_start_provider(self):
        transport, candidate = snapshot_transport()
        original = transport.control
        def changed(path):
            value = original(path)
            if path == '/system/active-baseline.json' and transport.active_reads == 3:
                return value[0], 'fictional-changed-revision'
            return value
        transport.control = changed
        with self.assertRaises(WorkspaceSnapshotError) as exc:
            load_verified_workspace(transport)
        self.assertEqual(exc.exception.code, 'VERSION_MISMATCH')
        transport, candidate = snapshot_transport()
        path = candidate.root + candidate.workspace_path
        transport.objects[path] = b'x' * len(transport.objects[path])
        with self.assertRaises(WorkspaceSnapshotError) as exc:
            load_verified_workspace(transport)
        self.assertEqual(exc.exception.code, 'VERSION_MISMATCH')

    def test_captured_snapshot_serves_existing_readonly_server_without_source(self):
        transport, _ = snapshot_transport()
        provider = load_verified_workspace(transport)
        server = make_server(0, mode='readonly', provider=ReadOnlyAdapter(provider.read))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
            conn.request('POST', '/api/read', body=json.dumps({'request':request('capabilities')}),
                         headers={'Content-Type':'application/json'})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
            self.assertEqual((response.status, body['status']), (200, 'OK'))
        finally:
            server.shutdown()
            server.server_close()
            provider.close()
            thread.join(3)


if __name__ == '__main__':
    unittest.main()
