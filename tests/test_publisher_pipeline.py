"""Entire publication chain with fictional SQLite and in-memory Dropbox only."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.publisher_pipeline import publish_snapshot
from app.publisher_reader import ACTIVE
from app.publisher_transport import RemoteMetadata, RemoteObject
from app.publisher_workspace import snapshot_workspace
from tests.test_publisher_workspace import make_fixture


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return (json.dumps(value, separators=(',', ':')) + '\n').encode()


class FakeSession:
    def is_valid(self):
        return True


class FakeTransport:
    def __init__(self, objects):
        self.objects = objects.copy()
        self.revision = 'rev-1'
        self.writes = []
        self.fail_cas = False
        self.corrupt_download = False
        self.mutate_parent_after_commit = None

    def bind_permit(self, permit):
        self.permit = permit

    def metadata(self, path):
        raw = self.objects.get(path)
        return None if raw is None else RemoteMetadata(len(raw), 'rev-1')

    def download(self, path, sink, *, max_bytes):
        raw = self.objects.get(path)
        if raw is None:
            return None
        if len(raw) > max_bytes:
            raise ValueError('private remote failure')
        data = raw + b'x' if self.corrupt_download and 'tables.json' in path else raw
        sink.write(data)
        return RemoteObject(len(raw), self.revision if path == ACTIVE else 'rev-1', sha(raw))

    def upload_add(self, path, stream, size):
        if path in self.objects:
            raise ValueError('already exists')
        data = stream.read()
        if len(data) != size:
            raise ValueError('size mismatch')
        self.objects[path] = data
        self.writes.append(('add', path))
        if path.endswith('/system/COMMIT.json') and self.mutate_parent_after_commit:
            self.objects[self.mutate_parent_after_commit] = b'fictional changed parent'

    def update_if_revision(self, path, data, rev):
        self.writes.append(('cas', path))
        if self.fail_cas or rev != self.revision:
            raise RuntimeError('private credential diagnostic')
        self.objects[path] = data
        self.revision = 'rev-2'


def initial_objects(conn):
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='utt_object'").fetchone()[0]
    table = {'name': 'utt_object', 'columns': ['id'], 'rowid': False,
             'count': 0, 'shards': [], 'sql': schema}
    payloads = {'system/baseline.json': b'{}', 'system/schema.json': b'{}',
                'snapshots/tables.json': wire([table]), 'snapshots/objects.json': b'{}',
                'sources/index.json': b'{}'}
    files = [{'path': p, 'bytes': len(v), 'sha256': sha(v)} for p, v in payloads.items()]
    manifest = wire({'format': 'utt-baseline/1', 'baseline_id': 'fictional-baseline', 'files': files})
    generation = sha(manifest)
    root = '/snapshots/baselines/' + generation + '/'
    commit = wire({'format': 'utt-baseline/1', 'baseline_id': 'fictional-baseline',
                   'manifest_sha256': generation, 'state': 'complete'})
    active = wire({'format': 'utt-active-baseline/1', 'generation_id': generation,
                   'manifest_sha256': generation, 'baseline_id': 'fictional-baseline',
                   'root': root, 'commit_sha256': sha(commit), 'files': len(files) + 2,
                   'bytes': sum(len(v) for v in payloads.values()) + len(manifest) + len(commit)})
    objects = {root + p: v for p, v in payloads.items()}
    objects.update({root + 'system/manifest.json': manifest, root + 'system/COMMIT.json': commit,
                    ACTIVE: active})
    return objects


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fictional.sqlite'
        self.writer = make_fixture(self.path)
        self.addCleanup(self.writer.close)
        self.transport = FakeTransport(initial_objects(self.writer))
        self.session = FakeSession()

    def run_pipeline(self):
        with snapshot_workspace(self.path, directory=Path(self.tmp.name)) as snapshot:
            return publish_snapshot(snapshot, self.transport, self.session)

    def test_complete_flow_and_idempotent_recovery(self):
        result = self.run_pipeline()
        self.assertEqual((result.status, result.code), ('PUBLISHED', 'PUBLISHED'))
        self.assertEqual(sum(kind == 'cas' for kind, _ in self.transport.writes), 1)
        commit_index = next(i for i, (kind, path) in enumerate(self.transport.writes)
                            if kind == 'add' and path.endswith('/system/COMMIT.json'))
        self.assertEqual(self.transport.writes[-1][0], 'cas')
        self.assertLess(commit_index, len(self.transport.writes) - 1)
        self.transport.writes.clear()
        result = self.run_pipeline()
        self.assertEqual(result.code, 'ALREADY_ACTIVE')
        self.assertEqual(self.transport.writes, [])

    def test_read_only_preflight_verifies_parent_without_any_remote_writes(self):
        with snapshot_workspace(self.path, directory=Path(self.tmp.name)) as snap:
            result = publish_snapshot(snap, self.transport, self.session,
                                      preflight_only=True)
        self.assertEqual((result.status, result.code), ('PREFLIGHT', 'PREFLIGHT_OK'))
        self.assertEqual(self.transport.writes, [])
        self.assertFalse(hasattr(self.transport, 'permit'))

    def test_read_only_preflight_reports_precise_failure_and_does_not_upload(self):
        self.transport.corrupt_download = True
        with snapshot_workspace(self.path, directory=Path(self.tmp.name)) as snap:
            result = publish_snapshot(snap, self.transport, self.session,
                                      preflight_only=True)
        self.assertEqual((result.status, result.code), ('NOT_VERIFIED', 'REMOTE_PAYLOAD_MISMATCH'))
        self.assertEqual(self.transport.writes, [])

    def test_timed_out_active_control_read_uses_fresh_buffer_and_recovers(self):
        from app.publisher_transport import PublisherTransportError
        class OneTimeout(FakeTransport):
            def __init__(self, objects):
                super().__init__(objects)
                self.failed = False
            def download(self, path, sink, *, max_bytes):
                if path == ACTIVE and not self.failed:
                    self.failed = True
                    sink.write(b'partial-old-control')
                    raise PublisherTransportError('READ_TIMEOUT')
                return super().download(path, sink, max_bytes=max_bytes)
        self.transport = OneTimeout(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual(result.code, 'PUBLISHED')
        self.assertTrue(self.transport.failed)

    def test_timed_out_parent_shard_read_resumes_without_partial_content(self):
        from app.publisher_transport import PublisherTransportError
        class OneTimeout(FakeTransport):
            def __init__(self, objects):
                super().__init__(objects)
                self.failed = False
            def download(self, path, sink, *, max_bytes):
                if path.endswith('snapshots/tables.json') and not self.failed:
                    self.failed = True
                    sink.write(b'partial-shard')
                    raise PublisherTransportError('READ_TIMEOUT')
                return super().download(path, sink, max_bytes=max_bytes)
        self.transport = OneTimeout(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual(result.code, 'PUBLISHED')
        self.assertTrue(self.transport.failed)

    def test_timed_out_staging_metadata_read_recovers_without_duplicate_write(self):
        from app.publisher_transport import PublisherTransportError
        class OneTimeout(FakeTransport):
            def __init__(self, objects):
                super().__init__(objects)
                self.failed = False
            def metadata(self, path):
                if path.endswith('system/COMMIT.json') and not self.failed:
                    self.failed = True
                    raise PublisherTransportError('READ_TIMEOUT')
                return super().metadata(path)
        self.transport = OneTimeout(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual(result.code, 'PUBLISHED')
        self.assertTrue(self.transport.failed)
        self.assertEqual(sum(kind == 'cas' for kind, _ in self.transport.writes), 1)

    def test_repeated_read_timeout_stops_without_any_remote_writes(self):
        from app.publisher_transport import PublisherTransportError
        class AlwaysTimeout(FakeTransport):
            def download(self, path, sink, *, max_bytes):
                if path == ACTIVE:
                    sink.write(b'partial')
                    raise PublisherTransportError('READ_TIMEOUT')
                return super().download(path, sink, max_bytes=max_bytes)
        self.transport = AlwaysTimeout(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual((result.status, result.code), ('NOT_VERIFIED', 'ACTIVE_READ_TIMEOUT'))
        self.assertEqual(self.transport.writes, [])

    def test_uncertain_cas_is_never_retried_or_logged(self):
        self.transport.fail_cas = True
        result = self.run_pipeline()
        self.assertEqual(result.code, 'ACTIVATION_UNKNOWN')
        self.assertEqual(sum(kind == 'cas' for kind, _ in self.transport.writes), 1)
        self.assertNotIn('credential', repr(result))

    def test_corrupt_parent_stops_before_any_write(self):
        self.transport.corrupt_download = True
        before = self.transport.objects[ACTIVE]
        result = self.run_pipeline()
        self.assertEqual(result.code, 'REMOTE_PAYLOAD_MISMATCH')
        self.assertEqual(self.transport.writes, [])
        self.assertEqual(before, self.transport.objects[ACTIVE])

    def test_reader_validates_and_publishes_archived_record(self):
        self.writer.execute("UPDATE work_records SET archived_at='fictional-time'")
        self.writer.commit()
        result = self.run_pipeline()
        self.assertEqual((result.status, result.code), ('PUBLISHED', 'PUBLISHED'))

    def test_reader_validates_and_publishes_merged_record(self):
        self.writer.execute('''INSERT INTO work_records(record_id,title,body,body_sha256,
                            document_role,review_status,adoption_status)
                            SELECT 'fictional-target',title,body,body_sha256,
                            document_role,review_status,adoption_status FROM work_records
                            WHERE record_id='fictional-record' ''')
        self.writer.execute("UPDATE work_records SET merged_into='fictional-target' WHERE record_id='fictional-record'")
        self.writer.commit()
        result = self.run_pipeline()
        self.assertEqual((result.status, result.code), ('PUBLISHED', 'PUBLISHED'))

    def test_preflight_rejects_extended_detail_with_wrong_lifecycle(self):
        from app.publisher_workspace_reader import WorkspaceSnapshotProvider
        forged = {'status': 'OK', 'record': {'version': 1, 'archived_at': 'forged', 'merged_into': None}}
        with patch.object(WorkspaceSnapshotProvider, 'read_workspace_detail', return_value=forged):
            result = self.run_pipeline()
        self.assertEqual(result.code, 'READER_UNSUPPORTED')
        self.assertEqual(self.transport.writes, [])

    def test_changed_source_stops_before_any_write(self):
        with snapshot_workspace(self.path, directory=Path(self.tmp.name)) as snap:
            self.writer.execute("UPDATE work_records SET summary='fictional alteration'")
            self.writer.commit()
            result = publish_snapshot(snap, self.transport, self.session)
        self.assertEqual(result.code, 'SOURCE_CHANGED')
        self.assertEqual(self.transport.writes, [])

    def test_existing_workspace_history_conflict_stops_before_any_write(self):
        self.assertEqual(self.run_pipeline().code, 'PUBLISHED')
        self.transport.writes.clear()
        self.writer.execute("DELETE FROM work_events")
        self.writer.commit()
        result = self.run_pipeline()
        self.assertEqual(result.status, 'NOT_VERIFIED')
        self.assertTrue(result.code.startswith('PARENT_WORKSPACE_'), result.code)
        self.assertEqual(self.transport.writes, [])

    def test_committed_generation_is_never_repaired(self):
        self.assertEqual(self.run_pipeline().code, 'PUBLISHED')
        active = json.loads(self.transport.objects[ACTIVE])
        del self.transport.objects[active['root'] + 'system/baseline.json']
        self.transport.writes.clear()
        result = self.run_pipeline()
        self.assertEqual(result.code, 'REMOTE_PAYLOAD_MISMATCH')
        self.assertEqual(self.transport.writes, [])

    def test_parent_changed_after_copy_cannot_be_activated(self):
        active = json.loads(self.transport.objects[ACTIVE])
        self.transport.mutate_parent_after_commit = active['root'] + 'system/baseline.json'
        original = self.transport.objects[ACTIVE]
        result = self.run_pipeline()
        self.assertNotEqual(result.status, 'PUBLISHED')
        self.assertFalse(any(kind == 'cas' for kind, _ in self.transport.writes))
        self.assertEqual(self.transport.objects[ACTIVE], original)

    def test_active_workspace_is_downloaded_again_after_cas(self):
        class TrackingTransport(FakeTransport):
            def __init__(self, objects):
                super().__init__(objects)
                self.after_cas = False
                self.read_workspace_after_cas = False

            def update_if_revision(self, path, data, rev):
                super().update_if_revision(path, data, rev)
                self.after_cas = True

            def download(self, path, sink, *, max_bytes):
                if self.after_cas and '/workspace/snapshot-' in path:
                    self.read_workspace_after_cas = True
                return super().download(path, sink, max_bytes=max_bytes)

        self.transport = TrackingTransport(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual(result.code, 'PUBLISHED')
        self.assertTrue(self.transport.read_workspace_after_cas)

    def test_corrupted_workspace_after_cas_never_reports_success(self):
        class CorruptAfterCAS(FakeTransport):
            def update_if_revision(self, path, data, rev):
                super().update_if_revision(path, data, rev)
                active = json.loads(data)
                manifest = json.loads(self.objects[active['root'] + 'system/manifest.json'])
                target = active['root'] + manifest['utt_workspace']['path']
                self.objects[target] = b'x' * len(self.objects[target])

        self.transport = CorruptAfterCAS(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual((result.status, result.code), ('NOT_VERIFIED', 'ACTIVE_DB_UNVERIFIED'))
        self.assertEqual(sum(kind == 'cas' for kind, _ in self.transport.writes), 1)

    def test_active_revision_changed_during_workspace_read_is_unverified(self):
        class ChangeActiveAfterRead(FakeTransport):
            def __init__(self, objects):
                super().__init__(objects)
                self.after_cas = False

            def update_if_revision(self, path, data, rev):
                super().update_if_revision(path, data, rev)
                self.after_cas = True

            def download(self, path, sink, *, max_bytes):
                result = super().download(path, sink, max_bytes=max_bytes)
                if self.after_cas and '/workspace/snapshot-' in path:
                    self.revision = 'rev-fictional-other'
                return result

        self.transport = ChangeActiveAfterRead(initial_objects(self.writer))
        result = self.run_pipeline()
        self.assertEqual((result.status, result.code), ('NOT_VERIFIED', 'ACTIVE_DB_UNVERIFIED'))
        self.assertEqual(sum(kind == 'cas' for kind, _ in self.transport.writes), 1)


if __name__ == '__main__':
    unittest.main()
