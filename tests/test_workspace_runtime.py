"""Synthetic local database/runtime tests; no real workspace or Dropbox."""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.provider import request
from app.publisher_pipeline import PublicationResult
from app.workspace_runtime import LocalWorkspaceRuntime, WorkspaceRuntimeError
from app.workspace_write import OriginalWorkspaceEventCodec, WorkspaceWriteError, WorkspaceWriter
from tests.test_publisher_workspace import make_fixture


class LocalWorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-private-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'fictional-sensitive-path.sqlite'
        self.conn = make_fixture(self.path)
        self.addCleanup(self.conn.close)
        self.runtime = LocalWorkspaceRuntime(self.path, self.root, codec=OriginalWorkspaceEventCodec())
        self.addCleanup(self.runtime.close)

    def scope(self):
        state = self.runtime.publication_status()
        return {'dataset_id': state['dataset_id'], 'snapshot_id': state['snapshot_id']}

    def change(self, **kwargs):
        return {'operation': 'update', 'record_id': 'fictional-record', 'expected_version': 1,
                'reason': 'fictional reason', 'changes': {'body': 'fictional updated'}, **kwargs}

    def test_local_write_reloads_snapshot_and_preserves_history(self):
        old = self.scope()
        self.assertEqual(self.runtime.publication_status()['dropbox_status'], 'UNKNOWN')
        result = self.runtime.write(self.change(), **old)
        self.assertEqual((result['status'], result['local_status'], result['dropbox_status']),
                         ('OK', 'LOCAL_SAVED', 'UNPUBLISHED'))
        self.assertEqual(result['version'], 2)
        self.assertNotEqual(result['snapshot_id'], old['snapshot_id'])
        stale = self.runtime.write(self.change(), **old)
        self.assertEqual(stale['error']['code'], 'VERSION_MISMATCH')
        q = {'contract_version': 'UTT-WORKSPACE-READ/0.1', 'operation': 'detail',
             'record_id': 'fictional-record', 'dataset_id': result['dataset_id'], 'snapshot_id': result['snapshot_id']}
        detail = self.runtime.read_workspace_detail(q)
        self.assertEqual(detail['record']['body'], 'fictional updated')
        self.assertEqual([r['version'] for r in detail['revisions']], [2, 1])
        self.assertEqual(detail['events'][0]['actor'], 'USER')
        self.assertEqual(detail['record']['decision_status'], 'UNREVIEWED')

    def test_external_change_conflicts_and_explicit_refresh_recaptures(self):
        old = self.scope()
        self.conn.execute("UPDATE work_meta SET value='2' WHERE key='data_revision'")
        self.conn.commit()
        self.assertEqual(self.runtime.publication_status()['local_status'], 'REFRESH_REQUIRED')
        self.assertEqual(self.runtime.write(self.change(), **old)['error']['code'], 'DATA_REVISION_CONFLICT')
        state = self.runtime.refresh()
        self.assertEqual(state['local_status'], 'LOCAL_SAVED')
        self.assertEqual(state['dropbox_status'], 'UNKNOWN')
        self.assertNotEqual(state['snapshot_id'], old['snapshot_id'])

    def test_rolled_back_version_conflict_keeps_original_snapshot_and_cursor(self):
        scope = self.scope()
        invalid = self.change(expected_version=99)
        result = self.runtime.write(invalid, **scope)
        self.assertEqual(result['error']['code'], 'VERSION_CONFLICT')
        status = self.runtime.publication_status()
        self.assertEqual(status['local_status'], 'LOCAL_SAVED')
        self.assertEqual(status['snapshot_id'], scope['snapshot_id'])
        self.assertEqual(self.runtime.read(request('capabilities'))['status'], 'OK')

    def test_external_edit_during_rejected_write_requires_refresh(self):
        scope = self.scope()
        def conflicting(_writer, _command):
            self.conn.execute("UPDATE work_meta SET value='999' WHERE key='data_revision'")
            self.conn.commit()
            raise WorkspaceWriteError('VERSION_CONFLICT')
        with patch.object(WorkspaceWriter, 'execute', conflicting):
            result = self.runtime.write(self.change(), **scope)
        self.assertEqual(result['error']['code'], 'VERSION_CONFLICT')
        self.assertEqual(self.runtime.publication_status()['local_status'], 'REFRESH_REQUIRED')
        self.assertEqual(self.runtime.read(request('capabilities'))['error']['code'], 'CONNECTION_UNAVAILABLE')
        self.assertEqual(self.runtime.refresh()['local_status'], 'LOCAL_SAVED')

    def test_commit_unknown_invalidates_provider_until_explicit_refresh(self):
        old = self.scope()
        execute = WorkspaceWriter.execute
        def ambiguous(writer, command):
            execute(writer, command)
            raise WorkspaceWriteError('COMMIT_UNKNOWN')
        with patch.object(WorkspaceWriter, 'execute', ambiguous):
            value = self.runtime.write(self.change(), **old)
        self.assertEqual(value['error']['code'], 'COMMIT_UNKNOWN')
        self.assertEqual(self.runtime.publication_status()['local_status'], 'REFRESH_REQUIRED')
        self.assertEqual(self.runtime.write(self.change(), **old)['error']['code'], 'CONNECTION_UNAVAILABLE')
        status = self.runtime.refresh()
        q = {'contract_version': 'UTT-WORKSPACE-READ/0.1', 'operation': 'detail',
             'record_id': 'fictional-record', 'dataset_id': status['dataset_id'], 'snapshot_id': status['snapshot_id']}
        self.assertEqual(self.runtime.read_workspace_detail(q)['record']['body'], 'fictional updated')

    def test_success_is_distinct_from_unknown_and_pending_publication(self):
        old = self.scope()
        def publish(snapshot, transport, session):
            snapshot.assert_source_unchanged()
            snapshot.assert_snapshot_unchanged()
            return PublicationResult('PUBLISHED', 'PUBLISHED')
        with patch('app.workspace_runtime.publish_snapshot', side_effect=publish):
            result = self.runtime.publish(object(), object(), **old)
        self.assertEqual((result['status'], result['dropbox_status']), ('OK', 'PUBLISHED'))
        self.assertEqual(self.runtime.publication_status()['dropbox_status'], 'PUBLISHED')
        saved = self.runtime.write(self.change(), **old)
        self.assertEqual(saved['dropbox_status'], 'UNPUBLISHED')
        self.assertEqual(self.runtime.publication_status()['dropbox_status'], 'UNPUBLISHED')
        now = self.scope()
        with patch('app.workspace_runtime.publish_snapshot', return_value=PublicationResult('NOT_VERIFIED', 'ACTIVATION_UNKNOWN')):
            failed = self.runtime.publish(object(), object(), **now)
        self.assertEqual((failed['error']['code'], failed['dropbox_status']), ('ACTIVATION_UNKNOWN', 'UNKNOWN'))

    def test_publication_serializes_local_edits(self):
        scope = self.scope()
        entered, release, write_finished = threading.Event(), threading.Event(), threading.Event()
        results = {}
        def publish(snapshot, transport, session):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timeout')
            return PublicationResult('PUBLISHED', 'PUBLISHED')
        def do_publish():
            results['publish'] = self.runtime.publish(object(), object(), **scope)
        def do_write():
            results['write'] = self.runtime.write(self.change(), **scope)
            write_finished.set()
        with patch('app.workspace_runtime.publish_snapshot', side_effect=publish):
            p = threading.Thread(target=do_publish)
            p.start()
            self.assertTrue(entered.wait(5))
            w = threading.Thread(target=do_write)
            w.start()
            self.assertFalse(write_finished.wait(0.15))
            release.set()
            p.join(5); w.join(5)
        self.assertFalse(p.is_alive())
        self.assertFalse(w.is_alive())
        self.assertEqual(results['publish']['status'], 'OK')
        self.assertEqual(results['write']['status'], 'OK')
        self.assertEqual(self.runtime.publication_status()['dropbox_status'], 'UNPUBLISHED')

    def test_failed_republication_does_not_leave_prior_published_claim(self):
        scope = self.scope()
        with patch('app.workspace_runtime.publish_snapshot', return_value=PublicationResult('PUBLISHED', 'PUBLISHED')):
            self.assertEqual(self.runtime.publish(object(), object(), **scope)['dropbox_status'], 'PUBLISHED')
        with patch('app.workspace_runtime.publish_snapshot', return_value=PublicationResult('NOT_VERIFIED', 'ACTIVE_CHANGED')):
            result = self.runtime.publish(object(), object(), **scope)
        self.assertEqual((result['error']['code'], result['dropbox_status']), ('ACTIVE_CHANGED', 'UNKNOWN'))
        self.assertEqual(self.runtime.publication_status()['dropbox_status'], 'UNKNOWN')

    def test_refresh_failure_after_commit_does_not_invite_retry(self):
        old = self.scope()
        with patch.object(self.runtime, '_refresh_locked', side_effect=RuntimeError('fictional-secret')):
            result = self.runtime.write(self.change(), **old)
        self.assertEqual(result['error']['code'], 'LOCAL_SAVED_REFRESH_REQUIRED')
        self.assertNotIn('fictional-secret', str(result))
        self.assertEqual(self.runtime.publication_status()['local_status'], 'REFRESH_REQUIRED')
        self.assertEqual(self.runtime.refresh()['local_status'], 'LOCAL_SAVED')

    def test_missing_file_is_not_created_and_repr_redacts_path(self):
        self.assertNotIn(str(self.path), repr(self.runtime))
        missing = self.root / 'fictional-does-not-exist.sqlite'
        with self.assertRaises(WorkspaceRuntimeError):
            LocalWorkspaceRuntime(missing, self.root, codec=OriginalWorkspaceEventCodec())
        self.assertFalse(missing.exists())

    def test_windows_unc_input_rejected_before_filesystem_access(self):
        with patch('app.workspace_runtime.os.name', 'nt'):
            with self.assertRaises(WorkspaceRuntimeError) as ctx:
                LocalWorkspaceRuntime('\\\\fictional-server\\share\\db.sqlite', 'C:\\local', codec=OriginalWorkspaceEventCodec())
        self.assertEqual(ctx.exception.code, 'INVALID_LOCAL_INPUT')


if __name__ == '__main__':
    unittest.main()
