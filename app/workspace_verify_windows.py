"""Locally verify a user's workspace snapshot without contacting Dropbox.

Only fixed PASS/FAIL indicators and aggregate counts are printed. Input paths,
record identifiers, content and exceptions stay in the local process. This is
a coverage and consistency check; it does not verify external source truth,
approve decisions, prove OS isolation, or publish a Dropbox generation.
"""
from __future__ import annotations

from contextlib import closing
import getpass
import sys
import warnings

from app.publisher_pipeline import _preflight_reader
from app.publisher_workspace import snapshot_workspace
from app.workspace_extended_read import (list_workspace_catalog, list_workspace_members,
                                         read_workspace_record)


class _VerificationFailure(Exception):
    pass


_DIRECT = {
    'record_project_links': ('projects', 'work_record_projects'),
    'record_topic_links': ('topics', 'work_record_topics'),
    'source_evidence': ('sources', 'work_sources'),
    'record_decisions': ('decisions', 'work_decisions'),
    'record_revisions': ('revisions', 'work_revisions'),
    'record_events': ('events', 'work_events'),
    'record_notes': ('notes', 'work_notes'),
    'record_tags': ('tags', 'work_record_tags'),
    'record_proposals': ('proposals', 'work_proposals'),
}


def _count(conn, table, *, record_scoped=False):
    # Table names are exclusively fixed constants from this module.
    sql = 'SELECT count(*) FROM "' + table + '"'
    if record_scoped:
        sql += ' WHERE record_id IS NOT NULL'
    row = conn.execute(sql).fetchone()
    if row is None or type(row[0]) is not int or row[0] < 0:
        raise _VerificationFailure()
    return row[0]


def _catalog_and_members(conn, catalog, kind):
    offset = 0
    known = set()
    total_members = 0
    while True:
        page = list_workspace_catalog(conn, catalog, limit=1000, offset=offset)
        if page.get('status') != 'OK' or type(page.get('items')) is not list:
            raise _VerificationFailure()
        for item in page['items']:
            key = item['project_id' if kind == 'project' else 'topic_id']
            if type(key) is not str or not key or key in known:
                raise _VerificationFailure()
            known.add(key)
            child_offset = 0
            while True:
                links = list_workspace_members(conn, kind, key, limit=1000, offset=child_offset)
                if links.get('status') != 'OK' or type(links.get('items')) is not list:
                    raise _VerificationFailure()
                for item in links['items']:
                    link, record = item.get('link'), item.get('record')
                    foreign = 'project_id' if kind == 'project' else 'topic_id'
                    if (type(link) is not dict or type(record) is not dict or link.get(foreign) != key
                            or link.get('record_id') != record.get('record_id')):
                        raise _VerificationFailure()
                total_members += len(links['items'])
                new_offset = links.get('next_offset')
                if new_offset is None:
                    break
                if type(new_offset) is not int or new_offset <= child_offset:
                    raise _VerificationFailure()
                child_offset = new_offset
        new_offset = page.get('next_offset')
        if new_offset is None:
            break
        if type(new_offset) is not int or new_offset <= offset:
            raise _VerificationFailure()
        offset = new_offset
    return len(known), total_members


def verify_snapshot(snapshot):
    """Return only aggregate counts after checking every captured record.

    Snapshot provenance and runtime isolation are caller responsibilities. The
    reader's own complete search/detail preflight is run first. The additional
    pass checks workspace links, sources and record-associated history against
    the same fixed SQLite image. It does not inspect a live Dropbox account.
    """
    snapshot.assert_source_unchanged()
    _preflight_reader(snapshot)
    with closing(snapshot.open_readonly()) as conn:
        counts = {key: 0 for key in _DIRECT}
        counts.update(records=0, merged_children=0)
        cursor = conn.cursor()
        cursor.row_factory = None
        for (ident,) in cursor.execute('SELECT record_id FROM work_records ORDER BY record_id COLLATE BINARY'):
            detail = read_workspace_record(conn, ident)
            record = detail.get('record')
            if (detail.get('status') != 'OK' or type(record) is not dict
                    or record.get('record_id') != ident):
                raise _VerificationFailure()
            counts['records'] += 1
            for name, (field, _) in _DIRECT.items():
                entries = detail.get(field)
                if type(entries) is not list:
                    raise _VerificationFailure()
                counts[name] += len(entries)
            children = detail.get('merged_children')
            if type(children) is not list:
                raise _VerificationFailure()
            counts['merged_children'] += len(children)
        if counts['records'] != _count(conn, 'work_records'):
            raise _VerificationFailure()
        for name, (_, table) in _DIRECT.items():
            if counts[name] != _count(conn, table, record_scoped=True):
                raise _VerificationFailure()
        merged = conn.execute('SELECT count(*) FROM work_records WHERE merged_into IS NOT NULL').fetchone()
        if merged is None or merged[0] != counts['merged_children']:
            raise _VerificationFailure()
        counts['projects'], project_links = _catalog_and_members(conn, 'projects', 'project')
        counts['topics'], topic_links = _catalog_and_members(conn, 'topics', 'topic')
        if (counts['projects'] != _count(conn, 'work_projects')
                or counts['topics'] != _count(conn, 'work_topics')
                or project_links != counts['record_project_links']
                or topic_links != counts['record_topic_links']):
            raise _VerificationFailure()
    snapshot.assert_source_unchanged()
    snapshot.assert_snapshot_unchanged()
    return counts


def _secret(prompt):
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        return getpass.getpass(prompt)


def main():
    if sys.platform != 'win32':
        print('FAIL')
        return 2
    try:
        source = _secret('Local workspace SQLite path (hidden): ')
        directory = _secret('Protected local temporary directory (hidden): ')
        if not source or not directory:
            print('FAIL')
            return 2
        with snapshot_workspace(source, directory=directory) as snapshot:
            counts = verify_snapshot(snapshot)
        # The output key list is fixed, and values are verified integers only.
        for key in ('records', 'projects', 'topics', 'record_project_links',
                    'record_topic_links', 'source_evidence', 'record_decisions',
                    'record_revisions', 'record_events', 'record_notes',
                    'record_tags', 'record_proposals', 'merged_children'):
            print('PASS', key, counts[key])
        return 0
    except (Exception, KeyboardInterrupt):
        # Do not print even trusted-looking exception strings: a filesystem or
        # SQLite error can contain a user path, identifier or body fragment.
        print('FAIL')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
