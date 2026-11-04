"""Source endpoint checks using fictional source bodies and loopback HTTP only."""
from contextlib import contextmanager
import copy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from app.provider import request
from app.server import make_server
from app.source_read import source_error, source_result, validate_source_response
from app.transport import ConfigError
from fixtures.synthetic.generate import create, contract_examples


SOURCE_VERSION = 'UTT-SOURCE-READ/0.1.0'
SOURCE_TEXT = '별도로 보존된 합성 출처 본문입니다.\n🧭 독립 대상\n'


def source_request(**changes):
    value = dict(contract_version=SOURCE_VERSION, dataset_id='fictional-dataset',
                snapshot_id='fictional-snapshot', record_id='fictional-record',
                source_id='fictional-source', source_version='fictional-source-v1')
    value.update(changes)
    return value


def source_response(q):
    body_hash = hashlib.sha256(SOURCE_TEXT.encode('utf-8')).hexdigest()
    source = dict(source_id=q['source_id'], dataset_id=q['dataset_id'], availability='AVAILABLE',
                  version=q['source_version'], content_sha256=body_hash, location=None,
                  verification='UNVERIFIED')
    content = dict(availability='AVAILABLE', target_kind='SOURCE_BODY', target_id=q['source_id'],
                   target_version=q['source_version'], locator_target_version=None,
                   text=SOURCE_TEXT, text_sha256=body_hash)
    return source_result(q, source, content)


