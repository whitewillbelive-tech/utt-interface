"""Non-mutating reader for an explicit offline portable history snapshot.

No native D1.1 SQLite schema was supplied. UTT-D11-PORTABLE-HISTORY/0.1.0 is
therefore a new, deliberately narrow interchange format, not a native adapter.
Its two physical tables are exactly:

  utt_d11_portable_meta(format_version TEXT NOT NULL, dataset_id TEXT NOT NULL,
    completeness TEXT NOT NULL, first_ordinal INTEGER NOT NULL,
    last_ordinal INTEGER NOT NULL, event_count INTEGER NOT NULL)
  utt_d11_portable_events(ordinal INTEGER NOT NULL, event_id TEXT NOT NULL,
    record_id TEXT NOT NULL, decision_version INTEGER NOT NULL,
    supersedes_event_id TEXT, payload_json BLOB NOT NULL)

There is one meta row; completeness is COMPLETE/PARTIAL/UNKNOWN. Empty history
uses first/last/count=0. Ordinals establish order. Index identity/version fields
must equal the original JSON fields; non-null predecessor indices require the
same original supersedes_event_id field. An absent event_contract stays absent.
Portable v1 deliberately requires contiguous global ordinals from one and, per
record, decision versions from one with each later event superseding its prior
event. These are this portable version's constraints, not inferred native D1.1
rules. Missing predecessors/gaps yield PARTIAL; duplicate/reversed versions or
known out-of-order predecessors yield CONFLICT. COMPLETE is only a declaration.
Raw payload bytes, decisions, approval fields, and legacy hash labels are never
rewritten. Payloads are private in-memory evidence, not browser-ready output.

The caller must explicitly attest offline_immutable=True. Original bytes are
opened only with OS read-only descriptors, never with sqlite3. Exact sidecars and
WAL header mode are refused. Bounded bytes are deserialized into ':memory:';
SQLite is restricted to fixed read queries and memory temporary storage. Neither
validity nor a declared COMPLETE marker proves that an earlier tail was not
deleted, that approval was authorized, or that native provenance was verified.
"""
import copy
from dataclasses import dataclass, field
import errno
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

FORMAT = 'UTT-D11-PORTABLE-HISTORY/0.1.0'
_META = 'utt_d11_portable_meta'
_EVENTS = 'utt_d11_portable_events'
_COLUMNS = {
    _META: (('format_version', 'TEXT', 1), ('dataset_id', 'TEXT', 1),
            ('completeness', 'TEXT', 1), ('first_ordinal', 'INTEGER', 1),
            ('last_ordinal', 'INTEGER', 1), ('event_count', 'INTEGER', 1)),
    _EVENTS: (('ordinal', 'INTEGER', 1), ('event_id', 'TEXT', 1),
              ('record_id', 'TEXT', 1), ('decision_version', 'INTEGER', 1),
              ('supersedes_event_id', 'TEXT', 0), ('payload_json', 'BLOB', 1)),
}
_IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9:_-]{0,255}$')


