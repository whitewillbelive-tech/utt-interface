"""Local workspace snapshot and structural checks for the Windows publisher.

No DB is opened until the user selects a path in the approved local runtime.
The snapshot is a temporary local SQLite backup, including committed WAL data;
it is never sent to Codex or GitHub by this module. Structural success is not
proof of source/remote equivalence, historical preservation or authorization.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from typing import Iterator


_REFERENCE = Path(__file__).resolve().parents[1] / 'docs' / 'publisher-reference' / 'WORKSPACE_SCHEMA.sql'
_REQUIRED_META = frozenset({'format_version', 'dataset_id', 'base_dataset',
                            'active_dropbox_baseline', 'operational_adoption',
                            'read_contract', 'write_contract', 'data_revision'})
_HISTORY_TABLES = frozenset({'utt_object', 'utt_revision', 'utt_event', 'audit_log',
                             'message_version', 'conclusion', 'reorg_record', 'read_record',
                             'reorg_project', 'reorg_affiliation', 'reorg_evidence',
                             'topic', 'conversation', 'message'})
_SHADOWS = frozenset({'work_fts_data', 'work_fts_idx', 'work_fts_content',
                      'work_fts_docsize', 'work_fts_config'})
_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SQL_TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\n]*|/\*.*?\*/|[a-zA-Z_][a-zA-Z_0-9]*|[0-9]+|[^\s]", re.S)


class WorkspaceError(Exception):
    """Fixed, non-sensitive status; never includes file paths or DB content."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sql_shape(sql: str) -> tuple[str, ...]:
    tokens = _SQL_TOKEN.findall(sql)
    return tuple(t if t[0] in "'\"`[" else t.lower() for t in tokens
                 if not t.startswith('--') and not t.startswith('/*') and t != ';')


def _source_identity(path: Path) -> tuple[int, int, int, int, int]:
    try:
        st = path.stat()
        if not path.is_file():
            raise WorkspaceError('INVALID_SOURCE')
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    except FileNotFoundError:
        raise WorkspaceError('SOURCE_MISSING') from None
    except PermissionError:
        raise WorkspaceError('ACCESS_DENIED') from None
    except OSError:
        raise WorkspaceError('SOURCE_UNAVAILABLE') from None


def _file_digest(path: Path) -> tuple[int, bytes]:
    """Internal byte identity; no digest or path is returned to user output."""
    try:
        count, digest = 0, hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                digest.update(chunk)
        return count, digest.digest()
    except (OSError, ValueError):
        raise WorkspaceError('SNAPSHOT_UNAVAILABLE') from None


def _no_sidecars(path: Path) -> bool:
    return not any(Path(str(path) + ext).exists() for ext in ('-wal', '-shm', '-journal'))


@contextmanager
def _temporary_directory(directory: Path | None) -> Iterator[Path]:
    try:
        temporary = tempfile.TemporaryDirectory(prefix='utt-workspace-snapshot-', dir=directory)
    except OSError:
        raise WorkspaceError('LOCAL_TEMP_UNAVAILABLE') from None
    try:
        yield Path(temporary.name)
    finally:
        try:
            temporary.cleanup()
        except OSError:
            raise WorkspaceError('SNAPSHOT_CLEANUP_FAILED') from None


def _readonly(path: Path, timeout: float) -> sqlite3.Connection:
    conn = None
    try:
        conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=timeout)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        return conn
    except (sqlite3.Error, OSError, ValueError):
        if conn is not None:
            conn.close()
        raise WorkspaceError('SOURCE_UNAVAILABLE') from None


def _schema_catalog(conn: sqlite3.Connection) -> dict[tuple[str, str], tuple[str, ...]]:
    rows = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name GLOB 'work_*' AND sql IS NOT NULL")
    return {(kind, name): _sql_shape(sql) for kind, name, sql in rows if name not in _SHADOWS}


