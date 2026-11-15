"""Fictional publisher OAuth responses; never access a live account."""
import base64
import contextlib
import hashlib
import http.client
import io
import json
import socket
import sys
import threading
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from app.dropbox_auth import OAuthError, READ_SCOPES, token_transport
from app.publisher_auth import PublisherAuthAttempt, PublisherSession, PUBLISHER_SCOPES, run_publisher_flow


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeTransportError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fake_response(**changes):
    return {'access_token': 'fictional-write-token', 'token_type': 'bearer',
            'scope': 'files.metadata.read files.content.read files.content.write',
            'expires_in': 3600, **changes}


def accept(attempt):
    values = parse_qs(urlsplit(attempt.authorization_url()).query)
    attempt.accept_callback('/callback?' + urlencode({'state': values['state'][0], 'code': 'fictional-code'}))
    return values


def fake_session(response=None):
    attempt = PublisherAuthAttempt('fictional-app', 'http://127.0.0.1:54321/callback')
    accept(attempt)
    return attempt.exchange(lambda *_: fake_response() if response is None else response)


def trusted_session():
    """Use the real fixed transport with an in-memory fake HTTPS connection."""
    class Response:
        status = 200
        def read(self, _):
            return json.dumps(fake_response()).encode()

    class Connection:
        def request(self, *args, **kwargs):
            pass
        def getresponse(self):
            return Response()
        def close(self):
            pass

    attempt = PublisherAuthAttempt('fictional-app', 'http://127.0.0.1:54321/callback')
    accept(attempt)
    with patch('app.dropbox_auth.http.client.HTTPSConnection', return_value=Connection()):
        return attempt.exchange(token_transport)


class PublisherAuthTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_url_and_exchange_request_exact_write_scopes_pkce_online(self):
        attempt = PublisherAuthAttempt('fictional-app', 'http://127.0.0.1:54321/callback')
        values = accept(attempt)
        self.assertEqual(set(values['scope'][0].split(' ')), PUBLISHER_SCOPES)
        self.assertEqual(READ_SCOPES, PUBLISHER_SCOPES - {'files.content.write'})
        self.assertEqual(values['token_access_type'], ['online'])
        self.assertEqual(values['code_challenge_method'], ['S256'])
        self.assertNotIn('client_secret', values)
        captured = []
        def transport(endpoint, form):
            captured.append(form.copy())
            return fake_response()
        session = attempt.exchange(transport)
        self.addCleanup(session.close)
        verifier = captured[0]['code_verifier']
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(challenge, values['code_challenge'][0])
        self.assertTrue(session.is_valid())
        self.assertNotIn('fictional-write-token', repr(session))
        with self.assertRaises(OAuthError) as exc:
            attempt.exchange(transport)
        self.assertEqual(exc.exception.code, 'ATTEMPT_CLOSED')

    def test_each_scope_must_be_granted_without_any_extra(self):
        for scopes in ('files.metadata.read files.content.read',
                       'files.metadata.read files.content.read files.content.write sharing.write',
                       'files.metadata.read files.metadata.read files.content.write', ''):
            with self.subTest(scopes=scopes):
                attempt = PublisherAuthAttempt('fictional-app', 'http://localhost:54321/callback')
                accept(attempt)
                with self.assertRaises(OAuthError) as exc:
                    attempt.exchange(lambda *_: fake_response(scope=scopes))
                self.assertEqual(exc.exception.code, 'SCOPE_REJECTED')

    def test_missing_scope_refresh_token_bad_types_and_expiry_are_rejected(self):
        for response in (fake_response(scope=None), fake_response(refresh_token='fictional-refresh'),
                         fake_response(expires_in=True), fake_response(expires_in=0),
                         fake_response(access_token='x\nsecret'), fake_response(token_type='other')):
            with self.subTest(keys=sorted(response)):
                attempt = PublisherAuthAttempt('fictional-app', 'http://localhost:54321/callback')
                accept(attempt)
                with self.assertRaises(OAuthError):
                    attempt.exchange(lambda *_: response)

    def test_callback_state_and_cancellation_remain_one_shot(self):
        attempt = PublisherAuthAttempt('fictional-app', 'http://localhost:54321/callback')
        state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
        with self.assertRaises(OAuthError) as exc:
            attempt.accept_callback('/callback?state=wrong&code=fictional-code')
        self.assertEqual(exc.exception.code, 'STATE_MISMATCH')
        with self.assertRaises(OAuthError) as exc:
            attempt.accept_callback('/callback?' + urlencode({'state': state, 'error': 'access_denied'}))
        self.assertEqual(exc.exception.code, 'AUTH_CANCELLED')
        with self.assertRaises(OAuthError):
            attempt.exchange(lambda *_: fake_response())

    def test_verifier_mutation_and_attempt_expiry_prevent_exchange(self):
        clock = Clock()
        attempt = PublisherAuthAttempt('fictional-app', 'http://localhost:54321/callback', clock=clock)
        accept(attempt)
        attempt._verifier = 'A' * 43
        with self.assertRaises(OAuthError) as exc:
            attempt.exchange(lambda *_: fake_response())
        self.assertEqual(exc.exception.code, 'INVALID_VERIFIER')
        attempt = PublisherAuthAttempt('fictional-app', 'http://localhost:54321/callback', clock=clock)
        accept(attempt)
        clock.now += 300
        with self.assertRaises(OAuthError) as exc:
            attempt.exchange(lambda *_: fake_response())
        self.assertEqual(exc.exception.code, 'ATTEMPT_EXPIRED')

    def test_constructor_guard_and_hidden_result(self):
        with self.assertRaises(OAuthError):
            PublisherSession('fictional-write-token', 'fictional-app', lambda: 0, 10, True)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            session = fake_session()
            self.addCleanup(session.close)
            repr(session)
        self.assertNotIn('fictional-write-token', output.getvalue() + repr(session))

    def test_only_canonical_dispatcher_accepts_trusted_session(self):
        module = types.ModuleType('app.publisher_transport')
        module.PublisherTransportError = FakeTransportError
        seen = []
        def dispatch(token, operation):
            seen.append((token, operation))
            return 'FIXED_STATUS'
        module._dispatch = dispatch
        session = trusted_session()
        self.addCleanup(session.close)
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(lambda *_: None, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'PUBLISH_TRANSPORT_UNTRUSTED')
            for key, folder, code in [('fictional-other', 'USER_CONFIRMED', 'APP_KEY_MISMATCH'),
                                      ('fictional-app', 'unknown', 'APP_FOLDER_UNCONFIRMED')]:
                with self.subTest(code=code), self.assertRaises(OAuthError) as exc:
                    session._publisher_call(dispatch, 'operation', confirmed_app_key=key,
                                            app_folder_confirmation=folder)
                self.assertEqual(exc.exception.code, code)
            self.assertEqual(seen, [])
            self.assertEqual(session._publisher_call(dispatch, 'operation', confirmed_app_key='fictional-app',
                                                     app_folder_confirmation='USER_CONFIRMED'), 'FIXED_STATUS')
            self.assertEqual(len(seen), 1)

    def test_fake_transport_cannot_authorize_network_dispatch(self):
        module = types.ModuleType('app.publisher_transport')
        module.PublisherTransportError = FakeTransportError
        calls = []
        module._dispatch = lambda *x: calls.append(x)
        session = fake_session()
        self.addCleanup(session.close)
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(module._dispatch, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
        self.assertEqual(exc.exception.code, 'PUBLISH_AUTH_UNVERIFIED')
        self.assertEqual(calls, [])

    def test_auth_rejection_forgets_token_and_other_errors_are_sanitized(self):
        module = types.ModuleType('app.publisher_transport')
        module.PublisherTransportError = FakeTransportError
        def denied(_token, _operation):
            raise FakeTransportError('AUTH_REQUIRED')
        module._dispatch = denied
        session = trusted_session()
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            with self.assertRaises(FakeTransportError) as exc:
                session._publisher_call(denied, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'AUTH_REQUIRED')
            self.assertFalse(session.is_valid())
        def unknown(_token, _operation):
            raise RuntimeError('fictional-secret-token')
        module._dispatch = unknown
        session = trusted_session()
        self.addCleanup(session.close)
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(unknown, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'PUBLISH_REQUEST_UNCERTAIN')
            self.assertNotIn('fictional-secret-token', str(exc.exception))

    def test_expiry_after_dispatch_reports_uncertain_and_never_reuses_grant(self):
        module = types.ModuleType('app.publisher_transport')
        module.PublisherTransportError = FakeTransportError
        session = trusted_session()
        self.addCleanup(session.close)
        def dispatch(_token, _operation):
            # Deterministically model expiry while an operation is in flight.
            session._PublisherSession__deadline = 0
            return 'WRITE_MAY_HAVE_SUCCEEDED'
        module._dispatch = dispatch
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(dispatch, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'AUTH_EXPIRED_AFTER_REQUEST')
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(dispatch, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'AUTH_EXPIRED')

    def test_untrusted_test_clock_cannot_mint_publish_provenance(self):
        class Response:
            status = 200
            def read(self, _):
                return json.dumps(fake_response()).encode()
        class Connection:
            def request(self, *args, **kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        module = types.ModuleType('app.publisher_transport')
        module.PublisherTransportError = FakeTransportError
        module._dispatch = lambda *_: 'SHOULD_NOT_EXECUTE'
        attempt = PublisherAuthAttempt('fictional-app', 'http://127.0.0.1:54321/callback', clock=Clock())
        accept(attempt)
        with patch('app.dropbox_auth.http.client.HTTPSConnection', return_value=Connection()):
            session = attempt.exchange(token_transport)
        self.addCleanup(session.close)
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(module._dispatch, 'operation', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'PUBLISH_AUTH_UNVERIFIED')

    def test_close_serializes_with_request_and_invalidates_session(self):
        module = types.ModuleType('app.publisher_transport')
        module.PublisherTransportError = FakeTransportError
        entered, proceed, closed = threading.Event(), threading.Event(), threading.Event()
        def dispatch(_token, _operation):
            entered.set()
            self.assertTrue(proceed.wait(3))
            return 'FIXED_STATUS'
        module._dispatch = dispatch
        session = trusted_session()
        answers = []
        def execute():
            answers.append(session._publisher_call(dispatch, 'op', confirmed_app_key='fictional-app',
                                                   app_folder_confirmation='USER_CONFIRMED'))
        with patch.dict(sys.modules, {'app.publisher_transport': module}):
            worker = threading.Thread(target=execute)
            worker.start()
            self.assertTrue(entered.wait(3))
            closer = threading.Thread(target=lambda: (session.close(), closed.set()))
            closer.start()
            self.assertFalse(closed.wait(0.05))
            proceed.set()
            worker.join(3)
            closer.join(3)
            self.assertEqual(answers, ['FIXED_STATUS'])
            self.assertTrue(closed.is_set())
            with self.assertRaises(OAuthError) as exc:
                session._publisher_call(dispatch, 'op', confirmed_app_key='fictional-app',
                                        app_folder_confirmation='USER_CONFIRMED')
            self.assertEqual(exc.exception.code, 'AUTH_EXPIRED')

    def test_malformed_redirect_stops_before_browser(self):
        seen = []
        with self.assertRaises(OAuthError):
            run_publisher_flow('fictional-app', 'http://localhost:54321/callback',
                               'http://localhost:54321/callback', browser_open=lambda x: seen.append(x))
        self.assertEqual(seen, [])


class PublisherLoopbackTests(unittest.TestCase):
    def test_synthetic_browser_uses_existing_callback_and_closes_listener(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        uri = f'http://127.0.0.1:{port}/callback'
        errors = []
        worker = []
        def browser(url):
            def visit():
                try:
                    state = parse_qs(urlsplit(url).query)['state'][0]
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                    conn.request('GET', '/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
                    response = conn.getresponse()
                    self.assertEqual(response.status, 303)
                    location = response.getheader('Location')
                    self.assertNotIn('fictional-code', location)
                    response.read()
                    conn.close()
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
                    conn.request('GET', location)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b'fictional-code', response.read())
                    conn.close()
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=visit, daemon=True)
            worker.append(thread)
            thread.start()
            return True
        session = run_publisher_flow('fictional-app', uri, uri, browser_open=browser,
                                     transport=lambda *_: fake_response(), timeout=4)
        self.addCleanup(session.close)
        worker[0].join(4)
        self.assertFalse(worker[0].is_alive())
        if errors:
            raise errors[0]
        self.assertTrue(session.is_valid())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1)
            self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)


if __name__ == '__main__':
    unittest.main()
