"""Native D0 client tests use only a fictional loopback HTTP server."""
import hashlib
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from app.d0_adapter import D0Client, D0ConfigError, D0Error


class D0ClientTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.reply_status = 200
        self.reply_body = None
        self.reply_mime = 'application/json'
        self.session_value = 'fictional-local-session'
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                owner.requests.append((parsed.path, parse_qs(parsed.query, keep_blank_values=True),
                                       self.headers.get('X-UTT-D0-Session')))
                if parsed.path == '/fictional-api/session':
                    status = 200
                    body = json.dumps({'session': owner.session_value}).encode('utf-8')
                    mime = 'application/json'
                elif self.headers.get('X-UTT-D0-Session') != owner.session_value:
                    status, body, mime = 403, b'private-error-must-not-escape', 'application/json'
                else:
                    status, mime = owner.reply_status, owner.reply_mime
                    if owner.reply_body is not None:
                        body = owner.reply_body
                    elif parsed.path == '/fictional-api/records':
                        query = parse_qs(parsed.query, keep_blank_values=True)
                        start, limit = int(query['offset'][0]), int(query['limit'][0])
                        rows = [{'id': 'fictional-id-1', 'title': '가상 첫째', 'tags': ['NATIVE_TAG'],
                                 'native_evidence': {'not_a_relation_id': ['unmodified', None]}},
                                {'id': 'fictional-id-2', 'title': '가상 둘째', 'tags': []}]
                        body = json.dumps({'total': 2, 'results': rows[start:start + limit],
                                           'unmapped_native': {'status': 'NATIVE_UNKNOWN'}},
                                          ensure_ascii=False, indent=2).encode('utf-8')
                    elif parsed.path == '/fictional-api/detail':
                        body = json.dumps({'canonical_record': {'title': '가상 상세', 'original': None},
                                           'comparison': {'body_comparison': 'NATIVE_COMPARISON'},
                                           'stored_body': '명시적 B 저장 본문 🧭',
                                           'sources': [{'source_id': 'fictional-native-source',
                                                        'locator': {'native_offset': None}}],
                                           'lineage': {'candidate': ['fictional-parent']},
                                           'b_relations': [{'opaque_native_field': 'kept'}]},
                                          ensure_ascii=False).encode('utf-8')
                    elif parsed.path == '/fictional-api/summary':
                        body = b'{"stats":{"records":2},"unknown":null}'
                    elif parsed.path == '/fictional-api/filters':
                        body = b'{"tags":[],"projects":[],"sources":[],"native":true}'
                    else:
                        status, body = 404, b'{}'
                self.send_response(status)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(body)))
                if status in (301, 302, 307, 308):
                    self.send_header('Location', 'http://external.invalid/do-not-follow')
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root = 'http://127.0.0.1:' + str(self.server.server_port) + '/fictional-api/'
        self.client = D0Client(self.root)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_startup_does_not_connect_and_loopback_root_is_strict(self):
        self.assertEqual(self.requests, [])
        for root in ('http://localhost:1234/api/', 'https://127.0.0.1:1234/api/',
                     'http://127.0.0.2:1234/api/', 'http://127.1:1234/api/',
                     'http://2130706433:1234/api/', 'http://[::1]:1234/api/',
                     'http://127.0.0.1/api/', 'http://user:secret@127.0.0.1:1234/api/',
                     'http://127.0.0.1:0/api/', 'http://127.0.0.1:65536/api/',
                     'http://127.0.0.1:1234/api/?secret=value',
                     'http://127.0.0.1:1234/api/#private',
                     'http://127.0.0.1:1234/../api/',
                     'http://127.0.0.1:1234/api/%2f/',
                     'http://127.0.0.1:1234/api'):
            with self.subTest(root=root), self.assertRaises(D0ConfigError) as caught:
                D0Client(root)
            self.assertNotIn('secret', str(caught.exception))
            self.assertNotIn('127.0.0.1', str(caught.exception))
        self.assertEqual(self.requests, [])

    def test_lazy_session_then_native_search_two_pages_detail_preserve_evidence(self):
        first = self.client.records(q='가상', tag='NATIVE_TAG', project='fictional-project',
                                    source='fictional-source-name', sort='id', offset=0, limit=1)
        second = self.client.records(q='가상', tag='NATIVE_TAG', project='fictional-project',
                                     source='fictional-source-name', sort='id', offset=1, limit=1)
        detail = self.client.detail(first.document['results'][0]['id'])
        self.assertEqual([item[0] for item in self.requests],
                         ['/fictional-api/session', '/fictional-api/records',
                          '/fictional-api/records', '/fictional-api/detail'])
        self.assertIsNone(self.requests[0][2])
        self.assertTrue(all(item[2] == self.session_value for item in self.requests[1:]))
        self.assertEqual([first.document['results'][0]['id'], second.document['results'][0]['id']],
                         ['fictional-id-1', 'fictional-id-2'])
        self.assertEqual(self.requests[1][1], {'q': ['가상'], 'tag': ['NATIVE_TAG'],
                         'project': ['fictional-project'], 'source': ['fictional-source-name'],
                         'sort': ['id'], 'offset': ['0'], 'limit': ['1']})
        self.assertEqual(detail.document['stored_body'], '명시적 B 저장 본문 🧭')
        self.assertEqual(detail.document['b_relations'], [{'opaque_native_field': 'kept'}])
        self.assertEqual(json.loads(first.raw_body), first.document)
        self.assertEqual(first.response_sha256, hashlib.sha256(first.raw_body).hexdigest())
        self.assertIn(b'\n', first.raw_body)
        changed = first.document
        changed['results'][0]['title'] = 'local-mutation'
        self.assertEqual(first.document['results'][0]['title'], '가상 첫째')
        self.assertNotIn(self.session_value, repr(first))
        self.assertNotIn('가상', repr(first))
        self.assertNotIn(self.session_value, repr(self.client))

    def test_null_empty_whitespace_and_native_filter_names_are_distinct(self):
        self.client.records(q=None, tag=None, project=None, source=None)
        self.client.records(q='', tag='', project='', source='')
        self.client.records(q=' \t\n', tag='  ', project='\u3000', source=' S ')
        queries = [query for path, query, _ in self.requests if path.endswith('/records')]
        self.assertNotIn('q', queries[0])
        self.assertNotIn('tag', queries[0])
        self.assertEqual(queries[1]['q'], [''])
        self.assertEqual(queries[1]['tag'], [''])
        self.assertEqual(queries[2]['q'], [' \t\n'])
        self.assertEqual(queries[2]['project'], ['\u3000'])
        self.assertEqual(queries[2]['source'], [' S '])

    def test_unconfirmed_role_review_and_bad_native_arguments_fail_before_http(self):
        for values in ({'document_role': 'NOTE'}, {'review_status': 'HELD'},
                       {'q': 1}, {'tag': []}, {'sort': 'id_desc'}, {'offset': -1},
                       {'offset': True}, {'limit': 0}, {'limit': 1001}, {'limit': False}):
            with self.subTest(values=values), self.assertRaises(D0Error) as caught:
                self.client.records(**values)
            self.assertEqual(caught.exception.code, 'INVALID_REQUEST')
        self.assertEqual(self.requests, [])
        for record_id in (None, '', {}, True):
            with self.assertRaises(D0Error):
                self.client.detail(record_id)
        self.assertEqual(self.requests, [])

    def test_draft_read_integration_fails_without_fabricating_snapshot_or_requests(self):
        from app.provider import request
        result = self.client.read(request('capabilities'))
        self.assertEqual(result['error']['code'], 'UNSUPPORTED_CONTRACT')
        self.assertIsNone(result['snapshot'])
        self.assertIsNone(result['result'])
        self.assertEqual(self.requests, [])

    def test_summary_filters_and_explicit_session_reset(self):
        self.assertEqual(self.client.summary().document['stats'], {'records': 2})
        self.assertTrue(self.client.filters().document['native'])
        self.assertEqual(sum(path.endswith('/session') for path, _, _ in self.requests), 1)
        before = len(self.requests)
        self.client.reset_session()
        self.assertEqual(len(self.requests), before)
        self.client.records()
        self.assertEqual(sum(path.endswith('/session') for path, _, _ in self.requests), 2)

    def test_http_auth_redirects_and_failures_are_sanitized_without_retry(self):
        self.client.records()
        self.requests.clear()
        for status, code in ((401, 'AUTH_REQUIRED'), (403, 'ACCESS_DENIED'),
                             (404, 'CONNECTION_UNAVAILABLE'), (400, 'CONNECTION_UNAVAILABLE'),
                             (302, 'CONNECTION_UNAVAILABLE'), (500, 'CONNECTION_UNAVAILABLE')):
            self.reply_status, self.reply_body = status, b'private-upstream-body-and-key'
            with self.subTest(status=status), self.assertRaises(D0Error) as caught:
                self.client.records()
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.http_status, status)
            self.assertNotIn('private', str(caught.exception))
        self.assertEqual(len(self.requests), 6)
        self.assertTrue(all(path == '/fictional-api/records' for path, _, _ in self.requests))

    def test_strict_response_json_size_and_shape(self):
        self.client.records()
        self.requests.clear()
        for body in (b'not-json-private', b'\xff', b'[]',
                     b'{"total":0,"total":1,"results":[]}',
                     b'{"total":0,"results":[],"bad":NaN}',
                     b'{"total":0,"results":[],"bad":1e999}',
                     b'{"total":false,"results":[]}',
                     b'{"total":1,"results":[{}]}',
                     b'{"total":2,"results":[{"id":"duplicate"},{"id":"duplicate"}]}'):
            self.reply_body = body
            with self.subTest(body=body[:30]), self.assertRaises(D0Error) as caught:
                self.client.records()
            self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.reply_body = b'x' * 1025
        client = D0Client(self.root, max_response_bytes=1024)
        with self.assertRaises(D0Error) as caught:
            client.records()
        self.assertEqual(caught.exception.code, 'RESPONSE_TOO_LARGE')
        self.reply_body = b'{}'
        self.reply_mime = 'text/html'
        with self.assertRaises(D0Error) as caught:
            self.client.records()
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')

    def test_bad_local_session_is_not_forwarded_or_returned(self):
        self.session_value = 'fictional-bad\r\nHeader: injected'
        with self.assertRaises(D0Error) as caught:
            self.client.records()
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.assertEqual([path for path, _, _ in self.requests], ['/fictional-api/session'])
        self.assertNotIn('fictional-bad', str(caught.exception))

    def test_connection_failure_details_are_suppressed(self):
        with patch('app.d0_adapter.http.client.HTTPConnection') as connection:
            connection.return_value.request.side_effect = OSError('private-runtime-address-and-key')
            with self.assertRaises(D0Error) as caught:
                self.client.records()
            self.assertEqual(caught.exception.code, 'CONNECTION_UNAVAILABLE')
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertNotIn('private', str(caught.exception))
            self.assertEqual(connection.call_args.args[0], '127.0.0.1')
            connection.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
