"""Verify only the explicitly fictional file-backed D0 test environment."""
import json
from pathlib import Path
import tempfile
import unittest

from app.d0_adapter import D0Client, D0Error
from app.d0_projection import Presence, inspect_evidence
from synthetic_d0_server import SyntheticD0Server, fictional_payload, write_fixture


class SyntheticD0ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='utt-synthetic-d0-http-')
        self.path = Path(self.directory.name) / 'explicit-fiction.json'
        write_fixture(self.path)
        self.stub = SyntheticD0Server(self.path).start()
        self.client = D0Client(self.stub.api_root)

    def tearDown(self):
        self.stub.stop()
        self.directory.cleanup()

    def test_file_backed_native_search_pages_detail_and_structural_inspection(self):
        first = self.client.records(sort='id', offset=0, limit=3)
        second = self.client.records(sort='id', offset=3, limit=3)
        self.assertEqual(first.document['total'], 7)
        self.assertEqual(len(first.document['results']), 3)
        ids = [row['id'] for evidence in (first, second) for row in evidence.document['results']]
        self.assertEqual(ids, [f'synthetic-d0-{n:03d}' for n in range(1, 7)])
        detail = self.client.detail(ids[0])
        inspection = inspect_evidence(detail)
        self.assertEqual(inspection.issues, ())
        self.assertEqual(inspection.source_access_status, 'UNKNOWN')
        self.assertEqual(len(inspection.entries('/sources')), 2)
        self.assertEqual(inspection.at('/sources/0/version').presence, Presence.NULL)
        self.assertEqual(inspection.at('/sources/1/version').value, '')
        self.assertEqual(len(inspection.entries('/sources/0/evidence')), 2)
        self.assertEqual(inspection.at('/events').presence, Presence.ABSENT)
        self.assertEqual(inspection.document, detail.document)
        self.assertEqual(json.loads(first.raw_body), first.document)

    def test_query_null_empty_whitespace_and_like_metacharacters_are_literal(self):
        expected = [(None, 7), ('', 7), ('  ', 1), ('\t', 0), ('\u3000', 0),
                    ('%', 1), ('_', 2), ('\\', 1), ('ALPHA', 7), ('％', 0)]
        for query, total in expected:
            with self.subTest(query=query):
                result = self.client.records(q=query)
                self.assertEqual(result.document['total'], total)
        record_requests = [r for r in self.stub.requests if r['path'] == '/api/records']
        self.assertNotIn('q', record_requests[0]['query'])
        self.assertEqual(record_requests[1]['query']['q'], [''])
        self.assertEqual(record_requests[2]['query']['q'], ['  '])

    def test_native_filters_keep_spaces_and_source_id_is_exact_name_is_partial(self):
        for values, count in (
            ({'tag': 'SYNTHETIC_ODD'}, 4), ({'tag': ' SYNTHETIC_ODD'}, 0),
            ({'project': 'synthetic-project-a'}, 4), ({'project': 'synthetic-project-a '}, 0),
            ({'source': 'synthetic-source-1'}, 3), ({'source': 'synthetic-source-'}, 0),
            ({'source': '가상 출처  A'}, 3), ({'source': '출처  A'}, 3),
            ({'source': '가상 출처 A'}, 0), ({'source': ' '}, 7),
        ):
            with self.subTest(values=values):
                self.assertEqual(self.client.records(**values).document['total'], count)

    def test_mutation_replacement_and_restart_are_explicit_test_controls(self):
        first = self.client.records(sort='id', limit=1).document['results'][0]
        self.stub.mutate_file(lambda payload: payload['records'][0]['row'].update(title='same-fixture-mutated'))
        changed = self.client.records(sort='id', limit=1).document['results'][0]
        self.assertEqual(changed['id'], first['id'])
        self.assertEqual(changed['title'], 'same-fixture-mutated')
        inode = self.path.stat().st_ino
        self.stub.replace_file()
        self.assertNotEqual(self.path.stat().st_ino, inode)
        self.assertEqual(self.client.records(sort='id', limit=1).document['results'][0], changed)
        root = self.stub.api_root
        self.stub.restart()
        self.assertEqual(self.stub.api_root, root)
        with self.assertRaises(D0Error) as caught:
            self.client.records()
        self.assertEqual(caught.exception.code, 'ACCESS_DENIED')
        self.client.reset_session()
        self.assertEqual(self.client.records().document['total'], 7)

    def test_duplicate_and_omission_faults_do_not_fabricate_snapshot_guarantees(self):
        self.stub.set_fault('duplicate')
        with self.assertRaises(D0Error) as caught:
            self.client.records(limit=2, sort='id')
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.stub.set_fault('omit', offset=0)
        missing = self.client.records(limit=2, sort='id').document
        self.assertEqual(missing['total'], 7)
        self.assertEqual(len(missing['results']), 1)
        self.stub.set_fault('cross_page_duplicate', offset=2)
        first = self.client.records(offset=0, limit=2, sort='id').document
        second = self.client.records(offset=2, limit=2, sort='id').document
        self.assertEqual(first['results'][-1]['id'], second['results'][0]['id'])
        self.stub.set_fault('cross_page_omit', offset=2)
        second = self.client.records(offset=2, limit=2, sort='id').document
        self.assertEqual(second['results'][0]['id'], 'synthetic-d0-004')
        self.assertNotIn('snapshot_id', second)
        self.assertNotIn('cursor', second)

    def test_request_hook_modifies_only_explicit_file_and_logs_never_capture_tokens(self):
        def remove_last(stub, _query, number):
            if number == 2:
                def mutation(payload):
                    payload['records'].pop()
                stub.mutate_file(mutation)

        self.stub.on_records_request = remove_last
        self.assertEqual(self.client.records().document['total'], 7)
        self.assertEqual(self.client.records().document['total'], 6)
        self.assertTrue(all(set(r) == {'path', 'query', 'authenticated'} for r in self.stub.requests))
        self.assertNotIn(self.stub._session, repr(self.stub.requests))
        self.assertNotIn(str(self.path), repr(self.stub))

    def test_unmarked_or_missing_explicit_fixture_never_starts(self):
        self.stub.stop()
        self.path.write_text('{"records":[]}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.stub.start()
        self.path.unlink()
        with self.assertRaises(ValueError):
            SyntheticD0Server(self.path).start()
        self.assertFalse(self.path.exists())
        with self.assertRaises(ValueError):
            write_fixture(self.path, {'fixture_marker': 'REAL_DATA_NOT_ALLOWED', 'records': []})


if __name__ == '__main__':
    unittest.main()
