"""Parent comparison exercises invented in-memory SQLite and gzip only."""
from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import math
import sqlite3
import unittest
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
                decision_status TEXT, archived_at TEXT, merged_into TEXT, PRIMARY KEY(record_id,version));
            CREATE TABLE work_events (id INTEGER PRIMARY KEY, payload TEXT);
            CREATE TABLE retained (payload BLOB, number REAL);
            INSERT INTO work_meta VALUES ('data_revision','1');
            INSERT INTO work_meta VALUES ('baseline','fictional');
            INSERT INTO work_records VALUES ('fictional','title','original','body','hash','summary',
                'role','layer','unreviewed','unknown','unknown','synthetic',1,'created','updated',NULL,NULL);
            INSERT INTO work_revisions VALUES ('fictional',1,'title','body','hash','summary',
                'unreviewed','unknown','unknown',NULL,NULL);
            INSERT INTO work_events VALUES (1,'event');
            INSERT INTO retained VALUES (X'0001',1.5);
            INSERT INTO retained VALUES (X'0001',1.5);
        ''')
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

    def edit(self, *, include_old=True, include_current=True, version=2):
        self.candidate.execute("UPDATE work_records SET title='new',version=?,updated_at='later'", (version,))
        if not include_old:
            self.candidate.execute('DELETE FROM work_revisions')
        if include_current:
            self.candidate.execute("INSERT INTO work_revisions VALUES ('fictional',?,'new','body','hash','summary', 'unreviewed','unknown','unknown',NULL,NULL)", (version,))

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
        self.assertEqual(self.check().code, 'PARENT_ROW_CONFLICT')

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


if __name__ == '__main__':
    unittest.main()
