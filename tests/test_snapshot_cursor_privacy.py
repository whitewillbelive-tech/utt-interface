"""Opaque cursor and lifetime checks using newly generated fictional snapshots.

These tests cover one captured normalized file, not native D0 immutability or
the detection of unobserved A->B->A replacements between requests.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.provider import request
from app.snapshot_provider import SnapshotError, SnapshotProvider
from test_snapshot_provider import normalized_payload


class SnapshotCursorPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='utt-fiction-cursor-privacy-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fictional-input.json'
        self.payload = normalized_payload()
        for record in self.payload['records']:
            record['title'] = 'fictional sensitive query ' + record['record_id']
        self.original = json.dumps(self.payload, ensure_ascii=False).encode('utf-8')
        self.path.write_bytes(self.original)
        self.provider = SnapshotProvider(self.path)
        self.scope = self.scope_for(self.provider)

    def scope_for(self, provider):
        response = provider.read(request('capabilities'))
        self.assertEqual(response['status'], 'OK')
        return {key: response['snapshot'][key] for key in ('dataset_id', 'snapshot_id')}

    def q(self, **changes):
        return request('search', **self.scope, page_size=2, **changes)

    def ok(self, q, *, provider=None, sort='id_asc'):
        result = (provider or self.provider).read(q, sort)
        self.assertEqual(result['status'], 'OK', result)
        return result['result']

    def failure(self, q, code='VERSION_MISMATCH', *, provider=None, sort='id_asc'):
        result = (provider or self.provider).read(q, sort)
        self.assertEqual(result['status'], 'ERROR')
        self.assertIsNone(result['result'])
        self.assertEqual(result['error']['code'], code)
        self.assertNotIn(self.directory.name, json.dumps(result))
        self.assertNotIn('fictional sensitive query', json.dumps(result))
        return result

    def test_client_handle_contains_only_random_256_bits_and_no_dictionary_oracle(self):
        q = self.q(query='fictional sensitive query')
        token = self.ok(q)['page']['next_cursor']
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        self.assertNotIn('.', token)
        decoded = base64.urlsafe_b64decode(token + '=')
        self.assertEqual(len(decoded), 32)
        with self.assertRaises((UnicodeError, json.JSONDecodeError)):
            json.loads(decoded)
        # The previous representation disclosed an unkeyed digest of the full
        # request, allowing a client to test guessed low-entropy search terms.
        candidates = [None, '', ' ', 'fictional sensitive query', 'fictional query']
        for query in candidates:
            raw = json.dumps({**q, 'query': query, 'cursor': None, 'sort': 'id_asc'},
                             sort_keys=True).encode('utf-8')
            fingerprint = hashlib.sha256(raw)
            self.assertNotIn(fingerprint.hexdigest(), token)
            self.assertNotIn(fingerprint.hexdigest().encode('ascii'), decoded)
            self.assertNotEqual(fingerprint.digest(), decoded)
        self.assertNotIn(q['query'].encode('utf-8'), decoded)
        state = self.provider._cursors[token]
        self.assertEqual({field.name for field in fields(state)},
                         {'offset', 'context_hmac', 'issued_at', 'expires_at'})
        self.assertEqual(len(state.context_hmac), 32)
        restarted = SnapshotProvider(self.path)
        self.assertNotEqual(self.provider._fingerprint(q, 'id_asc'),
                            restarted._fingerprint(q, 'id_asc'))
        self.assertNotEqual(token, self.ok(q, provider=restarted)['page']['next_cursor'])

    def test_exact_original_context_and_offset_are_bound_without_normalizing_queries(self):
        q = self.q()
        token = self.ok(q)['page']['next_cursor']
        for changes in ({'query': ''}, {'query': ' '}, {'query': '\u3000'},
                        {'page_size': 3}, {'project_id': self.payload['projects'][0]['project_id']},
                        {'snapshot_id': 'fictional-old-snapshot'}, {'dataset_id': 'fictional-other-dataset'},
                        {'operation': 'projects'}):
            with self.subTest(changes=changes):
                self.failure({**q, 'cursor': token, **changes})
        self.failure({**q, 'cursor': token}, sort='id_desc')
        # A first request for an unknown dataset still means missing dataset;
        # only cursor-bearing scope mismatch is a stale/foreign cursor error.
        self.failure({**q, 'dataset_id': 'fictional-other-dataset'}, 'ITEM_NOT_FOUND')
        for token_variant in (('A' if token[0] != 'A' else 'B') + token[1:],
                              token[:-1], token + 'A', token + '.payload', 'A' * 43):
            self.failure({**q, 'cursor': token_variant})
        next_page = self.ok({**q, 'cursor': token})
        expected = sorted(record['record_id'] for record in self.payload['records'])[2:4]
        self.assertEqual([record['record_id'] for record in next_page['records']], expected)

    def test_reusable_previous_page_handles_keep_original_monotonic_expiry(self):
        q = self.q()
        with patch('app.snapshot_provider.time.monotonic', return_value=100.0) as clock:
            token = self.ok(q)['page']['next_cursor']
            state = self.provider._cursors[token]
            self.assertEqual((state.issued_at, state.expires_at), (100.0, 1000.0))
            clock.return_value = 999.0
            second = self.ok({**q, 'cursor': token})
            following = second['page']['next_cursor']
            self.assertEqual(self.ok(q)['page']['next_cursor'], token)
            self.assertEqual(self.ok({**q, 'cursor': token}), second)
            self.assertEqual(len(self.provider._cursors), 2)
            self.assertEqual(self.provider._cursors[token].expires_at, 1000.0)
            clock.return_value = 1000.0
            self.failure({**q, 'cursor': token})
            self.assertNotIn(token, self.provider._cursors)
            self.assertIn(following, self.provider._cursors)
            replacement = self.ok(q)['page']['next_cursor']
            self.assertNotEqual(replacement, token)

    def test_capacity_rejects_without_evicting_live_handles_then_recovers_after_expiry(self):
        q = self.q()
        with patch('app.snapshot_provider._MAX_CURSOR_HANDLES', 2), \
             patch('app.snapshot_provider.time.monotonic', return_value=10.0) as clock:
            first = self.ok(q)['page']['next_cursor']
            second_page = self.ok({**q, 'cursor': first})
            second = second_page['page']['next_cursor']
            live = set(self.provider._cursors)
            self.assertEqual(len(live), 2)
            exhausted = self.failure({**q, 'cursor': second}, 'CONNECTION_UNAVAILABLE')
            self.assertTrue(exhausted['error']['retryable'])
            self.failure({**q, 'query': ' '}, 'CONNECTION_UNAVAILABLE')
            self.assertEqual(set(self.provider._cursors), live)
            self.assertEqual(self.ok(q)['page']['next_cursor'], first)
            self.assertEqual(self.ok({**q, 'cursor': first}), second_page)
            clock.return_value = 910.0
            self.failure({**q, 'cursor': first})
            replacement = self.ok(q)['page']['next_cursor']
            self.assertNotIn(replacement, live)
            self.assertEqual(len(self.provider._cursors), 1)

    def test_concurrent_repeated_queries_reuse_live_handle_under_existing_lock(self):
        q = self.q()
        with patch('app.snapshot_provider._MAX_CURSOR_HANDLES', 1):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: self.provider.read(q), range(16)))
        self.assertTrue(all(result['status'] == 'OK' for result in results))
        tokens = {result['result']['page']['next_cursor'] for result in results}
        self.assertEqual(len(tokens), 1)
        self.assertEqual(len(self.provider._cursors), 1)

    def test_restart_snapshot_change_and_observed_missing_input_invalidate_handles(self):
        q = self.q()
        token = self.ok(q)['page']['next_cursor']
        restarted = SnapshotProvider(self.path)
        self.assertEqual(self.scope_for(restarted), self.scope)
        self.failure({**q, 'cursor': token}, provider=restarted)
        self.path.write_bytes(self.original + b'\n')
        self.failure({**q, 'cursor': token})
        self.path.write_bytes(self.original)
        self.failure({**q, 'cursor': token})

        self.provider = SnapshotProvider(self.path)
        token = self.ok(q)['page']['next_cursor']
        self.path.unlink()
        self.failure({**q, 'cursor': token}, 'CONNECTION_UNAVAILABLE')
        self.path.write_bytes(self.original)
        self.failure({**q, 'cursor': token})
        self.failure(q)
        # Explicit restart is required, even though identical bytes returned.
        self.assertEqual(self.scope_for(SnapshotProvider(self.path)), self.scope)
        self.path.unlink()
        with self.assertRaises(SnapshotError) as caught:
            SnapshotProvider(self.path)
        self.assertEqual(caught.exception.code, 'CONNECTION_UNAVAILABLE')


if __name__ == '__main__':
    unittest.main()
