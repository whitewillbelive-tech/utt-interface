"""Local workspace runtime with snapshot-bound reads and guarded writes.

Paths and data remain on the user's machine. Reads use an immutable SQLite
backup; each edit opens a separate existing-file connection, with its expected
data revision checked by WorkspaceWriter inside BEGIN IMMEDIATE. A single lock
serializes this runtime's reads, refreshes, writes and publication. It does not
prevent access by other OS processes or independently prove their isolation.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import threading

from app.provider import error, request
from app.publisher_pipeline import publish_snapshot
from app.publisher_workspace import snapshot_workspace
from app.publisher_workspace_reader import WorkspaceSnapshotProvider
from app.workspace_write import WorkspaceWriteError, WorkspaceWriter

_WRITE_VERSION = 'UTT-WORKSPACE-WRITE/0.1'
_STATUS_VERSION = 'UTT-WORKSPACE-STATUS/0.1'
_PUBLICATION_VERSION = 'UTT-WORKSPACE-PUBLICATION/0.1'


class WorkspaceRuntimeError(Exception):
    def __init__(self, code='RUNTIME_UNAVAILABLE'):
        self.code = code
        super().__init__(code)


def _failure(version, code, **extra):
    return {'contract_version': version, 'status': 'ERROR', 'error': {'code': code}, **extra}


def _observation(path):
    """Detect ordinary replacement or modification; not an ABA proof."""
    values = []
    for target in (path, Path(str(path) + '-wal'), Path(str(path) + '-journal')):
        try:
            info = os.stat(target, follow_symlinks=False)
            values.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        except FileNotFoundError:
            if target == path:
                raise WorkspaceRuntimeError('SOURCE_UNAVAILABLE') from None
            values.append(None)
        except OSError:
            raise WorkspaceRuntimeError('SOURCE_UNAVAILABLE') from None
    return tuple(values)


def _connect(path, mode):
    try:
        # URI mode=rw/ro never creates a missing database.
        return sqlite3.connect(path.as_uri() + '?mode=' + mode, uri=True,
                               timeout=30.0, isolation_level=None)
    except (sqlite3.Error, OSError, ValueError):
        raise WorkspaceRuntimeError('SOURCE_UNAVAILABLE') from None


class LocalWorkspaceRuntime:
    """One selected local DB with snapshot-scoped reading and local editing.

    ``codec`` must be the exact verified workspace event codec; this class does
    not accept an implicit or synthetic fallback. The selected temporary area
    must be protected by the user's local OS policies, which are not verified.
    """

    def __init__(self, source_path, protected_dir, *, codec):
        self.__lock = threading.RLock()
        self.__provider = None
        self.__scope = None
        self.__observation = None
        self.__closed = False
        self.__dropbox_status = 'UNKNOWN'
        self.__refresh_required = False
        if (codec is None or not callable(getattr(codec, 'verify_history', None))
                or not callable(getattr(codec, 'encode', None))):
            raise WorkspaceRuntimeError('VERIFIED_EVENT_CODEC_REQUIRED')
        self.__codec = codec
        try:
            # The editable working file and backup directory are local user
            # inputs, not a request to access a UNC share. Mapped drives can
            # still be remote; OS policy must establish the actual boundary.
            if os.name == 'nt' and any(str(value).startswith('\\\\') for value in (source_path, protected_dir)):
                raise WorkspaceRuntimeError('INVALID_LOCAL_INPUT')
            self.__source = Path(source_path).resolve(strict=True)
            self.__directory = Path(protected_dir).resolve(strict=True)
            if not self.__source.is_file() or not self.__directory.is_dir():
                raise WorkspaceRuntimeError('INVALID_LOCAL_INPUT')
            self._refresh_locked()
        except WorkspaceRuntimeError:
            raise
        except Exception:
            raise WorkspaceRuntimeError('RUNTIME_UNAVAILABLE') from None

    def __repr__(self):
        return '<LocalWorkspaceRuntime redacted>'

    def _assert_open(self):
        if self.__closed or self.__provider is None or self.__scope is None:
            raise WorkspaceRuntimeError('CONNECTION_UNAVAILABLE')

    def _source_revision(self):
        with closing(_connect(self.__source, 'ro')) as conn:
            try:
                row = conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()
            except sqlite3.Error:
                raise WorkspaceRuntimeError('SOURCE_UNAVAILABLE') from None
        if row is None or type(row[0]) is not str or not row[0]:
            raise WorkspaceRuntimeError('SOURCE_UNAVAILABLE')
        return row[0]

    def _source_is_current(self):
        self._assert_open()
        before = _observation(self.__source)
        revision = self._source_revision()
        after = _observation(self.__source)
        return (before == after == self.__observation
                and revision == self.__scope['source_version'])

    def _latch_refresh_required(self):
        self.__refresh_required = True
        self.__dropbox_status = 'UNKNOWN'
        if self.__provider is not None:
            self.__provider.close()
        self.__provider, self.__scope = None, None

    def _revalidate_after_rollback(self):
        """Accept changed WAL metadata only if captured source bytes are identical.

        This keeps the old provider and cursor tokens alive. Failure leaves the
        caller to explicitly refresh; it never retries the rejected mutation.
        """
        self._assert_open()
        if self._source_is_current():
            return
        expected_snapshot = self.__scope['snapshot_id']
        expected_revision = self.__scope['source_version']
        with snapshot_workspace(self.__source, directory=self.__directory) as snap:
            _, digest = snap.describe_snapshot()
            if expected_snapshot != 'sha256:' + digest:
                raise WorkspaceRuntimeError('SOURCE_CHANGED')
            snap.assert_source_unchanged()
            snap.assert_snapshot_unchanged()
        before = _observation(self.__source)
        revision = self._source_revision()
        after = _observation(self.__source)
        if before != after or revision != expected_revision:
            raise WorkspaceRuntimeError('SOURCE_CHANGED')
        self.__observation = after

    def _refresh_locked(self):
        provider = None
        try:
            with snapshot_workspace(self.__source, directory=self.__directory) as snap:
                size, digest = snap.describe_snapshot()
                if size > 512 * 1024 * 1024:
                    raise WorkspaceRuntimeError('READER_SIZE_UNSUPPORTED')
                raw = snap.path.read_bytes()
                if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
                    raise WorkspaceRuntimeError('SNAPSHOT_CHANGED')
                provider = WorkspaceSnapshotProvider(raw, lifecycle_enabled=True)
                cap = provider.read(request('capabilities'))
                if cap.get('status') != 'OK' or type(cap.get('snapshot')) is not dict:
                    raise WorkspaceRuntimeError('READER_UNSUPPORTED')
                scope = cap['snapshot'].copy()
                snap.assert_source_unchanged()
                snap.assert_snapshot_unchanged()
            # Capture after closing backup connections, which may release WAL
            # sidecars. Check revision and both stat observations before use.
            before = _observation(self.__source)
            revision = self._source_revision()
            after = _observation(self.__source)
            if before != after or revision != scope.get('source_version'):
                raise WorkspaceRuntimeError('SOURCE_CHANGED')
            old = self.__provider
            old_id = self.__scope.get('snapshot_id') if self.__scope else None
            self.__provider, self.__scope, self.__observation = provider, scope, after
            self.__refresh_required = False
            provider = None
            if old is not None:
                old.close()
            if old_id is not None and old_id != scope['snapshot_id'] and self.__dropbox_status == 'PUBLISHED':
                self.__dropbox_status = 'UNPUBLISHED'
        except WorkspaceRuntimeError:
            raise
        except Exception:
            raise WorkspaceRuntimeError('SNAPSHOT_UNAVAILABLE') from None
        finally:
            if provider is not None:
                provider.close()

    def read(self, q, sort='id_asc'):
        with self.__lock:
            try:
                self._assert_open()
                return self.__provider.read(q, sort)
            except Exception:
                return error(q.get('operation') if type(q) is dict else None, 'CONNECTION_UNAVAILABLE')

    def lifecycle(self, **kwargs):
        with self.__lock:
            self._assert_open()
            return self.__provider.lifecycle(**kwargs)

    def read_workspace_detail(self, q):
        with self.__lock:
            try:
                self._assert_open()
                return self.__provider.read_workspace_detail(q)
            except Exception:
                return _failure('UTT-WORKSPACE-DETAIL/0.1', 'CONNECTION_UNAVAILABLE')

    def workspace_catalog(self, q):
        with self.__lock:
            try:
                self._assert_open()
                return self.__provider.workspace_catalog(q)
            except Exception:
                return _failure('UTT-WORKSPACE-DETAIL/0.1', 'CONNECTION_UNAVAILABLE')

    def workspace_features(self):
        with self.__lock:
            available = not self.__closed and self.__provider is not None and not self.__refresh_required
            return {'contract_version': 'UTT-WORKSPACE-FEATURES/0.1',
                    'read_detail': available, 'writes_supported': available}

    def publication_status(self):
        with self.__lock:
            if self.__closed:
                return _failure(_STATUS_VERSION, 'CONNECTION_UNAVAILABLE')
            if self.__refresh_required or self.__scope is None:
                return {'contract_version': _STATUS_VERSION, 'status': 'OK', 'dataset_id': None,
                        'snapshot_id': None, 'local_status': 'REFRESH_REQUIRED', 'dropbox_status': 'UNKNOWN'}
            try:
                current = self._source_is_current()
            except Exception:
                current = False
            if not current:
                self.__dropbox_status = 'UNKNOWN'
            return {'contract_version': _STATUS_VERSION, 'status': 'OK',
                    'dataset_id': self.__scope['dataset_id'], 'snapshot_id': self.__scope['snapshot_id'],
                    'local_status': 'LOCAL_SAVED' if current else 'REFRESH_REQUIRED',
                    'dropbox_status': self.__dropbox_status}

    def refresh(self):
        """Explicitly capture external changes; no write or Dropbox request."""
        with self.__lock:
            if self.__closed:
                return _failure(_STATUS_VERSION, 'CONNECTION_UNAVAILABLE')
            try:
                self._refresh_locked()
                return self.publication_status()
            except WorkspaceRuntimeError as exc:
                return _failure(_STATUS_VERSION, exc.code)
            except Exception:
                return _failure(_STATUS_VERSION, 'RUNTIME_UNAVAILABLE')

    def _scope_matches(self, dataset_id, snapshot_id):
        return (type(dataset_id) is str and type(snapshot_id) is str and self.__scope is not None
                and dataset_id == self.__scope['dataset_id'] and snapshot_id == self.__scope['snapshot_id'])

    def write(self, command, *, dataset_id, snapshot_id):
        with self.__lock:
            try:
                self._assert_open()
                if not self._scope_matches(dataset_id, snapshot_id):
                    raise WorkspaceRuntimeError('VERSION_MISMATCH')
                if not self._source_is_current():
                    self.__dropbox_status = 'UNKNOWN'
                    raise WorkspaceRuntimeError('DATA_REVISION_CONFLICT')
                if type(command) is not dict or 'expected_data_revision' in command:
                    raise WorkspaceRuntimeError('INVALID_REQUEST')
                scoped = dict(command)
                scoped['expected_data_revision'] = self.__scope['source_version']
                with closing(_connect(self.__source, 'rw')) as conn:
                    result = WorkspaceWriter(conn, codec=self.__codec).execute(scoped)
                # The transaction committed; never retry it if refresh fails.
                self.__dropbox_status = 'UNPUBLISHED'
                try:
                    self._refresh_locked()
                    detail = self.__provider.read_workspace_detail({'contract_version': 'UTT-WORKSPACE-READ/0.1',
                        'operation': 'detail', 'dataset_id': self.__scope['dataset_id'],
                        'snapshot_id': self.__scope['snapshot_id'], 'record_id': result.record_id})
                    if detail.get('status') != 'OK' or detail.get('record', {}).get('version') != result.version:
                        raise WorkspaceRuntimeError('SOURCE_CHANGED')
                except Exception:
                    self._latch_refresh_required()
                    return _failure(_WRITE_VERSION, 'LOCAL_SAVED_REFRESH_REQUIRED')
                return {'contract_version': _WRITE_VERSION, 'status': 'OK', 'local_status': 'LOCAL_SAVED',
                        'dropbox_status': 'UNPUBLISHED', 'record_id': result.record_id, 'version': result.version,
                        'dataset_id': self.__scope['dataset_id'], 'snapshot_id': self.__scope['snapshot_id']}
            except WorkspaceWriteError as exc:
                if exc.code == 'COMMIT_UNKNOWN':
                    # The durable commit may have succeeded.  Do not let a
                    # stale captured view invite a repeat of that write.
                    self._latch_refresh_required()
                else:
                    try:
                        self._revalidate_after_rollback()
                    except Exception:
                        self._latch_refresh_required()
                return _failure(_WRITE_VERSION, exc.code)
            except WorkspaceRuntimeError as exc:
                return _failure(_WRITE_VERSION, exc.code)
            except Exception:
                return _failure(_WRITE_VERSION, 'WRITE_FAILED')

    def publish(self, transport, session, *, dataset_id, snapshot_id):
        """Publish only the selected current snapshot under the runtime lock."""
        with self.__lock:
            try:
                self._assert_open()
                if not self._scope_matches(dataset_id, snapshot_id):
                    raise WorkspaceRuntimeError('VERSION_MISMATCH')
                if not self._source_is_current():
                    self.__dropbox_status = 'UNKNOWN'
                    raise WorkspaceRuntimeError('DATA_REVISION_CONFLICT')
                with snapshot_workspace(self.__source, directory=self.__directory) as snap:
                    _, digest = snap.describe_snapshot()
                    if snapshot_id != 'sha256:' + digest:
                        raise WorkspaceRuntimeError('VERSION_MISMATCH')
                    result = publish_snapshot(snap, transport, session)
                    if result.status != 'PUBLISHED' or result.code not in ('PUBLISHED', 'ALREADY_ACTIVE'):
                        # A failed attempt does not prove which generation is
                        # currently active, including after a concurrent CAS.
                        self.__dropbox_status = 'UNKNOWN'
                        return _failure(_PUBLICATION_VERSION, result.code, dropbox_status=self.__dropbox_status)
                    try:
                        snap.assert_source_unchanged()
                        snap.assert_snapshot_unchanged()
                    except Exception:
                        self.__dropbox_status = 'UNKNOWN'
                        return _failure(_PUBLICATION_VERSION, 'PUBLISHED_LOCAL_CHANGED', dropbox_status='UNKNOWN')
                if not self._source_is_current():
                    self.__dropbox_status = 'UNKNOWN'
                    return _failure(_PUBLICATION_VERSION, 'PUBLISHED_LOCAL_CHANGED', dropbox_status='UNKNOWN')
                self.__dropbox_status = 'PUBLISHED'
                return {'contract_version': _PUBLICATION_VERSION, 'status': 'OK', 'code': result.code,
                        'dataset_id': dataset_id, 'snapshot_id': snapshot_id, 'dropbox_status': 'PUBLISHED'}
            except WorkspaceRuntimeError as exc:
                return _failure(_PUBLICATION_VERSION, exc.code, dropbox_status=self.__dropbox_status)
            except Exception:
                self.__dropbox_status = 'UNKNOWN'
                return _failure(_PUBLICATION_VERSION, 'PUBLICATION_FAILED', dropbox_status='UNKNOWN')

    def close(self):
        with self.__lock:
            self.__closed = True
            if self.__provider is not None:
                self.__provider.close()
            self.__provider, self.__scope = None, None