def _validate_workspace(conn: sqlite3.Connection) -> None:
    """Fail closed on malformed schema, integrity, FKs or work body digests."""
    ref = None
    try:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise WorkspaceError('INTEGRITY_FAILED')
        if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise WorkspaceError('FOREIGN_KEY_FAILED')
        ref = sqlite3.connect(':memory:')
        ref.executescript(_REFERENCE.read_text(encoding='utf-8'))
        expected, actual = _schema_catalog(ref), _schema_catalog(conn)
        if not expected or set(actual) != set(expected) or any(actual[key] != value for key, value in expected.items()):
            raise WorkspaceError('SCHEMA_MISMATCH')
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _HISTORY_TABLES.issubset(tables):
            raise WorkspaceError('HISTORY_TABLES_MISSING')
        wanted = tuple(sorted(_REQUIRED_META))
        placeholders = ','.join('?' for _ in wanted)
        meta = dict(conn.execute('SELECT key,value FROM work_meta WHERE key IN (' + placeholders + ')', wanted))
        if not _REQUIRED_META.issubset(meta) or meta['format_version'] != 'UTT-INTEGRATED-WORKSPACE/0.1':
            raise WorkspaceError('WORKSPACE_FORMAT_MISMATCH')
        for table in ('work_records', 'work_revisions'):
            rows = conn.execute('SELECT body,body_sha256 FROM ' + table)
            for body, expected_hash in rows:
                if (not isinstance(body, str) or not isinstance(expected_hash, str)
                        or not _HEX.fullmatch(expected_hash)
                        or not hmac.compare_digest(hashlib.sha256(body.encode('utf-8')).hexdigest(), expected_hash)):
                    raise WorkspaceError('WORK_BODY_HASH_MISMATCH')
    except WorkspaceError:
        raise
    except (sqlite3.Error, UnicodeError, OSError, ValueError, TypeError):
        raise WorkspaceError('INVALID_WORKSPACE') from None
    finally:
        if ref is not None:
            ref.close()


class WorkspaceSnapshot:
    """In-process handle; pathname and all content stay on the user's computer."""

    def __init__(self, path: Path, source: Path, source_conn: sqlite3.Connection,
                 source_identity: tuple[int, int, int, int, int], data_version: int,
                 snapshot_digest: tuple[int, bytes]):
        self.path = path
        self.__source = source
        self.__source_conn = source_conn
        self.__source_identity = source_identity
        self.__data_version = data_version
        self.__snapshot_digest = snapshot_digest
        self.__closed = False

    def __repr__(self) -> str:
        return '<WorkspaceSnapshot redacted>'

    def assert_source_unchanged(self) -> None:
        """Detect observed local changes; does not establish immutable/ABA safety."""
        if self.__closed:
            raise WorkspaceError('SNAPSHOT_CLOSED')
        try:
            if (_source_identity(self.__source) != self.__source_identity
                    or self.__source_conn.execute('PRAGMA data_version').fetchone()[0] != self.__data_version):
                raise WorkspaceError('SOURCE_CHANGED')
        except WorkspaceError:
            raise
        except (sqlite3.Error, OSError, TypeError, IndexError):
            raise WorkspaceError('SOURCE_UNAVAILABLE') from None

    def assert_snapshot_unchanged(self) -> None:
        """Detect changes to backup bytes; no comparison result is logged."""
        if self.__closed:
            raise WorkspaceError('SNAPSHOT_CLOSED')
        if not _no_sidecars(self.path) or _file_digest(self.path) != self.__snapshot_digest:
            raise WorkspaceError('SNAPSHOT_CHANGED')

    def describe_snapshot(self) -> tuple[int, str]:
        """Return size and ordinary SHA-256 in-process, after rechecking bytes.

        This return value is for the protected local plan builder only. Neither
        digest nor path may be emitted to shared diagnostics or development.
        """
        self.assert_snapshot_unchanged()
        size, digest = self.__snapshot_digest
        return size, digest.hex()

    def open_readonly(self) -> sqlite3.Connection:
        """Open a pinned read transaction on the checked temporary snapshot.

        Caller owns the connection and must close it. Recheck snapshot bytes
        after comparison and before activation. This is not an ABA guarantee.
        """
        self.assert_snapshot_unchanged()
        try:
            path = self.path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise WorkspaceError('SNAPSHOT_UNAVAILABLE') from None
        conn = _readonly(path, 30.0)
        try:
            conn.execute('BEGIN')
            conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
            return conn
        except sqlite3.Error:
            conn.close()
            raise WorkspaceError('SNAPSHOT_UNAVAILABLE') from None

    def _close(self) -> None:
        self.__closed = True
        self.__source_conn.close()


