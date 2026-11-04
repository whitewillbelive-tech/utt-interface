"""Transport checks use fictional URLs and stub responses; no network is opened."""
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from app.transport import (ConfigError, HttpReadTransport, TransportError,
                           _NoRedirect, load_transport_from_env)


class Response:
    def __init__(self, body=b'{"status":"fiction"}', status=200, headers=None):
        self.body = body
        self.status = status
        self.headers = {'Content-Type': 'application/json'} if headers is None else headers
        self.closed = False
        self.read_sizes = []

    def read(self, size):
        self.read_sizes.append(size)
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True


class Opener:
    def __init__(self, response=None, failure=None):
        self.response = response or Response()
        self.failure = failure
        self.calls = []

    def open(self, req, timeout):
        self.calls.append((req, timeout))
        if self.failure:
            raise self.failure
        return self.response


class TransportTests(unittest.TestCase):
    def make(self, response=None, failure=None, auth=None):
        opener = Opener(response, failure)
        transport = HttpReadTransport('https://read.example.invalid/api/read',
                                      auth or (lambda: {'Authorization': 'Bearer fictional'}),
                                      max_response_bytes=1024, opener=opener)
        return transport, opener

    def test_wire_read_auth_rotation_and_bounded_read(self):
        token = ['first-fiction']
        response = Response(b'{"result":null}', headers={'Content-Type': 'application/json; charset=utf-8'})
        transport, opener = self.make(response, auth=lambda: {'Authorization': 'Bearer ' + token[0]})
        q = {'operation': 'search', 'query': '가상'}
        self.assertEqual(transport(q, 'id_desc'), {'result': None})
        token[0] = 'second-fiction'
        transport(q)
        first, timeout = opener.calls[0]
        self.assertEqual(first.method, 'POST')
        self.assertEqual(timeout, 10)
        self.assertEqual(json.loads(first.data), {'request': q, 'sort': 'id_desc'})
        self.assertEqual(first.get_header('Authorization'), 'Bearer first-fiction')
        self.assertEqual(opener.calls[1][0].get_header('Authorization'), 'Bearer second-fiction')
        self.assertEqual(first.get_header('Accept'), 'application/json')
        self.assertEqual(first.get_header('Content-type'), 'application/json')
        self.assertEqual(response.read_sizes, [1025, 1025])
        self.assertTrue(response.closed)
        self.assertNotIn('fictional', repr(transport))

    def test_http_status_mapping_and_upstream_body_not_read(self):
        for status, expected_code, expected_http in [
            (400, 'CONNECTION_UNAVAILABLE', 503), (401, 'CONNECTION_UNAVAILABLE', 401),
            (403, 'CONNECTION_UNAVAILABLE', 403), (404, 'CONNECTION_UNAVAILABLE', 503),
            (409, 'CONNECTION_UNAVAILABLE', 503), (410, 'CONNECTION_UNAVAILABLE', 503),
            (301, 'CONNECTION_UNAVAILABLE', 503), (429, 'CONNECTION_UNAVAILABLE', 503),
            (500, 'CONNECTION_UNAVAILABLE', 503), (503, 'CONNECTION_UNAVAILABLE', 503),
        ]:
            with self.subTest(status=status):
                body = io.BytesIO(b'private-upstream-body')
                failure = HTTPError('https://private.invalid/secret', status,
                                    'private-exception', {}, body)
                transport, opener = self.make(failure=failure)
                with self.assertRaises(TransportError) as caught:
                    transport({'operation': 'search'})
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(caught.exception.http_status, expected_http)
                self.assertNotIn('private', str(caught.exception))
                self.assertTrue(body.closed)
                self.assertEqual(len(opener.calls), 1)

    def test_bad_response_and_size_are_sanitized(self):
        cases = [
            Response(b'private-not-json'), Response(b'\xff'), Response(b'[]'),
            Response(b'{"x":1,"x":2}'), Response(b'{"x":NaN}'),
            Response(b'{"x":Infinity}'), Response(b'{"x":1e999}'),
            Response(b'{}', headers={'Content-Type': 'text/html'}),
            Response(b'x' * 1025),
            Response(b'{}', headers={'Content-Type': 'application/json', 'Content-Length': '1025'}),
            Response(b'{}', headers={'Content-Type': 'application/json', 'Content-Length': '-1'}),
            Response(b'{}', headers={'Content-Type': 'application/json', 'Content-Length': 'private-invalid'}),
            Response(b'{}', status=204),
        ]
        for response in cases:
            with self.subTest(body=response.body[:20]):
                transport, opener = self.make(response)
                with self.assertRaises(TransportError) as caught:
                    transport({})
                self.assertEqual(caught.exception.http_status, 503)
                self.assertNotIn('private', str(caught.exception))
                self.assertTrue(response.closed)
                self.assertEqual(len(opener.calls), 1)

    def test_network_auth_and_serialization_failures_are_sanitized(self):
        for failure in (URLError('private-address'), TimeoutError('private-token'), OSError('private-path')):
            transport, opener = self.make(failure=failure)
            with self.assertRaises(TransportError) as caught:
                transport({})
            self.assertNotIn('private', str(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertEqual(len(opener.calls), 1)
        for headers in ({}, {'Authorization': 'Bearer bad\r\nInjected: yes'},
                        {'Host': 'private.invalid'}, {'Bad\nName': 'private'},
                        {'Authorization': None}, {'Authorization': 'private\tvalue'}):
            transport, opener = self.make(auth=lambda: headers)
            with self.assertRaises(TransportError):
                transport({})
            self.assertEqual(opener.calls, [])
        transport, opener = self.make()
        with self.assertRaises(TransportError):
            transport({'bad': float('nan')})
        self.assertEqual(opener.calls, [])

    def test_redirect_handler_denies_without_followup(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302,
                                                       'fiction', {}, 'https://other.invalid/'))
        with patch('app.transport.build_opener') as build:
            HttpReadTransport('https://read.example.invalid/read', lambda: {'Authorization': 'fiction'})
            self.assertIsInstance(build.call_args.args[0], _NoRedirect)

    def test_endpoint_validation_and_limits(self):
        urls = ['', None, 'http://read.example.invalid/read', 'https://user:pass@read.example.invalid',
                'https://read.example.invalid/read?token=private', 'https://read.example.invalid/#private',
                'https://read.example.invalid/?', 'https://read.example.invalid/#',
                'https://read.example.invalid:0/read', 'https://read.example.invalid:65536/read',
                'https://read.example.invalid:/read', 'https://read.example.invalid:bad/read',
                'https://read.example.invalid/\nprivate', 'https://한글.invalid/read',
                'https://read.example.invalid/has space', 'https://read.example.invalid\\private']
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ConfigError) as caught:
                    HttpReadTransport(url, lambda: {'Authorization': 'fiction'})
                self.assertNotIn('private', str(caught.exception))
        for timeout in (False, 0, 0.09, 61, 10 ** 1000, float('nan'), float('inf'), '10'):
            with self.subTest(timeout=timeout), self.assertRaises(ConfigError):
                HttpReadTransport('https://read.example.invalid', lambda: {}, timeout=timeout)
        for limit in (True, 1023, 67108865, 1024.0):
            with self.subTest(limit=limit), self.assertRaises(ConfigError):
                HttpReadTransport('https://read.example.invalid', lambda: {}, max_response_bytes=limit)

    def test_environment_is_explicit_required_and_never_connects(self):
        base = {'UTT_RUNTIME_APPROVED': '1', 'UTT_READ_URL': 'https://read.example.invalid/read',
                'UTT_READ_TOKEN': 'fictional-token'}
        for change in ({'UTT_RUNTIME_APPROVED': '0'}, {'UTT_READ_TOKEN': ''},
                       {'UTT_READ_TOKEN': 'bad\nvalue'}, {'UTT_READ_TIMEOUT_SECONDS': 'nan'},
                       {'UTT_READ_TIMEOUT_SECONDS': '0'}, {'UTT_READ_MAX_RESPONSE_BYTES': 'bad'}):
            with self.subTest(change=change), self.assertRaises(ConfigError):
                load_transport_from_env({**base, **change})
        for missing in ('UTT_RUNTIME_APPROVED', 'UTT_READ_URL', 'UTT_READ_TOKEN'):
            settings = {k: v for k, v in base.items() if k != missing}
            with self.subTest(missing=missing), self.assertRaises(ConfigError):
                load_transport_from_env(settings)
        with patch('app.transport.build_opener', return_value=Opener()) as build:
            settings = dict(base)
            transport = load_transport_from_env(settings)
            self.assertEqual(build.return_value.calls, [])
            settings['UTT_READ_TOKEN'] = 'rotated-fiction'
            transport({})
            self.assertEqual(build.return_value.calls[0][0].get_header('Authorization'),
                             'Bearer rotated-fiction')
            settings['UTT_READ_TOKEN'] = ''
            with self.assertRaises(TransportError):
                transport({})
            self.assertEqual(len(build.return_value.calls), 1)


if __name__ == '__main__':
    unittest.main()
