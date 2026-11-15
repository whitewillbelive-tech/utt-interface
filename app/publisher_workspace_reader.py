"""Read an explicitly captured, immutable workspace SQLite image.

This provider never opens a path, follows a Dropbox link, mutates a source, or
reads a live database. The caller supplies bytes of a completed SQLite backup
whose provenance and authorized acquisition it has verified separately. It is
not a D0 adapter, a publisher, a source-body reader, or proof of deployment.

Only capabilities, search and detail are exposed by READ/0.1. Source/evidence,
project relations, history, adoption and decision semantics remain unknown. Search is
SQLite LIKE over title/body/record_id with escaped wildcards and literal spaces;
the captured image makes process-local, context-bound page cursors stable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time

from app.provider import SORTS, VERSION, error, validate
from app.validation import validate_response

_FORMAT = 'UTT-INTEGRATED-WORKSPACE/0.1'
_HEADER = b'SQLite format 3\x00'
_HASH = re.compile(r'^[0-9a-f]{64}$')
_TOKEN = re.compile(r'^[A-Za-z0-9_-]{43}$')
_MAX_BYTES = 512 * 1024 * 1024
_CURSOR_LIMIT = 10000
_CURSOR_LIFETIME = 900
_OPERATIONS = ('capabilities', 'search', 'detail')


class WorkspaceSnapshotError(Exception):
    """A fixed failure code; never includes paths, records, data or diagnostics."""

    def __init__(self, code='UNSUPPORTED_CONTRACT'):
        self.code = code
        super().__init__('Workspace snapshot unavailable or unsupported.')


class WorkspaceSnapshotProvider:
    """Serve a single in-memory copy of a completed standalone SQLite backup.

    ``snapshot_bytes`` must contain the entire consistent SQLite backup and no
    required WAL/journal sidecar. External code must verify capture and security;
    this class does not infer those assurances from a filename or hash. A 512 MiB
    default ceiling supports inputs larger than Dropbox's single-upload limit;
    callers may choose a smaller positive ceiling, but not exceed 512 MiB.

    The opt-in ``lifecycle_enabled`` is an integration assertion by the host:
    it must expose and display this provider's lifecycle response. The base
    READ/0.1 object alone does not contain archive or merge information.
    """

    def __init__(self, snapshot_bytes: bytes, *, max_bytes: int = _MAX_BYTES,
                 lifecycle_enabled: bool = False):
        self._lock = threading.RLock()
        self._db = None
        self._closed = False
        # Opt in ONLY when the hosting UI also exposes and displays the separate
        # lifecycle response for every listed/opened record. READ/0.1 has no
        # archive timestamp or merge target field. The default fails closed.
        if type(lifecycle_enabled) is not bool:
            raise WorkspaceSnapshotError()
        self._lifecycle_enabled = lifecycle_enabled
        self._key = secrets.token_bytes(32)
        self._cursors = {}
        if (type(max_bytes) is not int or not 1024 <= max_bytes <= _MAX_BYTES
                or type(snapshot_bytes) is not bytes or not 100 <= len(snapshot_bytes) <= max_bytes
                or not snapshot_bytes.startswith(_HEADER) or snapshot_bytes[18:20] != b'\x01\x01'):
            raise WorkspaceSnapshotError()
        try:
            digest = hashlib.sha256(snapshot_bytes).hexdigest()
            self._db = sqlite3.connect(':memory:', check_same_thread=False, isolation_level=None)
            self._db.execute('PRAGMA trusted_schema=OFF')
            self._db.deserialize(snapshot_bytes)
            self._db.execute('PRAGMA query_only=ON')
            if self._db.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
                raise ValueError()
            kinds = dict(self._db.execute("SELECT name,type FROM sqlite_master WHERE name IN ('work_meta','work_records')"))
            if kinds != {'work_meta': 'table', 'work_records': 'table'}:
                raise ValueError()
            record_columns = list(self._db.execute('PRAGMA table_info(work_records)'))
            meta_columns = list(self._db.execute('PRAGMA table_info(work_meta)'))
            cols = {row[1] for row in record_columns}
            if (not {'record_id', 'title', 'body', 'body_sha256', 'summary', 'document_role',
                    'review_status', 'archived_at', 'merged_into'} <= cols
                    or [(row[1], row[5]) for row in record_columns if row[5]] != [('record_id', 1)]
                    or [(row[1], row[5]) for row in meta_columns if row[5]] != [('key', 1)]):
                raise ValueError()
            if self._db.execute("SELECT 1 FROM work_records WHERE record_id IS NULL OR typeof(record_id) != 'text' OR record_id='' LIMIT 1").fetchone():
                raise ValueError()
            meta_rows = self._db.execute("SELECT key,value FROM work_meta WHERE key IN ('format_version','dataset_id','data_revision')").fetchall()
            if len(meta_rows) != len({row[0] for row in meta_rows}):
                raise ValueError()
            meta = dict(meta_rows)
            if (meta.get('format_version') != _FORMAT or not isinstance(meta.get('dataset_id'), str)
                    or not meta['dataset_id']):
                raise ValueError()
            version = meta.get('data_revision')
            if version is not None and (not isinstance(version, str) or not version):
                raise ValueError()
            self._dataset = {'dataset_id': meta['dataset_id'], 'dataset_kind': 'WORKSPACE_SNAPSHOT',
                             'snapshot_id': 'sha256:' + digest, 'source_version': version,
                             'legacy_contracts': []}
        except Exception:
            if self._db is not None:
                self._db.close()
            self._db = None
            raise WorkspaceSnapshotError() from None

    def close(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._cursors.clear()
                if self._db is not None:
                    self._db.close()
                    self._db = None

    def _ok(self, operation, result):
        return {'contract_version': VERSION, 'operation': operation, 'status': 'OK',
                'snapshot': self._dataset.copy(), 'result': result, 'error': None}

    def _fingerprint(self, q, sort):
        raw = json.dumps({**q, 'cursor': None, 'sort': sort}, sort_keys=True,
                         separators=(',', ':'), ensure_ascii=False).encode('utf-8')
        return hmac.digest(self._key, b'UTT-workspace-snapshot-cursor\0' + raw, 'sha256')

    def _expire(self, now):
        for token, value in list(self._cursors.items()):
            if now >= value[2]:
                del self._cursors[token]

    def _offset(self, q, sort):
        if q['cursor'] is None:
            return 0
        cursor = q['cursor']
        if not _TOKEN.fullmatch(cursor):
            raise WorkspaceSnapshotError('VERSION_MISMATCH')
        now = time.monotonic()
        self._expire(now)
        entry = self._cursors.get(cursor)
        if (entry is None or now < entry[1] or now >= entry[2]
                or not hmac.compare_digest(entry[3], self._fingerprint(q, sort))):
            raise WorkspaceSnapshotError('VERSION_MISMATCH')
        return entry[0]

    def _cursor(self, q, sort, offset):
        now = time.monotonic()
        self._expire(now)
        fingerprint = self._fingerprint(q, sort)
        for token, entry in self._cursors.items():
            if entry[0] == offset and entry[1] <= now < entry[2] and hmac.compare_digest(entry[3], fingerprint):
                return token
        if len(self._cursors) >= _CURSOR_LIMIT:
            raise WorkspaceSnapshotError('CONNECTION_UNAVAILABLE')
        for _ in range(4):
            token = secrets.token_urlsafe(32)
            if token not in self._cursors:
                self._cursors[token] = (offset, now, now + _CURSOR_LIFETIME, fingerprint)
                return token
        raise WorkspaceSnapshotError('CONNECTION_UNAVAILABLE')

    def _record(self, row, *, detail):
        ident, title, role, summary, review, body, digest, archived, merged = row
        if (not all(isinstance(x, str) and x for x in (ident, title, role, review))
                or summary is not None and not isinstance(summary, str)
                or not isinstance(body, str) or not isinstance(digest, str) or not _HASH.fullmatch(digest)
                or archived is not None and not isinstance(archived, str)
                or merged is not None and (not isinstance(merged, str) or not merged)):
            raise WorkspaceSnapshotError()
        if not hmac.compare_digest(hashlib.sha256(body.encode('utf-8')).hexdigest(), digest):
            raise WorkspaceSnapshotError('VERSION_MISMATCH')
        # No READ/0.1 field can preserve these values. Only an explicitly
        # lifecycle-capable hosting surface may serve the base representation.
        if (archived is not None or merged is not None) and not self._lifecycle_enabled:
            raise WorkspaceSnapshotError()
        return {'record_id': ident, 'dataset_id': self._dataset['dataset_id'], 'original_ids': [],
                'title': title, 'document_role': role,
                'state': {'observation': None, 'review': review, 'proposal': None, 'adoption': None},
                'display_summary': summary, 'project_relations': [], 'source_refs': [],
                'source_status': 'UNKNOWN', 'project_relations_status': 'UNKNOWN',
                'content_availability': 'AVAILABLE' if detail else 'NOT_REQUESTED',
                'raw_text': body if detail else None, 'raw_text_sha256': digest if detail else None}

    def lifecycle(self, *, dataset_id, snapshot_id, record_ids):
        """Return unmodified archive and merge state for this exact snapshot.

        This separate contract is required alongside READ/0.1 for a hosting UI
        that opts into archived/merged records. It neither invents an active
        status nor follows a merge; both source columns are preserved as-is.
        The response does not establish history or restore support.
        """
        if (type(record_ids) is not list or len(record_ids) > 100
                or any(type(ident) is not str or not ident for ident in record_ids)
                or len(set(record_ids)) != len(record_ids)):
            raise WorkspaceSnapshotError('INVALID_REQUEST')
        with self._lock:
            if self._closed or self._db is None:
                raise WorkspaceSnapshotError('CONNECTION_UNAVAILABLE')
            if not self._lifecycle_enabled:
                raise WorkspaceSnapshotError('UNSUPPORTED_CONTRACT')
            if (type(dataset_id) is not str or type(snapshot_id) is not str
                    or dataset_id != self._dataset['dataset_id']
                    or snapshot_id != self._dataset['snapshot_id']):
                raise WorkspaceSnapshotError('VERSION_MISMATCH')
            records = []
            for ident in record_ids:
                row = self._db.execute('SELECT archived_at,merged_into FROM work_records WHERE record_id=?',
                                       (ident,)).fetchone()
                if row is None:
                    raise WorkspaceSnapshotError('ITEM_NOT_FOUND')
                archived, merged = row
                if (archived is not None and not isinstance(archived, str)
                        or merged is not None and (not isinstance(merged, str) or not merged)):
                    raise WorkspaceSnapshotError()
                records.append({'record_id': ident, 'archived_at': archived, 'merged_into': merged})
            return {'contract_version': 'UTT-WORKSPACE-LIFECYCLE/0.1',
                    'snapshot': self._dataset.copy(), 'records': records}

    def workspace_features(self):
        """Describe this separate local workspace surface, not READ/0.1 writes."""
        with self._lock:
            return {'contract_version': 'UTT-WORKSPACE-FEATURES/0.1',
                    'read_detail': bool(self._lifecycle_enabled and not self._closed and self._db is not None),
                    'writes_supported': False}

    def read_workspace_detail(self, q):
        """Read extended details in the exact immutable capture used by READ/0.1."""
        def failed(code):
            return {'contract_version': 'UTT-WORKSPACE-DETAIL/0.1', 'operation': 'detail',
                    'status': 'ERROR', 'error': {'code': code}}
        if (type(q) is not dict or set(q) != {'contract_version', 'operation', 'dataset_id', 'snapshot_id', 'record_id'}
                or q['contract_version'] != 'UTT-WORKSPACE-READ/0.1' or q['operation'] != 'detail'
                or any(type(q[k]) is not str or not q[k] for k in ('dataset_id', 'snapshot_id', 'record_id'))):
            return failed('INVALID_REQUEST')
        with self._lock:
            if self._closed or self._db is None:
                return failed('CONNECTION_UNAVAILABLE')
            if not self._lifecycle_enabled:
                return failed('UNSUPPORTED_CONTRACT')
            if q['dataset_id'] != self._dataset['dataset_id'] or q['snapshot_id'] != self._dataset['snapshot_id']:
                return failed('VERSION_MISMATCH')
            try:
                from app.workspace_extended_read import read_workspace_record
                result = read_workspace_record(self._db, q['record_id'])
                if type(result) is not dict or result.get('contract_version') != 'UTT-WORKSPACE-DETAIL/0.1':
                    return failed('READ_FAILED')
                if result.get('status') != 'OK':
                    error_value = result.get('error')
                    code = error_value.get('code') if type(error_value) is dict else None
                    if code not in ('INVALID_REQUEST', 'ITEM_NOT_FOUND', 'DATA_INTEGRITY', 'CONTENT_MISMATCH',
                                    'UNSUPPORTED_SCHEMA', 'UNSUPPORTED_VALUE', 'READ_FAILED', 'RESULT_LIMIT_EXCEEDED'):
                        code = 'READ_FAILED'
                    return failed(code)
                actual = self.lifecycle(dataset_id=q['dataset_id'], snapshot_id=q['snapshot_id'],
                                        record_ids=[q['record_id']])['records'][0]
                record = result.get('record')
                if (type(record) is not dict or record.get('record_id') != q['record_id']
                        or record.get('archived_at') != actual['archived_at']
                        or record.get('merged_into') != actual['merged_into']):
                    return failed('DATA_INTEGRITY')
                return {**result, 'operation': 'detail', 'dataset_id': q['dataset_id'],
                        'snapshot_id': q['snapshot_id'], 'record_id': q['record_id']}
            except WorkspaceSnapshotError as exc:
                return failed(exc.code)
            except Exception:
                return failed('READ_FAILED')

    def workspace_catalog(self, q):
        """List projects, topics, or members within this immutable capture."""
        def failed(code):
            return {'contract_version': 'UTT-WORKSPACE-DETAIL/0.1', 'status': 'ERROR',
                    'error': {'code': code}}
        if type(q) is not dict:
            return failed('INVALID_REQUEST')
        operation = q.get('operation')
        members = operation in ('project_members', 'topic_members')
        fields = {'contract_version', 'operation', 'dataset_id', 'snapshot_id', 'limit', 'offset'}
        if members:
            fields.add('identifier')
        if (set(q) != fields or q['contract_version'] != 'UTT-WORKSPACE-READ/0.1'
                or operation not in ('projects', 'topics', 'project_members', 'topic_members')
                or members and (type(q['identifier']) is not str or not q['identifier'])
                or any(type(q[k]) is not str or not q[k] for k in ('dataset_id', 'snapshot_id'))
                or type(q['limit']) is not int or not 1 <= q['limit'] <= 1000
                or type(q['offset']) is not int or not 0 <= q['offset'] <= 2**31 - 1):
            return failed('INVALID_REQUEST')
        with self._lock:
            if self._closed or self._db is None:
                return failed('CONNECTION_UNAVAILABLE')
            if not self._lifecycle_enabled:
                return failed('UNSUPPORTED_CONTRACT')
            if q['dataset_id'] != self._dataset['dataset_id'] or q['snapshot_id'] != self._dataset['snapshot_id']:
                return failed('VERSION_MISMATCH')
            try:
                from app.workspace_extended_read import list_workspace_catalog, list_workspace_members
                if members:
                    kind = 'project' if operation == 'project_members' else 'topic'
                    result = list_workspace_members(self._db, kind, q['identifier'],
                                                    limit=q['limit'], offset=q['offset'])
                else:
                    result = list_workspace_catalog(self._db, operation, limit=q['limit'], offset=q['offset'])
                if type(result) is not dict or result.get('contract_version') != 'UTT-WORKSPACE-DETAIL/0.1':
                    return failed('READ_FAILED')
                if result.get('status') != 'OK':
                    value = result.get('error')
                    code = value.get('code') if type(value) is dict else None
                    if code not in ('INVALID_REQUEST', 'ITEM_NOT_FOUND', 'DATA_INTEGRITY', 'UNSUPPORTED_SCHEMA', 'UNSUPPORTED_VALUE',
                                    'READ_FAILED', 'RESULT_LIMIT_EXCEEDED'):
                        code = 'READ_FAILED'
                    return failed(code)
                return {**result, 'operation': q['operation'], 'dataset_id': q['dataset_id'],
                        'snapshot_id': q['snapshot_id']}
            except Exception:
                return failed('READ_FAILED')

    def read(self, q, sort='id_asc'):
        op = q.get('operation') if isinstance(q, dict) else None
        try:
            code = validate(q)
            if code:
                return error(op, code)
            if not isinstance(sort, str) or sort not in SORTS:
                return error(op, 'INVALID_REQUEST')
            if any(q[k] is not None for k in ('project_id', 'document_role', 'review_status')):
                return error(op, 'INVALID_REQUEST')
            with self._lock:
                if self._closed or self._db is None:
                    return error(op, 'CONNECTION_UNAVAILABLE')
                if q['dataset_id'] not in (None, self._dataset['dataset_id']):
                    return error(op, 'VERSION_MISMATCH' if q['cursor'] else 'ITEM_NOT_FOUND')
                if q['snapshot_id'] not in (None, self._dataset['snapshot_id']):
                    return error(op, 'VERSION_MISMATCH')
                if op not in _OPERATIONS:
                    return error(op, 'UNSUPPORTED_CONTRACT')
                if op == 'capabilities':
                    if any(q[k] is not None for k in ('record_id', 'query', 'cursor')):
                        return error(op, 'INVALID_REQUEST')
                    value = self._ok(op, {'dataset': self._dataset.copy(), 'operations': list(_OPERATIONS),
                        'max_page_size': 100, 'writes_supported': False,
                        'sort_order': ['dataset_id:asc', 'record_id:' + SORTS[sort]]})
                elif op == 'search':
                    if q['record_id'] is not None:
                        return error(op, 'INVALID_REQUEST')
                    offset = self._offset(q, sort)
                    text = q['query']
                    clauses = ''
                    args = []
                    if text is not None and text != '':
                        term = text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        clauses = " WHERE (title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR record_id LIKE ? ESCAPE '\\')"
                        args = ['%' + term + '%'] * 3
                    sql = ('SELECT record_id,title,document_role,summary,review_status,body,body_sha256,archived_at,merged_into '
                           'FROM work_records' + clauses + ' ORDER BY record_id COLLATE BINARY ' + SORTS[sort].upper() + ' LIMIT ? OFFSET ?')
                    rows = self._db.execute(sql, (*args, q['page_size'] + 1, offset)).fetchall()
                    records = [self._record(row, detail=False) for row in rows[:q['page_size']]]
                    next_cursor = self._cursor(q, sort, offset + q['page_size']) if len(rows) > q['page_size'] else None
                    value = self._ok(op, {'records': records, 'page': {'page_size': q['page_size'],
                        'sort': ['dataset_id:asc', 'record_id:' + SORTS[sort]], 'next_cursor': next_cursor}})
                else:
                    if q['query'] is not None or q['cursor'] is not None:
                        return error(op, 'INVALID_REQUEST')
                    row = self._db.execute('SELECT record_id,title,document_role,summary,review_status,body,body_sha256,archived_at,merged_into FROM work_records WHERE record_id=?', (q['record_id'],)).fetchone()
                    if row is None:
                        return error(op, 'ITEM_NOT_FOUND')
                    value = self._ok(op, {'record': self._record(row, detail=True), 'related_projects': [], 'sources': []})
                validate_response(value, q, sort)
                return value
        except WorkspaceSnapshotError as exc:
            return error(op, exc.code)
        except Exception:
            return error(op, 'INTERNAL_ERROR')
