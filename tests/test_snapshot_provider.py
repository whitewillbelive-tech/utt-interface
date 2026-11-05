"""Direct file-provider tests using only newly generated fictional input files."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.provider import request
from app.snapshot_provider import FORMAT, SnapshotError, SnapshotProvider
from fixtures.synthetic.generate import contract_examples, create


def normalized_payload(include_source=True):
    """Fictional normalized input; this is not a native D0 conversion example."""
    fiction = create()
    examples = contract_examples(fiction)
    dataset = copy.deepcopy(examples['capabilities']['snapshot'])
    dataset.pop('snapshot_id')
    records = copy.deepcopy([r for name in ('search_page_1', 'search_page_2', 'search_page_3')
                             for r in examples[name]['result']['records']])
    # The test input explicitly declares complete confirmed membership. This
    # changes only fiction and supplies no mapping for native D0 state values.
    for record in records:
        record['project_relations_status'] = 'AVAILABLE'
        for link in record['project_relations']:
            link['assertion_status'] = 'USER_CONFIRMED'
    result = {'format': FORMAT, 'dataset': dataset, 'records': records,
              'projects': copy.deepcopy(examples['projects']['result']['projects']),
              'components': [], 'relations': [],
              'history': [{'record_id': 'synthetic-record-006', 'history_status': 'AVAILABLE',
                           'related_record_ids': ['synthetic-record-001'], 'relations': [],
                           'events': copy.deepcopy(examples['history']['result']['events'])}]}
    if include_source:
        result['source_contents'] = [{
            'record_id': records[0]['record_id'],
            'source_id': records[0]['source_refs'][0]['source_id'],
            'source_version': records[0]['source_refs'][0]['version'],
            'content': {'availability': 'AVAILABLE', 'target_kind': 'SOURCE_BODY',
                        'target_id': records[0]['source_refs'][0]['source_id'],
                        'target_version': 'fictional-normalized-source-v1',
                        'locator_target_version': 'fictional-normalized-source-v1',
                        'text': fiction['sources'][0]['text'],
                        'text_sha256': hashlib.sha256(fiction['sources'][0]['text'].encode('utf-8')).hexdigest()}}]
    return result


class SnapshotProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='utt-fictional-snapshot-')
        self.path = Path(self.directory.name) / 'fictional-normalized.json'
        self.payload = normalized_payload()
        self.write()
        self.provider = SnapshotProvider(self.path)
        capabilities = self.provider.read(request('capabilities'))
        self.assertEqual(capabilities['status'], 'OK')
        self.scope = {key: capabilities['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}

    def tearDown(self):
        self.directory.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.payload, ensure_ascii=False), encoding='utf-8')

    def read(self, operation, **values):
        return self.provider.read(request(operation, **self.scope, **values))

    def collect(self, operation, **values):
        pages = []
        cursor = None
        while True:
            result = self.read(operation, cursor=cursor, **values)
            self.assertEqual(result['status'], 'OK', result)
            pages.append(result['result'])
            cursor = result['result']['page']['next_cursor']
            if cursor is None:
                return pages

    def test_direct_file_search_pages_detail_metadata_and_exact_digest(self):
        self.assertEqual(self.scope['snapshot_id'], 'sha256:' + hashlib.sha256(self.path.read_bytes()).hexdigest())
        pages = self.collect('search', page_size=4)
        self.assertEqual([len(p['records']) for p in pages], [4, 3])
        ids = [r['record_id'] for p in pages for r in p['records']]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertTrue(all(r['raw_text'] is None and r['content_availability'] == 'NOT_REQUESTED'
                            for p in pages for r in p['records']))
        detail = self.read('detail', record_id=ids[0])['result']
        self.assertEqual(detail['record'], self.payload['records'][0])
        self.assertEqual(detail['sources'], self.payload['records'][0]['source_refs'])
        detail['record']['title'] = 'mutated-return-value'
        self.assertNotEqual(self.read('detail', record_id=ids[0])['result']['record']['title'], 'mutated-return-value')
        missing = self.read('detail', record_id='synthetic-record-004')['result']['record']
        self.assertEqual(missing['source_status'], 'MISSING')

    def test_search_null_empty_whitespace_nfkc_and_reverse(self):
        for query in (None, '', '  \t\n', '\u3000'):
            pages = self.collect('search', query=query)
            self.assertEqual(sum(len(p['records']) for p in pages), 7)
        self.assertEqual(self.read('search', query='fictional-absent')['result']['records'], [])
        result = self.read('search', query='🧭 동쪽')
        self.assertGreater(len(result['result']['records']), 0)
        self.payload['records'][0]['title'] = 'ＡＢＣ Test'
        self.write()
        provider = SnapshotProvider(self.path)
        caps = provider.read(request('capabilities'))
        scope = {key: caps['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
        found = provider.read(request('search', **scope, query='abc test'))
        self.assertEqual([r['record_id'] for r in found['result']['records']], ['synthetic-record-001'])
        reverse = provider.read(request('search', **scope, page_size=3), 'id_desc')
        self.assertEqual([r['record_id'] for r in reverse['result']['records']],
                         ['synthetic-record-007', 'synthetic-record-006', 'synthetic-record-005'])

    def test_unsupported_filters_and_uncertain_project_membership_are_errors(self):
        for values in ({'document_role': 'SYNTHETIC_NOTE'}, {'review_status': 'HELD'},
                       {'document_role': 'not-a-real-role'}, {'project_id': 'unknown-project'}):
            self.assertEqual(self.read('search', **values)['error']['code'], 'INVALID_REQUEST')
        filtered = self.collect('search', project_id='synthetic-project-cloud')
        self.assertEqual(sum(len(p['records']) for p in filtered), 3)
        self.payload['records'][0]['project_relations'][0]['assertion_status'] = 'UNRESOLVED'
        self.write()
        provider = SnapshotProvider(self.path)
        caps = provider.read(request('capabilities'))
        scope = {key: caps['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
        rejected = provider.read(request('search', **scope, project_id='synthetic-project-clock'))
        self.assertEqual(rejected['error']['code'], 'INVALID_REQUEST')

    def test_history_preserves_events_and_unknown_is_distinct_from_empty(self):
        pages = self.collect('related_history', record_id='synthetic-record-006', page_size=1)
        self.assertEqual(len(pages), 3)
        events = [e for p in pages for e in p['events']]
        self.assertEqual(events, self.payload['history'][0]['events'])
        self.assertTrue(all(p['history_status'] == 'AVAILABLE' for p in pages))
        unknown = self.read('related_history', record_id='synthetic-record-001')['result']
        self.assertEqual(unknown['history_status'], 'UNKNOWN')
        self.assertEqual(unknown['events'], [])
        self.assertEqual(unknown['related_records'], [])
        self.assertEqual(self.read('detail', record_id='unknown')['error']['code'], 'ITEM_NOT_FOUND')

    def test_cursor_bound_to_conditions_sort_scope_and_provider_process(self):
        q = request('search', **self.scope, page_size=2)
        q['cursor'] = self.provider.read(q)['result']['page']['next_cursor']
        for changes in ({'query': '가상'}, {'page_size': 3}, {'snapshot_id': 'old'},
                        {'cursor': q['cursor'] + 'x'}, {'cursor': 'malformed'}):
            changed = self.provider.read({**q, **changes})
            self.assertEqual(changed['error']['code'], 'VERSION_MISMATCH')
        self.assertEqual(self.provider.read(q, 'id_desc')['error']['code'], 'VERSION_MISMATCH')
        restarted = SnapshotProvider(self.path)
        self.assertEqual(restarted.read(q)['error']['code'], 'VERSION_MISMATCH')
        self.assertEqual(self.provider.read({**q, 'dataset_id': 'foreign'})['error']['code'], 'VERSION_MISMATCH')

    def test_external_file_change_never_auto_reloads_even_if_restored(self):
        original = self.path.read_bytes()
        self.path.write_bytes(original + b'\n')
        self.assertEqual(self.read('search')['error']['code'], 'VERSION_MISMATCH')
        self.path.write_bytes(original)
        self.assertEqual(self.provider.read(request('capabilities'))['error']['code'], 'VERSION_MISMATCH')
        self.assertEqual(SnapshotProvider(self.path).read(request('capabilities'))['status'], 'OK')

    def test_missing_file_is_connection_failure_without_path_leak(self):
        self.path.unlink()
        result = self.read('search')
        self.assertEqual(result['error']['code'], 'CONNECTION_UNAVAILABLE')
        self.assertNotIn(self.directory.name, json.dumps(result))
        with self.assertRaises(SnapshotError) as caught:
            SnapshotProvider(self.path)
        self.assertNotIn(self.directory.name, str(caught.exception))

    def test_loader_rejects_bad_schema_duplicate_ids_hashes_and_location_paths(self):
        mutations = [
            lambda p: p.update(format='D0-native-is-not-supported'),
            lambda p: p['records'].append(copy.deepcopy(p['records'][0])),
            lambda p: p['records'][0].update(dataset_id='foreign'),
            lambda p: p['records'][0].update(raw_text_sha256='0' * 64),
            lambda p: p['records'][0].update(unexpected='value'),
            lambda p: p['records'][0]['source_refs'][0]['location'].update(target='/private/path'),
            lambda p: p['records'][0]['source_refs'][0]['location'].update(target='https://private.invalid'),
            lambda p: p['source_contents'][0]['content'].update(target_id='unbound-source'),
            lambda p: p['source_contents'][0]['content'].update(text_sha256='0' * 64),
            lambda p: p['records'][0]['project_relations'][0].update(to_id='dangling-local-project'),
            lambda p: p['dataset'].update(snapshot_id='cannot-self-declare-file-digest'),
        ]
        for mutate in mutations:
            self.payload = normalized_payload()
            mutate(self.payload)
            self.write()
            with self.subTest(mutation=mutate), self.assertRaises(SnapshotError):
                SnapshotProvider(self.path)
        self.path.write_text('{"format":1,"format":2}', encoding='utf-8')
        with self.assertRaises(SnapshotError):
            SnapshotProvider(self.path)
        self.path.write_bytes(b'\xff')
        with self.assertRaises(SnapshotError):
            SnapshotProvider(self.path)

    def test_file_size_symlink_and_directory_are_rejected(self):
        with self.assertRaises(SnapshotError):
            SnapshotProvider(self.path, max_input_bytes=1024)
        link = Path(self.directory.name) / 'fictional-link.json'
        link.symlink_to(self.path)
        with self.assertRaises(SnapshotError):
            SnapshotProvider(link)
        with self.assertRaises(SnapshotError):
            SnapshotProvider(self.directory.name)

    def source_request(self, **changes):
        from app.source_read import VERSION
        item = self.payload['source_contents'][0]
        q = {'contract_version': VERSION, **self.scope,
             'record_id': item['record_id'], 'source_id': item['source_id'],
             'source_version': item['source_version']}
        return {**q, **changes}

    def test_explicit_source_content_read_and_no_record_body_fallback(self):
        q = self.source_request()
        result = self.provider.read_source(q)
        self.assertEqual(result['status'], 'OK', result)
        self.assertEqual(result['result']['content'], self.payload['source_contents'][0]['content'])
        self.assertEqual(result['result']['locator_verification']['status'], 'VERIFIED')
        no_entry = self.provider.read_source({**q, 'record_id': 'synthetic-record-002'})
        self.assertEqual(no_entry['error']['code'], 'NOT_SUPPORTED')
        self.assertEqual(self.provider.read_source({**q, 'source_version': 'old'})['error']['code'], 'VERSION_MISMATCH')
        unbound = self.provider.read_source({**q, 'record_id': 'synthetic-record-003'})
        self.assertEqual(unbound['error']['code'], 'NOT_SUPPORTED')

    def test_source_body_unknown_target_version_and_snapshot_change(self):
        self.payload['source_contents'][0]['content']['locator_target_version'] = None
        self.write()
        self.provider = SnapshotProvider(self.path)
        caps = self.provider.read(request('capabilities'))
        self.scope = {key: caps['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
        result = self.provider.read_source(self.source_request())
        self.assertEqual(result['result']['locator_verification']['status'], 'UNVERIFIED')
        self.path.write_bytes(self.path.read_bytes() + b'\n')
        self.assertEqual(self.provider.read_source(self.source_request())['error']['code'], 'VERSION_MISMATCH')

    def test_unknown_source_membership_is_never_reported_as_confirmed_missing(self):
        from app.source_read import VERSION
        for status in ('UNKNOWN', 'INACCESSIBLE', 'NOT_REQUESTED', 'MISSING'):
            self.payload = normalized_payload(include_source=False)
            record = self.payload['records'][0]
            record.update(source_refs=[], source_status=status)
            self.write()
            provider = SnapshotProvider(self.path)
            caps = provider.read(request('capabilities'))
            q = {'contract_version': VERSION,
                 **{key: caps['snapshot'][key] for key in ('dataset_id', 'snapshot_id')},
                 'record_id': record['record_id'], 'source_id': 'fictional-unlisted-source',
                 'source_version': 'fictional-version'}
            result = provider.read_source(q)
            self.assertEqual(result['error']['code'], 'SOURCE_MISSING' if status == 'MISSING' else 'NOT_SUPPORTED')

    def test_explicit_unavailable_source_preserves_status_and_nullable_target(self):
        self.payload['records'][0]['source_refs'][0]['availability'] = 'INACCESSIBLE'
        content = self.payload['source_contents'][0]['content']
        content.update(availability='INACCESSIBLE', target_kind=None, target_id=None,
                       target_version=None, locator_target_version=None, text=None, text_sha256=None)
        self.write()
        self.provider = SnapshotProvider(self.path)
        caps = self.provider.read(request('capabilities'))
        self.scope = {key: caps['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}
        result = self.provider.read_source(self.source_request())
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['result']['source']['availability'], 'INACCESSIBLE')
        self.assertEqual(result['result']['content'], content)
        self.assertEqual(result['result']['locator_verification']['status'], 'NOT_AVAILABLE')


if __name__ == '__main__':
    unittest.main()
