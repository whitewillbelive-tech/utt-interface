"""Fictional Dropbox publisher transport tests; never use network or user data."""
import hashlib
import io
import json
import socket
import unittest
from unittest.mock import patch

from app import publisher_transport as p


MANIFEST = b'fictional-manifest'
COMMIT = b'fictional-commit'
GENERATION = hashlib.sha256(MANIFEST).hexdigest()
GEN = '/snapshots/baselines/' + GENERATION + '/'
FILE = GEN + 'snapshots/tables.json'


def metadata(path, size=0, rev='fictional-rev', content_hash=None):
    data = {'path_lower': path.lower(), 'size': size, 'rev': rev, 'id': 'fictional-id', 'name': 'fictional'}
    if content_hash is not None:
        data['content_hash'] = content_hash
    return data


def payload(value):
    return json.dumps(value, separators=(',', ':')).encode('utf-8')


class Session:
    def is_valid(self):
        return True

    def _publisher_call(self, callback, operation, *, confirmed_app_key, app_folder_confirmation):
        assert callback is p._dispatch
        assert confirmed_app_key == 'fictional-app-key'
        assert app_folder_confirmation == 'USER_CONFIRMED'
        return callback('fictional-token', operation)


class Response:
    def __init__(self, status=200, body=b'', headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, count):
        return self.body.read(count)

    def getheader(self, name):
        return self.headers.get(name)


class Connections:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def make(self, host, port, *, timeout, context):
        self.assertion = (host, port, timeout, context.check_hostname, context.verify_mode)
        outer = self

        class Connection:
            def set_debuglevel(self, value):
                assert value == 0
            def request(self, method, route, *, body, headers):
                outer.calls.append((host, method, route, len(body), dict(headers)))
                if outer.responses and isinstance(outer.responses[0], Exception):
                    raise outer.responses.pop(0)
            def getresponse(self):
                return outer.responses.pop(0)
            def close(self):
                pass
        return Connection()


def missing():
    return Response(409, payload({'error': {'.tag': 'path', 'path': {'.tag': 'not_found'}}}))


class PublisherTransportTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.session = Session()
        self.transport = p.PublisherTransport(self.session, confirmed_app_key='fictional-app-key',
                                              app_folder_confirmation='USER_CONFIRMED')

    def bind(self, data=b'', *, digest=None, previous=b'old', previous_rev='old-rev'):
        sha = digest or hashlib.sha256(data).hexdigest()
        commit_digest = hashlib.sha256(COMMIT).hexdigest()
        files = {FILE: (len(data), sha), GEN + 'system/manifest.json': (len(MANIFEST), GENERATION),
                 GEN + 'system/COMMIT.json': (len(COMMIT), commit_digest)}
        active = {'format': 'utt-active-baseline/1', 'generation_id': GENERATION,
                  'manifest_sha256': GENERATION, 'baseline_id': 'fictional-baseline',
                  'root': GEN, 'commit_sha256': commit_digest,
                  'files': len(files), 'bytes': sum(v[0] for v in files.values())}
        encoded = (json.dumps(active, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()
        permit = p._mint_write_permit(self.session, files=files, active_bytes=encoded,
                                      previous_rev=previous_rev,
                                      previous_sha256=hashlib.sha256(previous).hexdigest())
        self.transport.bind_permit(permit)
        return encoded

    def verified_catalog(self, data=b''):
        content = {FILE: data, GEN + 'system/manifest.json': MANIFEST, GEN + 'system/COMMIT.json': COMMIT}
        return [Response(200, value, {'Dropbox-API-Result': json.dumps(metadata(path, len(value)))})
                for path, value in sorted(content.items())]

    def call(self, responses, callback):
        f = Connections(responses)
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=f.make):
            result = callback()
        return result, f

    def test_paths_reject_escape_scrivener_and_non_native_root(self):
        for path in ('/outside/file', '/system/other', GEN + '../other', GEN + 'scrivener/file',
                     GEN + 'name%2fsecret', GEN + 'a//b', 'https://attacker.invalid/x'):
            with self.subTest(path=path), self.assertRaises(p.PublisherTransportError) as cm:
                self.transport.metadata(path)
            self.assertEqual(cm.exception.code, 'PATH_INVALID')

    def test_metadata_missing_is_distinct_from_empty_or_malformed_success(self):
        result, fake = self.call([missing()], lambda: self.transport.metadata(FILE))
        self.assertIsNone(result)
        self.assertEqual(fake.calls[0][0:3], ('api.dropboxapi.com', 'POST', '/2/files/get_metadata'))
        for body in (b'', b'{}', b'{"size": 1, "size": 2}'):
            with self.subTest(body=body), self.assertRaises(p.PublisherTransportError):
                self.call([Response(200, body)], lambda: self.transport.metadata(FILE))

    def test_native_mixed_case_commit_path_uses_dropbox_lowercase_metadata(self):
        path = GEN + 'system/COMMIT.json'
        result, fake = self.call([Response(200, payload(metadata(path, len(COMMIT))))],
                                 lambda: self.transport.metadata(path))
        self.assertEqual(result.size, len(COMMIT))
        self.assertEqual(json.loads(fake.calls[0][4].get('Dropbox-API-Arg', '{}')), {})

    def test_valid_oauth_token_length_is_independent_of_revision_limit(self):
        # Auth exchange accepts printable ASCII tokens up to 8192 bytes.
        # Before this fix, the transport used the <=1024-character REV regex.
        body = b'fictional-active-content'
        md = metadata(p.ACTIVE, len(body))
        for length in (1024, 1025, 2048, 8192):
            with self.subTest(length=length):
                sink = io.BytesIO()
                fake = Connections([Response(
                    200, body, {'Dropbox-API-Result': json.dumps(md)})])
                with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make):
                    result = p._dispatch('T' * length, p._Operation(
                        'download', p.ACTIVE, stream=sink, size=len(body)))
                self.assertEqual(result.size, len(body))
                self.assertEqual(sink.getvalue(), body)
                self.assertEqual(len(fake.calls), 1)

    def test_invalid_oauth_token_never_starts_a_request(self):
        for bad in ('', 'T' * 8193, 'bad\nvalue', 'bad\x00value'):
            with self.subTest(length=len(bad)):
                with patch.object(p.http.client, 'HTTPSConnection',
                                  side_effect=AssertionError('network attempted')):
                    with self.assertRaises(p.PublisherTransportError) as err:
                        p._dispatch(bad, p._Operation(
                            'download', p.ACTIVE, stream=io.BytesIO(), size=256))
                self.assertEqual(err.exception.code, 'REQUEST_INVALID')

    def test_read_socket_timeout_is_longer_than_uncertain_write_timeout(self):
        # Exercise only the mocked HTTP boundary; do not simulate a real
        # upload plan or start any network traffic.
        reader = Connections([Response(200, b'fictional-active')])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=reader.make):
            connection, response = p._request(
                'fictional-token', 'content.dropboxapi.com', '/2/files/download',
                {'path': p.ACTIVE}, b'', streaming=True)
            self.assertEqual(response.status, 200)
            connection.close()
        self.assertEqual(reader.assertion[2], 60)

        writer = Connections([Response(200, payload({'session_id': 'fictional'}))])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=writer.make):
            connection, result = p._request(
                'fictional-token', 'content.dropboxapi.com', '/2/files/upload_session/start',
                {'close': False}, b'fictional')
        self.assertIsNone(connection)
        self.assertEqual(result['session_id'], 'fictional')
        self.assertEqual(writer.assertion[2], 30)

    def test_download_revision_is_from_same_response_as_bytes(self):
        body = b'fictional-active-content'
        md = metadata(p.ACTIVE, len(body), rev='same-response-rev')
        sink = io.BytesIO()
        result, fake = self.call([Response(200, body, {'Dropbox-API-Result': json.dumps(md)})],
                                 lambda: self.transport.download(p.ACTIVE, sink))
        self.assertEqual(sink.getvalue(), body)
        self.assertEqual(result.rev, 'same-response-rev')
        self.assertEqual(result.sha256, hashlib.sha256(body).hexdigest())
        self.assertNotIn('same-response-rev', repr(result))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][0:3], ('content.dropboxapi.com', 'POST', '/2/files/download'))

    def test_download_rejects_different_path_size_and_missing_header(self):
        body = b'fictional'
        for hdr in (None, json.dumps(metadata(GEN + 'other', len(body))),
                    json.dumps(metadata(FILE, len(body) + 1))):
            with self.subTest(header=hdr), self.assertRaises(p.PublisherTransportError):
                self.call([Response(200, body, {} if hdr is None else {'Dropbox-API-Result': hdr})],
                          lambda: self.transport.download(FILE, io.BytesIO()))

    def test_active_update_is_strict_revision_cas_and_never_retries(self):
        active = self.bind()
        previous = Response(200, b'old', {'Dropbox-API-Result': json.dumps(metadata(p.ACTIVE, 3, 'old-rev'))})
        result, fake = self.call(self.verified_catalog() + [previous, Response(200, payload(metadata(p.ACTIVE, len(active), 'new-rev')))],
                                 lambda: self.transport.update_if_revision(p.ACTIVE, active, 'old-rev'))
        arg = json.loads(fake.calls[-1][4]['Dropbox-API-Arg'])
        self.assertEqual(arg['mode'], {'.tag': 'update', 'update': 'old-rev'})
        self.assertIs(arg['autorename'], False)
        self.assertIs(arg['strict_conflict'], True)
        self.assertEqual(result.rev, 'new-rev')
        previous = Response(200, b'old', {'Dropbox-API-Result': json.dumps(metadata(p.ACTIVE, 3, 'old-rev'))})
        fake = Connections(self.verified_catalog() + [previous, Response(409, b'{}')])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.update_if_revision(p.ACTIVE, active, 'old-rev')
        self.assertEqual(cm.exception.code, 'CONFLICT')
        self.assertEqual(len(fake.calls), 5)

    def test_lost_or_invalid_success_for_write_is_uncertain(self):
        active = self.bind()
        for response in (OSError('fictional-path fictional-token'), Response(200, b'not-json'), Response(200, b'{}')):
            previous = Response(200, b'old', {'Dropbox-API-Result': json.dumps(metadata(p.ACTIVE, 3, 'old-rev'))})
            fake = Connections(self.verified_catalog() + [previous, response])
            with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
                self.transport.update_if_revision(p.ACTIVE, active, 'old-rev')
            self.assertEqual(cm.exception.code, 'WRITE_UNCERTAIN')
            self.assertNotIn('fictional-token', str(cm.exception))
            self.assertEqual(len(fake.calls), 5)

    def test_large_upload_chunks_add_only_and_validates_dropbox_hash(self):
        size = 151 * 1024 * 1024
        block = b'\0' * (4 * 1024 * 1024)
        outer = hashlib.sha256()
        for _ in range(size // len(block)):
            outer.update(hashlib.sha256(block).digest())
        tail = size % len(block)
        if tail:
            outer.update(hashlib.sha256(block[:tail]).digest())
        content_hash = outer.hexdigest()
        ordinary = hashlib.sha256()
        for _ in range(size // len(block)):
            ordinary.update(block)
        ordinary.update(block[:tail])

        class ZeroStream:
            remaining = size
            def read(self, length):
                assert length <= p._CHUNK
                count = min(length, self.remaining)
                self.remaining -= count
                return b'\0' * count
            def tell(self):
                return size - self.remaining
            def seek(self, position):
                assert 0 <= position <= size
                self.remaining = size - position

        # Preserve the expected size without allocating it as a single bytes object.
        class SizeOnly:
            def __len__(self):
                return size
        self.bind(SizeOnly(), digest=ordinary.hexdigest())

        responses = [missing(), Response(200, payload({'session_id': 'fictional-session'}))]
        responses.extend(Response(200) for _ in range((size - 1) // p._CHUNK))
        responses.append(missing())
        responses.append(Response(200, payload(metadata(FILE, size, content_hash=content_hash))))
        result, fake = self.call(responses, lambda: self.transport.upload_add(FILE, ZeroStream(), size))
        self.assertEqual(result.size, size)
        self.assertEqual(len(fake.calls), 22)
        self.assertTrue(all(call[3] <= p._CHUNK for call in fake.calls))
        arg = json.loads(fake.calls[-1][4]['Dropbox-API-Arg'])
        self.assertEqual(arg['cursor']['offset'], size)
        self.assertEqual(arg['commit']['mode'], {'.tag': 'add'})
        self.assertFalse(arg['commit']['autorename'])
        self.assertTrue(arg['commit']['strict_conflict'])

    def test_source_short_before_start_reports_changed_and_no_write(self):
        self.bind(b'a' * p._CHUNK + b'b')
        fake = Connections([Response(200, payload({'session_id': 'fictional-session'}))])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(FILE, io.BytesIO(b'a' * p._CHUNK), p._CHUNK + 1)
        self.assertEqual(cm.exception.code, 'SOURCE_CHANGED')
        self.assertEqual(len(fake.calls), 0)

    def test_unbound_or_unplanned_writes_never_reach_network(self):
        with self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(FILE, io.BytesIO(b'a'), 1)
        self.assertEqual(cm.exception.code, 'PLAN_REQUIRED')
        self.bind(b'a')
        with self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(GEN + 'other/file', io.BytesIO(b'a'), 1)
        self.assertEqual(cm.exception.code, 'PLAN_INVALID')
        with self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.update_if_revision(p.ACTIVE, b'not-the-plan', 'old-rev')
        self.assertEqual(cm.exception.code, 'PLAN_INVALID')

    def test_commit_requires_remote_payloads_before_any_write(self):
        self.bind()
        fake = Connections([missing(), missing()])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(GEN + 'system/COMMIT.json', io.BytesIO(COMMIT), len(COMMIT))
        self.assertEqual(cm.exception.code, 'REMOTE_INCOMPLETE')
        self.assertEqual([call[2] for call in fake.calls], ['/2/files/get_metadata', '/2/files/download'])

    def test_activation_requires_complete_remote_catalog_and_original_parent(self):
        active = self.bind()
        catalog = self.verified_catalog()
        catalog[1] = missing()  # Missing COMMIT: do not write active reference.
        fake = Connections(catalog)
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.update_if_revision(p.ACTIVE, active, 'old-rev')
        self.assertEqual(cm.exception.code, 'REMOTE_INCOMPLETE')
        self.assertNotIn('/2/files/upload', [call[2] for call in fake.calls])
        for body, rev in ((b'old', 'other-rev'), (b'new', 'old-rev')):
            previous = Response(200, body, {'Dropbox-API-Result': json.dumps(metadata(p.ACTIVE, len(body), rev))})
            fake = Connections(self.verified_catalog() + [previous])
            with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
                self.transport.update_if_revision(p.ACTIVE, active, 'old-rev')
            self.assertEqual(cm.exception.code, 'ACTIVE_CHANGED')
            self.assertNotIn('/2/files/upload', [call[2] for call in fake.calls])

    def test_mutation_between_preflight_and_upload_is_not_finished(self):
        self.bind(b'a')
        class Changed(io.BytesIO):
            def seek(self, position, whence=0):
                result = super().seek(position, whence)
                self.write(b'b')
                super().seek(position, whence)
                return result
        fake = Connections([missing(), Response(200, payload({'session_id': 'fictional-session'}))])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(FILE, Changed(b'a'), 1)
        self.assertEqual(cm.exception.code, 'STAGING_INCOMPLETE')
        self.assertEqual([call[2] for call in fake.calls], ['/2/files/get_metadata', '/2/files/upload_session/start'])

    def test_existing_commit_never_permits_implicit_generation_repair(self):
        self.bind(b'a')
        commit = lambda: Response(200, payload(metadata(GEN + 'system/COMMIT.json', len(COMMIT))))
        fake = Connections([commit(), missing()])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(FILE, io.BytesIO(b'a'), 1)
        self.assertEqual(cm.exception.code, 'COMMITTED_GENERATION_DAMAGED')
        self.assertNotIn('/2/files/upload_session/start', [call[2] for call in fake.calls])
        fake = Connections([commit(), Response(200, payload(metadata(FILE, 1)))])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(FILE, io.BytesIO(b'a'), 1)
        self.assertEqual(cm.exception.code, 'COMMITTED_GENERATION_IMMUTABLE')
        self.assertEqual(len(fake.calls), 2)

    def test_commit_appearing_during_session_prevents_finish(self):
        self.bind(b'a')
        commit = Response(200, payload(metadata(GEN + 'system/COMMIT.json', len(COMMIT))))
        fake = Connections([missing(), Response(200, payload({'session_id': 'fictional-session'})), commit, missing()])
        with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make), self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.upload_add(FILE, io.BytesIO(b'a'), 1)
        self.assertEqual(cm.exception.code, 'COMMITTED_GENERATION_DAMAGED')
        self.assertNotIn('/2/files/upload_session/finish', [call[2] for call in fake.calls])

    def test_permit_rejects_case_collisions_and_is_bound_to_session(self):
        active = self.bind()
        files = {FILE: (0, hashlib.sha256(b'').hexdigest()),
                 GEN + 'snapshots/TABLES.json': (0, hashlib.sha256(b'').hexdigest())}
        with self.assertRaises(p.PublisherTransportError):
            p._mint_write_permit(self.session, files=files, active_bytes=active,
                                 previous_rev='old-rev', previous_sha256=hashlib.sha256(b'old').hexdigest())
        other = p._mint_write_permit(Session(), files={
            FILE: (0, hashlib.sha256(b'').hexdigest()), GEN + 'system/manifest.json': (len(MANIFEST), GENERATION),
            GEN + 'system/COMMIT.json': (len(COMMIT), hashlib.sha256(COMMIT).hexdigest())},
            active_bytes=active, previous_rev='old-rev', previous_sha256=hashlib.sha256(b'old').hexdigest())
        with self.assertRaises(p.PublisherTransportError) as cm:
            self.transport.bind_permit(other)
        self.assertEqual(cm.exception.code, 'PLAN_REQUIRED')

    def test_read_transport_exception_classifier_never_leaks_private_values(self):
        import socket
        import ssl
        for exc, expected in (
            (socket.gaierror('fictional-private-host'), 'DNS_FAILED'),
            (ssl.SSLError('fictional-private-cert'), 'TLS_FAILED'),
            (TimeoutError('fictional-private-timeout'), 'READ_TIMEOUT'),
            (ConnectionError('fictional-private-disconnect'), 'NETWORK_FAILED'),
            (RuntimeError('fictional-private-other'), 'REQUEST_FAILED'),
        ):
            self.assertEqual(p._read_failure_code(exc), expected)

    def test_exception_code_is_bounded_and_redacted(self):
        error = p.PublisherTransportError('fictional-secret')
        self.assertEqual(str(error), 'REQUEST_FAILED')
        self.assertNotIn('fictional-secret', repr(error))
        self.assertNotIn('fictional-app-key', repr(self.transport))


class FullPublisherTransportTests(unittest.TestCase):
    def test_pipeline_through_actual_dispatch_and_fictional_https(self):
        from pathlib import Path
        import tempfile
        from app.publisher_pipeline import publish_snapshot
        from app.publisher_workspace import snapshot_workspace
        from tests.test_publisher_pipeline import initial_objects
        from tests.test_publisher_workspace import make_fixture

        class Dropbox:
            def __init__(self, objects):
                self.objects = dict(objects)
                self.revision = 'fictional-rev-1'
                self.sessions = {}
                self.writes = []

            def make(self, host, port, *, timeout, context):
                outer = self
                class Connection:
                    def set_debuglevel(self, value):
                        assert value == 0
                    def request(self, method, route, *, body, headers):
                        assert method == 'POST' and port == 443 and context.check_hostname
                        assert host in ('api.dropboxapi.com', 'content.dropboxapi.com')
                        args = json.loads(body if host == 'api.dropboxapi.com' else headers['Dropbox-API-Arg'])
                        self.reply = outer.handle(route, args, body)
                    def getresponse(self):
                        return self.reply
                    def close(self):
                        pass
                return Connection()

            def md(self, path, raw):
                chunks = (raw[i:i + 4 * 1024 * 1024] for i in range(0, len(raw), 4 * 1024 * 1024))
                content_hash = hashlib.sha256(b''.join(hashlib.sha256(chunk).digest() for chunk in chunks)).hexdigest()
                return metadata(path, len(raw), self.revision if path == p.ACTIVE else 'object-rev',
                                content_hash)

            def handle(self, route, args, body):
                if route == '/2/files/get_metadata':
                    raw = self.objects.get(args['path'])
                    return missing() if raw is None else Response(200, payload(self.md(args['path'], raw)))
                if route == '/2/files/download':
                    raw = self.objects.get(args['path'])
                    return missing() if raw is None else Response(200, raw, {'Dropbox-API-Result': json.dumps(self.md(args['path'], raw))})
                if route == '/2/files/upload_session/start':
                    sid = 'fictional-session-' + str(len(self.sessions))
                    self.sessions[sid] = bytearray(body)
                    return Response(200, payload({'session_id': sid}))
                if route in ('/2/files/upload_session/append_v2', '/2/files/upload_session/finish'):
                    cursor = args['cursor']
                    buf = self.sessions[cursor['session_id']]
                    assert cursor['offset'] == len(buf)
                    buf.extend(body)
                    if route.endswith('append_v2'):
                        return Response(200)
                    commit = args['commit']
                    path = commit['path']
                    assert commit['mode'] == {'.tag': 'add'} and path not in self.objects
                    raw = bytes(buf)
                    self.objects[path] = raw
                    self.writes.append(('add', path))
                    return Response(200, payload(self.md(path, raw)))
                if route == '/2/files/upload':
                    assert args['path'] == p.ACTIVE and args['mode'] == {'.tag': 'update', 'update': self.revision}
                    self.objects[p.ACTIVE] = body
                    self.revision = 'fictional-rev-2'
                    self.writes.append(('cas', p.ACTIVE))
                    return Response(200, payload(self.md(p.ACTIVE, body)))
                raise AssertionError('unexpected route')

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'fictional.sqlite'
            writer = make_fixture(db)
            try:
                fake = Dropbox(initial_objects(writer))
            finally:
                writer.close()
            session = Session()
            transport = p.PublisherTransport(session, confirmed_app_key='fictional-app-key',
                                             app_folder_confirmation='USER_CONFIRMED')
            with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')):
                with patch.object(p.http.client, 'HTTPSConnection', side_effect=fake.make):
                    with snapshot_workspace(db, directory=Path(tmp)) as snapshot:
                        result = publish_snapshot(snapshot, transport, session)
            self.assertEqual((result.status, result.code), ('PUBLISHED', 'PUBLISHED'))
            commit = next(i for i, (kind, path) in enumerate(fake.writes)
                          if kind == 'add' and path.endswith('/system/COMMIT.json'))
            self.assertEqual(fake.writes[-1], ('cas', p.ACTIVE))
            self.assertLess(commit, len(fake.writes) - 1)
            active = json.loads(fake.objects[p.ACTIVE])
            self.assertIn(active['root'] + 'system/COMMIT.json', fake.objects)


if __name__ == '__main__':
    unittest.main()
