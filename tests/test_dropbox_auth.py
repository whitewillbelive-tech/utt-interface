"""Fictional OAuth-only tests: no real Dropbox or network requests."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from app.dropbox_auth import AuthAttempt, OAuthError, READ_SCOPES, TOKEN_ENDPOINT, ReadOnlySession


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class DropboxAuthSyntheticTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.clock = FakeClock()
        self.attempt = AuthAttempt('fictional_app_key', 'http://127.0.0.1:54321/callback', clock=self.clock)
        self.auth = parse_qs(urlsplit(self.attempt.authorization_url()).query)

    def callback(self, **extra):
        return '/callback?' + urlencode({'state': self.auth['state'][0], **extra})

    def accepted(self):
        self.attempt.accept_callback(self.callback(code='fictional-code'))

    def good_response(self):
        return {'access_token': 'fictional-token', 'token_type': 'bearer',
                'scope': 'files.metadata.read files.content.read', 'expires_in': 3600}

    def assert_error(self, code, action):
        with self.assertRaises(OAuthError) as error:
            action()
        self.assertEqual(error.exception.code, code)
        return error.exception

    def test_identification_authorization_and_pkce_exchange(self):
        u = urlsplit(self.attempt.authorization_url())
        self.assertEqual((u.scheme, u.netloc, u.path), ('https', 'www.dropbox.com', '/oauth2/authorize'))
        self.assertEqual(self.auth['client_id'], ['fictional_app_key'])
        self.assertEqual(self.auth['redirect_uri'], ['http://127.0.0.1:54321/callback'])
        self.assertEqual(self.auth['response_type'], ['code'])
        self.assertEqual(self.auth['token_access_type'], ['online'])
        self.assertEqual(set(self.auth['scope'][0].split()), READ_SCOPES)
        self.assertEqual(self.auth['code_challenge_method'], ['S256'])
        self.assertNotIn('client_secret', self.auth)
        self.accepted()
        captured = []

        def transport(endpoint, form):
            captured.append((endpoint, form.copy()))
            return self.good_response()

        session = self.attempt.exchange(transport)
        self.assertEqual(captured[0][0], TOKEN_ENDPOINT)
        form = captured[0][1]
        self.assertEqual(form['client_id'], 'fictional_app_key')
        self.assertEqual(form['redirect_uri'], self.auth['redirect_uri'][0])
        self.assertEqual(form['code'], 'fictional-code')
        self.assertNotIn('client_secret', form)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode('ascii')).digest()).rstrip(b'=').decode('ascii')
        self.assertEqual(challenge, self.auth['code_challenge'][0])
        self.assertTrue(session.is_valid())
        self.assertEqual(session.scopes, READ_SCOPES)
        self.assertNotIn('fictional-token', repr(session))
        self.assertNotIn('fictional-code', repr(self.attempt))
        self.assert_error('ATTEMPT_CLOSED', lambda: self.attempt.exchange(transport))

    def test_bad_app_key_and_redirects_are_rejected(self):
        for key in ('', ' spaces', 'secret\n', None, 42):
            with self.subTest(key=key):
                self.assert_error('INVALID_APP_KEY', lambda: AuthAttempt(key, 'http://localhost:5000/callback'))
        for redirect in ('https://localhost:5000/callback', 'http://example.com:5000/callback',
                         'http://127.0.0.2:5000/callback', 'http://localhost/callback',
                         'http://localhost:5000/callback?x=1', 'http://localhost:5000/callback#fragment',
                         'http://name@localhost:5000/callback', 'http://localhost:5000//callback',
                         'http://localhost:5000/%63allback', 'http://localhost:80/callback'):
            with self.subTest(redirect=redirect):
                self.assert_error('INVALID_REDIRECT', lambda: AuthAttempt('fictional', redirect))

    def test_unique_state_and_verifier_for_each_attempt(self):
        other = AuthAttempt('fictional_app_key', 'http://127.0.0.1:54321/callback', clock=self.clock)
        other_auth = parse_qs(urlsplit(other.authorization_url()).query)
        self.assertNotEqual(self.auth['state'], other_auth['state'])
        self.assertNotEqual(self.auth['code_challenge'], other_auth['code_challenge'])
        self.assert_error('STATE_MISMATCH', lambda: other.accept_callback(self.callback(code='fictional-code')))

    def test_callback_state_origin_path_and_duplicate_checks(self):
        state = self.auth['state'][0]
        cases = [('/other?state=' + state + '&code=x', 'INVALID_CALLBACK'),
                 ('https://attacker.invalid/callback?state=' + state + '&code=x', 'INVALID_CALLBACK'),
                 ('/callback?state=' + state + '&code=x#fragment', 'INVALID_CALLBACK'),
                 ('/callback?state=' + state + '&state=' + state + '&code=x', 'INVALID_CALLBACK'),
                 ('/callback?state=' + state + '&code=x&code=y', 'INVALID_CALLBACK'),
                 ('/call\nback?state=' + state + '&code=x', 'INVALID_CALLBACK'),
                 ('/callback?state=' + state + '&code=x\r', 'INVALID_CALLBACK'),
                 ('/callback?state=' + state + '&code=x y', 'INVALID_CALLBACK'),
                 ('/callback?state=wrong&code=x', 'STATE_MISMATCH'),
                 ('/callback?code=x', 'STATE_MISMATCH'),
                 ('/callback?state=' + state + '&code=', 'INVALID_CALLBACK'),
                 ('/callback?state=' + state + '&code=x&error=access_denied', 'INVALID_CALLBACK')]
        for target, code in cases:
            with self.subTest(target=target):
                self.assert_error(code, lambda: self.attempt.accept_callback(target))
        self.accepted()
        self.assert_error('ATTEMPT_CLOSED', lambda: self.attempt.accept_callback(self.callback(code='x')))

    def test_cancel_and_error_are_terminal_and_do_not_echo_remote_text(self):
        error = self.assert_error('AUTH_CANCELLED', lambda: self.attempt.accept_callback(
            self.callback(error='access_denied', error_description='fictional-secret')))
        self.assertNotIn('fictional-secret', str(error))
        self.assert_error('ATTEMPT_CLOSED', lambda: self.attempt.accept_callback(self.callback(code='x')))
        other = AuthAttempt('fictional', 'http://localhost:5000/callback', clock=self.clock)
        auth = parse_qs(urlsplit(other.authorization_url()).query)
        self.assert_error('AUTH_FAILED', lambda: other.accept_callback('/callback?' + urlencode(
            {'state': auth['state'][0], 'error': 'server_error', 'error_description': 'fictional-secret'})))
        self.assert_error('ATTEMPT_CLOSED', lambda: other.exchange(lambda *_: self.good_response()))

    def test_explicit_cancel_and_attempt_expiry(self):
        self.attempt.cancel()
        self.assert_error('ATTEMPT_CLOSED', self.attempt.authorization_url)
        other = AuthAttempt('fictional', 'http://localhost:5000/callback', clock=self.clock)
        self.clock.now += 300
        self.assert_error('ATTEMPT_EXPIRED', other.authorization_url)
        self.assert_error('ATTEMPT_CLOSED', other.authorization_url)

    def test_exchange_expiry_prevents_transport_call(self):
        self.accepted()
        self.clock.now += 300
        calls = []
        self.assert_error('ATTEMPT_EXPIRED', lambda: self.attempt.exchange(lambda *a: calls.append(a)))
        self.assertEqual(calls, [])

    def test_scope_actual_token_response_fail_closed(self):
        for scope, expected in ((None, 'SCOPE_UNVERIFIED'), ('', 'SCOPE_REJECTED'),
                                ('files.metadata.read', 'SCOPE_REJECTED'),
                                ('files.metadata.read files.content.read files.content.write', 'SCOPE_REJECTED'),
                                ('files.metadata.read files.content.read sharing.write', 'SCOPE_REJECTED'),
                                ('files.metadata.read files.metadata.read', 'SCOPE_REJECTED'),
                                (['files.metadata.read', 'files.content.read'], 'SCOPE_UNVERIFIED')):
            with self.subTest(scope=scope):
                attempt = AuthAttempt('fictional', 'http://localhost:5000/callback', clock=self.clock)
                state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
                attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
                response = self.good_response()
                if scope is None:
                    del response['scope']
                else:
                    response['scope'] = scope
                self.assert_error(expected, lambda: attempt.exchange(lambda *_: response))
                self.assert_error('ATTEMPT_CLOSED', lambda: attempt.exchange(lambda *_: self.good_response()))

    def test_invalid_token_fields_and_refresh_token_are_rejected(self):
        for changes in ({'access_token': ''}, {'access_token': 'token\n'}, {'token_type': 'mac'},
                        {'token_type': None}, {'expires_in': 0}, {'expires_in': True},
                        {'expires_in': float('nan')}, {'expires_in': 86401},
                        {'refresh_token': 'fictional-refresh'}):
            with self.subTest(changes=changes):
                attempt = AuthAttempt('fictional', 'http://localhost:5000/callback', clock=self.clock)
                state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
                attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
                response = {**self.good_response(), **changes}
                self.assert_error('INVALID_TOKEN_RESPONSE', lambda: attempt.exchange(lambda *_: response))

    def test_token_expiry_close_and_slow_exchange(self):
        self.accepted()
        session = self.attempt.exchange(lambda *_: {**self.good_response(), 'expires_in': 10})
        self.clock.now += 9
        self.assertTrue(session.is_valid())
        self.clock.now += 1
        self.assertFalse(session.is_valid())
        session.close()
        self.assertFalse(session.is_valid())
        attempt = AuthAttempt('fictional', 'http://localhost:5000/callback', clock=self.clock)
        state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
        attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
        def slow_transport(*_):
            self.clock.now += 10
            return {**self.good_response(), 'expires_in': 10}
        self.assert_error('TOKEN_EXPIRED', lambda: attempt.exchange(slow_transport))

    def test_transport_error_redacted_and_one_shot(self):
        self.accepted()
        def failed(*_):
            raise RuntimeError('fictional-code fictional-token fictional-verifier')
        error = self.assert_error('TOKEN_EXCHANGE_FAILED', lambda: self.attempt.exchange(failed))
        self.assertNotIn('fictional-code', str(error))
        self.assertIsNone(error.__cause__)
        self.assert_error('ATTEMPT_CLOSED', lambda: self.attempt.exchange(lambda *_: self.good_response()))

    def test_verifier_tampering_rejected_before_transport(self):
        self.accepted()
        self.attempt._verifier = 'A' * 43
        calls = []
        self.assert_error('INVALID_VERIFIER', lambda: self.attempt.exchange(lambda *args: calls.append(args)))
        self.assertEqual(calls, [])

    def test_concurrent_callbacks_and_exchanges_are_single_use(self):
        target = self.callback(code='fictional-code')

        def callback(_):
            try:
                self.attempt.accept_callback(target)
                return 'accepted'
            except OAuthError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(callback, range(8)))
        self.assertEqual(results.count('accepted'), 1)
        self.assertEqual(results.count('ATTEMPT_CLOSED'), 7)
        calls = []

        def transport(*_):
            calls.append('called')
            return self.good_response()

        def exchange(_):
            try:
                self.attempt.exchange(transport)
                return 'accepted'
            except OAuthError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(exchange, range(8)))
        self.assertEqual(results.count('accepted'), 1)
        self.assertEqual(results.count('ATTEMPT_CLOSED'), 7)
        self.assertEqual(calls, ['called'])

    def test_direct_session_creation_without_verified_exchange_is_rejected(self):
        self.assert_error('INVALID_SESSION_CONSTRUCTION',
                          lambda: ReadOnlySession('fictional-token', 9999, self.clock))

    def test_production_command_fails_closed_without_accepting_configuration(self):
        # The actual CLI must neither ask for nor display the supplied marker.
        # All values here are fictional and the command has no network entrypoint.
        environment = {**os.environ, 'UTT_DROPBOX_APP_KEY': 'fictional-sensitive-marker'}
        result = subprocess.run([sys.executable, '-m', 'app.dropbox_auth'],
                                input='fictional-sensitive-marker\n', text=True,
                                capture_output=True, env=environment, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('fictional-sensitive-marker', result.stdout + result.stderr)
        self.assertIn('unavailable', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()