class _ReadFailure(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__('Offline history read rejected.')


@dataclass(frozen=True)
class HistoryEvent:
    ordinal: int = field(repr=False)
    event_id: str = field(repr=False)
    record_id: str = field(repr=False)
    decision_version: int = field(repr=False)
    supersedes_event_id: str | None = field(repr=False)
    _raw: bytes = field(repr=False)
    _payload: dict = field(repr=False)

    @property
    def raw_payload_json(self):
        return self._raw

    @property
    def payload(self):
        return copy.deepcopy(self._payload)


@dataclass(frozen=True)
class HistoryResult:
    status: str
    code: str
    diagnostics: tuple = ()
    completeness: str | None = None
    dataset_id: str | None = field(default=None, repr=False)
    events: tuple = field(default=(), repr=False)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _reject_number(_):
    raise ValueError()


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError()
    return result


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class D11Reader:
    def __init__(self, path, *, offline_immutable=False, max_file_bytes=33554432,
                 max_events=50000, max_payload_bytes=1048576, max_seconds=5):
        # Constructor performs no filesystem or SQLite access.
        self._path = path
        self._offline = offline_immutable
        self._max_file = max_file_bytes
        self._max_events = max_events
        self._max_payload = max_payload_bytes
        self._max_seconds = max_seconds

    def _config(self):
        if self._offline is not True:
            raise _ReadFailure('NOT_SUPPORTED', 'OFFLINE_IMMUTABLE_REQUIRED')
        if (not isinstance(self._path, (str, os.PathLike)) or not os.fspath(self._path)
                or not isinstance(os.fspath(self._path), str)
                or type(self._max_file) is not int or not 1024 <= self._max_file <= 67108864
                or type(self._max_events) is not int or not 1 <= self._max_events <= 100000
                or type(self._max_payload) is not int or not 64 <= self._max_payload <= self._max_file
                or type(self._max_seconds) not in (int, float) or not 0.05 <= self._max_seconds <= 30):
            raise _ReadFailure('INVALID', 'INVALID_CONFIGURATION')
        if (not hasattr(sqlite3.Connection, 'deserialize') or os.open not in os.supports_dir_fd
                or not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY')):
            raise _ReadFailure('NOT_SUPPORTED', 'UNSUPPORTED_RUNTIME')

    @staticmethod
    def _sidecars(parent, name):
        for suffix in ('-wal', '-shm', '-journal'):
            try:
                os.stat(name + suffix, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise _ReadFailure('NOT_SUPPORTED', 'JOURNAL_SIDECAR_PRESENT')

    def _open(self):
        parent = descriptor = None
        try:
            path = Path(os.path.abspath(os.fspath(self._path)))
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
            parent = os.open(path.anchor, flags | os.O_DIRECTORY)
            for part in path.parts[1:-1]:
                child = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
                os.close(parent)
                parent = child
            name = path.name
            descriptor = os.open(name, flags | getattr(os, 'O_NONBLOCK', 0), dir_fd=parent)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise _ReadFailure('INVALID', 'NONREGULAR_INPUT')
            if info.st_size > self._max_file:
                raise _ReadFailure('INVALID', 'RESOURCE_LIMIT')
            self._sidecars(parent, name)
            raw = self._bytes(descriptor)
            if (len(raw) < 100 or raw[:16] != b'SQLite format 3\0'
                    or raw[18] not in (1, 2) or raw[19] not in (1, 2)):
                raise _ReadFailure('INVALID', 'INVALID_SQLITE')
            if raw[18] == 2 or raw[19] == 2:
                raise _ReadFailure('NOT_SUPPORTED', 'WAL_MODE_UNSUPPORTED')
            self._unchanged(parent, name, descriptor, info, raw)
            result = parent, name, descriptor, info, raw
            parent = descriptor = None
            return result
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                os.close(parent)

    def _bytes(self, descriptor):
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks, total = [], 0
        while total <= self._max_file:
            block = os.read(descriptor, min(1048576, self._max_file + 1 - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
        if total > self._max_file:
            raise _ReadFailure('INVALID', 'RESOURCE_LIMIT')
        return b''.join(chunks)

    def _unchanged(self, parent, name, descriptor, info, raw):
        try:
            self._sidecars(parent, name)
            before_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
            before_fd = os.fstat(descriptor)
            current_bytes = self._bytes(descriptor)
            # Check again after the complete verification read. A replace or
            # mutation during that read must not escape as a successful result.
            after_fd = os.fstat(descriptor)
            after_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
            self._sidecars(parent, name)
        except (FileNotFoundError, _ReadFailure):
            raise _ReadFailure('CONFLICT', 'INPUT_CHANGED') from None
        if (not stat.S_ISREG(after_path.st_mode)
                or any(_identity(info) != _identity(current)
                       for current in (before_path, before_fd, after_fd, after_path))
                or current_bytes != raw):
            raise _ReadFailure('CONFLICT', 'INPUT_CHANGED')

    def _database(self, raw):
        connection = sqlite3.connect(':memory:')
        try:
            connection.enable_load_extension(False)
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA query_only=ON')
            for category, value in ((sqlite3.SQLITE_LIMIT_LENGTH, self._max_file),
                                    (sqlite3.SQLITE_LIMIT_SQL_LENGTH, 8192),
                                    (sqlite3.SQLITE_LIMIT_COLUMN, 32),
                                    (sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 32),
                                    (sqlite3.SQLITE_LIMIT_ATTACHED, 0)):
                connection.setlimit(category, value)
            connection.deserialize(raw)
            deadline = time.monotonic() + self._max_seconds
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)

            def authorize(action, arg1, arg2, _database, _trigger):
                if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ):
                    return sqlite3.SQLITE_OK
                if action == sqlite3.SQLITE_PRAGMA and arg1 == 'table_xinfo' and arg2 in _COLUMNS:
                    return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY

            connection.set_authorizer(authorize)
            return connection
        except Exception:
            connection.close()
            raise

    @staticmethod
    def _schema(connection):
        objects = connection.execute('SELECT name,type,sql FROM sqlite_schema').fetchall()
        if (len(objects) != 2 or {row[0] for row in objects} != set(_COLUMNS)
                or any(row[1] != 'table' or not isinstance(row[2], str)
                       or not re.match(r'^CREATE\s+TABLE\s', row[2], re.I)
                       for row in objects)):
            raise _ReadFailure('NOT_SUPPORTED', 'UNSUPPORTED_SCHEMA')
        for table, expected in _COLUMNS.items():
            rows = connection.execute('PRAGMA table_xinfo(' + table + ')').fetchall()
            actual = tuple((row[1], row[2].upper(), row[3]) for row in rows)
            if actual != expected or any(row[5] or row[6] for row in rows):
                # Primary keys/index constraints and generated/hidden columns
                # are outside this exact portable version, not native guesses.
                raise _ReadFailure('NOT_SUPPORTED', 'UNSUPPORTED_SCHEMA')

    def _decode(self, row):
        ordinal, event_id, record_id, version, supersedes, raw = row
        if (type(ordinal) is not int or ordinal < 1 or type(version) is not int or version < 1
                or not isinstance(event_id, str) or not _IDENTIFIER.fullmatch(event_id)
                or not isinstance(record_id, str) or not _IDENTIFIER.fullmatch(record_id)
                or supersedes is not None and
                (not isinstance(supersedes, str) or not _IDENTIFIER.fullmatch(supersedes))
                or not isinstance(raw, bytes) or len(raw) > self._max_payload):
            raise _ReadFailure('INVALID', 'INVALID_EVENT')
        try:
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs,
                                 parse_constant=_reject_number, parse_float=_finite_float)
            if (not isinstance(payload, dict) or payload.get('event_id') != event_id
                    or payload.get('record_id') != record_id
                    or type(payload.get('decision_version')) is not int
                    or payload['decision_version'] != version
                    or ('supersedes_event_id' in payload and payload['supersedes_event_id'] != supersedes)
                    or (supersedes is not None and payload.get('supersedes_event_id') != supersedes)):
                raise ValueError()
        except Exception:
            raise _ReadFailure('INVALID', 'INVALID_EVENT') from None
        return HistoryEvent(ordinal, event_id, record_id, version, supersedes, raw, payload)

    def _history(self, connection, record_id, raw):
        self._schema(connection)
        meta = connection.execute('SELECT format_version,dataset_id,completeness,first_ordinal,last_ordinal,event_count FROM utt_d11_portable_meta LIMIT 2').fetchall()
        if len(meta) != 1:
            raise _ReadFailure('INVALID', 'INVALID_METADATA')
        version, dataset, completeness, first, last, count = meta[0]
        if version != FORMAT:
            raise _ReadFailure('NOT_SUPPORTED', 'UNSUPPORTED_FORMAT')
        if (not isinstance(dataset, str) or not _IDENTIFIER.fullmatch(dataset)
                or completeness not in ('COMPLETE', 'PARTIAL', 'UNKNOWN')
                or any(type(value) is not int or value < 0 for value in (first, last, count))
                or (count == 0 and (first != 0 or last != 0))
                or (count > 0 and not 1 <= first <= last)):
            raise _ReadFailure('INVALID', 'INVALID_METADATA')
        cursor = connection.execute('SELECT ordinal,event_id,record_id,decision_version,supersedes_event_id,payload_json FROM utt_d11_portable_events')
        events = []
        for row in cursor:
            if len(events) >= self._max_events:
                raise _ReadFailure('INVALID', 'RESOURCE_LIMIT')
            events.append(self._decode(row))
        events.sort(key=lambda event: event.ordinal)
        diagnostics, conflict = set(), False
        ids, ordinals, previous = {}, set(), {}
        all_event_ids = {event.event_id for event in events}
        for event in events:
            if event.event_id in ids or event.ordinal in ordinals:
                diagnostics.add('DUPLICATE_IDENTITY_OR_ORDER')
                conflict = True
            ids[event.event_id] = event
            ordinals.add(event.ordinal)
            prior = previous.get(event.record_id)
            if prior is None:
                if event.decision_version != 1 or event.supersedes_event_id is not None:
                    diagnostics.add('MISSING_PREDECESSOR')
                    if event.supersedes_event_id in all_event_ids:
                        diagnostics.add('SUPERSEDES_ORDER_CONFLICT')
                        conflict = True
            elif event.decision_version <= prior.decision_version:
                diagnostics.add('DECISION_VERSION_CONFLICT')
                conflict = True
            elif event.decision_version != prior.decision_version + 1:
                diagnostics.add('DECISION_VERSION_GAP')
            if prior is not None and event.supersedes_event_id != prior.event_id:
                if event.supersedes_event_id in all_event_ids:
                    diagnostics.add('SUPERSEDES_ORDER_CONFLICT')
                    conflict = True
                else:
                    diagnostics.add('MISSING_PREDECESSOR')
            previous[event.record_id] = event
        order = [event.ordinal for event in events]
        if order and (order[0] != 1 or any(b != a + 1 for a, b in zip(order, order[1:]))):
            diagnostics.add('ORDINAL_GAP')
        if count != len(events) or (events and (first != order[0] or last != order[-1])):
            diagnostics.add('DECLARED_EXTENT_MISMATCH')
        status = ('CONFLICT' if conflict else 'PARTIAL' if diagnostics or completeness == 'PARTIAL'
                  else 'UNKNOWN' if completeness == 'UNKNOWN' else 'AVAILABLE')
        filtered = tuple(event for event in events if record_id is None or event.record_id == record_id)
        return HistoryResult(status, 'ORDER_CONFLICT' if conflict else 'OK', tuple(sorted(diagnostics)),
                             completeness, dataset, filtered)

    def read(self, record_id=None):
        parent = descriptor = connection = None
        try:
            self._config()
            if record_id is not None and (not isinstance(record_id, str) or not _IDENTIFIER.fullmatch(record_id)):
                raise _ReadFailure('INVALID', 'INVALID_REQUEST')
            parent, name, descriptor, info, raw = self._open()
            connection = self._database(raw)
            result = self._history(connection, record_id, raw)
            self._unchanged(parent, name, descriptor, info, raw)
            return result
        except _ReadFailure as exc:
            return HistoryResult(exc.status, exc.code)
        except FileNotFoundError:
            return HistoryResult('MISSING', 'FILE_MISSING')
        except PermissionError:
            return HistoryResult('INACCESSIBLE', 'ACCESS_DENIED')
        except OSError as exc:
            code = 'UNSAFE_INPUT' if exc.errno in (errno.ELOOP, errno.ENOTDIR) else 'READ_UNAVAILABLE'
            return HistoryResult('INACCESSIBLE', code)
        except sqlite3.Error:
            return HistoryResult('INVALID', 'INVALID_SQLITE')
        except Exception:
            return HistoryResult('INVALID', 'READ_REJECTED')
        finally:
            if connection is not None:
                connection.close()
            if descriptor is not None:
                os.close(descriptor)
            if parent is not None:
                os.close(parent)