@contextmanager
def snapshot_workspace(source: str | os.PathLike[str], *, timeout: float = 30.0,
                       directory: str | os.PathLike[str] | None = None) -> Iterator[WorkspaceSnapshot]:
    """Create and validate an isolated local backup, removed on context exit.

    The caller must supply a directory protected by local OS policies; this
    function does not establish process isolation or ACL correctness itself.
    The same read-only source connection remains open for data_version checks.
    Callers must separately quiesce writers and recheck before activation; file
    identity and data_version do not rule out unobserved replacement or ABA.
    """
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise WorkspaceError('INVALID_TIMEOUT')
    if directory is None:
        raise WorkspaceError('PROTECTED_TEMP_REQUIRED')
    try:
        path = Path(source).resolve(strict=True)
    except FileNotFoundError:
        raise WorkspaceError('SOURCE_MISSING') from None
    except PermissionError:
        raise WorkspaceError('ACCESS_DENIED') from None
    except (OSError, RuntimeError, TypeError, ValueError):
        raise WorkspaceError('INVALID_SOURCE') from None
    identity = _source_identity(path)
    try:
        local_dir = Path(directory).resolve(strict=True)
        if not local_dir.is_dir():
            raise WorkspaceError('INVALID_TEMP_DIRECTORY')
    except (OSError, RuntimeError, TypeError, ValueError):
        raise WorkspaceError('INVALID_TEMP_DIRECTORY') from None
    src = _readonly(path, float(timeout))
    handle = None
    try:
        with _temporary_directory(local_dir) as tmp_path:
            target = tmp_path / 'workspace.sqlite'
            dst = None
            try:
                before = src.execute('PRAGMA data_version').fetchone()[0]
                dst = sqlite3.connect(target, timeout=float(timeout))
                deadline = time.monotonic() + float(timeout)
                def progress(_status, _remaining, _total):
                    if time.monotonic() >= deadline:
                        raise WorkspaceError('SNAPSHOT_TIMEOUT')
                src.backup(dst, pages=256, progress=progress, sleep=0.05)
                # Backup from a WAL source can retain a WAL header. Convert
                # the temporary copy to a self-contained DELETE-mode DB.
                mode = dst.execute('PRAGMA journal_mode=DELETE').fetchone()
                if mode != ('delete',):
                    raise WorkspaceError('SNAPSHOT_SIDECAR_UNSUPPORTED')
                dst.close()
                dst = None
                if not _no_sidecars(target):
                    raise WorkspaceError('SNAPSHOT_SIDECAR_UNSUPPORTED')
                after = src.execute('PRAGMA data_version').fetchone()[0]
                if before != after or _source_identity(path) != identity:
                    raise WorkspaceError('SOURCE_CHANGED')
                check = _readonly(target.resolve(strict=True), float(timeout))
                try:
                    _validate_workspace(check)
                finally:
                    check.close()
                if not _no_sidecars(target):
                    raise WorkspaceError('SNAPSHOT_SIDECAR_UNSUPPORTED')
                digest = _file_digest(target)
                # A writer might commit while the copied DB is being checked.
                if src.execute('PRAGMA data_version').fetchone()[0] != after or _source_identity(path) != identity:
                    raise WorkspaceError('SOURCE_CHANGED')
                handle = WorkspaceSnapshot(target, path, src, identity, after, digest)
            except WorkspaceError:
                raise
            except (sqlite3.Error, OSError, ValueError, TypeError, IndexError):
                raise WorkspaceError('SNAPSHOT_FAILED') from None
            finally:
                if dst is not None:
                    dst.close()
            yield handle
    finally:
        if handle is not None:
            handle._close()
        else:
            src.close()
