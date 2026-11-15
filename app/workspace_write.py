"""Transactional workspace changes with an explicit, externally verified event codec.

This module does not define the existing workspace event/hash protocol.  No
codec is bundled: callers MUST supply an implementation verified against the
original workspace program and its event/history contract.  In particular a
schema alone is not evidence for the event hash or data_revision algorithm.
The callable codec is trusted application code, not a Python sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
import threading
from typing import Any, Protocol

from app.publisher_workspace import WorkspaceError, _validate_workspace


class WorkspaceWriteError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class EncodedEvent:
    event_id: str
    event_kind: str
    authorization_basis: str | None
    reason: str
    detail_json: str
    occurred_at: str
    preceding_hash: str | None
    event_hash: str
    data_revision: str

    def __repr__(self):
        return '<EncodedEvent redacted>'


class WorkspaceEventCodec(Protocol):
    """Original-program-compatible codec supplied by an approved runtime.

    verify_history is given ordered existing work_events (including the stored
    hashes), and must verify the complete original chain, including genesis.
    encode returns an event for precisely the proposed change and sequence.
    The caller must verify this implementation against original source before
    using it with production data. There is intentionally no fallback codec.
    """
    def verify_history(self, events: tuple[tuple[Any, ...], ...]) -> bool: ...
    def encode(self, *, sequence: int, previous_hash: str | None,
               current_data_revision: str, operation: str, record_id: str,
               record_version: int, before: dict | None, after: dict,
               detail: dict, reason: str) -> EncodedEvent: ...


@dataclass(frozen=True, repr=False)
class WriteResult:
    record_id: str = field(repr=False)
    version: int
    event_id: str = field(repr=False)
    status: str = 'LOCAL_SAVED'

    def __repr__(self):
        return '<WriteResult local state redacted>'


_RECORD_FIELDS = ('record_id', 'title', 'original_title', 'body', 'body_sha256', 'summary',
                  'document_role', 'original_layer', 'review_status', 'adoption_status',
                  'decision_status', 'source_kind', 'version', 'created_at', 'updated_at',
                  'archived_at', 'merged_into')
_REV_FIELDS = ('title', 'body', 'body_sha256', 'summary', 'review_status', 'adoption_status',
               'decision_status', 'archived_at', 'merged_into')
_EVENT_FIELDS = ('sequence', 'event_id', 'event_kind', 'record_id', 'record_version', 'actor',
                 'authorization_basis', 'reason', 'detail_json', 'occurred_at',
                 'preceding_hash', 'event_hash')
_MAX_EVENTS = 1_000_000
_MAX_HISTORY_BYTES = 64 * 1024 * 1024


def _text(value, *, nullable=False, limit=1024 * 1024):
    if value is None:
        return nullable
    return type(value) is str and len(value) <= limit and '\x00' not in value


def _id(value):
    return _text(value, limit=512) and bool(value.strip())


def _title(value):
    return _text(value, limit=65536) and bool(value)


def _hash_body(body):
    try:
        return hashlib.sha256(body.encode('utf-8')).hexdigest()
    except UnicodeError:
        raise WorkspaceWriteError('INVALID_REQUEST') from None


def _record(conn, record_id):
    row = conn.execute('SELECT ' + ','.join(_RECORD_FIELDS) + ' FROM work_records WHERE record_id=?',
                       (record_id,)).fetchone()
    return dict(zip(_RECORD_FIELDS, row)) if row is not None else None


def _project_link(conn, record_id, link_id):
    fields = ('link_id', 'record_id', 'project_id', 'role', 'scope', 'basis',
              'original_evidence_id', 'added_at', 'archived_at')
    row = conn.execute('SELECT ' + ','.join(fields) + ' FROM work_record_projects '
                       'WHERE record_id=? AND link_id=?', (record_id, link_id)).fetchone()
    return dict(zip(fields, row)) if row is not None else None


def _check_revision(conn, old):
    row = conn.execute('SELECT ' + ','.join(_REV_FIELDS) + ' FROM work_revisions '
                       'WHERE record_id=? AND version=?', (old['record_id'], old['version'])).fetchone()
    if row is None or tuple(row) != tuple(old[k] for k in _REV_FIELDS):
        raise WorkspaceWriteError('CURRENT_REVISION_MISSING_OR_MISMATCHED')
    if old['body_sha256'] != _hash_body(old['body']):
        raise WorkspaceWriteError('BODY_HASH_MISMATCH')


def _check_codec(codec):
    if codec is None or not callable(getattr(codec, 'verify_history', None)) or not callable(getattr(codec, 'encode', None)):
        raise WorkspaceWriteError('VERIFIED_EVENT_CODEC_REQUIRED')


def _event_context(conn, codec):
    rows = conn.execute('SELECT ' + ','.join(_EVENT_FIELDS) + ' FROM work_events ORDER BY sequence LIMIT ?',
                        (_MAX_EVENTS + 1,))
    events, budget = [], 0
    for row in rows:
        if len(events) >= _MAX_EVENTS:
            raise WorkspaceWriteError('HISTORY_LIMIT')
        item = tuple(row)
        for value in item:
            if type(value) is str:
                budget += len(value.encode('utf-8'))
            elif type(value) is int or value is None:
                budget += 8
            else:
                raise WorkspaceWriteError('INVALID_HISTORY')
            if budget > _MAX_HISTORY_BYTES:
                raise WorkspaceWriteError('HISTORY_LIMIT')
        events.append(item)
    events = tuple(events)
    try:
        verified = codec.verify_history(events)
    except Exception:
        raise WorkspaceWriteError('EVENT_HISTORY_UNVERIFIED') from None
    if verified is not True:
        raise WorkspaceWriteError('EVENT_HISTORY_UNVERIFIED')
    previous = events[-1][-1] if events else None
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='work_events'").fetchone()
    sequence = max(events[-1][0] if events else 0, row[0] if row is not None else 0) + 1
    value = conn.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()
    if value is None or not _id(value[0]):
        raise WorkspaceWriteError('DATA_REVISION_UNVERIFIED')
    return sequence, previous, value[0]


def _insert_event(conn, codec, *, context, operation, before, after, detail, reason):
    sequence, previous, data_revision = context
    try:
        event = codec.encode(sequence=sequence, previous_hash=previous,
                             current_data_revision=data_revision, operation=operation,
                             record_id=after['record_id'], record_version=after['version'],
                             before=dict(before) if before is not None else None,
                             after=dict(after), detail=dict(detail), reason=reason)
    except Exception:
        raise WorkspaceWriteError('EVENT_ENCODING_FAILED') from None
    if (type(event) is not EncodedEvent or not _id(event.event_id) or not _id(event.event_kind)
            or not _text(event.authorization_basis, nullable=True, limit=4096)
            or event.reason != reason or not _text(event.detail_json, limit=1024 * 1024)
            or not _id(event.occurred_at) or event.preceding_hash != previous
            or not _id(event.event_hash) or not _id(event.data_revision)
            or event.data_revision == data_revision):
        raise WorkspaceWriteError('INVALID_ENCODED_EVENT')
    try:
        json.loads(event.detail_json)
    except (ValueError, TypeError, RecursionError):
        raise WorkspaceWriteError('INVALID_ENCODED_EVENT') from None
    conn.execute('INSERT INTO work_events (' + ','.join(_EVENT_FIELDS) + ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (sequence, event.event_id, event.event_kind, after['record_id'], after['version'], 'USER',
                  event.authorization_basis, event.reason, event.detail_json, event.occurred_at,
                  event.preceding_hash, event.event_hash))
    changed = conn.execute("UPDATE work_meta SET value=? WHERE key='data_revision' AND value=?",
                           (event.data_revision, data_revision)).rowcount
    if changed != 1:
        raise WorkspaceWriteError('DATA_REVISION_CONFLICT')
    conn.execute('INSERT INTO work_revisions(record_id,version,' + ','.join(_REV_FIELDS) + ',event_id) '
                 'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                 (after['record_id'], after['version'], *(after[k] for k in _REV_FIELDS), event.event_id))
    return WriteResult(after['record_id'], after['version'], event.event_id)


class WorkspaceWriter:
    """Write an explicitly selected workspace connection; no Dropbox access.

    Every command is local, atomic and requires the original verified event
    codec. External changes require version conflict handling by the caller.
    """
    def __init__(self, conn: sqlite3.Connection, *, codec: WorkspaceEventCodec):
        if not isinstance(conn, sqlite3.Connection):
            raise WorkspaceWriteError('INVALID_CONNECTION')
        _check_codec(codec)
        self._conn, self._codec = conn, codec
        self._lock = threading.RLock()

    def execute(self, command: dict) -> WriteResult:
        # One connection cannot safely host concurrent transactions.  Callers
        # must not use the same connection through a second writer or directly.
        with self._lock:
            return self._execute(command)

    def _execute(self, command: dict) -> WriteResult:
        if type(command) is not dict:
            raise WorkspaceWriteError('INVALID_REQUEST')
        op = command.get('operation')
        rid, reason = command.get('record_id'), command.get('reason')
        if (op not in ('create', 'update', 'archive', 'merge', 'restore', 'project_add', 'project_archive', 'project_restore')
                or not _id(rid) or not _id(reason)):
            raise WorkspaceWriteError('INVALID_REQUEST')
        extras = {'create': {'record'}, 'update': {'changes'}, 'archive': set(),
                  'merge': {'target_record_id', 'target_expected_version'}, 'restore': {'restore_archive', 'restore_merge'},
                  'project_add': {'link_id', 'project_id', 'role', 'scope', 'basis'},
                  'project_archive': {'link_id'}, 'project_restore': {'link_id'}}
        if set(command) - ({'operation', 'record_id', 'reason', 'expected_version'} | extras[op]):
            raise WorkspaceWriteError('INVALID_REQUEST')
        conn = self._conn
        if conn.in_transaction:
            raise WorkspaceWriteError('TRANSACTION_ACTIVE')
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone() != (1,):
                raise WorkspaceWriteError('FOREIGN_KEYS_REQUIRED')
            conn.execute('BEGIN IMMEDIATE')
            try:
                _validate_workspace(conn)
            except WorkspaceError as exc:
                raise WorkspaceWriteError(exc.code) from None
            triggers = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='trigger' AND tbl_name GLOB 'work_*'")}
            if triggers != {'work_fts_insert', 'work_fts_update', 'work_fts_delete'}:
                raise WorkspaceWriteError('SCHEMA_MISMATCH')
            context = _event_context(conn, self._codec)
            old = _record(conn, rid)
            relation_before = None
            if op in ('project_add', 'project_archive', 'project_restore') and _id(command.get('link_id')):
                relation_before = _project_link(conn, rid, command['link_id'])
            target_before = _record(conn, command.get('target_record_id')) if op == 'merge' and _id(command.get('target_record_id')) else None
            if op == 'create':
                if old is not None or command.get('expected_version') is not None:
                    raise WorkspaceWriteError('RECORD_EXISTS')
                required = {'title', 'body', 'summary', 'document_role', 'review_status', 'adoption_status', 'source_kind'}
                values = command.get('record')
                if type(values) is not dict or set(values) != required:
                    raise WorkspaceWriteError('INVALID_REQUEST')
                if (not _title(values['title']) or not _text(values['body'], limit=16 * 1024 * 1024)
                        or not _text(values['summary'], nullable=True) or
                        any(not _id(values[k]) for k in ('document_role', 'review_status', 'adoption_status', 'source_kind'))):
                    raise WorkspaceWriteError('INVALID_REQUEST')
                # A new user-authored item must not be presented as an old
                # inherited source or an already approved/adopted decision.
                if (values['review_status'] != 'PENDING' or values['adoption_status'] != 'UNREVIEWED'
                        or values['source_kind'] != 'USER_CREATED'):
                    raise WorkspaceWriteError('NEW_RECORD_PROVENANCE_INVALID')
                conn.execute('INSERT INTO work_records(record_id,title,body,body_sha256,summary,document_role,'
                             'review_status,adoption_status,decision_status,source_kind,version) VALUES (?,?,?,?,?,?,?,?,?,?,1)',
                             (rid, values['title'], values['body'], _hash_body(values['body']), values['summary'],
                              values['document_role'], values['review_status'], values['adoption_status'],
                              'UNREVIEWED', values['source_kind']))
            else:
                version = command.get('expected_version')
                if type(version) is not int or not 1 <= version < 2**63 - 1:
                    raise WorkspaceWriteError('EXPECTED_VERSION_REQUIRED')
                if old is None:
                    raise WorkspaceWriteError('RECORD_NOT_FOUND')
                if old['version'] != version:
                    raise WorkspaceWriteError('VERSION_CONFLICT')
                _check_revision(conn, old)
                self._change(conn, old, op, command)
                updated = conn.execute('UPDATE work_records SET version=version+1,updated_at=CURRENT_TIMESTAMP '
                                       'WHERE record_id=? AND version=?', (rid, version)).rowcount
                if updated != 1:
                    raise WorkspaceWriteError('VERSION_CONFLICT')
            after = _record(conn, rid)
            detail = {k: v for k, v in command.items() if k not in ('record_id', 'reason', 'expected_version')}
            if op in ('project_add', 'project_archive', 'project_restore'):
                detail['relation_before'] = relation_before
                detail['relation_after'] = _project_link(conn, rid, command['link_id'])
            if op == 'merge':
                detail['target_before'] = target_before
            result = _insert_event(conn, self._codec, context=context, operation=op, before=old,
                                   after=after, detail=detail, reason=reason)
            # Validate the newly extended chain in the same transaction;
            # accepting only the previous chain would commit a bad encoder.
            checked_sequence, checked_hash, checked_revision = _event_context(conn, self._codec)
            last = conn.execute('SELECT event_id,event_hash FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
            if (checked_sequence != context[0] + 1 or checked_hash is None
                    or checked_revision == context[2] or last is None
                    or last[0] != result.event_id or last[1] != checked_hash):
                raise WorkspaceWriteError('EVENT_HISTORY_UNVERIFIED')
            try:
                conn.commit()
            except Exception:
                # A failed commit response may arrive after the durable commit.
                # Never claim failure or retry; the caller must re-open/re-read.
                try:
                    if conn.in_transaction:
                        conn.rollback()
                except Exception:
                    pass
                raise WorkspaceWriteError('COMMIT_UNKNOWN') from None
            return result
        except WorkspaceWriteError:
            if conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise WorkspaceWriteError('WRITE_FAILED') from None
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    @staticmethod
    def _change(conn, old, op, command):
        rid = old['record_id']
        if op == 'update':
            fields = command.get('changes')
            if type(fields) is not dict or not fields or not set(fields) <= {'title', 'body', 'summary'}:
                raise WorkspaceWriteError('INVALID_REQUEST')
            if (('title' in fields and not _title(fields['title'])) or
                    ('body' in fields and not _text(fields['body'], limit=16 * 1024 * 1024)) or
                    ('summary' in fields and not _text(fields['summary'], nullable=True))):
                raise WorkspaceWriteError('INVALID_REQUEST')
            values = dict(fields)
            if 'body' in values:
                values['body_sha256'] = _hash_body(values['body'])
            conn.execute('UPDATE work_records SET ' + ','.join(k + '=?' for k in values) + ' WHERE record_id=?',
                         (*values.values(), rid))
        elif op == 'archive':
            if old['archived_at'] is not None:
                raise WorkspaceWriteError('INVALID_STATE')
            conn.execute('UPDATE work_records SET archived_at=CURRENT_TIMESTAMP WHERE record_id=?', (rid,))
        elif op == 'merge':
            target = command.get('target_record_id')
            target_version = command.get('target_expected_version')
            if (not _id(target) or target == rid or old['merged_into'] is not None
                    or type(target_version) is not int or target_version < 1):
                raise WorkspaceWriteError('INVALID_REQUEST')
            other = _record(conn, target)
            if other is None or other['merged_into'] is not None or other['archived_at'] is not None:
                raise WorkspaceWriteError('INVALID_MERGE_TARGET')
            if other['version'] != target_version:
                raise WorkspaceWriteError('TARGET_VERSION_CONFLICT')
            _check_revision(conn, other)
            if conn.execute('SELECT 1 FROM work_records WHERE merged_into=? LIMIT 1', (rid,)).fetchone():
                raise WorkspaceWriteError('MERGE_CHAIN_CONFLICT')
            conn.execute('UPDATE work_records SET merged_into=? WHERE record_id=?', (target, rid))
        elif op == 'restore':
            archive, merge = command.get('restore_archive'), command.get('restore_merge')
            if type(archive) is not bool or type(merge) is not bool or not (archive or merge):
                raise WorkspaceWriteError('INVALID_REQUEST')
            if archive and old['archived_at'] is None or merge and old['merged_into'] is None:
                raise WorkspaceWriteError('INVALID_STATE')
            if archive:
                conn.execute('UPDATE work_records SET archived_at=NULL WHERE record_id=?', (rid,))
            if merge:
                conn.execute('UPDATE work_records SET merged_into=NULL WHERE record_id=?', (rid,))
        else:
            link = command.get('link_id')
            if not _id(link):
                raise WorkspaceWriteError('INVALID_REQUEST')
            if op == 'project_add':
                project, role, scope, basis = (command.get(k) for k in ('project_id', 'role', 'scope', 'basis'))
                if (not _id(project) or not _id(role) or not _text(scope, nullable=True, limit=512)
                        or basis not in ('USER_CONFIRMED', 'CANDIDATE')):
                    raise WorkspaceWriteError('INVALID_REQUEST')
                if not conn.execute('SELECT 1 FROM work_projects WHERE project_id=?', (project,)).fetchone():
                    raise WorkspaceWriteError('PROJECT_NOT_FOUND')
                if conn.execute('SELECT 1 FROM work_record_projects WHERE record_id=? AND project_id=? '
                                'AND role=? AND scope IS ? LIMIT 1', (rid, project, role, scope)).fetchone():
                    raise WorkspaceWriteError('RELATION_EXISTS')
                conn.execute('INSERT INTO work_record_projects(link_id,record_id,project_id,role,scope,basis) VALUES (?,?,?,?,?,?)',
                             (link, rid, project, role, scope, basis))
            else:
                row = conn.execute('SELECT archived_at FROM work_record_projects WHERE link_id=? AND record_id=?',
                                   (link, rid)).fetchone()
                if row is None:
                    raise WorkspaceWriteError('RELATION_NOT_FOUND')
                if op == 'project_archive' and row[0] is None:
                    conn.execute('UPDATE work_record_projects SET archived_at=CURRENT_TIMESTAMP WHERE link_id=?', (link,))
                elif op == 'project_restore' and row[0] is not None:
                    conn.execute('UPDATE work_record_projects SET archived_at=NULL WHERE link_id=?', (link,))
                else:
                    raise WorkspaceWriteError('INVALID_STATE')
