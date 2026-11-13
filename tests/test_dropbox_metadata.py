"""Fictional get_metadata responses only; no Dropbox or external network."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import socket
import ssl
import threading
import time
import unittest
import warnings
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from app import dropbox_auth as auth
from app.dropbox_auth import AuthAttempt
from app import dropbox_metadata as metadata
from app import dropbox_metadata_windows as metadata_windows


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fictional_session(clock):
    attempt = AuthAttempt('fictional-app-key', 'http://127.0.0.1:54321/callback', clock=clock)
    state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
    attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
    return attempt.exchange(lambda *_: {
        'access_token': 'fictional-token', 'token_type': 'bearer',
        'scope': 'files.metadata.read files.content.read', 'expires_in': 30,
    })


def fictional_file(**changes):
    return {'.tag': 'file', 'name': 'fictional-baseline', 'id': 'fictional-baseline-id',
            'path_lower': '/fictional-baseline', **changes}


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class SyntheticMetadataTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.clock = Clock()
        self.session = fictional_session(self.clock)
        self.addCleanup(self.session.close)

    def inspect(self, value=None, *, body=None, **kwargs):
        if body is None:
            body = encode(fictional_file() if value is None else value)
        fixture = metadata.SyntheticMetadataFixture(body=body, **kwargs)
        return metadata.inspect_synthetic_metadata(self.session, fixture)

    def test_found_metadata_does_not_establish_snapshot_or_integrity(self):
        result = self.inspect()
        self.assertEqual(result.status, 'FOUND_UNVERIFIED')
        self.assertEqual(result.snapshot, 'UNVERIFIED')
        self.assertEqual(result.integrity, 'UNVERIFIED')

    def test_missing_denial_and_request_failure_are_not_empty_success(self):
        cases = [(401, {}, 'AUTH_REQUIRED'),
                 (403, {}, 'ACCESS_DENIED'),
                 (404, {}, 'REQUEST_FAILED'),
                 (409, {'error': {'.tag': 'path', 'path': {'.tag': 'not_found'}}}, 'BASELINE_MISSING'),
                 (409, {'error': {'.tag': 'path', 'path': {'.tag': 'restricted_content'}}}, 'REQUEST_FAILED'),
                 (500, {}, 'REQUEST_FAILED')]
        for status, value, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(self.inspect(value, http_status=status).status, expected)

    def test_closed_or_expired_authentication_never_becomes_success(self):
        self.session.close()
        fixture = metadata.SyntheticMetadataFixture(body=encode(fictional_file()))
        self.assertEqual(metadata.inspect_synthetic_metadata(self.session, fixture).status, 'AUTH_EXPIRED')
        self.assertEqual(fixture.calls, 0)
        self.session = fictional_session(self.clock)
        self.addCleanup(self.session.close)
        self.clock.now += 30
        self.assertEqual(self.inspect().status, 'AUTH_EXPIRED')

    def test_wrong_app_and_non_app_folder_are_rejected(self):
        for kwargs, expected in (({'app_identity': 'fictional-other-app'}, 'WRONG_APP'),
                                 ({'access_type': 'full_dropbox'}, 'WRONG_SCOPE')):
            fixture = metadata.SyntheticMetadataFixture(body=encode(fictional_file()), **kwargs)
            self.assertEqual(metadata.inspect_synthetic_metadata(self.session, fixture).status, expected)
            self.assertEqual(fixture.calls, 0)

    def test_grant_with_write_scope_is_not_accepted(self):
        self.session.scopes = frozenset(('files.metadata.read', 'files.content.read', 'files.content.write'))
        self.assertNotEqual(self.inspect().status, 'FOUND_UNVERIFIED')

    def test_duplicate_json_object_key_and_multiple_metadata_objects_are_rejected(self):
        duplicate_key = (b'{".tag":"file","name":"fictional-baseline",'
                         b'"id":"fictional-id-1","id":"fictional-id-2",'
                         b'"path_lower":"/fictional-baseline"}')
        self.assertEqual(self.inspect(body=duplicate_key).status, 'DUPLICATE')
        self.assertEqual(self.inspect([fictional_file(), fictional_file()]).status, 'DUPLICATE')

    def test_untrusted_fake_session_does_not_authorize_metadata(self):
        class PretendSession:
            scopes = frozenset(('files.metadata.read', 'files.content.read'))
            def is_valid(self):
                return True
        fixture = metadata.SyntheticMetadataFixture(body=encode(fictional_file()))
        self.assertNotEqual(metadata.inspect_synthetic_metadata(PretendSession(), fixture).status, 'FOUND_UNVERIFIED')

    def test_unrecognized_payload_or_content_is_never_accepted(self):
        payloads = [b'not json', b'\xff', b' ' * 65537,
                    encode(fictional_file(name='ordinary-name')),
                    encode(fictional_file(file_body='fictional-content')),
                    encode({'.tag': 'folder', 'name': 'fictional-baseline',
                            'id': 'fictional-baseline-id', 'path_lower': '/fictional-baseline'})]
        for body in payloads:
            with self.subTest(size=len(body)):
                self.assertNotEqual(self.inspect(body=body).status, 'FOUND_UNVERIFIED')

    def test_result_does_not_leak_fictional_names_ids_paths_or_tokens(self):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = self.inspect()
        output = captured.getvalue() + repr(result) + str(result)
        for sensitive in ('fictional-baseline', 'fictional-baseline-id', '/fictional-baseline', 'fictional-token'):
            self.assertNotIn(sensitive, output)

    def test_live_metadata_gate_never_performs_network(self):
        transport = Mock(side_effect=AssertionError('must not call transport'))
        result = metadata.inspect_live_metadata(self.session, transport=transport)
        self.assertEqual(result.status, 'UNSUPPORTED_CONTRACT')
        transport.assert_not_called()


class PrivateMetadataTransportTests(unittest.TestCase):
    """Only fake connections and invented target names enter this private seam."""

    def setUp(self):
        self.network_guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)
        self.guard = object()
        guard_patch = patch.object(metadata, '_LIVE_AUTHORIZATION_GUARD', self.guard)
        guard_patch.start()
        self.addCleanup(guard_patch.stop)
        self.conn = Mock()
        self.response = Mock(status=200)
        self.response.read.return_value = encode(fictional_file())
        self.conn.getresponse.return_value = self.response
        self.factory = Mock(return_value=self.conn)

    def call(self, *, target='/fictional-baseline', bearer='fictional-token'):
        return metadata._request_metadata_https(target=target, bearer=bearer, _guard=self.guard,
                                                connection_factory=self.factory)

    def test_fixed_tls_host_single_metadata_operation_and_no_body_fetch(self):
        result = self.call()
        self.assertEqual(result.status, 'FOUND_UNVERIFIED')
        self.assertEqual(result.source, 'UNVERIFIED_TRANSPORT')
        self.assertEqual((result.snapshot, result.integrity), ('UNVERIFIED', 'UNVERIFIED'))
        args, kwargs = self.factory.call_args
        self.assertEqual(args, ('api.dropboxapi.com', 443))
        self.assertEqual(kwargs['timeout'], 10)
        self.assertEqual(kwargs['context'].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(kwargs['context'].check_hostname)
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ('POST', '/2/files/get_metadata'))
        self.assertEqual(json.loads(kwargs['body']), {'path': '/fictional-baseline',
            'include_media_info': False, 'include_deleted': False, 'include_has_explicit_shared_members': False})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer fictional-token')
        self.assertNotIn('Dropbox-API-Path-Root', kwargs['headers'])
        self.conn.close.assert_called_once()
        self.assertNotIn('fictional-baseline', repr(result))
        self.assertNotIn('fictional-token', repr(result))

    def test_guard_is_closed_before_factory_or_input_access(self):
        self.assertEqual(metadata._request_metadata_https(target=None, bearer=None, _guard=object(),
                         connection_factory=self.factory).status, 'UNSUPPORTED_CONTRACT')
        self.factory.assert_not_called()
        with patch.object(metadata, '_LIVE_AUTHORIZATION_GUARD', None):
            self.assertEqual(metadata._request_metadata_https(target='/fictional-baseline',
                bearer='fictional-token', _guard=None, connection_factory=self.factory).status, 'UNSUPPORTED_CONTRACT')
        self.factory.assert_not_called()

    def test_disallowed_paths_and_tokens_fail_before_connection(self):
        for target in ('/', '', 'id:fictional-id', 'https://example.invalid', '//fictional',
                       '/fictional/../else', '/fictional\\other', '/fictional\nother', '/fictional/',
                       '/fictional-\ud800', '/fictional-\udfff'):
            with self.subTest(length=len(target)):
                self.assertEqual(self.call(target=target).status, 'INVALID_REQUEST')
        self.assertEqual(self.call(bearer='fictional-token\n').status, 'INVALID_REQUEST')
        self.factory.assert_not_called()

    def test_unicode_canonical_target_matches_exactly_without_guessing(self):
        self.assertTrue(metadata._valid_target('/fictional-가상'))
        self.response.read.return_value = encode(fictional_file(
            name='fictional-가상', path_lower='/fictional-가상'))
        self.assertEqual(self.call(target='/fictional-가상').status, 'FOUND_UNVERIFIED')
        self.response.read.return_value = encode(fictional_file(
            name='fictional-é', path_lower='/fictional-é'))
        # Composed and decomposed spellings are not silently equated.
        self.assertEqual(self.call(target='/fictional-e\u0301').status, 'TARGET_MISMATCH')

    def test_remote_failure_mapping_and_redirects_remain_fail_closed(self):
        for status, expected in ((301, 'REQUEST_FAILED'), (302, 'REQUEST_FAILED'),
                                 (401, 'AUTH_REQUIRED'), (403, 'ACCESS_DENIED'),
                                 (404, 'REQUEST_FAILED'), (500, 'REQUEST_FAILED')):
            with self.subTest(status=status):
                self.response.status = status
                self.assertEqual(self.call().status, expected)
        self.response.status = 409
        self.response.read.return_value = encode({'error': {'.tag': 'path', 'path': {'.tag': 'not_found'}}})
        self.assertEqual(self.call().status, 'BASELINE_MISSING')
        self.assertEqual(self.conn.close.call_count, 7)

    def test_duplicate_oversize_and_mismatched_targets_cannot_succeed(self):
        for body, expected in ((b'{"id":"fictional-1","id":"fictional-2"}', 'DUPLICATE'),
                               (b' ' * 65537, 'INVALID_RESPONSE'),
                               (encode(fictional_file(path_lower='/fictional-other')), 'TARGET_MISMATCH'),
                               (encode(fictional_file(symlink_info={'target': '/fictional-outside'})), 'UNSUPPORTED_CONTRACT'),
                               (encode(fictional_file(**{'.tag': 'folder'})), 'TYPE_MISMATCH')):
            with self.subTest(expected=expected):
                self.response.read.return_value = body
                self.assertEqual(self.call().status, expected)

    def test_transport_errors_are_sanitized(self):
        self.factory.side_effect = OSError('fictional-token /fictional-baseline')
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            result = self.call()
        self.assertEqual(result.status, 'REQUEST_FAILED')
        text = captured.getvalue() + repr(result) + str(result)
        self.assertNotIn('fictional-token', text)
        self.assertNotIn('fictional-baseline', text)


class ConfirmedMetadataSessionTests(unittest.TestCase):
    """Exercise trusted-session policy only with fake TLS and invented metadata."""

    def setUp(self):
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.clock = Clock()
        token_reply = Mock(status=200)
        token_reply.read.return_value = encode({'access_token': 'fictional-token', 'token_type': 'bearer',
            'scope': 'files.metadata.read files.content.read', 'expires_in': 30})
        token_conn = Mock()
        token_conn.getresponse.return_value = token_reply
        self.token_factory = Mock(return_value=token_conn)
        # A production-trusted session uses the canonical monotonic clock.
        attempt = AuthAttempt('fictional-app-key', 'http://127.0.0.1:54321/callback')
        state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
        attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
        with patch.object(auth.http.client, 'HTTPSConnection', self.token_factory):
            self.session = attempt.exchange(auth.token_transport)
        self.addCleanup(self.session.close)
        self.reply = Mock(status=200)
        self.reply.read.return_value = encode(fictional_file())
        self.conn = Mock()
        self.conn.getresponse.return_value = self.reply
        self.factory = Mock(return_value=self.conn)

    def call(self, **overrides):
        args = {'confirmed_app_key': 'fictional-app-key', 'app_folder_confirmation': 'USER_CONFIRMED',
                'target': '/fictional-baseline', **overrides}
        with patch.object(metadata.http.client, 'HTTPSConnection', self.factory):
            return metadata.inspect_live_metadata(self.session, **args)

    def test_same_session_confirms_app_key_and_grant_before_one_metadata_request(self):
        result = self.call()
        self.assertEqual(result.status, 'FOUND_UNVERIFIED')
        self.assertEqual(result.source, 'USER_CONFIRMED')
        self.assertEqual((result.snapshot, result.integrity), ('UNVERIFIED', 'UNVERIFIED'))
        self.assertEqual(self.factory.call_count, 1)
        args, kwargs = self.conn.request.call_args
        self.assertEqual(args, ('POST', '/2/files/get_metadata'))
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer fictional-token')
        self.assertNotIn('fictional-baseline', repr(result))
        self.assertNotIn('fictional-token', repr(result))

    def test_wrong_key_and_absent_folder_confirmation_never_open_connection(self):
        result = self.call(confirmed_app_key='fictional-other-key')
        self.assertEqual((result.status, result.source), ('APP_KEY_MISMATCH', 'UNVERIFIED'))
        result = self.call(app_folder_confirmation='no')
        self.assertEqual((result.status, result.source), ('APP_FOLDER_UNCONFIRMED', 'UNVERIFIED'))
        self.factory.assert_not_called()

    def test_injected_fake_oauth_response_never_mints_trusted_session(self):
        self.session.close()
        self.session = fictional_session(self.clock)
        self.addCleanup(self.session.close)
        result = self.call()
        self.assertEqual((result.status, result.source), ('APP_UNVERIFIED', 'UNVERIFIED'))
        self.factory.assert_not_called()

    def test_mutating_visible_scope_and_expiry_cannot_expand_or_extend_session(self):
        self.session.scopes = frozenset(('files.metadata.read', 'files.content.read', 'files.content.write'))
        self.assertEqual(self.call().status, 'APP_UNVERIFIED')
        self.factory.assert_not_called()
        self.session.scopes = auth.READ_SCOPES
        self.session.expires_at = time.monotonic() + 1000
        try:
            self.session._clock = lambda: 0
        except AttributeError:
            pass
        # Controlled mutation of this fake session emulates passage of time;
        # no private field is changed in production.
        self.session._ReadOnlySession__deadline = 0
        result = self.call()
        self.assertEqual((result.status, result.source), ('AUTH_EXPIRED', 'UNVERIFIED'))
        self.factory.assert_not_called()

    def test_close_or_expiry_after_connection_preparation_prevents_post(self):
        self.factory.side_effect = lambda *a, **kw: (self.session.close(), self.conn)[1]
        self.assertEqual(self.call().status, 'AUTH_EXPIRED')
        self.conn.request.assert_not_called()
        self.conn.close.assert_called_once()

    def test_close_wins_threaded_race_before_send(self):
        ready, proceed = threading.Event(), threading.Event()
        def connection_after_close(*_args, **_kwargs):
            ready.set()
            if not proceed.wait(3):
                raise AssertionError('synthetic synchronization timeout')
            return self.conn
        self.factory.side_effect = connection_after_close
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.call)
            self.assertTrue(ready.wait(3))
            self.session.close()
            proceed.set()
            self.assertEqual(pending.result(timeout=3).status, 'AUTH_EXPIRED')
        self.conn.request.assert_not_called()
        self.conn.close.assert_called_once()

    def test_expiry_during_response_never_yields_found(self):
        def expire(*_):
            self.session._ReadOnlySession__deadline = 0
            return encode(fictional_file())
        self.reply.read.side_effect = expire
        self.assertEqual(self.call().status, 'AUTH_EXPIRED')
        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()

    def test_close_during_response_never_returns_found(self):
        reading, proceed = threading.Event(), threading.Event()
        def response_after_close(*_):
            reading.set()
            if not proceed.wait(3):
                raise AssertionError('synthetic synchronization timeout')
            return encode(fictional_file())
        self.reply.read.side_effect = response_after_close
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.call)
            self.assertTrue(reading.wait(3))
            self.session.close()
            proceed.set()
            self.assertEqual(pending.result(timeout=3).status, 'AUTH_EXPIRED')
        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()

    def test_close_waits_for_an_in_progress_send_and_invalidates_result(self):
        sending, finish_send, close_started, closed = (threading.Event() for _ in range(4))
        def blocked_send(*_args, **_kwargs):
            sending.set()
            if not finish_send.wait(3):
                raise AssertionError('synthetic synchronization timeout')
        def close_session():
            close_started.set()
            self.session.close()
            closed.set()
        def read_after_close(*_args):
            if not closed.wait(3):
                raise AssertionError('synthetic synchronization timeout')
            return encode(fictional_file())
        self.conn.request.side_effect = blocked_send
        self.reply.read.side_effect = read_after_close
        with ThreadPoolExecutor(max_workers=2) as pool:
            request = pool.submit(self.call)
            self.assertTrue(sending.wait(3))
            closing = pool.submit(close_session)
            self.assertTrue(close_started.wait(3))
            self.assertFalse(closed.wait(0.05))
            finish_send.set()
            closing.result(timeout=3)
            self.assertEqual(request.result(timeout=3).status, 'AUTH_EXPIRED')
        self.conn.request.assert_called_once()
        self.conn.close.assert_called_once()

    def test_expiry_after_connection_preparation_prevents_post(self):
        def expire_before_send(*_args, **_kwargs):
            self.session._ReadOnlySession__deadline = 0
            return self.conn
        self.factory.side_effect = expire_before_send
        self.assertEqual(self.call().status, 'AUTH_EXPIRED')
        self.conn.request.assert_not_called()
        self.conn.close.assert_called_once()

    def test_injected_clock_with_real_transport_cannot_mint_live_provenance(self):
        token_reply = Mock(status=200)
        token_reply.read.return_value = encode({'access_token': 'fictional-token', 'token_type': 'bearer',
            'scope': 'files.metadata.read files.content.read', 'expires_in': 30})
        token_conn = Mock()
        token_conn.getresponse.return_value = token_reply
        attempt = AuthAttempt('fictional-app-key', 'http://127.0.0.1:54321/callback', clock=Clock())
        state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
        attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
        with patch.object(auth.http.client, 'HTTPSConnection', Mock(return_value=token_conn)):
            self.session = attempt.exchange(auth.token_transport)
        self.addCleanup(self.session.close)
        self.assertEqual(self.call().status, 'APP_UNVERIFIED')
        self.factory.assert_not_called()

    def test_exchange_uses_the_same_app_key_sent_to_the_oauth_endpoint(self):
        token_reply = Mock(status=200)
        token_reply.read.return_value = encode({'access_token': 'fictional-token', 'token_type': 'bearer',
            'scope': 'files.metadata.read files.content.read', 'expires_in': 30})
        token_conn = Mock()
        token_conn.getresponse.return_value = token_reply
        attempt = AuthAttempt('fictional-app-key', 'http://127.0.0.1:54321/callback')
        state = parse_qs(urlsplit(attempt.authorization_url()).query)['state'][0]
        attempt.accept_callback('/callback?' + urlencode({'state': state, 'code': 'fictional-code'}))
        token_conn.request.side_effect = lambda *_args, **_kwargs: setattr(attempt, '_app_key', 'fictional-other-key')
        with patch.object(auth.http.client, 'HTTPSConnection', Mock(return_value=token_conn)):
            self.session = attempt.exchange(auth.token_transport)
        self.addCleanup(self.session.close)
        sent = parse_qs(token_conn.request.call_args.kwargs['body'].decode('ascii'))
        self.assertEqual(sent['client_id'], ['fictional-app-key'])
        self.assertEqual(self.call(confirmed_app_key='fictional-other-key').status, 'APP_KEY_MISMATCH')
        self.factory.assert_not_called()
        self.assertEqual(self.call().status, 'FOUND_UNVERIFIED')

    def test_closed_session_and_remote_unauthorized_are_not_treated_as_missing(self):
        self.reply.status = 401
        self.assertEqual(self.call().status, 'AUTH_REQUIRED')
        self.factory.reset_mock()
        self.assertEqual(self.call().status, 'AUTH_EXPIRED')
        self.factory.assert_not_called()

    def test_exact_unicode_canonical_target_and_normalization_mismatch(self):
        self.reply.read.return_value = encode(fictional_file(name='fictional-é', path_lower='/fictional-é'))
        self.assertEqual(self.call(target='/fictional-é').status, 'FOUND_UNVERIFIED')
        self.assertEqual(self.call(target='/fictional-e\u0301').status, 'TARGET_MISMATCH')


class MetadataWindowsCLITests(unittest.TestCase):
    """CLI interactions use fake values and never open a browser or socket."""

    def setUp(self):
        self.socket_guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)
        self.output = io.StringIO()
        self.stdout = contextlib.redirect_stdout(self.output)
        self.stderr = contextlib.redirect_stderr(self.output)
        self.stdout.__enter__()
        self.stderr.__enter__()
        self.addCleanup(self.stdout.__exit__, None, None, None)
        self.addCleanup(self.stderr.__exit__, None, None, None)
        self.redirect = 'http://127.0.0.1:54321/callback'

    def test_non_windows_fails_before_input_and_network(self):
        with patch.object(metadata_windows.sys, 'platform', 'linux'), \
             patch('builtins.input', side_effect=AssertionError('unexpected input')), \
             patch.object(metadata_windows, 'run_flow', side_effect=AssertionError('unexpected auth')):
            self.assertEqual(metadata_windows.main(), 2)

    def test_cli_only_reports_redacted_status_and_closes_session(self):
        session = Mock()
        result = metadata.MetadataInspection('FOUND_UNVERIFIED', source='USER_CONFIRMED')
        with patch.object(metadata_windows.sys, 'platform', 'win32'), \
             patch('builtins.input', side_effect=['CONFIRM', self.redirect, self.redirect, 'START']), \
             patch.object(metadata_windows.getpass, 'getpass', side_effect=[
                 'fictional-app-key', 'fictional-app-key', '/fictional-baseline']), \
             patch.object(metadata_windows, 'run_flow', return_value=session) as run_flow, \
             patch.object(metadata_windows, 'inspect_live_metadata', return_value=result) as inspect:
            self.assertEqual(metadata_windows.main(), 0)
        self.assertEqual(run_flow.call_count, 1)
        self.assertEqual(inspect.call_count, 1)
        session.close.assert_called_once()
        output = self.output.getvalue()
        self.assertIn('FOUND_UNVERIFIED', output)
        self.assertIn('UNVERIFIED', output)
        for secret in ('fictional-app-key', 'fictional-baseline', 'fictional-id', 'fictional-token'):
            self.assertNotIn(secret, output)

    def test_wrong_confirmed_key_and_cancel_do_not_start_authentication(self):
        for hidden, visible in ((['fictional-key-a', 'fictional-key-b'],
                                 ['CONFIRM', self.redirect, self.redirect]),
                                (['fictional-key-a', 'fictional-key-a', '/fictional-baseline'],
                                 ['CONFIRM', self.redirect, self.redirect, 'CANCEL'])):
            with self.subTest(step=len(hidden)), \
                 patch.object(metadata_windows.sys, 'platform', 'win32'), \
                 patch('builtins.input', side_effect=visible), \
                 patch.object(metadata_windows.getpass, 'getpass', side_effect=hidden), \
                 patch.object(metadata_windows, 'run_flow') as run_flow:
                self.assertEqual(metadata_windows.main(), 2)
                run_flow.assert_not_called()
        self.assertNotIn('fictional-key-a', self.output.getvalue())
        self.assertNotIn('fictional-baseline', self.output.getvalue())

    def test_insecure_getpass_fallback_is_rejected_before_auth(self):
        def insecure(*_args, **_kwargs):
            warnings.warn('fictional-secret', metadata_windows.getpass.GetPassWarning)
            return 'fictional-app-key'
        with patch.object(metadata_windows.sys, 'platform', 'win32'), \
             patch('builtins.input', side_effect=['CONFIRM', self.redirect, self.redirect]), \
             patch.object(metadata_windows.getpass, 'getpass', side_effect=insecure), \
             patch.object(metadata_windows, 'run_flow') as run_flow:
            self.assertEqual(metadata_windows.main(), 2)
            run_flow.assert_not_called()
        self.assertIn('SECURE_INPUT_UNAVAILABLE', self.output.getvalue())
        self.assertNotIn('fictional-secret', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