@contextmanager
def source_server(**kwargs):
    server = make_server(0, mode='readonly', **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def send(server, payload=None, *, raw=None, path='/api/source', method='POST', headers=None):
    if raw is None and payload is not None:
        raw = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        connection.request(method, path, body=raw,
                           headers={'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


class FakeSourceProvider:
    def __init__(self, responder=source_response):
        self.responder = responder
        self.requests = []

    def read_source(self, q):
        self.requests.append(copy.deepcopy(q))
        return self.responder(q)


class SourceServerTests(unittest.TestCase):
    def test_source_body_is_returned_through_its_own_versioned_endpoint(self):
        provider = FakeSourceProvider()
        q = source_request()
        with source_server(source_provider=provider) as server:
            status, response, headers = send(server, q)
        self.assertEqual(status, 200)
        validate_source_response(response, q)
        self.assertEqual(response['contract_version'], SOURCE_VERSION)
        self.assertEqual(response['status'], 'OK')
        self.assertEqual(response['result']['content']['text'], SOURCE_TEXT)
        self.assertEqual(response['result']['content']['target_kind'], 'SOURCE_BODY')
        self.assertEqual(response['result']['content']['text_sha256'],
                         hashlib.sha256(SOURCE_TEXT.encode('utf-8')).hexdigest())
        self.assertEqual(provider.requests, [q])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn("connect-src 'self'", headers['Content-Security-Policy'])

    def test_missing_provider_is_explicitly_not_supported(self):
        with source_server() as server:
            status, response, _ = send(server, source_request())
        self.assertEqual(status, 200)
        self.assertEqual(response, source_error('NOT_SUPPORTED'))
        self.assertIsNone(response['result'])

    def test_invalid_request_shape_never_calls_source_provider(self):
        provider = FakeSourceProvider()
        invalid = [None, [], {}, {**source_request(), 'unexpected': 'fictional'},
                   {**source_request(), 'source_version': None},
                   {**source_request(), 'contract_version': 'UTT-ACCESS-READ/0.1.0'},
                   {**source_request(), 'source_id': ''}]
        with source_server(source_provider=provider) as server:
            for value in invalid:
                with self.subTest(value=value):
                    status, response, _ = send(server, raw=json.dumps(value).encode('utf-8'))
                    self.assertEqual(status, 400)
                    code = ('UNSUPPORTED_CONTRACT' if isinstance(value, dict)
                            and value.get('contract_version') == 'UTT-ACCESS-READ/0.1.0' else 'INVALID_REQUEST')
                    self.assertEqual(response, source_error(code))
        self.assertEqual(provider.requests, [])

    def test_malformed_json_content_type_and_request_size_use_source_error_contract(self):
        provider = FakeSourceProvider()
        cases = [(b'{bad json', None), (b'', None), (b' ' * 16385, None),
                 (json.dumps(source_request()).encode('utf-8'), {'Content-Type': 'text/plain'})]
        with source_server(source_provider=provider) as server:
            for raw, headers in cases:
                with self.subTest(headers=headers, size=len(raw)):
                    status, response, _ = send(server, raw=raw, headers=headers)
                    self.assertEqual(status, 400)
                    self.assertEqual(response, source_error('INVALID_REQUEST'))
        self.assertEqual(provider.requests, [])

    def test_wrong_identity_hash_and_envelope_fail_closed(self):
        mutations = [(lambda r: r['result'].update(dataset_id='fictional-foreign'), 'INTERNAL_ERROR'),
                     (lambda r: r['result'].update(snapshot_id='fictional-other-snapshot'), 'VERSION_MISMATCH'),
                     (lambda r: r['result'].update(source_version='fictional-other-version'), 'VERSION_MISMATCH'),
                     (lambda r: r['result']['content'].update(text_sha256='0' * 64), 'INTERNAL_ERROR'),
                     (lambda r: r.update(unapproved='fictional-private-value'), 'INTERNAL_ERROR')]
        for number, (mutate, expected_code) in enumerate(mutations):
            with self.subTest(number=number):
                def bad_response(q):
                    response = source_response(q)
                    mutate(response)
                    return response
                with source_server(source_provider=FakeSourceProvider(bad_response)) as server:
                    status, response, _ = send(server, source_request())
                self.assertEqual(status, 200)
                self.assertEqual(response, source_error(expected_code))
                self.assertNotIn('fictional-private-value', json.dumps(response))

    def test_provider_request_mutation_cannot_change_the_validation_scope(self):
        def mutate_and_respond(q):
            q.update(snapshot_id='fictional-other-snapshot')
            return source_response(q)
        with source_server(source_provider=FakeSourceProvider(mutate_and_respond)) as server:
            status, response, _ = send(server, source_request())
        self.assertEqual(status, 200)
        self.assertEqual(response, source_error('VERSION_MISMATCH'))

    def test_provider_exception_and_error_diagnostics_are_sanitized(self):
        def failure(_):
            raise RuntimeError('fictional-private-value')
        with source_server(source_provider=FakeSourceProvider(failure)) as server:
            status, response, _ = send(server, source_request())
        self.assertEqual(status, 503)
        self.assertEqual(response, source_error('CONNECTION_UNAVAILABLE'))
        self.assertNotIn('fictional-private-value', json.dumps(response))
        supplied = source_error('VERSION_MISMATCH')
        supplied['error'].update(message='fictional-private-value', retryable=True)
        with source_server(source_provider=FakeSourceProvider(lambda _: supplied)) as server:
            status, response, _ = send(server, source_request())
        self.assertEqual(status, 200)
        self.assertEqual(response['error']['code'], 'VERSION_MISMATCH')
        self.assertTrue(response['error']['retryable'])
        self.assertNotIn('fictional-private-value', json.dumps(response))
        self.assertEqual(supplied['error']['message'], 'fictional-private-value')

    def test_host_origin_and_write_endpoints_are_denied_without_provider_access(self):
        provider = FakeSourceProvider()
        with source_server(source_provider=provider) as server:
            for headers in ({'Host': 'fictional-attacker.invalid'},
                            {'Origin': 'https://fictional-attacker.invalid'}):
                status, response, _ = send(server, source_request(), headers=headers)
                self.assertEqual((status, response), (403, {'error': 'ORIGIN_DENIED'}))
            for path in ('/api/write', '/api/restore', '/api/approve', '/api/source/write'):
                status, response, _ = send(server, {}, path=path)
                self.assertEqual((status, response), (405, {'error': 'READ_ONLY'}))
            status, _, _ = send(server, path='/api/source', method='GET')
            self.assertEqual(status, 404)
        self.assertEqual(provider.requests, [])

    def test_snapshot_environment_runs_real_loader_and_source_flow_in_one_process(self):
        # This input uses only the existing fictional fixture generator. The new
        # SnapshotProvider must read the actual temporary JSON, not a callback
        # around SyntheticProvider. Its source body deliberately differs from
        # the record body so a record-body fallback cannot pass this test.
        examples = contract_examples(create())
        dataset = copy.deepcopy(examples['capabilities']['snapshot'])
        dataset.pop('snapshot_id')
        records = [copy.deepcopy(record) for page in ('search_page_1', 'search_page_2', 'search_page_3')
                   for record in examples[page]['result']['records']]
        source = records[0]['source_refs'][0]
        source.update(content_sha256=hashlib.sha256(SOURCE_TEXT.encode('utf-8')).hexdigest(),
                      location=None, verification='UNVERIFIED')
        payload = dict(format='UTT-NORMALIZED-SNAPSHOT/0.1.0', dataset=dataset, records=records,
                       projects=examples['projects']['result']['projects'], components=[], relations=[], history=[],
                       source_contents=[dict(record_id=records[0]['record_id'], source_id=source['source_id'],
                                             source_version=source['version'],
                                             content=dict(availability='AVAILABLE', target_kind='SOURCE_BODY',
                                                          target_id=source['source_id'], target_version=source['version'],
                                                          locator_target_version=None, text=SOURCE_TEXT,
                                                          text_sha256=source['content_sha256']))])
        with tempfile.TemporaryDirectory(prefix='utt-source-server-fiction-') as directory:
            snapshot = Path(directory) / 'fictional-snapshot.json'
            encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            snapshot.write_bytes(encoded)
            with mock.patch.dict('os.environ', {'UTT_RUNTIME_APPROVED': '1', 'UTT_SNAPSHOT_PATH': str(snapshot)}):
                with source_server(snapshot_env=True) as server:
                    status, capabilities, _ = send(server, {'request': request('capabilities')}, path='/api/read')
                    self.assertEqual((status, capabilities['status']), (200, 'OK'))
                    expected_snapshot = 'sha256:' + hashlib.sha256(encoded).hexdigest()
                    self.assertEqual(capabilities['snapshot']['snapshot_id'], expected_snapshot)
                    scope = {key: capabilities['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
                    search = request('search', **scope, page_size=3)
                    status, first, _ = send(server, {'request': search}, path='/api/read')
                    self.assertEqual((status, first['status']), (200, 'OK'))
                    self.assertEqual([row['record_id'] for row in first['result']['records']],
                                     sorted(record['record_id'] for record in records)[:3])
                    self.assertTrue(first['result']['page']['next_cursor'])
                    self.assertTrue(all(row['raw_text'] is None for row in first['result']['records']))
                    search['cursor'] = first['result']['page']['next_cursor']
                    status, second, _ = send(server, {'request': search}, path='/api/read')
                    self.assertEqual((status, second['status']), (200, 'OK'))
                    self.assertEqual([row['record_id'] for row in second['result']['records']],
                                     sorted(record['record_id'] for record in records)[3:6])
                    record_id = first['result']['records'][0]['record_id']
                    status, detail, _ = send(server, {'request': request('detail', **scope, record_id=record_id)}, path='/api/read')
                    self.assertEqual((status, detail['status']), (200, 'OK'))
                    q = source_request(**scope, record_id=record_id, source_id=source['source_id'],
                                       source_version=source['version'])
                    status, result, _ = send(server, q)
                    self.assertEqual((status, result['status']), (200, 'OK'))
                    validate_source_response(result, q)
                    self.assertEqual(result['result']['content']['text'], SOURCE_TEXT)
                    self.assertNotEqual(result['result']['content']['text'], detail['result']['record']['raw_text'])

    def test_snapshot_environment_requires_marker_and_mutually_exclusive_connection(self):
        with mock.patch.dict('os.environ', {'UTT_RUNTIME_APPROVED': '0', 'UTT_SNAPSHOT_PATH': 'fictional-only.json'}):
            with self.assertRaises(ConfigError):
                make_server(0, mode='readonly', snapshot_env=True)
        with self.assertRaises(ValueError):
            make_server(0, mode='readonly', snapshot_env=True, connect_env=True)


if __name__ == '__main__':
    unittest.main()
