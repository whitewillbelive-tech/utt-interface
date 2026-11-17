"""Windows publisher CLI control flow with entirely synthetic dependencies."""
import contextlib
import getpass
import io
import unittest
from unittest import mock

from app import publisher_windows as cli
from app.dropbox_auth import OAuthError
from app.publisher_pipeline import PublicationResult


REDIRECT = 'http://127.0.0.1:43821/oauth/callback'
KEY = 'fictional_app_key'
SOURCE = r'C:\fictional\private-workspace.sqlite'
TEMP = r'C:\fictional\protected-temp'


class PublisherWindowsCLITests(unittest.TestCase):
    def invoke(self, *, answers=None, secrets=None, outcome=None, session_error=None, platform='win32', preflight=False):
        output = io.StringIO()
        session = mock.Mock()
        snapshot = mock.Mock()
        transport = mock.Mock()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(cli.sys, 'platform', platform))
            stack.enter_context(mock.patch('builtins.input', side_effect=answers or []))
            secret = stack.enter_context(mock.patch.object(cli, '_secret', side_effect=secrets or []))
            capture = stack.enter_context(mock.patch.object(cli, 'snapshot_workspace'))
            capture.return_value.__enter__.return_value = snapshot
            auth = stack.enter_context(mock.patch.object(cli, 'run_publisher_flow',
                                                           return_value=session, side_effect=session_error))
            make_transport = stack.enter_context(mock.patch.object(cli, 'PublisherTransport', return_value=transport))
            pipeline = stack.enter_context(mock.patch.object(cli, 'publish_snapshot',
                    return_value=outcome or PublicationResult('PUBLISHED', 'PUBLISHED')))
            stack.enter_context(contextlib.redirect_stdout(output))
            code = cli.main(preflight=preflight)
        return code, output.getvalue(), secret, capture, auth, make_transport, pipeline, session, snapshot, transport

    def test_explicit_confirmation_and_start_required_before_local_or_remote_actions(self):
        for answers, secrets in [(['NO'], []),
                                  (['CONFIRM', REDIRECT, REDIRECT, 'no'], [KEY, KEY, SOURCE, TEMP])]:
            with self.subTest(answers=answers):
                code, output, _, capture, auth, make_transport, pipeline, *_ = self.invoke(answers=answers, secrets=secrets)
                self.assertEqual(code, 2)
                self.assertIn('Cancelled.', output)
                capture.assert_not_called()
                auth.assert_not_called()
                make_transport.assert_not_called()
                pipeline.assert_not_called()

    def test_publishes_only_after_secret_input_and_closes_session(self):
        code, output, secret, capture, auth, make_transport, pipeline, session, snapshot, transport = self.invoke(
            answers=['CONFIRM', REDIRECT, REDIRECT, 'START'], secrets=[KEY, KEY, SOURCE, TEMP])
        self.assertEqual(code, 0)
        self.assertIn('Publication status: PUBLISHED', output)
        self.assertEqual(secret.call_count, 4)
        capture.assert_called_once_with(SOURCE, directory=TEMP)
        auth.assert_called_once_with(KEY, REDIRECT, REDIRECT)
        make_transport.assert_called_once_with(session, confirmed_app_key=KEY, app_folder_confirmation='USER_CONFIRMED')
        pipeline.assert_called_once_with(snapshot, transport, session)
        session.close.assert_called_once_with()
        for value in (KEY, SOURCE, TEMP):
            self.assertNotIn(value, output)

    def test_preflight_uses_read_only_pipeline_mode_not_normal_publisher(self):
        answers = ['CONFIRM', REDIRECT, REDIRECT, 'PRECHECK']
        secrets = [KEY, KEY, SOURCE, TEMP]
        result = self.invoke(answers=answers, secrets=secrets, preflight=True,
                             outcome=PublicationResult('PREFLIGHT', 'PREFLIGHT_OK'))
        code, output, _, _, _, _, pipeline, session, snapshot, transport = result
        self.assertEqual(code, 0)
        self.assertIn('Preflight status: PREFLIGHT_OK', output)
        self.assertIn('no upload, no COMMIT', output)
        pipeline.assert_called_once_with(snapshot, transport, session, preflight_only=True)

    def test_mismatch_or_insecure_secret_input_stops_before_network(self):
        cases = [(['CONFIRM', REDIRECT, REDIRECT + '/'], [], 'REDIRECT_MISMATCH'),
                 (['CONFIRM', REDIRECT, REDIRECT], [KEY, 'different_key'], 'APP_KEY_MISMATCH'),
                 (['CONFIRM', REDIRECT, REDIRECT], getpass.GetPassWarning(), 'SECURE_INPUT_UNAVAILABLE')]
        for answers, secrets, expected in cases:
            with self.subTest(expected=expected):
                code, output, _, capture, auth, _, pipeline, *_ = self.invoke(answers=answers, secrets=secrets)
                self.assertEqual(code, 2)
                self.assertIn(expected, output)
                capture.assert_not_called()
                auth.assert_not_called()
                pipeline.assert_not_called()

    def test_result_failure_and_auth_error_are_fixed_nonsecret_statuses(self):
        answers = ['CONFIRM', REDIRECT, REDIRECT, 'START']
        secrets = [KEY, KEY, SOURCE, TEMP]
        code, output, _, _, _, _, _, session, *_ = self.invoke(answers=answers, secrets=secrets,
            outcome=PublicationResult('NOT_VERIFIED', 'ACTIVE_CHANGED'))
        self.assertEqual(code, 2)
        self.assertIn('ACTIVE_CHANGED', output)
        session.close.assert_called_once_with()
        code, output, _, _, _, _, pipeline, session, *_ = self.invoke(answers=answers, secrets=secrets,
            session_error=OAuthError('AUTH_CANCELLED'))
        self.assertEqual(code, 2)
        self.assertIn('AUTH_CANCELLED', output)
        pipeline.assert_not_called()
        session.close.assert_not_called()
        self.assertNotIn(SOURCE, output)
        self.assertNotIn(KEY, output)

    def test_other_platform_never_starts(self):
        code, _, secret, capture, auth, _, pipeline, *_ = self.invoke(platform='linux')
        self.assertEqual(code, 2)
        secret.assert_not_called()
        capture.assert_not_called()
        auth.assert_not_called()
        pipeline.assert_not_called()


if __name__ == '__main__':
    unittest.main()
