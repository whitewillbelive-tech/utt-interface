"""Adversarial snapshot checks against explicitly fictional temporary inputs.

The independent completeness oracle is the authored input entity set, not the
provider's private dictionaries, counts or cursors. It proves no omitted or
duplicated entities relative to that fixture, not real-world source completeness.
Snapshot identity is the exact file-byte digest. Atomic replacement with equal
bytes between requests is logically the same snapshot; acceptance is not proof
that the file's inode, owner, provenance or history remained unchanged.
"""
from collections import Counter
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from app.provider import ReadOnlyAdapter, request
from app.snapshot_provider import SnapshotError, SnapshotProvider
from test_snapshot_provider import normalized_payload


def authored_input():
    payload = normalized_payload()
    # Input order deliberately differs from every advertised page order.
    payload['records'].reverse()
    payload['projects'].reverse()
    dataset = payload['dataset']['dataset_id']
    payload['components'] = [
        dict(component_id=identifier, dataset_id=dataset, name='Fictional component',
             parent_component_id=None, relationship_status='OBSERVED')
        for identifier in ('fiction-component-z', 'fiction-component-a')]
    template = payload['records'][-1]['project_relations'][0]
    payload['relations'] = []
    for index, component in enumerate(payload['components']):
        relation = copy.deepcopy(template)
        relation.update(relation_id='fiction-navigation-' + str(index),
                        from_id=component['component_id'],
                        to_id=payload['projects'][index % len(payload['projects'])]['project_id'])
        payload['relations'].append(relation)
    return payload


class SnapshotAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='utt-fiction-adversarial-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fictional-input.json'
        self.payload = authored_input()
        self.original = self.encode(self.payload)
        self.path.write_bytes(self.original)
        self.provider = SnapshotProvider(self.path)
        self.scope = self.scope_for(self.provider)

    @staticmethod
    def encode(payload):
        return json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    def scope_for(self, provider):
        response = provider.read(request('capabilities'))
        self.assertEqual(response['status'], 'OK')
        return {key: response['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}

    def replace(self, raw):
        replacement = self.path.with_name('fictional-replacement.json')
        replacement.write_bytes(raw)
        os.replace(replacement, self.path)

    def q(self, operation='search', **values):
        return request(operation, **self.scope, **values)

    def assert_error(self, response, code):
        self.assertEqual(response['status'], 'ERROR')
        self.assertEqual(response['error']['code'], code)
        self.assertIsNone(response['result'])
        self.assertNotIn(self.directory.name, json.dumps(response))

    def pages(self, q, *, provider=None, sort='id_asc'):
        # Include the public response-validation boundary used by the server.
        adapter = ReadOnlyAdapter((provider or self.provider).read)
        result, seen = [], set()
        cursor = None
        for _ in range(100):
            current = {**q, 'cursor': cursor}
            response = adapter.read(current, sort)
            self.assertEqual(response['status'], 'OK', response)
            self.assertEqual(response['snapshot']['snapshot_id'], q['snapshot_id'])
            result.append(response['result'])
            next_cursor = response['result']['page']['next_cursor']
            if next_cursor is None:
                return result
            self.assertNotIn(next_cursor, seen, 'pagination cursor cycle')
            self.assertNotEqual(next_cursor, cursor, 'pagination cursor did not advance')
            seen.add(next_cursor)
            cursor = next_cursor
        self.fail('fictional pagination did not terminate within its entity bound')

    @staticmethod
    def page_identities(page, operation):
        if operation == 'search':
            return [('record', item['record_id']) for item in page['records']]
        collections = (('component', 'components', 'component_id'),
                       ('project', 'projects', 'project_id'),
                       ('relation', 'relations', 'relation_id')) if operation == 'projects' else (
                           ('event', 'events', 'event_id'),
                           ('record', 'related_records', 'record_id'),
                           ('relation', 'relations', 'relation_id'))
        return [(kind, item[key]) for kind, collection, key in collections
                for item in page[collection]]

    def assert_oracle(self, pages, operation, expected):
        actual = [identity for page in pages for identity in self.page_identities(page, operation)]
        self.assertEqual(Counter(actual), Counter(expected), 'page duplication or omission')
        self.assertEqual(len(actual), len(set(actual)), 'entity repeated between pages')
        self.assertEqual(actual, expected, 'complete page order differs from independent oracle')

    def test_complete_search_pages_match_independent_input_oracle_in_both_orders(self):
        # Public protocol ordering is lexical stable record ID, not input order.
        for sort, reverse in (('id_asc', False), ('id_desc', True)):
            for size in (1, 2, 3, 4, 7, 100):
                with self.subTest(sort=sort, size=size):
                    expected = [('record', item['record_id']) for item in
                                sorted(self.payload['records'], key=lambda item: item['record_id'],
                                       reverse=reverse)]
                    pages = self.pages(self.q(page_size=size), sort=sort)
                    self.assert_oracle(pages, 'search', expected)
                    self.assertTrue(all(record['raw_text'] is None and record['raw_text_sha256'] is None
                                        for page in pages for record in page['records']))

    def test_mixed_project_and_history_pages_neither_duplicate_nor_omit_entities(self):
        projects = []
        for kind, collection, key in (('component', 'components', 'component_id'),
                                      ('project', 'projects', 'project_id'),
                                      ('relation', 'relations', 'relation_id')):
            projects.extend((kind, item[key]) for item in
                            sorted(self.payload[collection], key=lambda item: item[key]))
        history = self.payload['history'][0]
        events = [('event', item['event_id']) for item in
                  sorted(history['events'], key=lambda item: item['event_id'])]
        peers = [('record', identifier) for identifier in sorted(history['related_record_ids'])]
        links = [('relation', item['relation_id']) for item in
                 sorted(history['relations'], key=lambda item: item['relation_id'])]
        for size in (1, 2, 3, 4, 100):
            with self.subTest(size=size):
                self.assert_oracle(self.pages(self.q('projects', page_size=size)), 'projects', projects)
                pages = self.pages(self.q('related_history', record_id=history['record_id'], page_size=size))
                self.assert_oracle(pages, 'related_history', events + peers + links)
                self.assertTrue(all(page['history_status'] == 'AVAILABLE' for page in pages))

    def test_atomic_different_bytes_replacement_invalidates_and_never_auto_reloads(self):
        first = self.provider.read(self.q(page_size=2))
        continued = self.q(page_size=2, cursor=first['result']['page']['next_cursor'])
        changed = copy.deepcopy(self.payload)
        changed['records'][0]['title'] = 'Fictional replacement content'
        replacement = self.encode(changed)
        self.replace(replacement)
        self.assert_error(self.provider.read(continued), 'VERSION_MISMATCH')
        self.assert_error(self.provider.read(request('capabilities')), 'VERSION_MISMATCH')
        replacement_provider = SnapshotProvider(self.path)
        replacement_scope = self.scope_for(replacement_provider)
        self.assertEqual(replacement_scope['snapshot_id'], 'sha256:' + hashlib.sha256(replacement).hexdigest())
        self.assertNotEqual(replacement_scope['snapshot_id'], self.scope['snapshot_id'])
        self.replace(self.original)
        self.assert_error(self.provider.read(self.q()), 'VERSION_MISMATCH')

    def test_atomic_same_bytes_replacement_preserves_content_snapshot_and_same_process_cursor(self):
        q = self.q(page_size=2)
        first = self.provider.read(q)
        cursor = first['result']['page']['next_cursor']
        self.replace(self.original)
        self.assertEqual(self.scope_for(self.provider), self.scope)
        second = self.provider.read({**q, 'cursor': cursor})
        self.assertEqual(second['status'], 'OK')
        expected = sorted(item['record_id'] for item in self.payload['records'])
        self.assertEqual([item['record_id'] for item in second['result']['records']], expected[2:4])
        self.assert_oracle(self.pages(q), 'search', [('record', identifier) for identifier in expected])
        # This asserts content-addressed identity, not continuity of inode/provenance.
        self.assertEqual(self.scope['snapshot_id'], 'sha256:' + hashlib.sha256(self.original).hexdigest())

    def test_restart_retains_content_digest_but_rejects_previous_process_cursor(self):
        q = self.q(page_size=2)
        first = self.provider.read(q)
        restarted = SnapshotProvider(self.path)
        self.assertEqual(self.scope_for(restarted), self.scope)
        self.assert_error(restarted.read({**q, 'cursor': first['result']['page']['next_cursor']}),
                          'VERSION_MISMATCH')
        expected = [('record', identifier) for identifier in
                    sorted(item['record_id'] for item in self.payload['records'])]
        self.assert_oracle(self.pages(q, provider=restarted), 'search', expected)

    def test_cursor_tampering_and_condition_reuse_are_rejected(self):
        q = self.q(page_size=2)
        token = self.provider.read(q)['result']['page']['next_cursor']
        changed_token = ('A' if token[0] != 'A' else 'B') + token[1:]
        for cursor in (changed_token, token + '.extra', 'invalid',
                       token[:-1], 'x' * 2049):
            with self.subTest(cursor_kind=len(cursor)):
                self.assert_error(self.provider.read({**q, 'cursor': cursor}), 'VERSION_MISMATCH')
        for change in ({'query': ''}, {'query': 'fictional query'}, {'page_size': 3},
                       {'project_id': self.payload['projects'][0]['project_id']}):
            with self.subTest(change=change):
                self.assert_error(self.provider.read({**q, 'cursor': token, **change}), 'VERSION_MISMATCH')
        self.assert_error(self.provider.read({**q, 'cursor': token}, 'id_desc'), 'VERSION_MISMATCH')
        self.assert_error(self.provider.read({**q, 'cursor': token, 'dataset_id': 'fiction-foreign'}),
                          'VERSION_MISMATCH')

    def test_unobserved_aba_does_not_change_the_captured_normalized_image(self):
        q = self.q(page_size=2)
        cursor = self.provider.read(q)['result']['page']['next_cursor']
        changed = copy.deepcopy(self.payload)
        changed['records'][0]['title'] = 'Fictional unobserved intermediate image'
        # Both replacements occur between requests. A before/after byte digest
        # cannot detect this ABA; the provider still serves its captured image.
        self.replace(self.encode(changed))
        self.replace(self.original)
        continued = self.provider.read({**q, 'cursor': cursor})
        self.assertEqual(continued['status'], 'OK')
        self.assertEqual(continued['snapshot']['snapshot_id'], self.scope['snapshot_id'])
        expected = sorted(self.payload['records'], key=lambda item: item['record_id'])[2:4]
        self.assertEqual([item['title'] for item in continued['result']['records']],
                         [item['title'] for item in expected])
        record = self.payload['records'][0]
        detail = self.provider.read(self.q('detail', record_id=record['record_id']))
        self.assertEqual(detail['result']['record'], record)
        # Acceptance proves neither native D0 immutability nor ABA detection.

    def test_in_place_mutation_and_deliberate_fixture_omission_change_snapshot(self):
        old = self.q(page_size=1)
        old['cursor'] = self.provider.read(old)['result']['page']['next_cursor']
        # Remove a standalone fictional input row; no claim that a fresh provider
        # could detect external-world omissions without an independent manifest.
        omitted_id = 'synthetic-record-007'
        changed = copy.deepcopy(self.payload)
        changed['records'] = [item for item in changed['records'] if item['record_id'] != omitted_id]
        self.path.write_bytes(self.encode(changed))
        self.assert_error(self.provider.read(old), 'VERSION_MISMATCH')
        fresh = SnapshotProvider(self.path)
        fresh_scope = self.scope_for(fresh)
        self.assertNotEqual(fresh_scope['snapshot_id'], self.scope['snapshot_id'])
        expected = [('record', identifier) for identifier in sorted(item['record_id'] for item in changed['records'])]
        pages = self.pages(request('search', **fresh_scope, page_size=2), provider=fresh)
        self.assert_oracle(pages, 'search', expected)
        self.assertNotIn(('record', omitted_id), expected)

    def test_duplicate_input_entities_are_rejected_instead_of_deduplicated(self):
        for collection in ('records', 'projects', 'components', 'relations'):
            with self.subTest(collection=collection):
                changed = copy.deepcopy(self.payload)
                changed[collection].append(copy.deepcopy(changed[collection][0]))
                self.path.write_bytes(self.encode(changed))
                with self.assertRaises(SnapshotError):
                    SnapshotProvider(self.path)
        changed = copy.deepcopy(self.payload)
        changed['history'][0]['events'].append(copy.deepcopy(changed['history'][0]['events'][0]))
        self.path.write_bytes(self.encode(changed))
        with self.assertRaises(SnapshotError):
            SnapshotProvider(self.path)


if __name__ == '__main__':
    unittest.main()
