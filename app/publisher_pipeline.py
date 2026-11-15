"""Publication orchestration over a pinned local workspace and checked parent.

Only a caller-created, user-authorized session and path-bound transport perform
I/O. Errors and results contain fixed codes only. This is not an OS isolation
boundary or proof of merge approval; the operator must quiesce local writers.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import io
import sqlite3
import tempfile

from app.provider import request
from app.dropbox_auth import OAuthError
from app.publisher_candidate import CandidateError, build_candidate
from app.publisher_parent import compare_parent_tables, compare_workspace_snapshots
from app.publisher_reader import ACTIVE, ReaderError, assert_native_payloads, assert_parent_preserved, parse_manifest, resolve_active
from app.publisher_transport import PublisherTransportError, _mint_write_permit
from app.publisher_workspace import WorkspaceError
from app.publisher_workspace_reader import WorkspaceSnapshotError, WorkspaceSnapshotProvider

_CONTROL_LIMIT = 16 * 1024 * 1024
_WORKSPACE_LIMIT = 512 * 1024 * 1024


@dataclass(frozen=True)
class PublicationResult:
    status: str
    code: str


class PublicationError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


_Stop = PublicationError


def _read_object(transport, path, limit):
    out = io.BytesIO()
    result = transport.download(path, out, max_bytes=limit)
    if result is None:
        return None
    data = out.getvalue()
    if len(data) != result.size or hashlib.sha256(data).hexdigest() != result.sha256:
        raise _Stop('REMOTE_PAYLOAD_MISMATCH')
    return data, result.rev


def _copy_verified(transport, path, size, digest, directory):
    out = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, dir=directory, mode='w+b')
    try:
        result = transport.download(path, out, max_bytes=size)
        if result is None or result.size != size or result.sha256 != digest:
            raise _Stop('REMOTE_PAYLOAD_MISMATCH')
        out.seek(0)
        h = hashlib.sha256()
        count = 0
        while True:
            block = out.read(1024 * 1024)
            if not block:
                break
            count += len(block)
            h.update(block)
        if count != size or h.hexdigest() != digest:
            raise _Stop('REMOTE_PAYLOAD_MISMATCH')
        out.seek(0)
        return out
    except BaseException:
        out.close()
        raise


def _preflight_reader(snapshot):
    size, digest = snapshot.describe_snapshot()
    if size > _WORKSPACE_LIMIT:
        raise _Stop('READER_SIZE_UNSUPPORTED')
    raw = snapshot.path.read_bytes()
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise _Stop('SNAPSHOT_CHANGED')
    provider = WorkspaceSnapshotProvider(raw)
    try:
        cap = provider.read(request('capabilities'))
        if cap.get('status') != 'OK':
            raise _Stop('READER_UNSUPPORTED')
        scope = {'dataset_id': cap['snapshot']['dataset_id'], 'snapshot_id': cap['snapshot']['snapshot_id']}
        cursor = None
        seen = set()
        while True:
            page = provider.read(request('search', page_size=100, cursor=cursor, **scope))
            if page.get('status') != 'OK':
                raise _Stop('READER_UNSUPPORTED')
            for record in page['result']['records']:
                ident = record['record_id']
                if ident in seen:
                    raise _Stop('READER_UNSUPPORTED')
                seen.add(ident)
                detail = provider.read(request('detail', record_id=ident, **scope))
                if detail.get('status') != 'OK':
                    raise _Stop('READER_UNSUPPORTED')
            next_cursor = page['result']['page']['next_cursor']
            if next_cursor is None:
                break
            if next_cursor == cursor:
                raise _Stop('READER_UNSUPPORTED')
            cursor = next_cursor
        with closing(snapshot.open_readonly()) as conn:
            if len(seen) != conn.execute('SELECT count(*) FROM work_records').fetchone()[0]:
                raise _Stop('READER_UNSUPPORTED')
    finally:
        provider.close()


def publish_snapshot(snapshot, transport, session):
    """Verify, stage, re-read and activate once; never automatically retry a write.

    ``snapshot`` must remain within its context until return, with all local
    writers quiescent throughout. The transport must be authenticated and
    bound to the same user-confirmed App Folder as the caller's session.
    """
    activation_started = False
    try:
        snapshot.assert_source_unchanged()
        _preflight_reader(snapshot)
        fetch = lambda p: _read_object(transport, p, _CONTROL_LIMIT)
        parent = resolve_active(fetch)
        assert_native_payloads(parent.manifest)
        manifest_reply = fetch(parent.root + 'system/manifest.json')
        if manifest_reply is None:
            raise _Stop('PARENT_MANIFEST_MISSING')
        parent_raw = manifest_reply[0]
        if parse_manifest(parent_raw) != parent.manifest:
            raise _Stop('PARENT_CHANGED')
        files = {e.path: {'bytes': e.size, 'sha256': e.sha256} for e in parent.manifest.entries}
        entry_by_path = {e.path: e for e in parent.manifest.entries}
        catalog_entry = entry_by_path['snapshots/tables.json']
        if catalog_entry.size > _CONTROL_LIMIT:
            raise _Stop('PARENT_CATALOG_UNSUPPORTED')
        with _copy_verified(transport, parent.root + catalog_entry.path, catalog_entry.size,
                            catalog_entry.sha256, snapshot.path.parent) as catalog:
            catalog_raw = catalog.read()
        def shard(path):
            entry = entry_by_path.get(path)
            if entry is None:
                raise _Stop('PARENT_SHARD_MISSING')
            return _copy_verified(transport, parent.root + path, entry.size, entry.sha256, snapshot.path.parent)
        with closing(snapshot.open_readonly()) as candidate_conn:
            comparison = compare_parent_tables(candidate_conn, catalog_raw, files, shard)
            if comparison.status not in ('PARENT_ROWS_PRESENT', 'PARENT_LOGICAL_ROWS_PRESENT'):
                raise _Stop('PARENT_' + comparison.code)
            overlay = parent.manifest.workspace
            if overlay is not None:
                with tempfile.TemporaryDirectory(prefix='utt-parent-', dir=snapshot.path.parent) as temp:
                    path = __import__('pathlib').Path(temp) / 'parent.sqlite'
                    with _copy_verified(transport, parent.root + overlay.path, overlay.size,
                                        overlay.sha256, snapshot.path.parent) as src, path.open('wb') as dst:
                        while block := src.read(1024 * 1024):
                            dst.write(block)
                    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as parent_conn:
                        parent_conn.execute('PRAGMA query_only=ON')
                        parent_conn.execute('PRAGMA trusted_schema=OFF')
                        parent_conn.execute('BEGIN')
                        parent_conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
                        comparison = compare_workspace_snapshots(parent_conn, candidate_conn)
                        if comparison.status != 'WORKSPACE_HISTORY_PRESERVED':
                            raise _Stop('PARENT_WORKSPACE_' + comparison.code)
        size, digest = snapshot.describe_snapshot()
        candidate = build_candidate(parent, parent_raw, workspace_size=size, workspace_sha256=digest)
        assert_parent_preserved(parent.manifest, parse_manifest(candidate.manifest_bytes))
        specs = {candidate.root + e.path: (e.size, e.sha256) for e in candidate.entries}
        manifest_path = candidate.root + 'system/manifest.json'
        commit_path = candidate.root + 'system/COMMIT.json'
        specs[manifest_path] = (len(candidate.manifest_bytes), hashlib.sha256(candidate.manifest_bytes).hexdigest())
        specs[commit_path] = (len(candidate.commit_bytes), hashlib.sha256(candidate.commit_bytes).hexdigest())
        permit = _mint_write_permit(session, files=specs, active_bytes=candidate.active_bytes,
                                    previous_rev=parent.active_revision,
                                    previous_sha256=hashlib.sha256(parent.active_bytes).hexdigest())
        transport.bind_permit(permit)
        # A committed generation is immutable: if damaged, do not repair it.
        committed = transport.metadata(commit_path) is not None
        def stage(path, stream, length, sha):
            meta = transport.metadata(path)
            if meta is None:
                if committed:
                    raise _Stop('COMMITTED_GENERATION_DAMAGED')
                transport.upload_add(path, stream, length)
            with _copy_verified(transport, path, length, sha, snapshot.path.parent):
                pass
        for entry in candidate.entries:
            target = candidate.root + entry.path
            if entry.path == candidate.workspace_path:
                snapshot.assert_snapshot_unchanged()
                with snapshot.path.open('rb') as stream:
                    stage(target, stream, entry.size, entry.sha256)
            else:
                old = entry_by_path.get(entry.path)
                if old != entry:
                    raise _Stop('PARENT_PAYLOAD_CONFLICT')
                with _copy_verified(transport, parent.root + old.path, old.size, old.sha256,
                                    snapshot.path.parent) as stream:
                    stage(target, stream, entry.size, entry.sha256)
        stage(manifest_path, io.BytesIO(candidate.manifest_bytes), *specs[manifest_path])
        # Confirm staged payloads before publishing COMMIT, independently of metadata.
        for path, (length, sha) in specs.items():
            if path != commit_path:
                with _copy_verified(transport, path, length, sha, snapshot.path.parent):
                    pass
        stage(commit_path, io.BytesIO(candidate.commit_bytes), *specs[commit_path])
        # The active reference revision does not protect individual objects.
        # Re-read the complete parent immediately before activation as well as
        # the reference. Dropbox has no multi-object transaction here: changes
        # after these checks remain an operational race and require isolation.
        for entry in parent.manifest.entries:
            with _copy_verified(transport, parent.root + entry.path, entry.size,
                                entry.sha256, snapshot.path.parent):
                pass
        observed = resolve_active(fetch)
        if observed.active_bytes != parent.active_bytes or observed.active_revision != parent.active_revision:
            raise _Stop('ACTIVE_CHANGED')
        snapshot.assert_source_unchanged()
        snapshot.assert_snapshot_unchanged()
        if candidate.active_bytes == parent.active_bytes:
            current = resolve_active(fetch)
            if (current.active_bytes != parent.active_bytes
                    or current.active_revision != parent.active_revision):
                raise _Stop('ACTIVE_CHANGED')
            return PublicationResult('PUBLISHED', 'ALREADY_ACTIVE')
        activation_started = True
        transport.update_if_revision(ACTIVE, candidate.active_bytes, parent.active_revision)
        current = resolve_active(fetch)
        if current.active_bytes != candidate.active_bytes:
            raise _Stop('ACTIVATION_NOT_CONFIRMED')
        return PublicationResult('PUBLISHED', 'PUBLISHED')
    except _Stop as exc:
        return PublicationResult('NOT_VERIFIED', exc.code)
    except (OAuthError, CandidateError, ReaderError, PublisherTransportError,
            WorkspaceError, WorkspaceSnapshotError) as exc:
        return PublicationResult('NOT_VERIFIED', 'ACTIVATION_UNKNOWN' if activation_started else exc.code)
    except Exception:
        # Do not leak exception text (possibly source values or credentials), and
        # do not repeat an uncertain upload or conditional active update.
        return PublicationResult('NOT_VERIFIED', 'ACTIVATION_UNKNOWN' if activation_started else 'PUBLICATION_FAILED')
