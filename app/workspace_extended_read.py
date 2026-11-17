"""Read the editable workspace's own fields without changing READ/0.1.

The caller supplies an existing SQLite connection and must serialize access to
it. No paths, network calls, schema mutation, or inferred approval occurs here.
Rows come from the documented work_* schema, not the separately sourced body.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import re
import sqlite3

VERSION = 'UTT-WORKSPACE-DETAIL/0.1'
_MAX_ROWS = 10000
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_SCHEMA = {
    'work_records': ('record_id','title','original_title','body','body_sha256','summary','document_role','original_layer','review_status','adoption_status','decision_status','source_kind','version','created_at','updated_at','archived_at','merged_into'),
    'work_revisions': ('record_id','version','title','body','body_sha256','summary','review_status','adoption_status','decision_status','archived_at','merged_into','event_id','created_at'),
    'work_events': ('sequence','event_id','event_kind','record_id','record_version','actor','authorization_basis','reason','detail_json','occurred_at','preceding_hash','event_hash'),
    'work_projects': ('project_id','name','state','provenance'),
    'work_record_projects': ('link_id','record_id','project_id','role','scope','basis','original_evidence_id','added_at','archived_at'),
    'work_sources': ('evidence_id','record_id','source_id','locator','offset_unit','validated_in_this_workspace'),
    'work_topics': ('topic_id','name','parent_id','original_axis','notes','provenance'),
    'work_record_topics': ('record_id','topic_id','status'),
    'work_tags': ('tag_id','name'),
    'work_record_tags': ('record_id','tag_id'),
    'work_notes': ('note_id','record_id','topic_id','author','note_text','state','created_at','updated_at'),
    'work_decisions': ('decision_id','record_id','decision','explanation','actor','user_authorized','evidence_message_ref','created_at'),
    'work_proposals': ('proposal_id','record_id','proposed_action','proposal_json','proposer','state','created_at','resolved_at'),
}


class _Problem(Exception):
    def __init__(self, code):
        self.code = code


def _error(code):
    return {'contract_version': VERSION, 'status': 'ERROR', 'error': {'code': code}}


def _identifier(value):
    return type(value) is str and 0 < len(value) <= 1024 and '\x00' not in value


@contextmanager
def _read_transaction(conn):
    if not isinstance(conn, sqlite3.Connection):
        raise _Problem('INVALID_REQUEST')
    owned = not conn.in_transaction
    try:
        if owned:
            conn.execute('BEGIN')
        # Establish the SQLite snapshot before issuing the individual queries.
        conn.execute('SELECT count(*) FROM sqlite_schema').fetchone()
        yield
    finally:
        if owned and conn.in_transaction:
            conn.rollback()


def _check_schema(conn, tables):
    cursor = conn.cursor()
    cursor.row_factory = None
    for table in tables:
        row = cursor.execute("SELECT type FROM sqlite_schema WHERE name=? COLLATE BINARY", (table,)).fetchone()
        if row != ('table',):
            raise _Problem('UNSUPPORTED_SCHEMA')
        columns = {r[1] for r in cursor.execute('PRAGMA table_info("' + table + '")')}
        if not set(_SCHEMA[table]) <= columns:
            raise _Problem('UNSUPPORTED_SCHEMA')


def _rows(conn, sql, args=(), *, limit=_MAX_ROWS):
    cursor = conn.cursor()
    cursor.row_factory = None
    cursor.execute(sql, args)
    keys = tuple(d[0] for d in cursor.description)
    if len(keys) != len(set(keys)):
        raise _Problem('UNSUPPORTED_SCHEMA')
    raw = cursor.fetchmany(limit + 1)
    if len(raw) > limit:
        raise _Problem('RESULT_LIMIT_EXCEEDED')
    result = []
    for row in raw:
        if any(type(value) not in (str, int, type(None)) for value in row):
            raise _Problem('UNSUPPORTED_VALUE')
        result.append(dict(zip(keys, row)))
    return result


def _read_table(conn, table, where='', args=(), order=''):
    cols = ','.join('"' + col + '"' for col in _SCHEMA[table])
    return _rows(conn, 'SELECT ' + cols + ' FROM "' + table + '"' + where + order, args)


def _joined(parent_rows, associated, key):
    lookup = {row[key]: row for row in associated}
    if len(lookup) != len(associated):
        raise _Problem('DATA_INTEGRITY')
    result = []
    for link in parent_rows:
        target = lookup.get(link[key])
        if target is None:
            raise _Problem('DATA_INTEGRITY')
        result.append({'link': link, 'project' if key == 'project_id' else 'topic': target})
    return result


def read_workspace_record(connection, record_id):
    """Read one record and all its documented workspace relationships.

    Values, NULLs, source evidence, archive/merge flags, events, decisions,
    revisions, and authorization markers are returned as stored. Empty arrays
    mean an existing table has no matching rows, not that the table is missing.
    Oversized result sets fail explicitly. The response contains user data and
    must stay in the authorized local runtime.
    """
    if not _identifier(record_id):
        return _error('INVALID_REQUEST')
    try:
        with _read_transaction(connection):
            _check_schema(connection, _SCHEMA)
            record = _read_table(connection, 'work_records', ' WHERE record_id=? COLLATE BINARY', (record_id,))
            if not record:
                raise _Problem('ITEM_NOT_FOUND')
            if len(record) != 1:
                raise _Problem('DATA_INTEGRITY')
            body, digest = record[0]['body'], record[0]['body_sha256']
            if (type(body) is not str or type(digest) is not str or not _HASH.fullmatch(digest)
                    or not hmac.compare_digest(hashlib.sha256(body.encode('utf-8')).hexdigest(), digest)):
                raise _Problem('CONTENT_MISMATCH')
            project_links = _read_table(connection, 'work_record_projects', ' WHERE record_id=? COLLATE BINARY', (record_id,), ' ORDER BY link_id COLLATE BINARY')
            topic_links = _read_table(connection, 'work_record_topics', ' WHERE record_id=? COLLATE BINARY', (record_id,), ' ORDER BY topic_id COLLATE BINARY')
            # Fetch only rows associated with this record, preserving every link.
            projects = _read_table(connection, 'work_projects', ' WHERE project_id IN (SELECT project_id FROM work_record_projects WHERE record_id=? COLLATE BINARY)', (record_id,))
            topics = _read_table(connection, 'work_topics', ' WHERE topic_id IN (SELECT topic_id FROM work_record_topics WHERE record_id=? COLLATE BINARY)', (record_id,))
            tag_links = _read_table(connection, 'work_record_tags', ' WHERE record_id=? COLLATE BINARY', (record_id,), ' ORDER BY tag_id COLLATE BINARY')
            tags = _read_table(connection, 'work_tags', ' WHERE tag_id IN (SELECT tag_id FROM work_record_tags WHERE record_id=? COLLATE BINARY)', (record_id,), ' ORDER BY tag_id COLLATE BINARY')
            if {x['tag_id'] for x in tag_links} != {x['tag_id'] for x in tags}:
                raise _Problem('DATA_INTEGRITY')
            result = {'contract_version': VERSION, 'status': 'OK', 'record': record[0],
                      'projects': _joined(project_links, projects, 'project_id'),
                      'topics': _joined(topic_links, topics, 'topic_id'),
                      'tags': tags}
            for key, table, order in (
                ('sources','work_sources','evidence_id COLLATE BINARY'),
                ('decisions','work_decisions','created_at DESC, decision_id COLLATE BINARY'),
                ('revisions','work_revisions','version DESC'),
                ('events','work_events','sequence DESC'),
                ('notes','work_notes','created_at DESC, note_id COLLATE BINARY'),
                ('proposals','work_proposals','created_at DESC, proposal_id COLLATE BINARY'),
            ):
                result[key] = _read_table(connection, table, ' WHERE record_id=? COLLATE BINARY', (record_id,), ' ORDER BY ' + order)
            result['merged_children'] = _rows(connection,
                'SELECT record_id,title,version,archived_at,merged_into FROM work_records '
                'WHERE merged_into=? COLLATE BINARY ORDER BY record_id COLLATE BINARY', (record_id,))
            return result
    except _Problem as exc:
        return _error(exc.code)
    except (sqlite3.Error, UnicodeError, TypeError, ValueError):
        return _error('READ_FAILED')


def list_workspace_catalog(connection, kind, *, limit=100, offset=0):
    """Page actual project or topic records; callers bind pages to a snapshot.

    On a mutable connection, separate calls can observe different snapshots;
    a caller requiring stable pagination must hold a read transaction or use a
    verified immutable snapshot. This function does not invent a cursor.
    """
    mapping = {'projects': ('work_projects', 'project_id'), 'topics': ('work_topics', 'topic_id')}
    if (type(kind) is not str or kind not in mapping or type(limit) is not int or not 1 <= limit <= 1000
            or type(offset) is not int or not 0 <= offset <= 2**31 - 1):
        return _error('INVALID_REQUEST')
    try:
        with _read_transaction(connection):
            table, ident = mapping[kind]
            _check_schema(connection, (table,))
            cols = ','.join('"' + col + '"' for col in _SCHEMA[table])
            rows = _rows(connection, 'SELECT ' + cols + ' FROM "' + table + '" ORDER BY "' + ident
                         + '" COLLATE BINARY LIMIT ? OFFSET ?', (limit + 1, offset), limit=limit + 1)
            more = len(rows) > limit
            return {'contract_version': VERSION, 'status': 'OK', 'kind': kind,
                    'items': rows[:limit], 'next_offset': offset + limit if more else None}
    except _Problem as exc:
        return _error(exc.code)
    except (sqlite3.Error, UnicodeError, TypeError, ValueError):
        return _error('READ_FAILED')


def list_workspace_members(connection, kind, identifier, *, limit=100, offset=0):
    """Page the stored links and record summaries for one project or topic.

    Archived links/records and their basis or status are retained. As with the
    catalog API, callers must bind all pages to the same SQLite snapshot.
    """
    mapping = {
        'project': ('work_projects', 'project_id', 'work_record_projects', 'link_id'),
        'topic': ('work_topics', 'topic_id', 'work_record_topics', 'record_id'),
    }
    if (type(kind) is not str or kind not in mapping or not _identifier(identifier)
            or type(limit) is not int or not 1 <= limit <= 1000
            or type(offset) is not int or not 0 <= offset <= 2**31 - 1):
        return _error('INVALID_REQUEST')
    try:
        with _read_transaction(connection):
            table, key, links_table, order = mapping[kind]
            _check_schema(connection, (table, links_table, 'work_records'))
            if not _read_table(connection, table, ' WHERE "' + key + '"=? COLLATE BINARY', (identifier,)):
                raise _Problem('ITEM_NOT_FOUND')
            cols = ','.join('"' + col + '"' for col in _SCHEMA[links_table])
            links = _rows(connection, 'SELECT ' + cols + ' FROM "' + links_table + '" WHERE "' + key
                          + '"=? COLLATE BINARY ORDER BY "' + order + '" COLLATE BINARY LIMIT ? OFFSET ?',
                          (identifier, limit + 1, offset), limit=limit + 1)
            more = len(links) > limit
            items = []
            for link in links[:limit]:
                records = _rows(connection, 'SELECT record_id,title,version,archived_at,merged_into '
                                'FROM work_records WHERE record_id=? COLLATE BINARY', (link['record_id'],))
                if len(records) != 1:
                    raise _Problem('DATA_INTEGRITY')
                items.append({'link': link, 'record': records[0]})
            return {'contract_version': VERSION, 'status': 'OK', 'kind': kind,
                    'items': items, 'next_offset': offset + limit if more else None}
    except _Problem as exc:
        return _error(exc.code)
    except (sqlite3.Error, UnicodeError, TypeError, ValueError):
        return _error('READ_FAILED')
