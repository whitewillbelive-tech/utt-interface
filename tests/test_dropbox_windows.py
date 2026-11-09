"""Fictional loopback OAuth tests; never contact Dropbox or use real credentials."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import http.client
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from app.dropbox_auth import OAuthError, READ_SCOPES, TOKEN_ENDPOINT
from app import dropbox_windows as windows


def unused_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def request(port, target, *, headers=None, method='GET'):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        conn.request(method, target, headers=headers or {})
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def target_for(url, **parameters):
    auth = parse_qs(urlsplit(url).query)
    state = auth['state'][0]
    redirect = urlsplit(auth['redirect_uri'][0])
    return (redirect.path or '/') + '?' + urlencode({'state': state, **parameters})


def complete(port, response):
    status, headers, _ = response
    if status == 303:
        location = {key.lower(): value for key, value in headers.items()}['location']
        return request(port, location)
    return None


class SyntheticBrowser:
    """One user-initiated launch; browser actions run on a separate thread."""
    def __init__(self, port, actions):
        self.port = port
        self.actions = actions
        self.urls = []
        self.responses = []
        self.errors = []
        self.thread = None

    def open(self, url):
        self.urls.append(url)
        def worker():
            try:
                self.actions(self, url)
            except BaseException as error:
                self.errors.append(error)
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()
        return True

    def wait(self):
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise AssertionError('synthetic browser thread did not stop')
        if self.errors:
            raise self.errors[0]


class WindowsOAuthTests(unittest.TestCase):
    def setUp(self):
        self.port = unused_loopback_port()
        self.redirect = f'http://127.0.0.1:{self.port}/oauth/callback'
        self.forms = []

    def fake_exchange(self, endpoint, form):
        self.forms.append((endpoint, form.copy()))
        return {'access_token': 'fictional-token-secret', 'token_type': 'bearer',
                'scope': 'files.metadata.read files.content.read', 'expires_in': 3600}

    def run_with_browser(self, actions, transport=None, *, timeout=2):
        browser = SyntheticBrowser(self.port, actions)
        try:
            session = windows.run_flow('fictional_app_key', self.redirect, self.redirect,
                                       browser_open=browser.open,
                                       transport=transport or self.fake_exchange,
                                       timeout=timeout)
            return session, browser
        finally:
            browser.wait()

    def test_registered_redirect_must_match_and_be_literal_loopback(self):
        windows.validate_redirect(self.redirect, self.redirect)
        for configured, registered in [
            (self.redirect, self.redirect + '/'),
            (self.redirect, self.redirect.replace('/oauth/', '/other/')),
            (self.redirect.replace('127.0.0.1', 'localhost'), self.redirect.replace('127.0.0.1', 'localhost')),
            (self.redirect.replace('127.0.0.1', '127.0.0.2'), self.redirect.replace('127.0.0.1', '127.0.0.2')),
            (self.redirect.replace('http://', 'https://'), self.redirect.replace('http://', 'https://')),
            (self.redirect + '/./x', self.redirect + '/./x'),
            (self.redirect + '/../x', self.redirect + '/../x'),
            (self.redirect + '?code=x', self.redirect + '?code=x'),
            (self.redirect + '#fragment', self.redirect + '#fragment'),
        ]:
            with self.subTest(configured=configured, registered=registered):
                with self.assertRaises(OAuthError):
                    windows.validate_redirect(configured, registered)
        calls = []
        with self.assertRaises(OAuthError):
            windows.run_flow('fictional_app_key', self.redirect, self.redirect + '/',
                             browser_open=lambda url: calls.append(url),
                             transport=lambda *_: calls.append('transport'), timeout=0.1)
        self.assertEqual(calls, [])

    def test_fictional_browser_flow_redacts_and_cleans_up(self):
        def action(browser, url):
            response = request(self.port, target_for(url, code='fictional-code-secret'))
            browser.responses.append(response)
            browser.responses.append(complete(self.port, response))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            session, browser = self.run_with_browser(action)
        self.assertTrue(session.is_valid())
        self.assertEqual(session.scopes, READ_SCOPES)
        self.assertEqual(len(browser.urls), 1)
        parsed = parse_qs(urlsplit(browser.urls[0]).query)
        self.assertEqual(parsed['redirect_uri'], [self.redirect])
        self.assertEqual(parsed['code_challenge_method'], ['S256'])
        self.assertEqual(parsed['token_access_type'], ['online'])
        self.assertEqual(set(parsed['scope'][0].split()), READ_SCOPES)
        self.assertEqual(len(self.forms), 1)
        self.assertEqual(self.forms[0][0], TOKEN_ENDPOINT)
        self.assertNotIn('client_secret', self.forms[0][1])
        status, headers, body = browser.responses[0]
        self.assertEqual(status, 303)
        normalized = {key.lower(): value for key, value in headers.items()}
        self.assertNotIn('fictional-code-secret', normalized.get('location', ''))
        self.assertNotIn('?', normalized.get('location', ''))
        self.assertIn('no-store', normalized.get('cache-control', '').lower())
        self.assertEqual(normalized.get('referrer-policy', '').lower(), 'no-referrer')
        self.assertNotIn(b'fictional-code-secret', body)
        self.assertEqual(browser.responses[1][0], 200)
        complete_headers = {key.lower(): value for key, value in browser.responses[1][1].items()}
        self.assertIn('no-store', complete_headers.get('cache-control', '').lower())
        self.assertEqual(complete_headers.get('referrer-policy', '').lower(), 'no-referrer')
        self.assertNotIn(b'fictional-code-secret', browser.responses[1][2])
        captured = stdout.getvalue() + stderr.getvalue() + repr(session)
        for secret in ('fictional-code-secret', 'fictional-token-secret', self.forms[0][1]['code_verifier']):
            self.assertNotIn(secret, captured)
        # Returning from the flow must have closed the listener.
        with self.assertRaises(OSError):
            request(self.port, '/oauth/callback')
        session.close()
        self.assertFalse(session.is_valid())

    def test_bad_state_bad_host_and_wrong_path_cannot_complete_flow(self):
        def action(browser, url):
            state = parse_qs(urlsplit(url).query)['state'][0]
            browser.responses.append(request(self.port, '/wrong?' + urlencode({'state': state, 'code': 'fake'})))
            browser.responses.append(request(self.port, '/oauth/callback?state=wrong&code=fake'))
            browser.responses.append(request(self.port, target_for(url, code='fictional-code-secret'),
                                             headers={'Host': 'attacker.invalid'}))
            response = request(self.port, target_for(url, code='fictional-code-secret'))
            browser.responses.append(response)
            complete(self.port, response)
        session, browser = self.run_with_browser(action)
        self.assertTrue(session.is_valid())
        self.assertEqual(browser.responses[0][0], 404)
        # Even an invalid state is redirected to remove its query from the
        # address bar, but it must not consume the genuine attempt.
        self.assertEqual(browser.responses[1][0], 303)
        self.assertNotIn('?', {k.lower(): v for k, v in browser.responses[1][1].items()}['location'])
        self.assertEqual(browser.responses[2][0], 403)
        self.assertEqual(browser.responses[3][0], 303)
        self.assertEqual(len(self.forms), 1)
        session.close()

    def test_cancellation_does_not_exchange_and_stops_listener(self):
        def action(browser, url):
            response = request(self.port, target_for(url, error='access_denied',
                                                     error_description='fictional-secret'))
            browser.responses.append(response)
            browser.responses.append(complete(self.port, response))
        with self.assertRaises(OAuthError) as caught:
            self.run_with_browser(action)
        self.assertNotIn('fictional-secret', str(caught.exception))
        self.assertEqual(self.forms, [])
        with self.assertRaises(OSError):
            request(self.port, '/oauth/callback')

    def test_registered_root_uri_without_trailing_slash_is_preserved(self):
        self.redirect = f'http://127.0.0.1:{self.port}'
        def action(browser, url):
            response = request(self.port, target_for(url, code='fictional-code-secret'))
            complete(self.port, response)
        session, browser = self.run_with_browser(action)
        self.assertEqual(parse_qs(urlsplit(browser.urls[0]).query)['redirect_uri'], [self.redirect])
        self.assertEqual(self.forms[0][1]['redirect_uri'], self.redirect)
        session.close()

    def test_slow_partial_headers_cannot_extend_global_timeout(self):
        stopped = threading.Event()
        def action(browser, url):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.5)
                sock.connect(('127.0.0.1', self.port))
                try:
                    sock.sendall(f'GET /oauth/callback HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'.encode())
                    while not stopped.wait(0.03):
                        sock.sendall(b'X-Fictional: value\r\n')
                except OSError:
                    pass
        browser = SyntheticBrowser(self.port, action)
        started = time.monotonic()
        try:
            with self.assertRaises(OAuthError) as caught:
                windows.run_flow('fictional_app_key', self.redirect, self.redirect,
                                 browser_open=browser.open, transport=self.fake_exchange, timeout=0.3)
            elapsed = time.monotonic() - started
        finally:
            stopped.set()
            browser.wait()
        self.assertEqual(caught.exception.code, 'AUTH_TIMEOUT')
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.forms, [])
        with self.assertRaises(OSError):
            request(self.port, '/oauth/callback')

    def test_timeout_closes_listener_and_does_not_exchange(self):
        with self.assertRaises(OAuthError):
            self.run_with_browser(lambda *_: None, timeout=0.2)
        self.assertEqual(self.forms, [])
        with self.assertRaises(OSError):
            request(self.port, '/oauth/callback')

    def test_bound_port_fails_without_browser_launch(self):
        calls = []
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(('127.0.0.1', self.port))
            occupied.listen()
            with self.assertRaises(OAuthError):
                windows.run_flow('fictional_app_key', self.redirect, self.redirect,
                                 browser_open=lambda url: calls.append(url),
                                 transport=self.fake_exchange, timeout=0.2)
        self.assertEqual(calls, [])
        self.assertEqual(self.forms, [])

    def test_token_scope_write_refresh_or_missing_fails_closed(self):
        for extra in ({'scope': 'files.metadata.read files.content.read files.content.write'},
                      {'scope': None}, {'refresh_token': 'fictional-refresh'}):
            with self.subTest(extra=extra):
                self.port = unused_loopback_port()
                self.redirect = f'http://127.0.0.1:{self.port}/oauth/callback'
                def bad_transport(endpoint, form):
                    return {'access_token': 'fictional-token-secret', 'token_type': 'bearer',
                            'scope': 'files.metadata.read files.content.read', 'expires_in': 3600, **extra}
                def action(browser, url):
                    response = request(self.port, target_for(url, code='fictional-code-secret'))
                    browser.responses.append(response)
                    complete(self.port, response)
                with self.assertRaises(OAuthError) as caught:
                    self.run_with_browser(action, bad_transport)
                self.assertNotIn('fictional-token-secret', str(caught.exception))
                with self.assertRaises(OSError):
                    request(self.port, '/oauth/callback')

    def test_duplicate_callback_exchanges_only_once(self):
        def action(browser, url):
            target = target_for(url, code='fictional-code-secret')
            first = request(self.port, target)
            second = request(self.port, target)
            browser.responses.extend([first, second])
            complete(self.port, first)
        session, browser = self.run_with_browser(action)
        self.assertTrue(session.is_valid())
        self.assertEqual(len(self.forms), 1)
        for status, headers, body in browser.responses:
            self.assertEqual(status, 303)
            self.assertNotIn('?', {k.lower(): v for k, v in headers.items()}['location'])
            self.assertNotIn(b'fictional-code-secret', body)
        session.close()

    def test_concurrent_callbacks_exchange_only_once(self):
        def action(browser, url):
            target = target_for(url, code='fictional-code-secret')
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: request(self.port, target), range(2)))
            browser.responses.extend(responses)
            complete(self.port, responses[0])
        session, browser = self.run_with_browser(action)
        self.assertTrue(session.is_valid())
        self.assertEqual(len(self.forms), 1)
        self.assertEqual([response[0] for response in browser.responses], [303, 303])
        session.close()

    def test_unsupported_method_never_echoes_fictional_values(self):
        def action(browser, url):
            response = request(self.port, target_for(url, code='fictional-code-secret'), method='POST')
            browser.responses.append(response)
            response = request(self.port, target_for(url, code='fictional-code-secret'))
            browser.responses.append(response)
            complete(self.port, response)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            session, browser = self.run_with_browser(action)
        self.assertEqual(browser.responses[0][0], 405)
        self.assertNotIn(b'fictional-code-secret', browser.responses[0][2])
        self.assertNotIn('fictional-code-secret', stdout.getvalue() + stderr.getvalue())
        session.close()

    def test_browser_unavailable_or_error_cleans_up(self):
        for opener in (lambda _: False, lambda _: (_ for _ in ()).throw(
                RuntimeError('fictional-browser-secret'))):
            with self.subTest(opener=opener):
                self.port = unused_loopback_port()
                self.redirect = f'http://127.0.0.1:{self.port}/oauth/callback'
                with self.assertRaises(OAuthError) as caught:
                    windows.run_flow('fictional_app_key', self.redirect, self.redirect,
                                     browser_open=opener, transport=self.fake_exchange, timeout=0.2)
                self.assertEqual(caught.exception.code, 'BROWSER_UNAVAILABLE')
                self.assertNotIn('fictional-browser-secret', str(caught.exception))
                with self.assertRaises(OSError):
                    request(self.port, '/oauth/callback')
        self.assertEqual(self.forms, [])

    def test_listener_closed_before_token_exchange(self):
        close_calls = []
        original_close = windows._LocalServer.server_close
        def monitored_close(server):
            original_close(server)
            close_calls.append(True)
        def exchange(endpoint, form):
            self.assertTrue(close_calls, 'token exchange started while callback listener remained open')
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(1)
                self.assertNotEqual(probe.connect_ex(('127.0.0.1', self.port)), 0)
            return self.fake_exchange(endpoint, form)
        def action(browser, url):
            response = request(self.port, target_for(url, code='fictional-code-secret'))
            complete(self.port, response)
        with patch.object(windows._LocalServer, 'server_close', monitored_close):
            session, _ = self.run_with_browser(action, exchange)
        session.close()

    def test_token_response_expiry_decimal_string_and_invalid_values(self):
        for expiry in ('3600', '0', '-1', '1e3', '3.5', '999999999999999999999999', True, None):
            with self.subTest(expiry=expiry):
                self.port = unused_loopback_port()
                self.redirect = f'http://127.0.0.1:{self.port}/oauth/callback'
                def transport(endpoint, form):
                    return {'access_token': 'fictional-token-secret', 'token_type': 'bearer',
                            'scope': 'files.metadata.read files.content.read', 'expires_in': expiry}
                def action(browser, url):
                    response = request(self.port, target_for(url, code='fictional-code-secret'))
                    complete(self.port, response)
                if expiry == '3600':
                    session, _ = self.run_with_browser(action, transport)
                    self.assertTrue(session.is_valid())
                    session.close()
                else:
                    with self.assertRaises(OAuthError):
                        self.run_with_browser(action, transport)

    def test_fixed_https_transport_tls_timeout_post_and_redaction(self):
        form = {'code': 'fictional-code-secret', 'grant_type': 'authorization_code',
                'client_id': 'fictional-app', 'redirect_uri': self.redirect,
                'code_verifier': 'fictional-verifier'}
        response_body = json.dumps({'access_token': 'fictional-token-secret', 'token_type': 'bearer',
                                    'scope': 'files.metadata.read files.content.read', 'expires_in': '3600'}).encode()
        calls = []
        class FakeResponse:
            status = 200
            def read(self, size):
                calls.append(('read', size))
                return response_body
        class FakeConnection:
            def __init__(self, host, port, **kwargs):
                calls.append(('connect', host, port, kwargs))
            def request(self, method, path, **kwargs):
                calls.append(('request', method, path, kwargs))
            def getresponse(self):
                return FakeResponse()
            def close(self):
                calls.append(('close',))
        result = windows.token_transport(TOKEN_ENDPOINT, form, connection_factory=FakeConnection)
        self.assertEqual(result['access_token'], 'fictional-token-secret')
        _, host, port, options = calls[0]
        self.assertEqual((host, port), ('api.dropboxapi.com', 443))
        self.assertGreater(options['timeout'], 0)
        self.assertTrue(options['context'].check_hostname)
        self.assertNotEqual(options['context'].verify_mode, 0)
        self.assertEqual(calls[1][1:3], ('POST', '/oauth2/token'))
        self.assertEqual(calls[2], ('read', 65537))
        self.assertEqual(calls[-1], ('close',))
        calls.clear()
        with self.assertRaises(OAuthError):
            windows.token_transport('https://attacker.invalid/oauth2/token', form, connection_factory=FakeConnection)
        self.assertEqual(calls, [])

    def test_https_redirect_invalid_json_duplicate_keys_and_overlong_response_rejected(self):
        form = {'code': 'fictional-code-secret', 'grant_type': 'authorization_code',
                'client_id': 'fictional-app', 'redirect_uri': self.redirect,
                'code_verifier': 'fictional-verifier'}
        scenarios = [(302, b'{}'), (307, b'{}'), (500, b'fictional-token-secret'),
                     (200, b'{invalid'), (200, b'{"scope":"one","scope":"two"}'),
                     (200, b'x' * 65537)]
        for status, body in scenarios:
            with self.subTest(status=status, length=len(body)):
                closed = []
                class Response:
                    def __init__(self):
                        self.status = status
                    def read(self, size):
                        return body
                class Connection:
                    def __init__(self, *args, **kwargs):
                        pass
                    def request(self, *args, **kwargs):
                        pass
                    def getresponse(self):
                        return Response()
                    def close(self):
                        closed.append(True)
                with self.assertRaises(OAuthError) as caught:
                    windows.token_transport(TOKEN_ENDPOINT, form, connection_factory=Connection)
                self.assertNotIn('fictional-token-secret', str(caught.exception))
                self.assertEqual(closed, [True])

    def test_https_certificate_failure_sanitized(self):
        form = {'code': 'fictional-code-secret', 'grant_type': 'authorization_code',
                'client_id': 'fictional-app', 'redirect_uri': self.redirect,
                'code_verifier': 'fictional-verifier'}
        class FailingConnection:
            def __init__(self, *args, **kwargs):
                raise OSError('fictional-code-secret')
        with self.assertRaises(OAuthError) as caught:
            windows.token_transport(TOKEN_ENDPOINT, form, connection_factory=FailingConnection)
        self.assertNotIn('fictional-code-secret', str(caught.exception))

    def test_windows_cli_reads_only_local_key_and_displays_scope_and_expiry(self):
        class Session:
            scopes = READ_SCOPES
            expires_at = windows.time.monotonic() + 300
            def is_valid(self):
                return True
            def close(self):
                self.closed = True
        session = Session()
        output = io.StringIO()
        with patch.object(windows.sys, 'platform', 'win32'), \
             patch('builtins.input', side_effect=[self.redirect, self.redirect, 'START']), \
             patch.object(windows.getpass, 'getpass', return_value='fictional-app-key'), \
             patch.object(windows, 'run_flow', return_value=session) as run, \
             contextlib.redirect_stdout(output):
            self.assertEqual(windows.main([]), 0)
        run.assert_called_once_with('fictional-app-key', self.redirect, self.redirect)
        self.assertTrue(session.closed)
        self.assertIn('files.metadata.read', output.getvalue())
        self.assertIn('files.content.read', output.getvalue())
        self.assertNotIn('fictional-app-key', output.getvalue())
        with patch.object(windows.sys, 'platform', 'win32'), \
             patch('builtins.input', side_effect=[self.redirect, self.redirect + '/']), \
             patch.object(windows.getpass, 'getpass') as get_key, \
             patch.object(windows, 'run_flow') as run, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(windows.main([]), 2)
        get_key.assert_not_called()
        run.assert_not_called()

    def test_windows_diagnostic_uses_local_browser_no_dropbox_or_key(self):
        responses, errors, threads = [], [], []
        def synthetic_browser(url):
            parsed = urlsplit(url)
            self.assertEqual(parsed.netloc, f'127.0.0.1:{self.port}')
            self.assertNotIn('dropbox', url)
            def worker():
                try:
                    response = request(self.port, (parsed.path or '/') + '?' + parsed.query)
                    responses.append(response)
                    responses.append(complete(self.port, response))
                except BaseException as error:
                    errors.append(error)
            thread = threading.Thread(target=worker, daemon=True)
            threads.append(thread)
            thread.start()
            return True
        output = io.StringIO()
        with patch.object(windows.sys, 'platform', 'win32'), \
             patch('builtins.input', side_effect=[self.redirect]), \
             patch.object(windows.getpass, 'getpass') as get_key, \
             patch.object(windows.webbrowser, 'open', side_effect=synthetic_browser), \
             patch.object(windows.http.client, 'HTTPSConnection', side_effect=AssertionError('HTTPS forbidden')), \
             contextlib.redirect_stdout(output):
            result = windows.main(['--diagnose'])
        for thread in threads:
            thread.join(timeout=5)
        if errors:
            raise errors[0]
        self.assertEqual(result, 0)
        self.assertEqual([response[0] for response in responses], [303, 200])
        self.assertIn('PASS', output.getvalue())
        get_key.assert_not_called()
