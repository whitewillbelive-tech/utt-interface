"""Synthetic edit, publication and reconnect through the real local runtime.

Dropbox is an in-memory fake; no network or user file is accessed.
"""
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.provider import request
from app.publisher_workspace_server import load_verified_workspace
from app.workspace_publisher import WorkspacePublisher
from app.workspace_runtime import LocalWorkspaceRuntime
from app.workspace_write import OriginalWorkspaceEventCodec
from tests.test_publisher_pipeline import FakeSession, FakeTransport, initial_objects
from tests.test_publisher_workspace import make_fixture


class _Session(FakeSession):
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _ReaderTransport:
    def __init__(self, transport):
        self.transport = transport

    def control(self, path):
        sink = io.BytesIO()
        meta = self.transport.download(path, sink, max_bytes=16 * 1024 * 1024)
        return None if meta is None else (sink.getvalue(), meta.rev)

    def workspace(self, path, *, expected_size):
        sink = io.BytesIO()
        meta = self.transport.download(path, sink, max_bytes=expected_size)
        return None if meta is None else (sink.getvalue(), meta.rev)


class WorkspaceEndToEndTests(unittest.TestCase):
    def test_edit_archive_merge_restore_publish_and_verified_reconnect(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'fictional.sqlite'
            conn = make_fixture(path)
            try:
                transport = FakeTransport(initial_objects(conn))
                runtime = LocalWorkspaceRuntime(path, root, codec=OriginalWorkspaceEventCodec())
                try:
                    def scope():
                        current = runtime.publication_status()
                        return {k: current[k] for k in ('dataset_id', 'snapshot_id')}

                    def change(command):
                        result = runtime.write(command, **scope())
                        self.assertEqual(result['status'], 'OK', result)
                        self.assertEqual(result['local_status'], 'LOCAL_SAVED')
                        self.assertEqual(result['dropbox_status'], 'UNPUBLISHED')
                        return result

                    ident = 'fictional-record'
                    before = scope()
                    detail = runtime.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
                        'operation': 'detail', 'record_id': ident, **before})
                    self.assertEqual(detail['record']['body'], 'fictional body 🧭')
                    change({'operation': 'update', 'record_id': ident, 'expected_version': 1,
                            'reason': 'fictional edit', 'changes': {'body': 'fictional updated 🧭'}})
                    target = 'usr-' + 'a' * 32
                    change({'operation': 'create', 'record_id': target, 'reason': 'fictional create',
                            'record': {'title': 'fictional target', 'body': 'fictional target body',
                                       'summary': None, 'document_role': 'NOTE',
                                       'review_status': 'PENDING_USER_REVIEW',
                                       'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}})
                    change({'operation': 'archive', 'record_id': ident, 'expected_version': 2,
                            'reason': 'fictional archive'})
                    change({'operation': 'restore', 'record_id': ident, 'expected_version': 3,
                            'reason': 'fictional restore', 'restore_archive': True, 'restore_merge': False})
                    change({'operation': 'merge', 'record_id': ident, 'expected_version': 4,
                            'reason': 'fictional merge', 'target_record_id': target, 'target_expected_version': 1})
                    change({'operation': 'restore', 'record_id': ident, 'expected_version': 5,
                            'reason': 'fictional unmerge', 'restore_archive': False, 'restore_merge': True})
                    selected = scope()
                    session = _Session()
                    controller = WorkspacePublisher(runtime, 'fictional-key',
                        'http://127.0.0.1:43155/callback', 'http://127.0.0.1:43155/callback')
                    try:
                        with patch('app.workspace_publisher.run_publisher_flow', return_value=session), \
                             patch('app.workspace_publisher.PublisherTransport', return_value=transport):
                            self.assertEqual(controller.start(**selected)['status'], 'RUNNING')
                            deadline = time.monotonic() + 10
                            while time.monotonic() < deadline:
                                status = controller.status()
                                if status['status'] != 'RUNNING':
                                    break
                                threading.Event().wait(.01)
                            self.assertEqual(status['status'], 'PUBLISHED', status)
                            self.assertEqual((status['dataset_id'], status['snapshot_id']),
                                             (selected['dataset_id'], selected['snapshot_id']))
                            self.assertTrue(session.closed)
                        self.assertEqual(runtime.publication_status()['dropbox_status'], 'PUBLISHED')
                        self.assertEqual(sum(kind == 'cas' for kind, _ in transport.writes), 1)
                    finally:
                        controller.close()
                finally:
                    runtime.close()
                # Reconnect from the verified active fake Dropbox payload, not
                # from the still-open local editable DB.
                provider = load_verified_workspace(_ReaderTransport(transport))
                try:
                    snapshot = provider.read(request('capabilities'))['snapshot']
                    scope = {k: snapshot[k] for k in ('dataset_id', 'snapshot_id')}
                    self.assertEqual(scope, selected)
                    detail = provider.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
                        'operation': 'detail', 'record_id': ident, **scope})
                    self.assertEqual(detail['status'], 'OK')
                    self.assertEqual(detail['record']['body'], 'fictional updated 🧭')
                    self.assertEqual(detail['record']['version'], 6)
                    self.assertIsNone(detail['record']['archived_at'])
                    self.assertIsNone(detail['record']['merged_into'])
                    self.assertEqual([row['version'] for row in detail['revisions']], [6, 5, 4, 3, 2, 1])
                    kinds = [row['event_kind'] for row in detail['events']]
                    self.assertEqual(kinds[:5], ['UNMERGE', 'MERGE', 'RESTORE', 'ARCHIVE', 'EDIT'])
                    target_detail = provider.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
                        'operation': 'detail', 'record_id': target, **scope})
                    self.assertEqual(target_detail['status'], 'OK')
                    self.assertEqual(target_detail['record']['source_kind'], 'USER_WORKSPACE')
                finally:
                    provider.close()
            finally:
                conn.close()

    def test_relation_events_survive_a_second_publication(self):
        class ChangingRevisionTransport(FakeTransport):
            def __init__(self, objects):
                super().__init__(objects)
                self.generation_count = 1

            def update_if_revision(self, path, data, rev):
                super().update_if_revision(path, data, rev)
                self.generation_count += 1
                self.revision = 'rev-' + str(self.generation_count)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'fictional.sqlite'
            conn = make_fixture(path)
            try:
                conn.execute("INSERT INTO work_projects(project_id,name) VALUES ('project-fictional','project')")
                conn.execute("INSERT INTO work_topics(topic_id,name) VALUES ('topic-fictional','topic')")
                conn.commit()
                transport = ChangingRevisionTransport(initial_objects(conn))
                runtime = LocalWorkspaceRuntime(path, root, codec=OriginalWorkspaceEventCodec())
                try:
                    def scope():
                        state = runtime.publication_status()
                        return {k: state[k] for k in ('dataset_id', 'snapshot_id')}

                    self.assertEqual(runtime.publish(transport, _Session(), **scope())['status'], 'OK')
                    def change(operation, **args):
                        result = runtime.write({'operation': operation, 'record_id': 'fictional-record',
                            'expected_version': 1, 'reason': 'fictional relation change', **args}, **scope())
                        self.assertEqual(result['status'], 'OK', result)
                        self.assertEqual(result['version'], 1)
                    change('project_add', link_id='link-fictional', project_id='project-fictional',
                           role='RELATED', scope=None, basis='USER_CONFIRMED')
                    change('project_archive', link_id='link-fictional')
                    change('project_restore', link_id='link-fictional')
                    change('tag_add', tag_id='tag-fictional', tag_name='fictional tag')
                    change('tag_remove', tag_id='tag-fictional')
                    change('topic_add', topic_id='topic-fictional', status='USER_CONFIRMED')
                    change('topic_remove', topic_id='topic-fictional')
                    result = runtime.publish(transport, _Session(), **scope())
                    self.assertEqual(result['status'], 'OK', result)
                    self.assertEqual(result['dropbox_status'], 'PUBLISHED')
                    self.assertEqual(sum(kind == 'cas' for kind, _ in transport.writes), 2)
                finally:
                    runtime.close()
                provider = load_verified_workspace(_ReaderTransport(transport))
                try:
                    snapshot = provider.read(request('capabilities'))['snapshot']
                    scope = {k: snapshot[k] for k in ('dataset_id', 'snapshot_id')}
                    result = provider.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
                        'operation': 'detail', 'record_id': 'fictional-record', **scope})
                    self.assertEqual(result['status'], 'OK')
                    self.assertEqual(len(result['projects']), 1)
                    self.assertIsNone(result['projects'][0]['link']['archived_at'])
                    self.assertEqual(result['tags'], [])
                    self.assertEqual(result['topics'], [])
                    self.assertEqual([row['event_kind'] for row in result['events'][:7]],
                        ['TOPIC_UNLINK', 'TOPIC_LINK', 'TAG_REMOVE', 'TAG',
                         'PROJECT_RESTORE', 'PROJECT_ARCHIVE', 'PROJECT_LINK'])
                finally:
                    provider.close()
            finally:
                conn.close()


if __name__ == '__main__':
    unittest.main()
