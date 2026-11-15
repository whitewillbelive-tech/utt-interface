"""Publication controller tests; all OAuth, transport and runtime data are fake."""
from contextlib import redirect_stderr, redirect_stdout
import io
import threading
import time
import unittest
from unittest.mock import patch

from app.workspace_publisher import WorkspacePublisher


class Session:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class Runtime:
    def __init__(self):
        self.scope = ('fictional-dataset', 'fictional-snapshot')
        self.remote = 'UNKNOWN'
        self.local = 'LOCAL_SAVED'
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.result = None

    def publication_status(self):
        return {'contract_version': 'UTT-WORKSPACE-STATUS/0.1', 'status': 'OK', 'dataset_id': self.scope[0], 'snapshot_id': self.scope[1],
                'local_status': self.local, 'dropbox_status': self.remote}

    def publish(self, transport, session, *, dataset_id, snapshot_id):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('fictional timeout secret')
        if self.result is not None:
            return self.result
        self.remote = 'PUBLISHED'
        return {'contract_version': 'UTT-WORKSPACE-PUBLICATION/0.1', 'status': 'OK', 'code': 'PUBLISHED', 'dropbox_status': 'PUBLISHED',
                'dataset_id': dataset_id, 'snapshot_id': snapshot_id}


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.publisher = WorkspacePublisher(self.runtime, 'fictional-key',
                                            'http://127.0.0.1:43155/callback',
                                            'http://127.0.0.1:43155/callback')
        self.session = Session()

    def wait_done(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.publisher.status()
            if result['status'] != 'RUNNING':
                return result
            threading.Event().wait(.01)
        self.fail('controller did not finish')

    def test_explicit_start_serializes_attempt_and_reports_scope(self):
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session) as auth, \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.assertTrue(self.publisher.owns(self.runtime))
            self.assertFalse(self.publisher.owns(Runtime()))
            self.assertEqual(self.publisher.status()['status'], 'READY')
            mismatch = self.publisher.start('fictional-dataset', 'stale')
            self.assertEqual(mismatch['error']['code'], 'VERSION_MISMATCH')
            auth.assert_not_called()
            result = self.publisher.start(*self.runtime.scope)
            self.assertEqual((result['contract_version'], result['status']), ('UTT-WORKSPACE-PUBLISH/0.1', 'RUNNING'))
            self.assertTrue(self.runtime.entered.wait(3))
            self.assertEqual(self.publisher.start(*self.runtime.scope)['status'], 'RUNNING')
            self.assertEqual(self.publisher.status()['status'], 'RUNNING')
            self.assertEqual(self.runtime.calls, 1)
            self.runtime.release.set()
            done = self.wait_done()
            self.assertEqual(done['status'], 'PUBLISHED')
            self.assertEqual((done['dataset_id'], done['snapshot_id']), self.runtime.scope)
            self.assertTrue(self.session.closed.is_set())
            self.assertEqual(auth.call_count, 1)

    def test_local_change_or_external_unknown_invalidates_published_status(self):
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session), \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(self.runtime.entered.wait(3))
            self.runtime.release.set()
            self.assertEqual(self.wait_done()['status'], 'PUBLISHED')
            self.runtime.remote = 'UNKNOWN'
            self.assertEqual(self.publisher.status()['status'], 'UNKNOWN')
            self.runtime.scope = ('fictional-dataset', 'different-snapshot')
            self.runtime.remote = 'UNPUBLISHED'
            self.assertEqual(self.publisher.status()['status'], 'UNPUBLISHED')
            self.runtime.local = 'REFRESH_REQUIRED'
            self.assertEqual(self.publisher.status()['status'], 'UNKNOWN')
            self.assertEqual(self.publisher.start(*self.runtime.scope)['error']['code'], 'STATUS_UNAVAILABLE')

    def test_ambiguous_active_update_latches_unknown_and_never_retries(self):
        self.runtime.result = {'contract_version': 'UTT-WORKSPACE-PUBLICATION/0.1', 'status': 'ERROR', 'error': {'code': 'ACTIVE_DB_UNVERIFIED'},
                               'dropbox_status': 'UNKNOWN'}
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session) as auth, \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(self.runtime.entered.wait(3))
            self.runtime.release.set()
            self.assertEqual(self.wait_done()['status'], 'UNKNOWN')
            self.assertEqual(self.publisher.start(*self.runtime.scope)['status'], 'UNKNOWN')
            self.assertEqual(auth.call_count, 1)
            self.assertEqual(self.runtime.calls, 1)

    def test_preflight_failure_with_initial_unknown_remains_retryable(self):
        self.runtime.result = {'contract_version': 'UTT-WORKSPACE-PUBLICATION/0.1', 'status': 'ERROR', 'error': {'code': 'PARENT_UNSUPPORTED_CELL_ENCODING'},
                               'dropbox_status': 'UNKNOWN'}
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session) as auth, \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(self.runtime.entered.wait(3))
            self.runtime.release.set()
            done = self.wait_done()
            self.assertEqual((done['status'], done['error']['code']), ('ERROR', 'PARENT_UNSUPPORTED_CELL_ENCODING'))
            self.publisher.start(*self.runtime.scope)
            self.wait_done()
            self.assertEqual(auth.call_count, 2)

    def test_auth_or_runtime_exceptions_are_redacted(self):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output), \
             patch('app.workspace_publisher.run_publisher_flow', side_effect=RuntimeError('fictional-secret')):
            self.publisher.start(*self.runtime.scope)
            done = self.wait_done()
        self.assertEqual((done['status'], done['error']['code']), ('ERROR', 'AUTH_FAILED'))
        self.assertNotIn('fictional-secret', str(done) + output.getvalue() + repr(self.publisher))
        self.assertEqual(self.runtime.calls, 0)

    def test_refresh_with_missing_scope_is_unknown_not_fabricated(self):
        self.runtime.scope = (None, None)
        self.runtime.local = 'REFRESH_REQUIRED'
        result = self.publisher.status()
        self.assertEqual((result['status'], result['dataset_id'], result['snapshot_id']), ('UNKNOWN', None, None))
        self.assertEqual(self.publisher.start('old-dataset', 'old-snapshot')['error']['code'], 'STATUS_UNAVAILABLE')

    def test_malformed_publication_result_latches_unknown(self):
        self.runtime.result = {'contract_version': 'fictional', 'status': 'OK', 'code': 'PUBLISHED',
                               'dropbox_status': 'PUBLISHED', 'dataset_id': self.runtime.scope[0],
                               'snapshot_id': self.runtime.scope[1]}
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session) as auth, \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(self.runtime.entered.wait(3))
            self.runtime.release.set()
            self.assertEqual(self.wait_done()['status'], 'UNKNOWN')
            self.assertEqual(self.publisher.start(*self.runtime.scope)['status'], 'UNKNOWN')
            self.assertEqual(auth.call_count, 1)

    def test_close_during_auth_prevents_publication(self):
        entered, release = threading.Event(), threading.Event()
        def auth(*_):
            entered.set()
            release.wait(3)
            return self.session
        with patch('app.workspace_publisher.run_publisher_flow', side_effect=auth), \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(entered.wait(3))
            self.publisher.close()
            self.assertEqual(self.publisher._WorkspacePublisher__app_key, '')
            release.set()
            self.assertTrue(self.session.closed.wait(3))
            self.assertEqual(self.wait_done()['error']['code'], 'PUBLISHER_CLOSED')
            self.assertEqual(self.runtime.calls, 0)

    def test_close_waits_for_already_admitted_publication_and_clears_key(self):
        close_done = threading.Event()
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session), \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(self.runtime.entered.wait(3))
            def close():
                self.publisher.close()
                close_done.set()
            closer = threading.Thread(target=close)
            closer.start()
            self.assertFalse(close_done.wait(.05))
            self.assertEqual(self.publisher._WorkspacePublisher__app_key, '')
            self.runtime.release.set()
            self.assertTrue(close_done.wait(3))
            closer.join(3)
            self.assertFalse(closer.is_alive())
            self.assertEqual(self.runtime.calls, 1)
            self.assertTrue(self.session.closed.is_set())
            self.assertEqual(self.publisher.start(*self.runtime.scope)['error']['code'], 'PUBLISHER_CLOSED')

    def test_write_is_rejected_while_publishing(self):
        invoked = []
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session), \
             patch('app.workspace_publisher.PublisherTransport', return_value=object()):
            self.publisher.start(*self.runtime.scope)
            self.assertTrue(self.runtime.entered.wait(3))
            result = self.publisher.write_if_idle(lambda: invoked.append(True))
            self.assertEqual(result['error']['code'], 'PUBLISH_IN_PROGRESS')
            self.assertEqual(invoked, [])
            self.runtime.release.set()
            self.assertEqual(self.wait_done()['status'], 'PUBLISHED')

    def test_start_waits_for_local_write_and_rechecks_snapshot(self):
        entered, release, started = threading.Event(), threading.Event(), threading.Event()
        outcomes = {}
        old_scope = self.runtime.scope
        def write():
            def callback():
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('fictional timeout')
                self.runtime.scope = ('fictional-dataset', 'newer-snapshot')
                return {'status': 'OK'}
            outcomes['write'] = self.publisher.write_if_idle(callback)
        def start():
            outcomes['start'] = self.publisher.start(*old_scope)
            started.set()
        with patch('app.workspace_publisher.run_publisher_flow', return_value=self.session) as auth:
            write_thread = threading.Thread(target=write)
            write_thread.start()
            self.assertTrue(entered.wait(3))
            start_thread = threading.Thread(target=start)
            start_thread.start()
            self.assertFalse(started.wait(.05))
            release.set()
            write_thread.join(3)
            start_thread.join(3)
            self.assertFalse(write_thread.is_alive())
            self.assertFalse(start_thread.is_alive())
            self.assertEqual(outcomes['write']['status'], 'OK')
            self.assertEqual(outcomes['start']['error']['code'], 'VERSION_MISMATCH')
            auth.assert_not_called()


if __name__ == '__main__':
    unittest.main()
