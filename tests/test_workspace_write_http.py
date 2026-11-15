"""Synthetic loopback tests for the real local runtime and HTTP write API.

No Dropbox, external network, production records or credentials are used.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from app.provider import request
from app.server import make_server
from app.workspace_publisher import WorkspacePublisher
from app.workspace_runtime import LocalWorkspaceRuntime
from app.workspace_write import OriginalWorkspaceEventCodec
from tests.test_publisher_pipeline import FakeSession, FakeTransport, initial_objects
from tests.test_publisher_workspace import make_fixture


class WorkspaceWriteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fictional-utt-http-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'fictional.sqlite'
        writer = make_fixture(self.path)
        writer.execute("INSERT INTO work_projects(project_id,name) VALUES ('fictional-project','fictional project')")
        writer.execute("INSERT INTO work_topics(topic_id,name) VALUES ('fictional-topic','fictional topic')")
        writer.commit()
        self.parent_objects = initial_objects(writer)
        writer.close()
        self.runtime = LocalWorkspaceRuntime(self.path, self.root, codec=OriginalWorkspaceEventCodec())
        self.addCleanup(self.runtime.close)
        self.publisher = WorkspacePublisher(self.runtime, 'fictional-app',
            'http://127.0.0.1:34567/oauth', 'http://127.0.0.1:34567/oauth')
        self.addCleanup(self.publisher.close)
        self.server = make_server(0, mode='readonly', provider=self.runtime, publisher=self.publisher)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop_server)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        status, body = self.call('POST', '/api/read', {'request': request('capabilities')})
        self.assertEqual((status, body['status']), (200, 'OK'))
        self.scope = {key: body['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)

    def call(self, method, path, payload=None, *, origin=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=10)
        try:
            headers = {}
            if origin is not None:
                headers['Origin'] = origin
            if payload is not None:
                headers['Content-Type'] = 'application/json'
                payload = json.dumps(payload).encode('utf-8')
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read().decode('utf-8'))
        finally:
            conn.close()

    def detail(self, record_id='fictional-record', *, scope=None):
        payload = {'contract_version': 'UTT-WORKSPACE-READ/0.1', 'operation': 'detail',
                   **(self.scope if scope is None else scope), 'record_id': record_id}
        return self.call('POST', '/api/workspace/read', payload)

    def write(self, operation, *, dataset_scope=None, origin='same', **values):
        payload = {'contract_version': 'UTT-WORKSPACE-WRITE/0.1',
                   **(self.scope if dataset_scope is None else dataset_scope), 'operation': operation,
                   'reason': 'fictional direct edit', **values}
        selected_origin = self.origin if origin == 'same' else origin
        return self.call('POST', '/api/workspace/write', payload, origin=selected_origin)

    def accepted(self, operation, **values):
        status, result = self.write(operation, **values)
        self.assertEqual((status, result['status']), (200, 'OK'), result)
        self.assertEqual((result['local_status'], result['dropbox_status']), ('LOCAL_SAVED', 'UNPUBLISHED'))
        self.scope = {key: result[key] for key in ('dataset_id', 'snapshot_id')}
        return result

    def create_target(self):
        target = 'usr-' + 'a' * 32
        record = {'title': 'fictional new target', 'body': 'fictional target body', 'summary': None,
                  'document_role': 'NOTE', 'review_status': 'PENDING_USER_REVIEW',
                  'adoption_status': 'NEW_WORKSPACE_RECORD', 'source_kind': 'USER_WORKSPACE'}
        result = self.accepted('create', record_id=target, record=record)
        self.assertEqual((result['record_id'], result['version']), (target, 1))
        return target

    def test_update_version_conflict_history_and_reopen(self):
        status, features = self.call('GET', '/api/workspace/features')
        self.assertEqual((status, features['read_detail'], features['writes_supported']), (200, True, True))
        previous_scope = dict(self.scope)
        result = self.accepted('update', record_id='fictional-record', expected_version=1,
                               changes={'title': 'fictional edited title', 'body': 'fictional edited body'})
        self.assertEqual(result['version'], 2)
        self.assertNotEqual(previous_scope['snapshot_id'], self.scope['snapshot_id'])
        status, details = self.detail()
        self.assertEqual((status, details['status'], details['record']['title'], details['record']['body'],
                          details['record']['version']), (200, 'OK', 'fictional edited title', 'fictional edited body', 2))
        self.assertEqual([row['version'] for row in details['revisions']], [2, 1])
        self.assertEqual(details['events'][0]['event_kind'], 'EDIT')
        self.assertEqual(self.detail(scope=previous_scope)[1]['error']['code'], 'VERSION_MISMATCH')
        status, stale = self.write('update', record_id='fictional-record', expected_version=1,
                                   changes={'title': 'fictional stale edit'})
        self.assertEqual((status, stale['status'], stale['error']['code']), (200, 'ERROR', 'VERSION_CONFLICT'))
        status, publication = self.call('GET', '/api/workspace/publish-status')
        self.assertEqual((status, publication['status']), (200, 'UNPUBLISHED'))
        reopened = LocalWorkspaceRuntime(self.path, self.root, codec=OriginalWorkspaceEventCodec())
        self.addCleanup(reopened.close)
        cap = reopened.read(request('capabilities'))
        fresh_scope = {key: cap['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
        detail = reopened.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
            'operation': 'detail', **fresh_scope, 'record_id': 'fictional-record'})
        self.assertEqual((detail['record']['title'], detail['record']['version']), ('fictional edited title', 2))
        self.assertEqual(reopened.publication_status()['dropbox_status'], 'UNKNOWN')

    def test_update_cannot_assign_approval_state_through_text_editor(self):
        status, value = self.write('update', record_id='fictional-record', expected_version=1,
                                   changes={'review_status': 'fictional-confirmed'})
        self.assertEqual((status, value['status'], value['error']['code']), (400, 'ERROR', 'INVALID_REQUEST'))
        _, detail = self.detail()
        self.assertEqual(detail['record']['version'], 1)

    def test_refresh_requires_same_origin_and_reopens_local_snapshot(self):
        payload = {'contract_version': 'UTT-WORKSPACE-STATUS/0.1'}
        self.assertEqual(self.call('POST', '/api/workspace/refresh', payload)[0], 403)
        status, result = self.call('POST', '/api/workspace/refresh', payload, origin=self.origin)
        self.assertEqual((status, result['status'], result['local_status']), (200, 'OK', 'LOCAL_SAVED'))
        self.assertEqual(result['dataset_id'], self.scope['dataset_id'])

    def test_create_archive_merge_and_restore_preserve_revision_history(self):
        target = self.create_target()
        self.assertEqual(self.detail(target)[1]['record']['version'], 1)
        self.accepted('archive', record_id='fictional-record', expected_version=1)
        self.assertIsNotNone(self.detail()[1]['record']['archived_at'])
        self.accepted('restore', record_id='fictional-record', expected_version=2,
                      restore_archive=True, restore_merge=False)
        self.assertIsNone(self.detail()[1]['record']['archived_at'])
        self.accepted('merge', record_id='fictional-record', expected_version=3,
                      target_record_id=target, target_expected_version=1)
        merged = self.detail()[1]['record']
        self.assertEqual(merged['merged_into'], target)
        self.assertIsNotNone(merged['archived_at'])
        self.accepted('restore', record_id='fictional-record', expected_version=4,
                      restore_archive=True, restore_merge=True)
        details = self.detail()[1]
        self.assertEqual((details['record']['version'], details['record']['merged_into'],
                          details['record']['archived_at']), (5, None, None))
        self.assertEqual([row['version'] for row in details['revisions']], [5, 4, 3, 2, 1])
        self.assertEqual([row['event_kind'] for row in details['events'][:4]],
                         ['UNMERGE', 'MERGE', 'RESTORE', 'ARCHIVE'])

    def test_missing_or_foreign_origin_cannot_write(self):
        for origin in (None, 'https://fictional.invalid'):
            with self.subTest(origin=origin):
                status, result = self.write('update', origin=origin, record_id='fictional-record',
                    expected_version=1, changes={'title': 'fictional forbidden change'})
                self.assertEqual((status, result['error']), (403, 'ORIGIN_DENIED'))
        self.assertEqual(self.detail()[1]['record']['version'], 1)
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM work_events').fetchone()[0], 2)

    def test_project_relation_changes_are_recorded_without_new_record_revision(self):
        status, forged = self.write('project_add', record_id='fictional-record', expected_version=1,
                                    link_id='fictional-forged', project_id='fictional-project',
                                    role='related', scope=None, basis='USER_CONFIRMED')
        self.assertEqual((status, forged['error']['code']), (400, 'INVALID_REQUEST'))
        self.accepted('project_add', record_id='fictional-record', expected_version=1,
                      link_id='fictional-link', project_id='fictional-project',
                      role='related', scope=None)
        details = self.detail()[1]
        self.assertEqual(details['record']['version'], 1)
        self.assertEqual(details['projects'][0]['link']['basis'], 'USER_CONFIRMED')
        self.assertIsNone(details['projects'][0]['link']['archived_at'])
        self.accepted('project_archive', record_id='fictional-record', expected_version=1,
                      link_id='fictional-link')
        self.assertIsNotNone(self.detail()[1]['projects'][0]['link']['archived_at'])
        self.accepted('project_restore', record_id='fictional-record', expected_version=1,
                      link_id='fictional-link')
        details = self.detail()[1]
        self.assertIsNone(details['projects'][0]['link']['archived_at'])
        self.assertEqual([row['event_kind'] for row in details['events'][:3]],
                         ['PROJECT_RESTORE', 'PROJECT_ARCHIVE', 'PROJECT_LINK'])
        self.assertEqual([row['version'] for row in details['revisions']], [1])

    def test_external_data_revision_change_requires_refresh_before_write(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE work_meta SET value='900' WHERE key='data_revision'")
            conn.commit()
        status, result = self.write('update', record_id='fictional-record', expected_version=1,
                                    changes={'title': 'fictional stale scope'})
        self.assertEqual((status, result['status'], result['error']['code']),
                         (200, 'ERROR', 'DATA_REVISION_CONFLICT'))
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT title,version FROM work_records').fetchone(),
                             ('fictional title', 1))
            self.assertEqual(conn.execute('SELECT count(*) FROM work_events').fetchone()[0], 2)

    def test_topic_and_tag_links_record_removal_history(self):
        status, forged = self.write('topic_add', record_id='fictional-record', expected_version=1,
                                    topic_id='fictional-topic', status='USER_CONFIRMED')
        self.assertEqual((status, forged['error']['code']), (400, 'INVALID_REQUEST'))
        self.accepted('topic_add', record_id='fictional-record', expected_version=1,
                      topic_id='fictional-topic')
        self.accepted('tag_add', record_id='fictional-record', expected_version=1,
                      tag_id='fictional-tag', tag_name='fictional tag')
        details = self.detail()[1]
        self.assertEqual(details['topics'][0]['link']['status'], 'USER_CONFIRMED')
        self.assertEqual(details['tags'][0]['name'], 'fictional tag')
        self.accepted('topic_remove', record_id='fictional-record', expected_version=1,
                      topic_id='fictional-topic')
        self.accepted('tag_remove', record_id='fictional-record', expected_version=1,
                      tag_id='fictional-tag')
        details = self.detail()[1]
        self.assertEqual((details['topics'], details['tags']), ([], []))
        self.assertEqual([event['event_kind'] for event in details['events'][:4]],
                         ['TAG_REMOVE', 'TOPIC_UNLINK', 'TAG', 'TOPIC_LINK'])
        self.assertEqual(details['record']['version'], 1)

    def test_http_publish_and_reconnect_with_synthetic_dropbox(self):
        from app.publisher_reader import ACTIVE
        from app.publisher_workspace_server import load_verified_workspace

        class Session(FakeSession):
            def close(self):
                pass

        self.accepted('update', record_id='fictional-record', expected_version=1,
                      changes={'body': 'fictional published body'})
        fake = FakeTransport(self.parent_objects)
        payload = {'contract_version': 'UTT-WORKSPACE-PUBLISH/0.1', **self.scope}
        with patch('app.workspace_publisher.run_publisher_flow', return_value=Session()), \
             patch('app.workspace_publisher.PublisherTransport', return_value=fake):
            status, result = self.call('POST', '/api/workspace/publish', payload, origin=self.origin)
            self.assertEqual((status, result['status']), (200, 'RUNNING'))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status, result = self.call('GET', '/api/workspace/publish-status')
                if result['status'] != 'RUNNING':
                    break
                time.sleep(0.01)
        self.assertEqual((status, result['status']), (200, 'PUBLISHED'), result)
        self.assertEqual(fake.writes[-1], ('cas', ACTIVE))

        # Re-open the active bytes from the simulated remote using the actual
        # verified reader bridge; no network or live credentials are involved.
        class RemoteRead:
            def control(self, path):
                value = fake.objects.get(path)
                return None if value is None else (value, fake.revision if path == ACTIVE else 'rev-1')

            def workspace(self, path, *, expected_size):
                value = fake.objects[path]
                assert len(value) == expected_size
                return value, 'rev-1'

        provider = load_verified_workspace(RemoteRead())
        self.addCleanup(provider.close)
        cap = provider.read(request('capabilities'))
        scope = {key: cap['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
        detail = provider.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
            'operation': 'detail', **scope, 'record_id': 'fictional-record'})
        self.assertEqual((detail['status'], detail['record']['body'], detail['record']['version']),
                         ('OK', 'fictional published body', 2))


if __name__ == '__main__':
    unittest.main()
