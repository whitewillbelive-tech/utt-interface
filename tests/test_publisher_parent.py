"""Parent comparison exercises invented in-memory SQLite and gzip only."""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.publisher_parent import _Problem, _local_row, _parent_row, compare_parent_tables


def enc(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class ParentComparisonTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.sql = 'CREATE TABLE records (value TEXT, number INTEGER)'
        self.db.execute(self.sql)
        self.db.executemany('INSERT INTO records VALUES (?,?)', [('가상', 2**60), ('가상', 2**60), (None, -2)])
        self.db.commit()
        self.db.execute('PRAGMA query_only=ON')
        self.db.execute('BEGIN')
        self.addCleanup(self.db.close)
        self.rows = [
            [['text', '가상'], ['integer', str(2**60)]],
            [['text', '가상'], ['integer', str(2**60)]],
            [['null', None], ['integer', '-2']],
        ]

    def input_for(self, rows=None, *, rowid=False):
        records = self.rows if rows is None else rows
        raw = b''.join(enc(row) + b'\n' for row in records)
        compressed = gzip.compress(raw, mtime=0)
        path = 'snapshots/tables/fictional-0.jsonl.gz'
        shard = {'path': path, 'bytes': len(compressed), 'raw_bytes': len(raw), 'rows': len(records),
                 'sha256': sha(compressed), 'raw_sha256': sha(raw)}
        table = {'name': 'records', 'columns': ['value', 'number'], 'count': len(records),
                 'rowid': rowid, 'foreign_keys': [], 'sql': self.sql,
                 'rows_sha256': '0' * 64, 'shards': [shard]}
        catalog = enc([table])
        manifest = {'snapshots/tables.json': {'bytes': len(catalog), 'sha256': sha(catalog)},
                    path: {'bytes': len(compressed), 'sha256': sha(compressed)}}
        def open_shard(requested):
            self.assertEqual(requested, path)
            return io.BytesIO(compressed)
        return catalog, manifest, open_shard

    def check(self, data=None, *, conn=None):
        args = self.input_for() if data is None else data
        return compare_parent_tables(self.db if conn is None else conn, *args)

    def test_exact_parent_multiset_is_only_structural(self):
        result = self.check()
        self.assertEqual((result.status, result.code), ('PARENT_ROWS_PRESENT', 'SEMANTIC_MERGE_UNVERIFIED'))
        self.assertTrue(self.db.in_transaction)

    def test_duplicate_parent_occurrences_are_not_deduplicated(self):
        more = self.rows + [copy.deepcopy(self.rows[0])]
        result = self.check(self.input_for(more))
        self.assertEqual((result.status, result.code), ('NOT_VERIFIED', 'PARENT_ROW_CONFLICT'))

    def test_extra_workspace_row_does_not_erase_parent(self):
        parent = [self.rows[0], self.rows[2]]
        self.assertEqual(self.check(self.input_for(parent)).status, 'PARENT_ROWS_PRESENT')

    def test_type_distinction_and_signed_64bit_are_preserved(self):
        wrong = copy.deepcopy(self.rows)
        wrong[0][1] = ['text', str(2**60)]
        self.assertEqual(self.check(self.input_for(wrong)).code, 'PARENT_ROW_CONFLICT')
        invalid = copy.deepcopy(self.rows)
        invalid[0][1] = ['integer', str(2**63)]
        self.assertNotEqual(self.check(self.input_for(invalid)).status, 'PARENT_ROWS_PRESENT')

    def test_unverified_blob_remains_unsupported_and_invalid_real_is_rejected(self):
        bad = copy.deepcopy(self.rows)
        bad[0][0] = ['blob', 'not-interpreted']
        self.assertEqual(self.check(self.input_for(bad)).code, 'UNSUPPORTED_CELL_ENCODING')
        bad[0][0] = ['real', 'not-interpreted']
        self.assertEqual(self.check(self.input_for(bad)).code, 'INVALID_TYPED_CELL')

    def test_catalog_and_shards_are_bound_to_manifest(self):
        catalog, manifest, opener = self.input_for()
        changed = copy.deepcopy(manifest)
        changed['snapshots/tables.json']['sha256'] = '0' * 64
        self.assertEqual(self.check((catalog, changed, opener)).code, 'MANIFEST_MISMATCH')
        changed = copy.deepcopy(manifest)
        changed['snapshots/tables/fictional-0.jsonl.gz']['bytes'] += 1
        self.assertEqual(self.check((catalog, changed, opener)).code, 'MANIFEST_MISMATCH')

    def test_corrupt_compressed_bytes_and_missing_source_fail_closed(self):
        catalog, manifest, _ = self.input_for()
        def corrupt(_):
            return io.BytesIO(b'bad')
        self.assertEqual(self.check((catalog, manifest, corrupt)).status, 'NOT_VERIFIED')
        def unavailable(_):
            raise OSError('fictional-secret-path')
        result = self.check((catalog, manifest, unavailable))
        self.assertEqual(result.status, 'NOT_VERIFIED')
        self.assertNotIn('fictional-secret-path', repr(result))

    def test_missing_or_different_table_schema_is_not_success(self):
        catalog, manifest, opener = self.input_for()
        tables = json.loads(catalog)
        tables[0]['sql'] += ' -- altered'
        catalog = enc(tables)
        manifest['snapshots/tables.json'] = {'bytes': len(catalog), 'sha256': sha(catalog)}
        self.assertEqual(self.check((catalog, manifest, opener)).code, 'TABLE_SCHEMA_MISMATCH')

    def test_unpinned_or_writable_connection_is_rejected_before_source_read(self):
        other = sqlite3.connect(':memory:')
        self.addCleanup(other.close)
        def forbidden(_):
            self.fail('source accessed')
        catalog, manifest, _ = self.input_for()
        self.assertEqual(self.check((catalog, manifest, forbidden), conn=other).code, 'SNAPSHOT_REQUIRED')
        other.execute('BEGIN')
        self.assertEqual(self.check((catalog, manifest, forbidden), conn=other).code, 'SNAPSHOT_REQUIRED')

    def test_scrivener_and_traversal_paths_are_never_opened(self):
        for bad in ('snapshots/../fictional', 'snapshots/scrivener/fictional'):
            with self.subTest(path=bad):
                catalog, manifest, _ = self.input_for()
                tables = json.loads(catalog)
                tables[0]['shards'][0]['path'] = bad
                catalog = enc(tables)
                manifest['snapshots/tables.json'] = {'bytes': len(catalog), 'sha256': sha(catalog)}
                def forbidden(_):
                    self.fail('unexpected source access')
                self.assertEqual(self.check((catalog, manifest, forbidden)).code, 'INVALID_SHARD')

    def test_file_and_network_are_not_opened_by_helper(self):
        with patch('builtins.open', side_effect=AssertionError('filesystem forbidden')):
            self.assertEqual(self.check().status, 'PARENT_ROWS_PRESENT')

    def test_documented_fts_shadow_exception_requires_logical_fts_comparison(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        try:
            db.execute('CREATE VIRTUAL TABLE item_fts USING fts5(body)')
        except sqlite3.OperationalError:
            self.skipTest('SQLite build lacks FTS5')
        db.execute('INSERT INTO item_fts(body) VALUES (?)', ('fictional logical row',))
        db.commit()
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')

        def descriptor(name, rows):
            sql = db.execute('SELECT sql FROM sqlite_schema WHERE name=?', (name,)).fetchone()[0]
            cols = [r[1] for r in db.execute('PRAGMA table_info("' + name + '")')]
            raw = b''.join(enc(row) + b'\n' for row in rows)
            blob = gzip.compress(raw, mtime=0)
            path = 'snapshots/tables/fictional-' + name + '.gz'
            shard = {'path': path, 'bytes': len(blob), 'raw_bytes': len(raw), 'rows': len(rows),
                     'sha256': sha(blob), 'raw_sha256': sha(raw)}
            return {'name': name, 'columns': cols, 'count': len(rows), 'rowid': False,
                    'sql': sql, 'shards': [shard]}, path, blob

        logical = descriptor('item_fts', [[['text', 'fictional logical row']]])
        # The historical physical layout may differ: preserve its compressed
        # bytes separately, but never guess its blob representation here.
        shadow = descriptor('item_fts_data', [[['integer', '1'], ['blob', 'opaque-fake']]])
        blobs = {logical[1]: logical[2], shadow[1]: shadow[2]}
        def package(tables):
            catalog = enc(tables)
            manifest = {'snapshots/tables.json': {'bytes': len(catalog), 'sha256': sha(catalog)}}
            manifest.update({path: {'bytes': len(blob), 'sha256': sha(blob)} for path, blob in blobs.items()})
            return catalog, manifest, lambda path: io.BytesIO(blobs[path])

        result = compare_parent_tables(db, *package([logical[0], shadow[0]]))
        self.assertEqual((result.status, result.code), ('PARENT_LOGICAL_ROWS_PRESENT', 'FTS_SHADOWS_NOT_COMPARED'))
        missing = compare_parent_tables(db, *package([shadow[0]]))
        self.assertEqual(missing.code, 'FTS_LOGICAL_MISSING')
        # A changed logical row cannot be hidden by the physical-shadow exception.
        changed = descriptor('item_fts', [[['text', 'different fictional row']]])
        blobs[changed[1]] = changed[2]
        conflict = compare_parent_tables(db, *package([changed[0], shadow[0]]))
        self.assertEqual(conflict.code, 'PARENT_ROW_CONFLICT')


class NativeRealComparisonTests(unittest.TestCase):
    def test_float_hex_is_compared_as_binary64_with_real_type(self):
        for value in (1.5, -0.0, 0.0, -3.25, float.fromhex('0x0.0000000000001p-1022'),
                      float.fromhex('0x1.fffffffffffffp+1023')):
            with self.subTest(value=value.hex()):
                self.assertEqual(_parent_row([['real', value.hex()]], 1), _local_row((value,)))
        self.assertNotEqual(_parent_row([['real', '0x1.8000000000000p+0']], 1),
                            _local_row((math.nextafter(1.5, math.inf),)))
        self.assertNotEqual(_parent_row([['real', '0x1.0000000000000p+0']], 1), _local_row((1,)))
        self.assertNotEqual(_parent_row([['real', '-0x0.0p+0']], 1), _local_row((0.0,)))

    def test_invalid_nonfinite_overflow_and_blob_fail_closed(self):
        for value in ('nan', 'inf', '-inf', '1.5', '0x1p+9999', '0x1p', '', None,
                      1.5, '0x' + '1' * 129 + 'p+1'):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(_Problem):
                    _parent_row([['real', value]], 1)
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(_Problem):
                    _local_row((value,))
        with self.assertRaises(_Problem) as exc:
            _parent_row([['blob', 'fictional-encoding']], 1)
        self.assertEqual(exc.exception.code, 'UNSUPPORTED_CELL_ENCODING')

    def test_real_rows_preserve_rowid_occurrences_and_reject_wrong_value(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        sql = 'CREATE TABLE sample (value)'
        db.execute(sql)
        db.executemany('INSERT INTO sample VALUES (?)', [(1.5,), (1.5,), (1,)])
        db.commit()
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')

        def compare(rows):
            raw = b''.join(enc(row) + b'\n' for row in rows)
            compressed = gzip.compress(raw, mtime=0)
            path = 'snapshots/tables/fictional-real.gz'
            shard = {'path': path, 'bytes': len(compressed), 'raw_bytes': len(raw), 'rows': len(rows),
                     'sha256': sha(compressed), 'raw_sha256': sha(raw)}
            catalog = enc([{'name': 'sample', 'columns': ['value'], 'count': len(rows),
                            'rowid': True, 'sql': sql, 'shards': [shard]}])
            files = {'snapshots/tables.json': {'bytes': len(catalog), 'sha256': sha(catalog)},
                     path: {'bytes': len(compressed), 'sha256': sha(compressed)}}
            return compare_parent_tables(db, catalog, files, lambda _: io.BytesIO(compressed))

        rows = [[['integer', '1'], ['real', '0x1.8000000000000p+0']],
                [['integer', '2'], ['real', '0x1.8000000000000p+0']],
                [['integer', '3'], ['integer', '1']]]
        self.assertEqual(compare(rows).status, 'PARENT_ROWS_PRESENT')
        wrong = copy.deepcopy(rows)
        wrong[0][1] = ['real', '0x1.8000000000001p+0']
        self.assertEqual(compare(wrong).code, 'PARENT_ROW_CONFLICT')
        wrong = copy.deepcopy(rows)
        wrong[2][1] = ['real', '0x1.0000000000000p+0']
        self.assertEqual(compare(wrong).code, 'PARENT_ROW_CONFLICT')
        wrong = copy.deepcopy(rows)
        wrong[0][0] = ['integer', '2']
        self.assertEqual(compare(wrong).code, 'PARENT_ROW_CONFLICT')

    def test_fictional_69_tables_86868_rows_with_1518_real_cells(self):
        # The scale matches the independently reported historical comparison;
        # the data below are entirely synthetic and prove no production match.
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        tables = []
        shards = {}
        total = real_total = 0
        for index in range(69):
            name = 'fictional_' + str(index)
            sql = f'CREATE TABLE "{name}" (n INTEGER, value)'
            db.execute(sql)
            count = 1259 if index < 66 else 1258
            rows = [(n, float(n) + 0.25 if n < 22 else None) for n in range(count)]
            db.executemany(f'INSERT INTO "{name}" VALUES (?,?)', rows)
            native = [ [['integer', str(n)], ['real', value.hex()] if value is not None else ['null', None]]
                       for n, value in rows ]
            raw = b''.join(enc(row) + b'\n' for row in native)
            compressed = gzip.compress(raw, mtime=0)
            path = f'snapshots/tables/fictional-{index}.jsonl.gz'
            shards[path] = compressed
            shard = {'path': path, 'bytes': len(compressed), 'raw_bytes': len(raw), 'rows': count,
                     'sha256': sha(compressed), 'raw_sha256': sha(raw)}
            tables.append({'name': name, 'columns': ['n', 'value'], 'count': count,
                           'rowid': False, 'sql': sql, 'shards': [shard]})
            total += count
            real_total += 22
        self.assertEqual((len(tables), total, real_total), (69, 86868, 1518))
        db.commit()
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        catalog = enc(tables)
        files = {'snapshots/tables.json': {'bytes': len(catalog), 'sha256': sha(catalog)}}
        files.update({path: {'bytes': len(data), 'sha256': sha(data)} for path, data in shards.items()})
        result = compare_parent_tables(db, catalog, files, lambda path: io.BytesIO(shards[path]))
        self.assertEqual((result.status, result.code), ('PARENT_ROWS_PRESENT', 'SEMANTIC_MERGE_UNVERIFIED'))


class WorkspaceHistoryTests(unittest.TestCase):
    def setUp(self):
        self.parent = sqlite3.connect(':memory:')
        self.parent.executescript('''
            CREATE TABLE work_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE work_records (record_id TEXT PRIMARY KEY, title TEXT, original_title TEXT,
                body TEXT, body_sha256 TEXT, summary TEXT, document_role TEXT, original_layer TEXT,
                review_status TEXT, adoption_status TEXT, decision_status TEXT, source_kind TEXT,
                version INTEGER, created_at TEXT, updated_at TEXT, archived_at TEXT, merged_into TEXT);
            CREATE TABLE work_revisions (record_id TEXT, version INTEGER, title TEXT, body TEXT,
                body_sha256 TEXT, summary TEXT, review_status TEXT, adoption_status TEXT,
                decision_status TEXT, archived_at TEXT, merged_into TEXT, event_id TEXT, PRIMARY KEY(record_id,version));
            CREATE TABLE work_events (sequence INTEGER PRIMARY KEY, event_id TEXT UNIQUE, event_kind TEXT,
                record_id TEXT, record_version INTEGER, actor TEXT, authorization_basis TEXT, reason TEXT,
                detail_json TEXT, occurred_at TEXT, preceding_hash TEXT, event_hash TEXT);
            CREATE TABLE work_record_projects (link_id TEXT PRIMARY KEY, record_id TEXT, project_id TEXT,
                role TEXT, scope TEXT, basis TEXT, original_evidence_id TEXT, added_at TEXT, archived_at TEXT);
            CREATE TABLE work_record_tags (record_id TEXT,tag_id TEXT,PRIMARY KEY(record_id,tag_id));
            CREATE TABLE work_record_topics (record_id TEXT,topic_id TEXT,status TEXT,PRIMARY KEY(record_id,topic_id));
            CREATE TABLE retained (payload BLOB, number REAL);
            INSERT INTO work_meta VALUES ('data_revision','1');
            INSERT INTO work_meta VALUES ('baseline','fictional');
            INSERT INTO work_records VALUES ('fictional','title','original','body','hash','summary',
                'role','layer','unreviewed','unknown','unknown','synthetic',1,'created','updated',NULL,NULL);
            INSERT INTO work_revisions VALUES ('fictional',1,'title','body','hash','summary',
                'unreviewed','unknown','unknown',NULL,NULL,'initial-historical-b-import');
            INSERT INTO work_record_projects VALUES ('fictional-link','fictional','fictional-project',
                'RELATED',NULL,'INHERITED_CLASSIFICATION_ONLY',NULL,'fictional-time',NULL);
            INSERT INTO work_record_tags VALUES ('fictional','fictional-tag');
            INSERT INTO work_record_topics VALUES ('fictional','fictional-topic','INHERITED');
            INSERT INTO retained VALUES (X'0001',1.5);
            INSERT INTO retained VALUES (X'0001',1.5);
        ''')
        seed = hashlib.sha256(b'initial-historical-b-import|UTT-INTEGRATED-WORKSPACE/0.1').hexdigest()
        self.parent.execute('INSERT INTO work_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (1, 'initial-historical-b-import', 'IMPORT', None, None, 'SYSTEM',
                             'USER_DIRECTED_DB_UPGRADE', 'fictional', '{}', 'fictional-time', None, seed))
        self.parent.commit()
        self.candidate = sqlite3.connect(':memory:')
        self.parent.backup(self.candidate)
        self.addCleanup(self.parent.close)
        self.addCleanup(self.candidate.close)

    def check(self):
        from app.publisher_parent import compare_workspace_snapshots
        self.parent.commit()
        self.candidate.commit()
        for conn in (self.parent, self.candidate):
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
        return compare_workspace_snapshots(self.parent, self.candidate)

    def add_event(self, *, kind, record_id, record_version, details, actor='USER'):
        previous = self.candidate.execute('SELECT sequence,event_hash FROM work_events ORDER BY sequence DESC LIMIT 1').fetchone()
        sequence = previous[0] + 1
        event_id = 'fictional-event-' + str(sequence)
        payload = {'id': event_id, 'kind': kind, 'record_id': record_id, 'version': record_version,
                   'actor': actor, 'reason': 'fictional', 'details': details,
                   'predecessor': previous[1], 'at': 'fictional-time'}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        digest = hashlib.sha256(raw.encode()).hexdigest()
        self.candidate.execute('INSERT INTO work_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                               (sequence, event_id, kind, record_id, record_version, actor, 'EXPLICIT_WORKSPACE_WRITE',
                                'fictional', json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                                'fictional-time', previous[1], digest))
        self.candidate.execute("UPDATE work_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) WHERE key='data_revision'")
        return event_id

    def edit(self, *, include_old=True, include_current=True, version=2):
        self.candidate.execute("UPDATE work_records SET title='new',version=?,updated_at='later'", (version,))
        if not include_old:
            self.candidate.execute('DELETE FROM work_revisions')
        event_id = self.add_event(kind='EDIT', record_id='fictional', record_version=version, details={'fields': ['title']})
        if include_current:
            self.candidate.execute("INSERT INTO work_revisions VALUES ('fictional',?,'new','body','hash','summary', 'unreviewed','unknown','unknown',NULL,NULL,?)", (version, event_id))

    def test_unchanged_preserves_all_rows(self):
        result = self.check()
        self.assertEqual((result.status, result.code), ('WORKSPACE_HISTORY_PRESERVED', 'SEMANTICS_UNVERIFIED'))

    def test_versioned_edit_preserves_previous_and_current_revisions(self):
        self.edit()
        result = self.check()
        self.assertEqual((result.status, result.code), ('WORKSPACE_HISTORY_PRESERVED', 'TARGET_CHANGES_SEMANTICS_UNVERIFIED'))

    def test_deleted_record_is_rejected(self):
        self.candidate.execute('DELETE FROM work_records')
        self.assertEqual(self.check().code, 'WORK_RECORD_DELETED')

    def test_unversioned_edit_is_rejected(self):
        self.candidate.execute("UPDATE work_records SET title='new'")
        self.assertEqual(self.check().code, 'WORK_VERSION_CONFLICT')

    def test_missing_prior_or_current_revision_is_rejected(self):
        self.edit(include_old=False)
        self.assertEqual(self.check().code, 'WORK_REVISION_MISMATCH')

    def test_revision_gap_is_rejected(self):
        self.edit(version=3)
        self.assertEqual(self.check().code, 'WORK_REVISION_GAP')

    def test_untracked_original_field_edit_is_rejected(self):
        self.edit()
        self.candidate.execute("UPDATE work_records SET original_title='changed'")
        self.assertEqual(self.check().code, 'WORK_UNTRACKED_CHANGE')

    def test_event_or_duplicate_historical_rows_may_not_disappear(self):
        self.candidate.execute('DELETE FROM work_events')
        self.assertEqual(self.check().code, 'EVENT_HISTORY_CONFLICT')

    def test_duplicate_occurrences_and_blob_real_are_compared(self):
        self.candidate.execute('DELETE FROM retained WHERE rowid=1')
        self.assertEqual(self.check().code, 'PARENT_ROW_CONFLICT')

    def test_baseline_metadata_cannot_change(self):
        self.candidate.execute("UPDATE work_meta SET value='other' WHERE key='baseline'")
        self.assertEqual(self.check().code, 'WORK_META_CONFLICT')

    def test_new_table_is_reported_as_unverified_change(self):
        self.candidate.execute('CREATE TABLE another_table(x)')
        self.assertEqual(self.check().code, 'TARGET_CHANGES_SEMANTICS_UNVERIFIED')

    def test_connection_must_have_query_only_transaction(self):
        from app.publisher_parent import compare_workspace_snapshots
        self.assertEqual(compare_workspace_snapshots(self.parent, self.candidate).code, 'SNAPSHOT_REQUIRED')

    def test_parent_foreign_key_corruption_fails_before_comparison(self):
        for conn in (self.parent, self.candidate):
            conn.execute('CREATE TABLE related_record(id TEXT REFERENCES work_records(record_id))')
        self.parent.execute("INSERT INTO related_record VALUES ('fictional-missing')")
        self.assertEqual(self.check().code, 'FOREIGN_KEY_CONFLICT')

    def test_parent_sqlite_integrity_failure_is_distinct(self):
        for conn in (self.parent, self.candidate):
            conn.execute('CREATE TABLE constrained_record(value INTEGER CHECK(value > 0))')
        self.parent.execute('PRAGMA ignore_check_constraints=ON')
        self.parent.execute('INSERT INTO constrained_record VALUES (-1)')
        self.parent.execute('PRAGMA ignore_check_constraints=OFF')
        self.assertEqual(self.check().code, 'SQLITE_INTEGRITY_CONFLICT')

    def test_old_view_and_trigger_are_preserved(self):
        for conn in (self.parent, self.candidate):
            conn.execute('CREATE VIEW example_view AS SELECT payload FROM retained')
        self.candidate.execute('DROP VIEW example_view')
        self.assertEqual(self.check().code, 'OBJECT_SCHEMA_MISMATCH')

    def test_work_fts_logical_content_tracks_edited_record(self):
        for conn in (self.parent, self.candidate):
            conn.execute('CREATE VIRTUAL TABLE work_fts USING fts5(record_id UNINDEXED,title,body)')
            conn.execute('INSERT INTO work_fts SELECT record_id,title,body FROM work_records')
        self.edit()
        self.candidate.execute('DELETE FROM work_fts')
        self.candidate.execute('INSERT INTO work_fts SELECT record_id,title,body FROM work_records')
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_work_fts_inconsistent_is_rejected(self):
        for conn in (self.parent, self.candidate):
            conn.execute('CREATE VIRTUAL TABLE work_fts USING fts5(record_id UNINDEXED,title,body)')
            conn.execute('INSERT INTO work_fts SELECT record_id,title,body FROM work_records')
        self.candidate.execute('DELETE FROM work_fts')
        self.assertEqual(self.check().code, 'WORK_FTS_MISMATCH')

    def project_row(self, conn, link='fictional-link'):
        names = ('link_id', 'record_id', 'project_id', 'role', 'scope', 'basis',
                 'original_evidence_id', 'added_at', 'archived_at')
        return dict(zip(names, conn.execute('SELECT ' + ','.join(names) + ' FROM work_record_projects WHERE link_id=?',
                                            (link,)).fetchone()))

    def test_project_archive_and_restore_preserve_exact_approved_history(self):
        before = self.project_row(self.candidate)
        self.candidate.execute("UPDATE work_record_projects SET archived_at='fictional-archive' WHERE link_id='fictional-link'")
        archived = self.project_row(self.candidate)
        self.add_event(kind='PROJECT_ARCHIVE', record_id='fictional', record_version=None,
                       details={'link_id': 'fictional-link', 'before': before, 'after': archived})
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_project_restore_event_preserves_original_basis_and_other_fields(self):
        for conn in (self.parent, self.candidate):
            conn.execute("UPDATE work_record_projects SET archived_at='fictional-archive' WHERE link_id='fictional-link'")
        before = self.project_row(self.candidate)
        self.candidate.execute("UPDATE work_record_projects SET archived_at=NULL WHERE link_id='fictional-link'")
        after = self.project_row(self.candidate)
        self.add_event(kind='PROJECT_RESTORE', record_id='fictional', record_version=None,
                       details={'link_id': 'fictional-link', 'before': before, 'after': after})
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_new_project_link_requires_original_event_and_user_basis(self):
        self.candidate.execute("INSERT INTO work_record_projects VALUES ('new-link','fictional','new-project',"
                               "'RELATED',NULL,'USER_CONFIRMED',NULL,'fictional-time',NULL)")
        self.add_event(kind='PROJECT_LINK', record_id='fictional', record_version=None,
                       details={'link_id': 'new-link', 'project_id': 'new-project', 'basis': 'USER_CONFIRMED'})
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_untracked_project_change_or_approval_promotion_fails(self):
        self.candidate.execute("UPDATE work_record_projects SET archived_at='fictional-archive' WHERE link_id='fictional-link'")
        self.assertEqual(self.check().code, 'PROJECT_LINK_EVENT_CONFLICT')

    def test_project_event_cannot_promote_an_inherited_basis(self):
        before = self.project_row(self.candidate)
        self.candidate.execute("UPDATE work_record_projects SET archived_at='fictional-archive',basis='USER_CONFIRMED' "
                               "WHERE link_id='fictional-link'")
        after = self.project_row(self.candidate)
        self.add_event(kind='PROJECT_ARCHIVE', record_id='fictional', record_version=None,
                       details={'link_id': 'fictional-link', 'before': before, 'after': after})
        self.assertEqual(self.check().code, 'PROJECT_LINK_EVENT_CONFLICT')

    def test_event_hash_and_revision_linkage_are_required(self):
        self.edit()
        self.candidate.execute("UPDATE work_revisions SET event_id='nonexistent' WHERE version=2")
        self.assertEqual(self.check().code, 'REVISION_EVENT_CONFLICT')

    def test_revision_cannot_exceed_the_current_record_version(self):
        event = self.add_event(kind='EDIT', record_id='fictional', record_version=2, details={'fields': ['title']})
        self.candidate.execute("INSERT INTO work_revisions VALUES ('fictional',2,'future','body','hash','summary',"
                               "'unreviewed','unknown','unknown',NULL,NULL,?)", (event,))
        self.assertEqual(self.check().code, 'WORK_REVISION_CONFLICT')

    def test_event_hash_corruption_is_rejected(self):
        self.edit()
        self.candidate.execute("UPDATE work_events SET reason='changed' WHERE sequence=2")
        self.assertEqual(self.check().code, 'EVENT_CHAIN_CONFLICT')

    def test_data_revision_cannot_roll_back_or_advance_without_event(self):
        self.candidate.execute("UPDATE work_meta SET value='0' WHERE key='data_revision'")
        self.assertEqual(self.check().code, 'DATA_REVISION_CONFLICT')

    def test_new_record_requires_create_event_and_exact_version_link(self):
        self.candidate.execute("INSERT INTO work_records VALUES ('fictional-new','new','original','body','hash',"
                               "'summary','role','layer','unreviewed','unknown','unknown','synthetic',1,"
                               "'created','updated',NULL,NULL)")
        event = self.add_event(kind='CREATE', record_id='fictional-new', record_version=1,
                               details={'data_scope': 'USER_WORKSPACE'})
        self.candidate.execute("INSERT INTO work_revisions VALUES ('fictional-new',1,'new','body','hash','summary',"
                               "'unreviewed','unknown','unknown',NULL,NULL,?)", (event,))
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_create_event_cannot_be_reused_as_edit_of_existing_record(self):
        self.candidate.execute("UPDATE work_records SET title='new',version=2,updated_at='later'")
        event = self.add_event(kind='CREATE', record_id='fictional', record_version=2,
                               details={'data_scope': 'USER_WORKSPACE'})
        self.candidate.execute("INSERT INTO work_revisions VALUES ('fictional',2,'new','body','hash','summary',"
                               "'unreviewed','unknown','unknown',NULL,NULL,?)", (event,))
        self.assertEqual(self.check().code, 'REVISION_EVENT_CONFLICT')

    def test_event_sequence_and_revision_cannot_be_silently_advanced(self):
        self.candidate.execute("INSERT INTO work_record_projects VALUES ('new-link','fictional','new-project',"
                               "'RELATED',NULL,'USER_CONFIRMED',NULL,'fictional-time',NULL)")
        self.assertEqual(self.check().code, 'PROJECT_LINK_EVENT_CONFLICT')

    def test_original_tag_add_and_new_tag_remove_event_are_preserved(self):
        self.candidate.execute("INSERT INTO work_record_tags VALUES ('fictional','new-tag')")
        self.add_event(kind='TAG', record_id='fictional', record_version=None, details={'tag_id': 'new-tag'})
        self.candidate.execute("DELETE FROM work_record_tags WHERE tag_id='fictional-tag'")
        self.add_event(kind='TAG_REMOVE', record_id='fictional', record_version=None,
                       details={'tag_id':'fictional-tag', 'before':{'record_id':'fictional','tag_id':'fictional-tag'},
                                'after':None})
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_topic_link_and_unlink_events_preserve_all_original_status(self):
        self.candidate.execute("DELETE FROM work_record_topics WHERE topic_id='fictional-topic'")
        self.add_event(kind='TOPIC_UNLINK', record_id='fictional', record_version=None,
                       details={'topic_id':'fictional-topic',
                                'before':{'record_id':'fictional','topic_id':'fictional-topic','status':'INHERITED'},
                                'after':None})
        self.candidate.execute("INSERT INTO work_record_topics VALUES ('fictional','new-topic','USER_CONFIRMED')")
        self.add_event(kind='TOPIC_LINK', record_id='fictional', record_version=None,
                       details={'topic_id':'new-topic', 'before':None,
                                'after':{'record_id':'fictional','topic_id':'new-topic','status':'USER_CONFIRMED'}})
        self.assertEqual(self.check().status, 'WORKSPACE_HISTORY_PRESERVED')

    def test_relation_deletion_without_exact_before_event_fails(self):
        self.candidate.execute("DELETE FROM work_record_tags WHERE tag_id='fictional-tag'")
        self.add_event(kind='TAG_REMOVE', record_id='fictional', record_version=None,
                       details={'tag_id':'fictional-tag', 'before':{'record_id':'different','tag_id':'fictional-tag'},
                                'after':None})
        self.assertEqual(self.check().code, 'RELATION_EVENT_CONFLICT')

    def test_inherited_topic_cannot_be_invented_by_new_relation_event(self):
        self.candidate.execute("INSERT INTO work_record_topics VALUES ('fictional','new-topic','INHERITED')")
        self.add_event(kind='TOPIC_LINK', record_id='fictional', record_version=None,
                       details={'topic_id':'new-topic', 'before':None,
                                'after':{'record_id':'fictional','topic_id':'new-topic','status':'INHERITED'}})
        self.assertEqual(self.check().code, 'RELATION_EVENT_CONFLICT')

    def test_assistant_event_cannot_invent_user_confirmed_topic(self):
        self.candidate.execute("INSERT INTO work_record_topics VALUES ('fictional','new-topic','USER_CONFIRMED')")
        self.add_event(kind='TOPIC_LINK', record_id='fictional', record_version=None, actor='ASSISTANT',
                       details={'topic_id':'new-topic', 'before':None,
                                'after':{'record_id':'fictional','topic_id':'new-topic','status':'USER_CONFIRMED'}})
        self.assertEqual(self.check().code, 'RELATION_EVENT_CONFLICT')


class OriginalWriterPublicationTests(unittest.TestCase):
    def test_original_codec_writer_history_can_be_published_without_losing_relations(self):
        from app.publisher_parent import compare_workspace_snapshots
        from app.workspace_write import OriginalWorkspaceEventCodec, WorkspaceWriter
        from tests.test_publisher_workspace import make_fixture

        with tempfile.TemporaryDirectory() as temp:
            source = make_fixture(Path(temp) / 'fictional.sqlite')
            parent, candidate = sqlite3.connect(':memory:'), sqlite3.connect(':memory:')
            try:
                source.execute("INSERT INTO work_projects(project_id,name) VALUES ('project','fictional project')")
                source.execute("INSERT INTO work_topics(topic_id,name) VALUES ('topic','fictional topic')")
                source.commit()
                writer = WorkspaceWriter(source, codec=OriginalWorkspaceEventCodec())
                source.backup(parent)

                def command(operation, **fields):
                    revision = source.execute("SELECT value FROM work_meta WHERE key='data_revision'").fetchone()[0]
                    return writer.execute({'operation': operation, 'record_id': 'fictional-record',
                                           'expected_version': 1, 'expected_data_revision': revision,
                                           'reason': 'fictional direct user action', **fields})
                command('project_add', link_id='link', project_id='project', role='RELATED',
                        scope=None, basis='USER_CONFIRMED')
                command('project_archive', link_id='link')
                command('tag_add', tag_id='tag', tag_name='fictional tag')
                command('tag_remove', tag_id='tag')
                command('topic_add', topic_id='topic', status='USER_CONFIRMED')
                command('topic_remove', topic_id='topic')
                command('update', changes={'body': 'fictional changed body'})
                source.backup(candidate)
                for conn in (parent, candidate):
                    conn.execute('PRAGMA query_only=ON')
                    conn.execute('BEGIN')
                result = compare_workspace_snapshots(parent, candidate)
                self.assertEqual(result.status, 'WORKSPACE_HISTORY_PRESERVED', result.code)
            finally:
                parent.close()
                candidate.close()
                source.close()


if __name__ == '__main__':
    unittest.main()
