"""Only temporary fictional portable history databases are created or read."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app.d11_reader import D11Reader, FORMAT


META_SQL = '''CREATE TABLE utt_d11_portable_meta (
    format_version TEXT NOT NULL, dataset_id TEXT NOT NULL,
    completeness TEXT NOT NULL, first_ordinal INTEGER NOT NULL,
    last_ordinal INTEGER NOT NULL, event_count INTEGER NOT NULL)'''
EVENT_SQL = '''CREATE TABLE utt_d11_portable_events (
    ordinal INTEGER NOT NULL, event_id TEXT NOT NULL, record_id TEXT NOT NULL,
    decision_version INTEGER NOT NULL, supersedes_event_id TEXT,
    payload_json BLOB NOT NULL)'''


def fictional_events():
    first = {'event_id': 'fictional-event-1', 'record_id': 'fictional-record',
             'decision_version': 1, 'action': 'DEFER_REVIEW', 'confirmed': False,
             'b_db_sha256': 'fictional-original-mislabeled-value',
             'native_extra': {'ratio': 0.125, 'missing': None, 'empty': ''}}
    second = {'event_id': 'fictional-event-2', 'record_id': 'fictional-record',
              'decision_version': 2, 'supersedes_event_id': 'fictional-event-1',
              'event_contract': 'UTT-D1-EVENT/2', 'action': 'CANCEL_DECISION',
              'confirmed': True, 'reviewed_categories': ['record_body'],
              'b_quality_sqlite_sha256': 'fictional-quality-original',
              'clean_parent_direct_bytes_verification': 'UNVERIFIED'}
    return [first, second]


class D11ReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='utt-fictional-d11-')
        self.path = Path(self.directory.name) / 'fictional-history.sqlite'
        self.events = fictional_events()
        self.write()

    def tearDown(self):
        self.directory.cleanup()

    def write(self, *, rows=None, completeness='COMPLETE', extent=None, format_version=FORMAT):
        if self.path.exists():
            self.path.unlink()
        if rows is None:
            rows = [(index, item['event_id'], item['record_id'], item['decision_version'],
                     item.get('supersedes_event_id'),
                     json.dumps(item, ensure_ascii=False, indent=2).encode('utf-8'))
                    for index, item in enumerate(self.events, 1)]
        if extent is None:
            extent = (rows[0][0], rows[-1][0], len(rows)) if rows else (0, 0, 0)
        with sqlite3.connect(self.path) as connection:
            connection.execute(META_SQL)
            connection.execute(EVENT_SQL)
            connection.execute('INSERT INTO utt_d11_portable_meta VALUES (?,?,?,?,?,?)',
                               (format_version, 'fictional-dataset', completeness, *extent))
            connection.executemany('INSERT INTO utt_d11_portable_events VALUES (?,?,?,?,?,?)', rows)
        return rows

    def read(self, **kwargs):
        return D11Reader(self.path, offline_immutable=True, **kwargs).read()

    def test_exact_payload_order_versions_supersedes_and_approval_are_preserved(self):
        rows = self.write()
        result = self.read()
        self.assertEqual(result.status, 'AVAILABLE')
        self.assertEqual(result.completeness, 'COMPLETE')
        self.assertEqual([e.ordinal for e in result.events], [1, 2])
        self.assertEqual([e.decision_version for e in result.events], [1, 2])
        self.assertEqual([e.raw_payload_json for e in result.events], [row[-1] for row in rows])
        self.assertEqual([e.payload for e in result.events], self.events)
        self.assertNotIn('event_contract', result.events[0].payload)
        self.assertEqual(result.events[1].payload['event_contract'], 'UTT-D1-EVENT/2')
        self.assertFalse(result.events[0].payload['confirmed'])
        self.assertTrue(result.events[1].payload['confirmed'])
        self.assertEqual(result.events[1].supersedes_event_id, 'fictional-event-1')
        self.assertEqual(result.events[0].payload['b_db_sha256'], 'fictional-original-mislabeled-value')
        self.assertNotIn('b_quality_sqlite_sha256', result.events[0].payload)
        changed = result.events[0].payload
        changed['action'] = 'mutated-copy'
        self.assertEqual(result.events[0].payload['action'], 'DEFER_REVIEW')
        self.assertNotIn('fictional-event', repr(result.events[0]))
        self.assertNotIn('fictional-dataset', repr(result))
        # AVAILABLE is structural readability of this portable snapshot only.
        self.assertFalse(hasattr(result, 'effective_approval'))
        self.assertFalse(hasattr(result, 'snapshot_id'))

    def test_reader_uses_memory_sqlite_only_and_leaves_original_files_unchanged(self):
        before = self.path.stat()
        content = self.path.read_bytes()
        names = {p.name for p in self.path.parent.iterdir()}
        actual_connect = sqlite3.connect
        with patch('app.d11_reader.sqlite3.connect', wraps=actual_connect) as connect:
            self.assertEqual(self.read().status, 'AVAILABLE')
        self.assertEqual([call.args for call in connect.call_args_list], [(':memory:',)])
        after = self.path.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual({p.name for p in self.path.parent.iterdir()}, names)

    def test_missing_parent_and_missing_main_with_sidecar_never_create_anything(self):
        missing_parent = self.path.parent / 'not-created'
        missing = missing_parent / 'absent.sqlite'
        with patch('app.d11_reader.sqlite3.connect') as connect:
            result = D11Reader(missing, offline_immutable=True).read()
        self.assertEqual(result.status, 'MISSING')
        self.assertEqual(result.code, 'FILE_MISSING')
        self.assertFalse(missing_parent.exists())
        connect.assert_not_called()
        self.path.unlink()
        sidecar = Path(str(self.path) + '-wal')
        sidecar.write_bytes(b'fictional-sidecar-not-read')
        original = sidecar.stat()
        result = self.read()
        self.assertEqual(result.status, 'MISSING')
        self.assertFalse(self.path.exists())
        self.assertEqual(sidecar.read_bytes(), b'fictional-sidecar-not-read')
        self.assertEqual(sidecar.stat().st_mtime_ns, original.st_mtime_ns)

    def test_live_default_and_permission_denied_are_distinct_without_leaks(self):
        with patch('app.d11_reader.sqlite3.connect') as connect:
            result = D11Reader(self.path).read()
        self.assertEqual((result.status, result.code), ('NOT_SUPPORTED', 'OFFLINE_IMMUTABLE_REQUIRED'))
        connect.assert_not_called()
        with patch.object(D11Reader, '_open', side_effect=PermissionError('private-path-and-token')):
            result = self.read()
        self.assertEqual((result.status, result.code), ('INACCESSIBLE', 'ACCESS_DENIED'))
        self.assertNotIn('private', repr(result))
        self.assertNotIn(str(self.path), repr(result))

    def test_sidecars_and_wal_header_are_refused_without_sqlite_open(self):
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.path) + suffix)
            sidecar.write_bytes(b'fictional-preserve')
            before = (self.path.read_bytes(), sidecar.read_bytes(), sidecar.stat().st_mtime_ns)
            with patch('app.d11_reader.sqlite3.connect') as connect:
                result = self.read()
            self.assertEqual((result.status, result.code), ('NOT_SUPPORTED', 'JOURNAL_SIDECAR_PRESENT'))
            connect.assert_not_called()
            self.assertEqual((self.path.read_bytes(), sidecar.read_bytes(), sidecar.stat().st_mtime_ns), before)
            sidecar.unlink()
        body = bytearray(self.path.read_bytes())
        body[18] = body[19] = 2
        self.path.write_bytes(body)
        with patch('app.d11_reader.sqlite3.connect') as connect:
            result = self.read()
        self.assertEqual((result.status, result.code), ('NOT_SUPPORTED', 'WAL_MODE_UNSUPPORTED'))
        connect.assert_not_called()

    def test_symlink_final_parent_and_nonregular_input_are_refused(self):
        link = self.path.parent / 'fictional-link.sqlite'
        link.symlink_to(self.path)
        self.assertEqual(D11Reader(link, offline_immutable=True).read().code, 'UNSAFE_INPUT')
        directory_link = self.path.parent / 'fictional-directory-link'
        directory_link.symlink_to(self.path.parent, target_is_directory=True)
        self.assertEqual(D11Reader(directory_link / self.path.name, offline_immutable=True).read().code,
                         'UNSAFE_INPUT')
        self.assertEqual(D11Reader(self.path.parent, offline_immutable=True).read().code, 'NONREGULAR_INPUT')

    def test_unknown_native_schema_view_and_generated_columns_fail_closed(self):
        self.path.unlink()
        with sqlite3.connect(self.path) as connection:
            connection.execute('CREATE TABLE native_decisions (anything TEXT)')
        self.assertEqual(self.read().code, 'UNSUPPORTED_SCHEMA')
        self.path.unlink()
        with sqlite3.connect(self.path) as connection:
            connection.execute(EVENT_SQL)
            connection.execute('CREATE VIEW utt_d11_portable_meta AS SELECT 1')
        self.assertEqual(self.read().code, 'UNSUPPORTED_SCHEMA')
        self.write()
        with sqlite3.connect(self.path) as connection:
            connection.execute('ALTER TABLE utt_d11_portable_events ADD COLUMN generated_value TEXT GENERATED ALWAYS AS (event_id) VIRTUAL')
        self.assertEqual(self.read().code, 'UNSUPPORTED_SCHEMA')

    def test_valid_empty_vs_declared_partial_unknown_and_missing_are_distinct(self):
        self.write(rows=[])
        empty = self.read()
        self.assertEqual((empty.status, empty.events), ('AVAILABLE', ()))
        for completeness in ('PARTIAL', 'UNKNOWN'):
            self.write(rows=[], completeness=completeness)
            result = self.read()
            self.assertEqual(result.status, completeness)
            self.assertEqual(result.events, ())
        self.write(rows=[], extent=(1, 2, 2))
        result = self.read()
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIn('DECLARED_EXTENT_MISMATCH', result.diagnostics)

    def test_sequence_and_decision_gaps_are_partial_without_rewriting_events(self):
        rows = self.write()
        rows[1] = (3, *rows[1][1:])
        self.write(rows=rows, extent=(1, 3, 2))
        result = self.read()
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIn('ORDINAL_GAP', result.diagnostics)
        self.assertEqual([e.ordinal for e in result.events], [1, 3])
        self.events[1]['decision_version'] = 4
        self.write()
        result = self.read()
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIn('DECISION_VERSION_GAP', result.diagnostics)
        self.assertEqual(result.events[1].payload['decision_version'], 4)

    def test_duplicate_reverse_or_wrong_supersedes_order_is_conflict(self):
        rows = self.write()
        self.write(rows=[rows[0], rows[0]], extent=(1, 1, 2))
        self.assertEqual(self.read().status, 'CONFLICT')
        self.events[1]['decision_version'] = 1
        self.write()
        self.assertIn('DECISION_VERSION_CONFLICT', self.read().diagnostics)
        self.events = fictional_events()
        self.events[0]['supersedes_event_id'] = 'fictional-event-2'
        self.write()
        result = self.read()
        self.assertEqual(result.status, 'CONFLICT')
        self.assertIn('SUPERSEDES_ORDER_CONFLICT', result.diagnostics)

    def test_missing_predecessor_is_partial_and_record_filter_keeps_global_status(self):
        self.events = [fictional_events()[1]]
        self.write()
        result = self.read()
        self.assertEqual(result.status, 'PARTIAL')
        self.assertIn('MISSING_PREDECESSOR', result.diagnostics)
        filtered = D11Reader(self.path, offline_immutable=True).read('fictional-absent-record')
        self.assertEqual(filtered.events, ())
        self.assertEqual(filtered.status, 'PARTIAL')

    def test_malformed_payload_index_mismatch_duplicates_and_unknown_version(self):
        rows = self.write()
        for raw in (b'not-json-private', b'\xff', b'[]', b'{"event_id":"x","event_id":"y"}',
                    b'{"event_id":"fictional-event-1","record_id":"fictional-record","decision_version":1,"x":NaN}'):
            self.write(rows=[(*rows[0][:-1], raw)])
            result = self.read()
            self.assertEqual((result.status, result.code), ('INVALID', 'INVALID_EVENT'))
            self.assertEqual(result.events, ())
        mismatch = copy.deepcopy(self.events[0])
        mismatch['decision_version'] = 999
        self.write(rows=[(*rows[0][:-1], json.dumps(mismatch).encode())])
        self.assertEqual(self.read().code, 'INVALID_EVENT')
        self.write(format_version='unknown-portable-version')
        self.assertEqual(self.read().code, 'UNSUPPORTED_FORMAT')

    def test_resource_bounds_corruption_and_internal_write_denial(self):
        self.assertEqual(self.read(max_file_bytes=1024, max_payload_bytes=1024).code, 'RESOURCE_LIMIT')
        self.assertEqual(self.read(max_events=1).code, 'RESOURCE_LIMIT')
        self.assertEqual(self.read(max_payload_bytes=64).code, 'INVALID_EVENT')
        self.assertEqual(self.read(max_seconds=float('nan')).code, 'INVALID_CONFIGURATION')
        reader = D11Reader(self.path, offline_immutable=True)
        connection = reader._database(self.path.read_bytes())
        try:
            for sql in ('CREATE TABLE forbidden_write (x)',
                        'INSERT INTO utt_d11_portable_events VALUES (1,2,3,4,NULL,NULL)',
                        "ATTACH DATABASE ':memory:' AS forbidden_attachment",
                        'PRAGMA journal_mode=WAL'):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(sql)
        finally:
            connection.close()
        self.path.write_bytes(b'fictional-corrupt-database')
        self.assertEqual(self.read().code, 'INVALID_SQLITE')

    def test_replacement_during_read_is_conflict_and_no_partial_success_escapes(self):
        actual = D11Reader._history

        def replace_after_read(reader, connection, record_id, raw):
            result = actual(reader, connection, record_id, raw)
            replacement = self.path.parent / 'fictional-replacement.sqlite'
            replacement.write_bytes(self.path.read_bytes())
            replacement.replace(self.path)
            return result

        with patch.object(D11Reader, '_history', replace_after_read):
            result = self.read()
        self.assertEqual((result.status, result.code), ('CONFLICT', 'INPUT_CHANGED'))
        self.assertEqual(result.events, ())
        self.assertFalse(Path(str(self.path) + '-wal').exists())
        self.assertFalse(Path(str(self.path) + '-shm').exists())

    def test_replacement_during_final_verification_read_is_detected_after_read(self):
        actual = D11Reader._bytes
        reads = 0

        def replace_while_final_read_returns(reader, descriptor):
            nonlocal reads
            body = actual(reader, descriptor)
            reads += 1
            if reads == 3:
                # Initial read and initial consistency read already succeeded.
                # Replace with identical bytes during the final full read: byte
                # equality alone cannot detect that this is a different inode.
                replacement = self.path.parent / 'fictional-final-replacement.sqlite'
                replacement.write_bytes(body)
                replacement.replace(self.path)
            return body

        with patch.object(D11Reader, '_bytes', replace_while_final_read_returns):
            result = self.read()
        self.assertEqual(reads, 3)
        self.assertEqual((result.status, result.code), ('CONFLICT', 'INPUT_CHANGED'))
        self.assertEqual(result.events, ())


if __name__ == '__main__':
    unittest.main()
