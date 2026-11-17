"""Transactional workspace changes with the original event-chain protocol.

The codec below is based on docs/publisher-reference/utt_workspace.py and its
documented historical import seed. Hash verification does not authenticate the
actor, authorization_basis, or decisions: a caller must provide a separately
authenticated direct user action. Python callbacks are not a security sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime
import hashlib
import json
import re
import sqlite3
import threading
import uuid
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
    The caller must verify any alternate implementation against original source
    before using it with production data. There is no implicit fallback codec.
    """
    def verify_history(self, events: tuple[tuple[Any, ...], ...]) -> bool: ...
    def encode(self, *, sequence: int, previous_hash: str | None,
               current_data_revision: str, operation: str, record_id: str,
               record_version: int | None, before: dict | None, after: dict,
               detail: dict, reason: str) -> EncodedEvent: ...


def _native_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _native_sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


class OriginalWorkspaceEventCodec:
    """Original non-import canonical hash and special fixed IMPORT seed.

    PROJECT_ARCHIVE/PROJECT_RESTORE are explicit extensions: details contain
    link_id and before/after rows, and are hashed by the original formula.
    """
    def verify_history(self, events):
        previous = None
        try:
            for index, row in enumerate(events):
                if len(row) != len(_EVENT_FIELDS):
                    return False
                event = dict(zip(_EVENT_FIELDS, row))
                if event['preceding_hash'] != previous:
                    return False
                if index == 0 and event['event_kind'] == 'IMPORT':
                    expected = _native_sha('initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1')
                else:
                    payload = {'id': event['event_id'], 'kind': event['event_kind'],
                               'record_id': event['record_id'], 'version': event['record_version'],
                               'actor': event['actor'], 'reason': event['reason'],
                               'details': json.loads(event['detail_json']),
                               'predecessor': event['preceding_hash'], 'at': event['occurred_at']}
                    expected = _native_sha(_native_json(payload))
                if expected != event['event_hash']:
                    return False
                previous = event['event_hash']
            return True
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False

    def encode(self, *, sequence, previous_hash, current_data_revision, operation, record_id,
               record_version, before, after, detail, reason):
        kinds = {'create': 'CREATE', 'update': 'EDIT', 'archive': 'ARCHIVE', 'merge': 'MERGE',
                 'project_add': 'PROJECT_LINK', 'project_archive': 'PROJECT_ARCHIVE',
                 'project_restore': 'PROJECT_RESTORE', 'restore_revision': 'RESTORE_REVISION',
                 'tag_add': 'TAG', 'tag_remove': 'TAG_REMOVE', 'topic_add': 'TOPIC_LINK', 'topic_remove': 'TOPIC_UNLINK'}
        if operation == 'restore':
            kind = 'UNMERGE' if detail['restore_merge'] else 'RESTORE'
        else:
            kind = kinds[operation]
        if operation == 'create':
            details = {'data_scope': 'USER_WORKSPACE'}
        elif operation == 'update':
            details = {'fields': sorted(detail['changes'])}
        elif operation in ('archive', 'merge', 'restore'):
            details = {'merged_target_present': operation == 'merge'}
        elif operation == 'project_add':
            details = {'project_id': detail['project_id'], 'link_id': detail['link_id'], 'basis': detail['basis']}
        elif operation in ('project_archive', 'project_restore'):
            details = {'link_id': detail['link_id'], 'before': detail['relation_before'], 'after': detail['relation_after']}
        elif operation == 'tag_add':
            details = {'tag_id': detail['tag_id']}
        elif operation == 'tag_remove':
            details = {'tag_id': detail['tag_id'], 'before': detail['relation_before'], 'after': detail['relation_after']}
        elif operation in ('topic_add', 'topic_remove'):
            details = {'topic_id': detail['topic_id'], 'before': detail['relation_before'], 'after': detail['relation_after']}
        else:
            details = {'restored_version': detail['restore_version']}
        event_id = 'evt-' + uuid.uuid4().hex
        at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        payload = {'id': event_id, 'kind': kind, 'record_id': record_id, 'version': record_version,
                   'actor': 'USER', 'reason': reason, 'details': details, 'predecessor': previous_hash, 'at': at}
        return EncodedEvent(event_id, kind, 'EXPLICIT_WORKSPACE_WRITE', reason, _native_json(details),
                            at, previous_hash, _native_sha(_native_json(payload)), str(int(current_data_revision) + 1))


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


