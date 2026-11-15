"""Bounded, non-mutating parent-table preservation checks for a publisher.

The caller must independently authenticate and bind the parent manifest to the
observed active reference/revision, supply a verified manifest file catalog,
and pin ``conn`` to an immutable local SQLite snapshot.  This module does not
authenticate Dropbox, perform network or filesystem access, infer approvals,
merge data, or activate a generation.  All returned errors are fixed codes and
contain no table names, paths, source values, identifiers, or hashes.

The management verification established native hexadecimal floating-point
strings for REAL cells. Compare finite values by their IEEE-754 binary64 bytes,
keeping REAL distinct from INTEGER. The native BLOB converter is unconfirmed and
remains unsupported, except that the two explicitly documented FTS *physical
shadow* tables are treated as opaque only when logical item_fts is also checked.
A pass means that the checked historical row occurrences are present; it is not
proof that local additions, decisions, or concurrent changes are safe.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import gzip
import hashlib
import hmac
import json
import math
import re
import sqlite3
import struct
from typing import Any, BinaryIO, Callable

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_INT = re.compile(r'(?:0|-[1-9][0-9]*|[1-9][0-9]*)\Z')
_REAL_HEX = re.compile(r'[+-]?0[xX](?:[0-9a-fA-F]+(?:\.[0-9a-fA-F]*)?|\.[0-9a-fA-F]+)[pP][+-]?[0-9]+\Z')
_PATH = re.compile(r'snapshots/[A-Za-z0-9_./-]+\Z')
_MAX_CATALOG = 8 * 1024 * 1024
_MAX_SHARD = 512 * 1024 * 1024
_MAX_RAW = 1024 * 1024 * 1024
_MAX_LINE = 32 * 1024 * 1024
_MAX_ROWS = 2_000_000
_FTS_SHADOWS = frozenset({'item_fts_data', 'item_fts_idx'})


@dataclass(frozen=True)
class ParentComparison:
    status: str
    code: str


class _Problem(Exception):
    def __init__(self, code: str):
        self.code = code


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _Problem('INVALID_JSON')
        result[key] = value
    return result


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(_Problem('INVALID_JSON')))
    except _Problem:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise _Problem('INVALID_JSON') from None


def _number(value: Any, maximum: int) -> bool:
    return type(value) is int and 0 <= value <= maximum


def _digest(value: Any) -> bool:
    return type(value) is str and _HEX.fullmatch(value) is not None


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _hash_row(values: list[tuple[str, Any]]) -> bytes:
    digest = hashlib.sha256()
    for tag, value in values:
        if tag == 'null' and value is None:
            kind, data = b'N', b''
        elif tag == 'integer' and type(value) is int and -(2**63) <= value < 2**63:
            kind, data = b'I', str(value).encode('ascii')
        elif tag == 'text' and type(value) is str:
            kind, data = b'T', value.encode('utf-8', 'strict')
        elif tag == 'real' and type(value) is float:
            if not math.isfinite(value):
                raise _Problem('UNSUPPORTED_CELL_ENCODING')
            kind, data = b'R', struct.pack('>d', value)
        elif tag == 'blob':
            raise _Problem('UNSUPPORTED_CELL_ENCODING')
        else:
            raise _Problem('INVALID_TYPED_CELL')
        digest.update(kind)
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.digest()


def _parent_row(row: Any, expected: int) -> bytes:
    if type(row) is not list or len(row) != expected:
        raise _Problem('INVALID_TYPED_ROW')
    values: list[tuple[str, Any]] = []
    for cell in row:
        if type(cell) is not list or len(cell) != 2 or type(cell[0]) is not str:
            raise _Problem('INVALID_TYPED_CELL')
        tag, value = cell
        if tag == 'integer':
            if type(value) is not str or _INT.fullmatch(value) is None or len(value) > 20:
                raise _Problem('INVALID_TYPED_CELL')
            value = int(value)
        elif tag == 'real':
            # A bounded hexadecimal float is required; fromhex alone also
            # accepts infinities, NaNs and strings without a binary exponent.
            if type(value) is not str or len(value) > 128 or _REAL_HEX.fullmatch(value) is None:
                raise _Problem('INVALID_TYPED_CELL')
            try:
                value = float.fromhex(value)
            except (ValueError, OverflowError):
                raise _Problem('INVALID_TYPED_CELL') from None
            if not math.isfinite(value):
                raise _Problem('UNSUPPORTED_CELL_ENCODING')
        values.append((tag, value))
    return _hash_row(values)


def _shadow_row_shape(row: Any, expected: int) -> None:
    """Validate the envelope without guessing physical FTS blob encodings."""
    if type(row) is not list or len(row) != expected:
        raise _Problem('INVALID_TYPED_ROW')
    for cell in row:
        if (type(cell) is not list or len(cell) != 2 or type(cell[0]) is not str
                or cell[0] not in ('null', 'integer', 'text', 'real', 'blob')):
            raise _Problem('INVALID_TYPED_CELL')


def _local_row(row: tuple[Any, ...]) -> bytes:
    values = []
    for value in row:
        if value is None:
            values.append(('null', None))
        elif type(value) is int:
            values.append(('integer', value))
        elif type(value) is str:
            values.append(('text', value))
        elif type(value) is float:
            values.append(('real', value))
        elif type(value) is bytes:
            raise _Problem('UNSUPPORTED_CELL_ENCODING')
        else:
            raise _Problem('INVALID_LOCAL_CELL')
    return _hash_row(values)


class _CompressedReader:
    """Hash and cap compressed bytes consumed by gzip without buffering a file."""
    def __init__(self, source: BinaryIO, expected: int):
        self.source, self.expected, self.count = source, expected, 0
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        if type(size) is not int or size < 0:
            raise _Problem('INVALID_SHARD_STREAM')
        data = self.source.read(size)
        if type(data) is not bytes or len(data) > size:
            raise _Problem('INVALID_SHARD_STREAM')
        self.count += len(data)
        if self.count > self.expected:
            raise _Problem('SHARD_SIZE_MISMATCH')
        self.digest.update(data)
        return data


def _manifest_file(catalog: Any, path: str, size: int, sha: str) -> None:
    if type(catalog) is not dict or type(path) is not str or type(size) is not int or not _digest(sha):
        raise _Problem('INVALID_MANIFEST')
    value = catalog.get(path)
    if (type(value) is not dict or value.get('bytes') != size
            or type(value.get('bytes')) is not int or value.get('sha256') != sha):
        raise _Problem('MANIFEST_MISMATCH')


def compare_parent_tables(conn: sqlite3.Connection, catalog_bytes: bytes,
                          manifest_files: dict[str, dict[str, Any]],
                          open_shard: Callable[[str], BinaryIO]) -> ParentComparison:
    """Compare parent table occurrences against a pinned query-only snapshot.

    ``manifest_files`` must come from an already verified current-parent
    manifest; its own authenticity, the active revision, and preservation of
    non-table assets are outside this helper. ``open_shard`` must only open the
    exact manifest-bound relative path on the approved runtime. It may stream
    from Dropbox locally; this module never calls a network or opens a file.

    ``PARENT_ROWS_PRESENT`` is a structural, occurrence-preserving subset
    check only. ``PARENT_LOGICAL_ROWS_PRESENT`` additionally signals that the
    two documented physical FTS shadow tables were not compared by row. In
    that case the caller MUST also preserve the original manifest payloads
    byte-identically; independently checking the logical FTS table does not
    authorize removing historical payloads. Neither status establishes the
    meaning or authorization of local additions, decision history, semantic
    merge policy, or snapshot freshness.
    """
    try:
        if (type(catalog_bytes) is not bytes or len(catalog_bytes) > _MAX_CATALOG
                or not callable(open_shard) or not isinstance(conn, sqlite3.Connection)):
            raise _Problem('INVALID_INPUT')
        if not conn.in_transaction or conn.text_factory is not str:
            raise _Problem('SNAPSHOT_REQUIRED')
        cursor = conn.cursor()
        cursor.row_factory = None
        if cursor.execute('PRAGMA query_only').fetchone() != (1,):
            raise _Problem('SNAPSHOT_REQUIRED')
        catalog_sha = hashlib.sha256(catalog_bytes).hexdigest()
        _manifest_file(manifest_files, 'snapshots/tables.json', len(catalog_bytes), catalog_sha)
        descriptors = _json(catalog_bytes)
        if type(descriptors) is not list or len(descriptors) > 10000:
            raise _Problem('INVALID_CATALOG')
        names = set()
        for table in descriptors:
            if type(table) is not dict or type(table.get('name')) is not str or not table['name']:
                raise _Problem('INVALID_CATALOG')
            folded = table['name'].casefold()
            if folded in names:
                raise _Problem('DUPLICATE_TABLE')
            names.add(folded)
        if not descriptors:
            raise _Problem('INVALID_CATALOG')
        has_fts_shadows = bool(_FTS_SHADOWS.intersection(t['name'] for t in descriptors))
        table_kinds = {(r[0], r[1]): r[2] for r in cursor.execute('PRAGMA main.table_list')}
        if has_fts_shadows:
            if 'item_fts' not in {t['name'] for t in descriptors}:
                raise _Problem('FTS_LOGICAL_MISSING')
            if table_kinds.get(('main', 'item_fts')) != 'virtual':
                raise _Problem('FTS_LOGICAL_UNSUPPORTED')
            definition = cursor.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name='item_fts' COLLATE BINARY").fetchone()
            if not definition or not re.search(r'\bUSING\s+fts5\s*\(', definition[0], re.IGNORECASE):
                raise _Problem('FTS_LOGICAL_UNSUPPORTED')
            for shadow in _FTS_SHADOWS.intersection(t['name'] for t in descriptors):
                if table_kinds.get(('main', shadow)) != 'shadow':
                    raise _Problem('FTS_SHADOW_UNSUPPORTED')
        total_rows = 0
        seen_paths: set[str] = set()
        logical_fts_checked = False
        for table in descriptors:
            name, columns, rowid, expected_rows, shards = (table.get(k) for k in ('name', 'columns', 'rowid', 'count', 'shards'))
            if (type(columns) is not list or not columns or any(type(c) is not str or not c for c in columns)
                    or len({c.casefold() for c in columns}) != len(columns)
                    or type(rowid) is not bool or not _number(expected_rows, _MAX_ROWS)
                    or type(shards) is not list or len(shards) > 10000):
                raise _Problem('UNSUPPORTED_TABLE_DESCRIPTOR')
            sql_row = cursor.execute('SELECT sql FROM main.sqlite_schema WHERE type=? AND name=? COLLATE BINARY',
                                     ('table', name)).fetchone()
            if not sql_row:
                raise _Problem('PARENT_TABLE_MISSING')
            if type(table.get('sql')) is not str or sql_row[0] != table['sql']:
                raise _Problem('TABLE_SCHEMA_MISMATCH')
            local_columns = [row[1] for row in cursor.execute('PRAGMA main.table_info(' + _quoted(name) + ')')]
            if local_columns != columns:
                raise _Problem('TABLE_SCHEMA_MISMATCH')
            rowid_expr = ''
            if rowid:
                shadows = {c.casefold() for c in columns}
                alias = next((s for s in ('rowid', '_rowid_', 'oid') if s not in shadows), None)
                if alias is None:
                    raise _Problem('UNSUPPORTED_ROWID')
                rowid_expr = _quoted(alias) + ','
            selected = rowid_expr + ','.join(_quoted(c) for c in columns)
            local: Counter[bytes] = Counter()
            shadow = name in _FTS_SHADOWS and has_fts_shadows
            if not shadow:
                rows = cursor.execute('SELECT ' + selected + ' FROM main.' + _quoted(name))
                for row in rows:
                    total_rows += 1
                    if total_rows > _MAX_ROWS:
                        raise _Problem('LIMIT_EXCEEDED')
                    local[_local_row(row)] += 1
            checked = 0
            for shard in shards:
                if type(shard) is not dict:
                    raise _Problem('INVALID_SHARD')
                path, size, raw_size, row_count, sha, raw_sha = (shard.get(k) for k in ('path', 'bytes', 'raw_bytes', 'rows', 'sha256', 'raw_sha256'))
                if (type(path) is not str or _PATH.fullmatch(path) is None
                        or any(part in ('', '.', '..') for part in path.split('/'))
                        or 'scrivener' in path.casefold()
                        or path.casefold() in seen_paths or not _number(size, _MAX_SHARD)
                        or not _number(raw_size, _MAX_RAW) or not _number(row_count, _MAX_ROWS)
                        or not _digest(sha) or not _digest(raw_sha)):
                    raise _Problem('INVALID_SHARD')
                seen_paths.add(path.casefold())
                _manifest_file(manifest_files, path, size, sha)
                source = open_shard(path)
                tracker = _CompressedReader(source, size)
                raw_hash, raw_count, actual_rows = hashlib.sha256(), 0, 0
                try:
                    with gzip.GzipFile(fileobj=tracker, mode='rb') as reader:
                        while True:
                            line = reader.readline(_MAX_LINE + 1)
                            if not line:
                                break
                            raw_count += len(line)
                            if len(line) > _MAX_LINE or raw_count > raw_size or not line.endswith(b'\n'):
                                raise _Problem('INVALID_SHARD')
                            raw_hash.update(line)
                            actual_rows += 1
                            if actual_rows > row_count or checked + actual_rows > expected_rows:
                                raise _Problem('TABLE_COUNT_MISMATCH')
                            decoded = _json(line)
                            if shadow:
                                _shadow_row_shape(decoded, len(columns) + int(rowid))
                            else:
                                key = _parent_row(decoded, len(columns) + int(rowid))
                                if local[key] <= 0:
                                    raise _Problem('PARENT_ROW_CONFLICT')
                                local[key] -= 1
                finally:
                    source.close()
                if (tracker.count != size or not hmac.compare_digest(tracker.digest.hexdigest(), sha)
                        or raw_count != raw_size or not hmac.compare_digest(raw_hash.hexdigest(), raw_sha)
                        or actual_rows != row_count):
                    raise _Problem('SHARD_INTEGRITY_MISMATCH')
                checked += actual_rows
            if checked != expected_rows:
                raise _Problem('TABLE_COUNT_MISMATCH')
            if name == 'item_fts':
                logical_fts_checked = True
        if has_fts_shadows and not logical_fts_checked:
            raise _Problem('FTS_LOGICAL_MISSING')
        if not conn.in_transaction:
            raise _Problem('SNAPSHOT_REQUIRED')
        if has_fts_shadows:
            return ParentComparison('PARENT_LOGICAL_ROWS_PRESENT', 'FTS_SHADOWS_NOT_COMPARED')
        return ParentComparison('PARENT_ROWS_PRESENT', 'SEMANTIC_MERGE_UNVERIFIED')
    except _Problem as exc:
        return ParentComparison('NOT_VERIFIED', exc.code)
    except Exception:
        return ParentComparison('NOT_VERIFIED', 'CHECK_FAILED')


def _sqlite_row(row: tuple[Any, ...]) -> bytes:
    """Hash SQLite values without conflating integer, real, text or blob."""
    digest = hashlib.sha256()
    for value in row:
        if value is None:
            tag, data = b'N', b''
        elif type(value) is int and -(2**63) <= value < 2**63:
            tag, data = b'I', str(value).encode('ascii')
        elif type(value) is float:
            tag, data = b'R', struct.pack('>d', value)
        elif type(value) is str:
            tag, data = b'T', value.encode('utf-8', 'strict')
        elif type(value) is bytes:
            tag, data = b'B', value
        else:
            raise _Problem('INVALID_LOCAL_CELL')
        digest.update(tag)
        digest.update(len(data).to_bytes(8, 'big'))
        digest.update(data)
    return digest.digest()


def _query_only_snapshot(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction or conn.text_factory is not str:
        raise _Problem('SNAPSHOT_REQUIRED')
    c = conn.cursor()
    c.row_factory = None
    if c.execute('PRAGMA query_only').fetchone() != (1,):
        raise _Problem('SNAPSHOT_REQUIRED')


def _workspace_tables(conn: sqlite3.Connection) -> dict[str, str | None]:
    c = conn.cursor()
    c.row_factory = None
    result: dict[str, str | None] = {}
    for name, sql in c.execute("SELECT name,sql FROM main.sqlite_schema WHERE type='table'"):
        if type(name) is not str or not name:
            raise _Problem('INVALID_LOCAL_SCHEMA')
        if name.startswith('sqlite_'):
            continue
        if name.casefold() in {x.casefold() for x in result}:
            raise _Problem('INVALID_LOCAL_SCHEMA')
        result[name] = sql
    return result


def _workspace_objects(conn: sqlite3.Connection) -> dict[tuple[str, str], str]:
    c = conn.cursor(); c.row_factory = None
    result: dict[tuple[str, str], str] = {}
    for kind, name, sql in c.execute("SELECT type,name,sql FROM main.sqlite_schema WHERE type IN ('view','trigger','index')"):
        if name.startswith('sqlite_'):
            continue
        if type(kind) is not str or type(name) is not str or type(sql) is not str:
            raise _Problem('INVALID_LOCAL_SCHEMA')
        result[(kind, name)] = sql
    return result


def _table_rows(conn: sqlite3.Connection, name: str):
    c = conn.cursor()
    c.row_factory = None
    columns = [r[1] for r in c.execute('PRAGMA main.table_info(' + _quoted(name) + ')')]
    if not columns or any(type(x) is not str or not x for x in columns):
        raise _Problem('INVALID_LOCAL_SCHEMA')
    type_rows = [r for r in c.execute('PRAGMA main.table_list') if r[0] == 'main' and r[1] == name]
    if len(type_rows) != 1:
        raise _Problem('INVALID_LOCAL_SCHEMA')
    without_rowid = type_rows[0][4]
    rowid = ''
    if not without_rowid:
        shadows = {x.casefold() for x in columns}
        alias = next((s for s in ('rowid', '_rowid_', 'oid') if s not in shadows), None)
        if alias is None:
            raise _Problem('UNSUPPORTED_ROWID')
        rowid = _quoted(alias) + ','
    return c.execute('SELECT ' + rowid + ','.join(_quoted(x) for x in columns) + ' FROM main.' + _quoted(name))


def _assert_rows_preserved(parent: sqlite3.Connection, candidate: sqlite3.Connection, name: str,
                           budget: list[int]) -> bool:
    """Return whether candidate has extra rows; count duplicate occurrences."""
    remaining: Counter[bytes] = Counter()
    for row in _table_rows(candidate, name):
        budget[0] += 1
        if budget[0] > _MAX_ROWS:
            raise _Problem('LIMIT_EXCEEDED')
        remaining[_sqlite_row(row)] += 1
    for row in _table_rows(parent, name):
        budget[0] += 1
        if budget[0] > _MAX_ROWS:
            raise _Problem('LIMIT_EXCEEDED')
        digest = _sqlite_row(row)
        if remaining[digest] <= 0:
            raise _Problem('PARENT_ROW_CONFLICT')
        remaining[digest] -= 1
    return any(remaining.values())


def _assert_work_fts_consistent(conn: sqlite3.Connection) -> None:
    c = conn.cursor()
    c.row_factory = None
    tables = {(r[0], r[1]): r[2] for r in c.execute('PRAGMA main.table_list')}
    if tables.get(('main', 'work_fts')) != 'virtual':
        raise _Problem('WORK_FTS_UNSUPPORTED')
    definition = c.execute("SELECT sql FROM main.sqlite_schema WHERE type='table' AND name='work_fts' COLLATE BINARY").fetchone()
    if not definition or not re.search(r'\bUSING\s+fts5\s*\(', definition[0], re.IGNORECASE):
        raise _Problem('WORK_FTS_UNSUPPORTED')
    expected: Counter[bytes] = Counter()
    for count, row in enumerate(c.execute('SELECT record_id,title,body FROM main.work_records'), 1):
        expected[_sqlite_row(row)] += 1
        if count > _MAX_ROWS:
            raise _Problem('LIMIT_EXCEEDED')
    actual: Counter[bytes] = Counter()
    for count, row in enumerate(c.execute('SELECT record_id,title,body FROM main.work_fts'), 1):
        actual[_sqlite_row(row)] += 1
        if count > _MAX_ROWS:
            raise _Problem('LIMIT_EXCEEDED')
    if expected != actual:
        raise _Problem('WORK_FTS_MISMATCH')


_RECORD_FIELDS = ('record_id', 'title', 'original_title', 'body', 'body_sha256', 'summary',
                  'document_role', 'original_layer', 'review_status', 'adoption_status',
                  'decision_status', 'source_kind', 'version', 'created_at', 'updated_at',
                  'archived_at', 'merged_into')
_REVISION_FIELDS = ('title', 'body', 'body_sha256', 'summary', 'review_status',
                    'adoption_status', 'decision_status', 'archived_at', 'merged_into')


def _records_query(conn: sqlite3.Connection):
    c = conn.cursor()
    c.row_factory = None
    return c.execute('SELECT ' + ','.join(_quoted(x) for x in _RECORD_FIELDS) + ' FROM main.work_records')


def _revision_matches(conn: sqlite3.Connection, record: dict[str, Any]) -> bool:
    c = conn.cursor()
    c.row_factory = None
    row = c.execute('SELECT ' + ','.join(_quoted(x) for x in _REVISION_FIELDS)
                    + ' FROM main.work_revisions WHERE record_id=? AND version=?',
                    (record['record_id'], record['version'])).fetchone()
    return row is not None and hmac.compare_digest(_sqlite_row(row),
                                                    _sqlite_row(tuple(record[x] for x in _REVISION_FIELDS)))


def _assert_work_records(parent: sqlite3.Connection, candidate: sqlite3.Connection,
                         budget: list[int]) -> bool:
    c = candidate.cursor()
    c.row_factory = None
    extra = c.execute('SELECT COUNT(*) FROM main.work_records').fetchone()[0]
    changed = False
    fields = ','.join(_quoted(x) for x in _RECORD_FIELDS)
    for row in _records_query(parent):
        budget[0] += 1
        if budget[0] > _MAX_ROWS:
            raise _Problem('LIMIT_EXCEEDED')
        old = dict(zip(_RECORD_FIELDS, row))
        if type(old['record_id']) is not str or not old['record_id']:
            raise _Problem('INVALID_WORK_RECORD')
        result = c.execute('SELECT ' + fields + ' FROM main.work_records WHERE record_id=? COLLATE BINARY',
                           (old['record_id'],)).fetchone()
        if result is None:
            raise _Problem('WORK_RECORD_DELETED')
        extra -= 1
        new = dict(zip(_RECORD_FIELDS, result))
        if hmac.compare_digest(_sqlite_row(row), _sqlite_row(result)):
            continue
        changed = True
        if (type(old['version']) is not int or type(new['version']) is not int
                or old['version'] < 1 or new['version'] <= old['version']
                or new['version'] - old['version'] > _MAX_ROWS):
            raise _Problem('WORK_VERSION_CONFLICT')
        mutable = set(_REVISION_FIELDS) | {'version', 'updated_at'}
        if any(not hmac.compare_digest(_sqlite_row((old[x],)), _sqlite_row((new[x],)))
               for x in _RECORD_FIELDS if x not in mutable):
            raise _Problem('WORK_UNTRACKED_CHANGE')
        if not _revision_matches(candidate, old) or not _revision_matches(candidate, new):
            raise _Problem('WORK_REVISION_MISMATCH')
        count, minimum, maximum = c.execute(
            'SELECT COUNT(*),MIN(version),MAX(version) FROM main.work_revisions '
            'WHERE record_id=? COLLATE BINARY AND version BETWEEN ? AND ?',
            (old['record_id'], old['version'], new['version'])).fetchone()
        if count != new['version'] - old['version'] + 1 or minimum != old['version'] or maximum != new['version']:
            raise _Problem('WORK_REVISION_GAP')
    if extra < 0:
        raise _Problem('INVALID_WORK_RECORD')
    return changed or extra > 0


def _assert_meta_preserved(parent: sqlite3.Connection, candidate: sqlite3.Connection) -> bool:
    p = parent.cursor(); p.row_factory = None
    c = candidate.cursor(); c.row_factory = None
    old = dict(p.execute('SELECT key,value FROM main.work_meta'))
    new = dict(c.execute('SELECT key,value FROM main.work_meta'))
    for key, value in old.items():
        if key not in new:
            raise _Problem('WORK_META_CONFLICT')
        if key != 'data_revision' and not hmac.compare_digest(_sqlite_row((value,)), _sqlite_row((new[key],))):
            raise _Problem('WORK_META_CONFLICT')
    return old != new


def _assert_work_keys(conn: sqlite3.Connection) -> None:
    expected = {'work_records': ['record_id'], 'work_revisions': ['record_id', 'version'],
                'work_meta': ['key']}
    c = conn.cursor(); c.row_factory = None
    for table, keys in expected.items():
        cols = c.execute('PRAGMA main.table_info(' + _quoted(table) + ')').fetchall()
        actual = [r[1] for r in sorted((r for r in cols if r[5]), key=lambda r: r[5])]
        if actual != keys:
            raise _Problem('INVALID_WORK_SCHEMA')


def _assert_sequences(parent: sqlite3.Connection, candidate: sqlite3.Connection) -> None:
    p = parent.cursor(); p.row_factory = None
    c = candidate.cursor(); c.row_factory = None
    old_exists = p.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='sqlite_sequence'").fetchone()
    if not old_exists:
        return
    new_exists = c.execute("SELECT 1 FROM main.sqlite_schema WHERE type='table' AND name='sqlite_sequence'").fetchone()
    if not new_exists:
        raise _Problem('SEQUENCE_CONFLICT')
    for name, seq in p.execute('SELECT name,seq FROM main.sqlite_sequence'):
        other = c.execute('SELECT seq FROM main.sqlite_sequence WHERE name=? COLLATE BINARY', (name,)).fetchall()
        if (type(seq) is not int or seq < 0 or len(other) != 1
                or type(other[0][0]) is not int or other[0][0] < seq):
            raise _Problem('SEQUENCE_CONFLICT')


def compare_workspace_snapshots(parent: sqlite3.Connection, candidate: sqlite3.Connection) -> ParentComparison:
    """Compare two already authenticated, local immutable SQLite snapshots.

    Every parent table's schema and every historical row occurrence must be
    retained. A changed work record is allowed only with contiguous candidate
    revisions containing the exact previous and current fields; fields not
    represented in revisions cannot change. Existing work_meta values except
    data_revision remain unchanged. Only the documented physical FTS shadows
    may differ, with work_fts validated against work_records in BOTH snapshots
    and historical item_fts independently compared. Parent generation payloads
    must ALSO be preserved byte-identically by the caller.

    Even a pass is structural and does not authenticate source provenance,
    validate event hash semantics, authorize conclusions or settle whether new
    user additions should be merged. Callers must separately handle conflicts,
    actual immutable capture, authorization and reader compatibility.
    """
    try:
        _query_only_snapshot(parent)
        _query_only_snapshot(candidate)
        previous, current = _workspace_tables(parent), _workspace_tables(candidate)
        required = {'work_meta', 'work_records', 'work_revisions', 'work_events'}
        if not required <= previous.keys() or not previous.keys() <= current.keys():
            raise _Problem('PARENT_TABLE_MISSING')
        _assert_work_keys(parent)
        _assert_work_keys(candidate)
        old_objects, new_objects = _workspace_objects(parent), _workspace_objects(candidate)
        if any(new_objects.get(key) != sql for key, sql in old_objects.items()):
            raise _Problem('OBJECT_SCHEMA_MISMATCH')
        for name, sql in previous.items():
            if type(sql) is not str or current[name] != sql:
                raise _Problem('TABLE_SCHEMA_MISMATCH')
        parent_kind = {(r[0], r[1]): r[2] for r in parent.execute('PRAGMA main.table_list')}
        candidate_kind = {(r[0], r[1]): r[2] for r in candidate.execute('PRAGMA main.table_list')}
        work_fts_shadows = {'work_fts_data', 'work_fts_idx', 'work_fts_content',
                            'work_fts_docsize', 'work_fts_config'}
        present_work_fts = 'work_fts' in previous
        if present_work_fts:
            _assert_work_fts_consistent(parent)
            _assert_work_fts_consistent(candidate)
            for name in work_fts_shadows.intersection(previous):
                if parent_kind.get(('main', name)) != 'shadow' or candidate_kind.get(('main', name)) != 'shadow':
                    raise _Problem('WORK_FTS_UNSUPPORTED')
        elif work_fts_shadows.intersection(previous):
            raise _Problem('WORK_FTS_UNSUPPORTED')
        parent_shadows = _FTS_SHADOWS.intersection(previous)
        if parent_shadows:
            if ('item_fts' not in previous or parent_kind.get(('main', 'item_fts')) != 'virtual'
                    or candidate_kind.get(('main', 'item_fts')) != 'virtual'):
                raise _Problem('FTS_LOGICAL_UNSUPPORTED')
            definition = previous['item_fts']
            if not re.search(r'\bUSING\s+fts5\s*\(', definition, re.IGNORECASE):
                raise _Problem('FTS_LOGICAL_UNSUPPORTED')
            for name in parent_shadows:
                if parent_kind.get(('main', name)) != 'shadow' or candidate_kind.get(('main', name)) != 'shadow':
                    raise _Problem('FTS_SHADOW_UNSUPPORTED')
        budget = [0]
        changed = _assert_meta_preserved(parent, candidate)
        changed = (previous.keys() != current.keys() or old_objects.keys() != new_objects.keys()) or changed
        changed = _assert_work_records(parent, candidate, budget) or changed
        ignored = {'work_meta', 'work_records'} | parent_shadows
        if present_work_fts:
            ignored |= {'work_fts'} | work_fts_shadows
        for name in previous:
            if name not in ignored:
                changed = _assert_rows_preserved(parent, candidate, name, budget) or changed
        _assert_sequences(parent, candidate)
        if not parent.in_transaction or not candidate.in_transaction:
            raise _Problem('SNAPSHOT_REQUIRED')
        code = 'TARGET_CHANGES_SEMANTICS_UNVERIFIED' if changed else 'SEMANTICS_UNVERIFIED'
        return ParentComparison('WORKSPACE_HISTORY_PRESERVED', code)
    except _Problem as exc:
        return ParentComparison('NOT_VERIFIED', exc.code)
    except Exception:
        return ParentComparison('NOT_VERIFIED', 'CHECK_FAILED')
