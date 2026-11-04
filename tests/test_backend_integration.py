"""Backend contract and loopback HTTP checks using generated fiction only."""
from contextlib import contextmanager
import copy
import hashlib
import http.client
import json
import threading
import unittest
from unittest import mock

from app.provider import ReadOnlyAdapter, SyntheticProvider, error, request
from app.server import make_server
from app.transport import TransportError


@contextmanager
def running_server(**kwargs):
    server = make_server(0, **kwargs)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if isinstance(server.provider, SyntheticProvider):
            server.provider.close()


def http_json(server, payload=None, *, path='/api/read', method='POST', headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    body = None if payload is None else json.dumps(payload).encode('utf-8')
    request_headers = {'Content-Type': 'application/json', **(headers or {})}
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, json.loads(response.read()), dict(response.getheaders())
    finally:
        connection.close()


class AdapterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.source = SyntheticProvider()
        self.adapter = ReadOnlyAdapter(self.source.read)
        self.scope = {key: self.source.dataset[key] for key in ('dataset_id', 'snapshot_id')}

    def tearDown(self):
        self.source.close()

    def q(self, operation, **values):
        return request(operation, **self.scope, **values)

    def changed(self, q, mutate):
        original = self.source.read(q)
        response = copy.deepcopy(original)
        mutate(response)
        return ReadOnlyAdapter(lambda *_: response).read(q), response

    def test_search_pages_detail_sources_and_history_are_preserved(self):
        rows = []
        q = self.q('search')
        while True:
            status, response = self.adapter.read_http(q)
            self.assertEqual(status, 200)
            self.assertEqual(response, self.source.read(q))
            self.assertEqual(response['status'], 'OK')
            rows.extend(response['result']['records'])
            q['cursor'] = response['result']['page']['next_cursor']
            if q['cursor'] is None:
                break
        self.assertEqual(len(rows), 7)
        self.assertEqual(len({row['record_id'] for row in rows}), 7)
        self.assertTrue(all(row['raw_text'] is None for row in rows))
        detail = self.q('detail', record_id='synthetic-record-001')
        self.assertEqual(self.adapter.read(detail), self.source.read(detail))
        self.assertEqual(len(self.adapter.read(detail)['result']['sources']), 2)
        history = self.q('related_history', record_id='synthetic-record-006', page_size=1)
        events = []
        while True:
            response = self.adapter.read(history)
            self.assertEqual(response, self.source.read(history))
            self.assertEqual(response['status'], 'OK')
            events.extend(response['result']['events'])
            history['cursor'] = response['result']['page']['next_cursor']
            if history['cursor'] is None:
                break
        self.assertEqual(events[1]['supersedes'], events[0]['event_id'])
        self.assertTrue(all(item['event_payload_preservation'] == 'UNTOUCHED' for item in events))

    def test_empty_missing_unknown_and_inaccessible_remain_distinct(self):
        empty = self.adapter.read(self.q('search', query='fiction-with-no-matches'))
        self.assertEqual(empty['status'], 'OK')
        self.assertEqual(empty['result']['records'], [])
        missing = self.adapter.read(self.q('detail', record_id='synthetic-record-004'))
        self.assertEqual(missing['result']['record']['source_status'], 'MISSING')
        q = self.q('related_history', record_id='synthetic-record-001')
        for availability in ('AVAILABLE', 'MISSING', 'UNKNOWN', 'INACCESSIBLE'):
            with self.subTest(availability=availability):
                response, supplied = self.changed(q, lambda r: r['result'].update(history_status=availability))
                self.assertEqual(response, supplied)
                self.assertEqual(response['result']['history_status'], availability)
                self.assertEqual(response['result']['events'], [])
        q = self.q('detail', record_id='synthetic-record-001')
        def blank_body(response):
            response['result']['record'].update(raw_text='', raw_text_sha256=hashlib.sha256(b'').hexdigest())
        response, supplied = self.changed(q, blank_body)
        self.assertEqual(response, supplied)
        self.assertEqual(response['result']['record']['content_availability'], 'AVAILABLE')
        self.assertEqual(response['result']['record']['raw_text'], '')

    def test_complete_schema_rejects_missing_extra_and_invalid_nested_values(self):
        q = self.q('detail', record_id='synthetic-record-001')
        mutations = [
            lambda r: r.pop('error'),
            lambda r: r.update(unapproved='fictional-private-diagnostic'),
            lambda r: r['result']['record'].pop('original_ids'),
            lambda r: r['result']['record']['state'].update(review=[]),
            lambda r: r['result']['sources'][0].update(availability='INVALID_ENUM'),
            lambda r: r['result']['record'].update(raw_text_sha256='0' * 64),
        ]
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                response, _ = self.changed(q, mutate)
                self.assertEqual(response['status'], 'ERROR')
                self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')
                self.assertIsNone(response['result'])
                self.assertNotIn('fictional-private-diagnostic', json.dumps(response))

    def test_response_scope_and_snapshot_are_checked_before_release(self):
        q = self.q('detail', record_id='synthetic-record-001')
        mutations = [
            lambda r: r['snapshot'].update(dataset_id='fictional-foreign'),
            lambda r: r['result']['record'].update(dataset_id='fictional-foreign'),
            lambda r: r['result']['record'].update(record_id='synthetic-record-002'),
            lambda r: r['result']['sources'][0].update(dataset_id='fictional-foreign'),
        ]
        for mutate in mutations:
            response, _ = self.changed(q, mutate)
            self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')
        response, _ = self.changed(q, lambda r: r['snapshot'].update(snapshot_id='fictional-stale'))
        self.assertEqual(response['error']['code'], 'VERSION_MISMATCH')
        q = request('capabilities')
        response, _ = self.changed(q, lambda r: r['result']['dataset'].update(dataset_id='fictional-foreign'))
        self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')

    def test_anchored_cross_dataset_relations_are_preserved_in_both_directions(self):
        q = self.q('detail', record_id='synthetic-record-001')
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                def cross_dataset(response):
                    relation = response['result']['record']['project_relations'][0]
                    relation.update(to_dataset_id='fictional-foreign', to_id='fictional-peer')
                    if reverse:
                        relation['from_dataset_id'], relation['to_dataset_id'] = relation['to_dataset_id'], relation['from_dataset_id']
                        relation['from_id'], relation['to_id'] = relation['to_id'], relation['from_id']
                response, supplied = self.changed(q, cross_dataset)
                self.assertEqual(response, supplied)
                self.assertEqual(response['status'], 'OK')
        response, _ = self.changed(q, lambda r: r['result']['record']['project_relations'][0].update(from_id='fictional-other'))
        self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')

    def test_history_wrong_record_and_invalid_page_are_rejected(self):
        q = self.q('related_history', record_id='synthetic-record-006')
        response, _ = self.changed(q, lambda r: r['result']['events'][0].update(record_id='synthetic-record-001'))
        self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')
        q = self.q('search')
        for mutate in (
            lambda r: r['result']['page'].update(page_size=2),
            lambda r: r['result']['page'].update(sort=['dataset_id:asc', 'record_id:desc']),
            lambda r: r['result']['records'][0].update(raw_text='', content_availability='AVAILABLE'),
            lambda r: r['result']['records'].reverse(),
            lambda r: r['result']['records'].__setitem__(1, copy.deepcopy(r['result']['records'][0])),
        ):
            response, _ = self.changed(q, mutate)
            self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')

        q['cursor'] = self.source.read(q)['result']['page']['next_cursor']
        response, _ = self.changed(q, lambda r: r['result']['page'].update(next_cursor=q['cursor']))
        self.assertEqual(response['error']['code'], 'INTERNAL_ERROR')

    def test_errors_are_sanitized_without_rewriting_code_or_retry_policy(self):
        q = self.q('search')
        supplied = error('search', 'VERSION_MISMATCH')
        supplied['error'].update(message='fictional-private-diagnostic', retryable=True)
        response = ReadOnlyAdapter(lambda *_: supplied).read(q)
        self.assertEqual(response['error']['code'], 'VERSION_MISMATCH')
        self.assertTrue(response['error']['retryable'])
        self.assertNotIn('fictional-private-diagnostic', json.dumps(response))
        self.assertEqual(supplied['error']['message'], 'fictional-private-diagnostic')
        def failure(*_):
            raise OSError('fictional-private-diagnostic')
        status, response = ReadOnlyAdapter(failure).read_http(q)
        self.assertEqual(status, 503)
        self.assertEqual(response['error']['code'], 'CONNECTION_UNAVAILABLE')
        self.assertNotIn('fictional-private-diagnostic', json.dumps(response))

    def test_invalid_request_never_calls_transport_or_mutates_request(self):
        transport = mock.Mock(side_effect=AssertionError('must not call transport'))
        adapter = ReadOnlyAdapter(transport)
        for q, sort in ((None, 'id_asc'), ({}, 'id_asc'), (self.q('search'), []),
                        (self.q('search', page_size=True), 'id_asc')):
            self.assertEqual(adapter.read_http(q, sort)[0], 400)
        transport.assert_not_called()
        q = self.q('search')
        original = copy.deepcopy(q)
        def mutating_transport(incoming, sort):
            response = self.source.read(incoming, sort)
            incoming['dataset_id'] = 'fictional-mutation'
            return response
        self.assertEqual(ReadOnlyAdapter(mutating_transport).read(q)['status'], 'OK')
        self.assertEqual(q, original)


class ServerIntegrationTests(unittest.TestCase):
    def test_readonly_http_search_to_detail_and_source_metadata(self):
        source = SyntheticProvider()
        self.addCleanup(source.close)
        adapter = ReadOnlyAdapter(source.read)
        with running_server(mode='readonly', provider=adapter) as server:
            status, capabilities, headers = http_json(server, {'request': request('capabilities')})
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertFalse(capabilities['result']['writes_supported'])
            scope = {key: capabilities['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
            status, search, _ = http_json(server, {'request': request('search', **scope)})
            self.assertEqual(status, 200)
            self.assertEqual(search['status'], 'OK')
            record_id = search['result']['records'][0]['record_id']
            self.assertIsNone(search['result']['records'][0]['raw_text'])
            status, detail, _ = http_json(server, {'request': request('detail', record_id=record_id, **scope)})
            self.assertEqual(status, 200)
            self.assertEqual(detail['result']['record']['record_id'], record_id)
            self.assertTrue(detail['result']['sources'])
            status, mode, _ = http_json(server, method='GET', path='/api/mode')
            self.assertEqual((status, mode), (200, {'mode': 'readonly', 'simulation_namespace': None}))

    def test_auth_status_is_preserved_per_http_request(self):
        for upstream_status in (401, 403):
            with self.subTest(upstream_status=upstream_status):
                def denied(*_):
                    raise TransportError(http_status=upstream_status)
                with running_server(mode='readonly', provider=ReadOnlyAdapter(denied)) as server:
                    status, response, _ = http_json(server, {'request': request('capabilities')})
                    self.assertEqual(status, upstream_status)
                    self.assertEqual(response['error']['code'], 'CONNECTION_UNAVAILABLE')
                    self.assertIsNone(response['result'])

    def test_readonly_never_autoloads_configuration_or_synthetic_fallback(self):
        with mock.patch('app.server.load_transport_from_env', side_effect=AssertionError('unexpected config read')) as loader:
            with mock.patch('app.server.SyntheticProvider', side_effect=AssertionError('unexpected fallback')):
                server = make_server(0, mode='readonly')
                try:
                    self.assertIsInstance(server.provider, ReadOnlyAdapter)
                    status, response = server.provider.read_http(request('capabilities'))
                    self.assertEqual(status, 503)
                    self.assertIsNone(response['result'])
                finally:
                    server.server_close()
            loader.assert_not_called()
        with mock.patch('app.server.load_transport_from_env', return_value=lambda *_: error('capabilities', 'CONNECTION_UNAVAILABLE')) as loader:
            server = make_server(0, mode='readonly', connect_env=True)
            server.server_close()
            loader.assert_called_once_with()

    def test_readonly_scenarios_host_origin_and_writes_are_denied(self):
        called = mock.Mock(side_effect=AssertionError('must remain uncalled'))
        with running_server(mode='readonly', provider=ReadOnlyAdapter(called)) as server:
            for scenario in ('connection', 'stale'):
                status, _, _ = http_json(server, {'request': request('capabilities'), 'scenario': scenario})
                self.assertEqual(status, 400)
            for headers in ({'Host': 'fictional-attacker.invalid'}, {'Origin': 'https://fictional-attacker.invalid'}):
                status, _, _ = http_json(server, {'request': request('capabilities')}, headers=headers)
                self.assertEqual(status, 403)
            for path in ('/api/write', '/api/restore', '/api/backup', '/api/approve'):
                status, response, _ = http_json(server, {}, path=path)
                self.assertEqual((status, response), (405, {'error': 'READ_ONLY'}))
            status, _, _ = http_json(server, method='GET', path='/api/mode', headers={'Host': 'fictional-attacker.invalid'})
            self.assertEqual(status, 403)
            called.assert_not_called()


if __name__ == '__main__':
    unittest.main()