def _small_link(conn, record_id, kind, identity):
    if kind == 'tag':
        fields, table, key = ('record_id', 'tag_id'), 'work_record_tags', 'tag_id'
    else:
        fields, table, key = ('record_id', 'topic_id', 'status'), 'work_record_topics', 'topic_id'
    row = conn.execute('SELECT ' + ','.join(fields) + ' FROM ' + table + ' WHERE record_id=? AND ' + key + '=?',
                       (record_id, identity)).fetchone()
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
    relation_only = operation in ('project_add', 'project_archive', 'project_restore', 'tag_add', 'tag_remove', 'topic_add', 'topic_remove')
    event_version = None if relation_only else after['version']
    try:
        event = codec.encode(sequence=sequence, previous_hash=previous,
                             current_data_revision=data_revision, operation=operation,
                             record_id=after['record_id'], record_version=event_version,
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
                 (sequence, event.event_id, event.event_kind, after['record_id'], event_version, 'USER',
                  event.authorization_basis, event.reason, event.detail_json, event.occurred_at,
                  event.preceding_hash, event.event_hash))
    changed = conn.execute("UPDATE work_meta SET value=? WHERE key='data_revision' AND value=?",
                           (event.data_revision, data_revision)).rowcount
    if changed != 1:
        raise WorkspaceWriteError('DATA_REVISION_CONFLICT')
    if not relation_only:
        conn.execute('INSERT INTO work_revisions(record_id,version,' + ','.join(_REV_FIELDS) + ',event_id,created_at) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (after['record_id'], after['version'], *(after[k] for k in _REV_FIELDS), event.event_id,
                      datetime.datetime.now(datetime.timezone.utc).isoformat()))
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
            row_factory = self._conn.row_factory
            self._conn.row_factory = None
            try:
                return self._execute(command)
            finally:
                self._conn.row_factory = row_factory

    def _execute(self, command: dict) -> WriteResult:
        if type(command) is not dict:
            raise WorkspaceWriteError('INVALID_REQUEST')
        op = command.get('operation')
        rid, reason = command.get('record_id'), command.get('reason')
        if (op not in ('create', 'update', 'archive', 'merge', 'restore', 'restore_revision', 'project_add', 'project_archive', 'project_restore', 'tag_add', 'tag_remove', 'topic_add', 'topic_remove')
                or not _id(rid) or not _id(reason)):
            raise WorkspaceWriteError('INVALID_REQUEST')
        extras = {'create': {'record'}, 'update': {'changes'}, 'archive': set(),
                  'merge': {'target_record_id', 'target_expected_version'}, 'restore': {'restore_archive', 'restore_merge'},
                  'restore_revision': {'restore_version'},
                  'project_add': {'link_id', 'project_id', 'role', 'scope', 'basis'},
                  'project_archive': {'link_id'}, 'project_restore': {'link_id'},
                  'tag_add': {'tag_id', 'tag_name'}, 'tag_remove': {'tag_id'},
                  'topic_add': {'topic_id', 'status'}, 'topic_remove': {'topic_id'}}
        if set(command) - ({'operation', 'record_id', 'reason', 'expected_version', 'expected_data_revision'} | extras[op]):
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
            expected_revision = command.get('expected_data_revision')
            if type(expected_revision) is not str or not expected_revision:
                raise WorkspaceWriteError('EXPECTED_DATA_REVISION_REQUIRED')
            if expected_revision != context[2]:
                raise WorkspaceWriteError('DATA_REVISION_CONFLICT')
            old = _record(conn, rid)
            relation_before = None
            if op in ('project_add', 'project_archive', 'project_restore') and _id(command.get('link_id')):
                relation_before = _project_link(conn, rid, command['link_id'])
            tag_id = command.get('tag_id')
            if op == 'tag_add' and _id(command.get('tag_name')):
                existing_tag = conn.execute('SELECT tag_id FROM work_tags WHERE name=? COLLATE NOCASE',
                                            (command['tag_name'].strip(),)).fetchone()
                if existing_tag:
                    tag_id = existing_tag[0]
            if op in ('tag_add', 'tag_remove') and _id(tag_id):
                relation_before = _small_link(conn, rid, 'tag', tag_id)
            if op in ('topic_add', 'topic_remove') and _id(command.get('topic_id')):
                relation_before = _small_link(conn, rid, 'topic', command['topic_id'])
            target_before = _record(conn, command.get('target_record_id')) if op == 'merge' and _id(command.get('target_record_id')) else None
            if op == 'create':
                if old is not None or command.get('expected_version') is not None:
                    raise WorkspaceWriteError('RECORD_EXISTS')
                if not re.fullmatch(r'usr-[0-9a-f]{32}', rid):
                    raise WorkspaceWriteError('INVALID_REQUEST')
                required = {'title', 'body', 'summary', 'document_role', 'review_status', 'adoption_status', 'source_kind'}
                values = command.get('record')
                if type(values) is not dict or set(values) != required:
                    raise WorkspaceWriteError('INVALID_REQUEST')
                if (not _title(values['title']) or not values['title'].strip() or not _text(values['body'], limit=16 * 1024 * 1024)
                        or not _text(values['summary'], nullable=True) or
                        any(not _id(values[k]) for k in ('document_role', 'review_status', 'adoption_status', 'source_kind'))):
                    raise WorkspaceWriteError('INVALID_REQUEST')
                # A new user-authored item must not be presented as an old
                # inherited source or an already approved/adopted decision.
                if (values['review_status'] != 'PENDING_USER_REVIEW' or values['adoption_status'] != 'NEW_WORKSPACE_RECORD'
                        or values['source_kind'] != 'USER_WORKSPACE'):
                    raise WorkspaceWriteError('NEW_RECORD_PROVENANCE_INVALID')
                now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                conn.execute('INSERT INTO work_records(record_id,title,body,body_sha256,summary,document_role,'
                             'review_status,adoption_status,decision_status,source_kind,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)',
                             (rid, values['title'].strip(), values['body'], _hash_body(values['body']), values['summary'],
                              values['document_role'], values['review_status'], values['adoption_status'],
                              'UNREVIEWED', values['source_kind'], now, now))
            else:
                version = command.get('expected_version')
                if type(version) is not int or not 1 <= version < 2**63 - 1:
                    raise WorkspaceWriteError('EXPECTED_VERSION_REQUIRED')
                if old is None:
                    raise WorkspaceWriteError('RECORD_NOT_FOUND')
                if old['version'] != version:
                    raise WorkspaceWriteError('VERSION_CONFLICT')
                _check_revision(conn, old)
                self._change(conn, old, op, command, tag_id=tag_id)
                if op not in ('project_add', 'project_archive', 'project_restore', 'tag_add', 'tag_remove', 'topic_add', 'topic_remove'):
                    updated = conn.execute('UPDATE work_records SET version=version+1,updated_at=? '
                                           'WHERE record_id=? AND version=?',
                                           (datetime.datetime.now(datetime.timezone.utc).isoformat(), rid, version)).rowcount
                    if updated != 1:
                        raise WorkspaceWriteError('VERSION_CONFLICT')
            after = _record(conn, rid)
            detail = {k: v for k, v in command.items() if k not in ('record_id', 'reason', 'expected_version', 'expected_data_revision')}
            if op in ('project_add', 'project_archive', 'project_restore'):
                detail['relation_before'] = relation_before
                detail['relation_after'] = _project_link(conn, rid, command['link_id'])
            if op in ('tag_add', 'tag_remove'):
                detail['tag_id'] = tag_id
                detail['relation_before'] = relation_before
                detail['relation_after'] = _small_link(conn, rid, 'tag', tag_id)
            if op in ('topic_add', 'topic_remove'):
                detail['relation_before'] = relation_before
                detail['relation_after'] = _small_link(conn, rid, 'topic', command['topic_id'])
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
    def _change(conn, old, op, command, *, tag_id=None):
        rid = old['record_id']
        if op == 'update':
            fields = command.get('changes')
            if type(fields) is not dict or not fields or not set(fields) <= {'title', 'body', 'summary', 'review_status'}:
                raise WorkspaceWriteError('INVALID_REQUEST')
            if (('title' in fields and (not _title(fields['title']) or not fields['title'].strip())) or
                    ('body' in fields and not _text(fields['body'], limit=16 * 1024 * 1024)) or
                    ('summary' in fields and not _text(fields['summary'], nullable=True)) or
                    ('review_status' in fields and not _id(fields['review_status']))):
                raise WorkspaceWriteError('INVALID_REQUEST')
            if all(old[k] == value for k, value in fields.items()):
                raise WorkspaceWriteError('NO_CHANGE')
            values = dict(fields)
            if 'body' in values:
                values['body_sha256'] = _hash_body(values['body'])
            conn.execute('UPDATE work_records SET ' + ','.join(k + '=?' for k in values) + ' WHERE record_id=?',
                         (*values.values(), rid))
        elif op == 'archive':
            if old['archived_at'] is not None:
                raise WorkspaceWriteError('INVALID_STATE')
            conn.execute('UPDATE work_records SET archived_at=? WHERE record_id=?',
                         (datetime.datetime.now(datetime.timezone.utc).isoformat(), rid))
        elif op == 'merge':
            target = command.get('target_record_id')
            target_version = command.get('target_expected_version')
            if (not _id(target) or target == rid or old['merged_into'] is not None or old['archived_at'] is not None
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
            conn.execute('UPDATE work_records SET merged_into=?,archived_at=? WHERE record_id=?',
                         (target, datetime.datetime.now(datetime.timezone.utc).isoformat(), rid))
        elif op == 'restore':
            archive, merge = command.get('restore_archive'), command.get('restore_merge')
            if type(archive) is not bool or type(merge) is not bool or not (archive or merge):
                raise WorkspaceWriteError('INVALID_REQUEST')
            if archive and old['archived_at'] is None or merge and old['merged_into'] is None:
                raise WorkspaceWriteError('INVALID_STATE')
            if archive and not merge and old['merged_into'] is not None:
                raise WorkspaceWriteError('INVALID_STATE')
            if archive or merge:
                conn.execute('UPDATE work_records SET archived_at=NULL WHERE record_id=?', (rid,))
            if merge:
                conn.execute('UPDATE work_records SET merged_into=NULL WHERE record_id=?', (rid,))
        elif op == 'restore_revision':
            restore_version = command.get('restore_version')
            if type(restore_version) is not int or restore_version < 1:
                raise WorkspaceWriteError('INVALID_REQUEST')
            row = conn.execute('SELECT ' + ','.join(_REV_FIELDS) + ' FROM work_revisions WHERE record_id=? AND version=?',
                               (rid, restore_version)).fetchone()
            if row is None:
                raise WorkspaceWriteError('REVISION_NOT_FOUND')
            values = dict(zip(_REV_FIELDS, row))
            if values['body_sha256'] != _hash_body(values['body']):
                raise WorkspaceWriteError('BODY_HASH_MISMATCH')
            prior_target = values['merged_into']
            if prior_target is not None:
                target_record = _record(conn, prior_target)
                if (target_record is None or target_record['archived_at'] is not None
                        or target_record['merged_into'] is not None):
                    raise WorkspaceWriteError('INVALID_MERGE_TARGET')
                if conn.execute('SELECT 1 FROM work_records WHERE merged_into=? LIMIT 1', (rid,)).fetchone():
                    raise WorkspaceWriteError('MERGE_CHAIN_CONFLICT')
            conn.execute('UPDATE work_records SET ' + ','.join(k + '=?' for k in _REV_FIELDS) + ' WHERE record_id=?',
                         (*(values[k] for k in _REV_FIELDS), rid))
        elif op in ('tag_add', 'tag_remove'):
            if not _id(tag_id):
                raise WorkspaceWriteError('INVALID_REQUEST')
            existing = _small_link(conn, rid, 'tag', tag_id)
            if op == 'tag_add':
                name = command.get('tag_name')
                if not _id(name) or not _id(command.get('tag_id')):
                    raise WorkspaceWriteError('INVALID_REQUEST')
                if existing is not None:
                    raise WorkspaceWriteError('RELATION_EXISTS')
                tag = conn.execute('SELECT name FROM work_tags WHERE tag_id=?', (tag_id,)).fetchone()
                if tag is None:
                    conn.execute('INSERT INTO work_tags(tag_id,name) VALUES (?,?)', (tag_id, name.strip()))
                elif conn.execute('SELECT 1 FROM work_tags WHERE tag_id=? AND name=? COLLATE NOCASE',
                                  (tag_id, name.strip())).fetchone() is None:
                    raise WorkspaceWriteError('TAG_ID_CONFLICT')
                conn.execute('INSERT INTO work_record_tags(record_id,tag_id) VALUES (?,?)', (rid, tag_id))
            else:
                if existing is None:
                    raise WorkspaceWriteError('RELATION_NOT_FOUND')
                conn.execute('DELETE FROM work_record_tags WHERE record_id=? AND tag_id=?', (rid, tag_id))
        elif op in ('topic_add', 'topic_remove'):
            topic_id = command.get('topic_id')
            if not _id(topic_id):
                raise WorkspaceWriteError('INVALID_REQUEST')
            existing = _small_link(conn, rid, 'topic', topic_id)
            if op == 'topic_add':
                if command.get('status') not in ('USER_CONFIRMED', 'CANDIDATE'):
                    raise WorkspaceWriteError('INVALID_REQUEST')
                if existing is not None:
                    raise WorkspaceWriteError('RELATION_EXISTS')
                if not conn.execute('SELECT 1 FROM work_topics WHERE topic_id=?', (topic_id,)).fetchone():
                    raise WorkspaceWriteError('TOPIC_NOT_FOUND')
                conn.execute('INSERT INTO work_record_topics(record_id,topic_id,status) VALUES (?,?,?)',
                             (rid, topic_id, command['status']))
            else:
                if existing is None:
                    raise WorkspaceWriteError('RELATION_NOT_FOUND')
                conn.execute('DELETE FROM work_record_topics WHERE record_id=? AND topic_id=?', (rid, topic_id))
        else:
            link = command.get('link_id')
            if not _id(link):
                raise WorkspaceWriteError('INVALID_REQUEST')
            if op == 'project_add':
                project, role, scope, basis = (command.get(k) for k in ('project_id', 'role', 'scope', 'basis'))
                if (not _id(project) or not _id(role) or not _text(scope, nullable=True, limit=512)
                        or scope is not None or basis != 'USER_CONFIRMED'):
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
