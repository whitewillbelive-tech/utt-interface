"""Native browse session tests against an explicitly fictional local source."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import gc
import json
from pathlib import Path
import tempfile
import unittest
import weakref
from unittest.mock import patch

from app.d0_adapter import D0Client, D0Evidence
from app.d0_browse import D0BrowseError, D0BrowseSession
from app.d0_projection import Presence
from app.provider import request
from synthetic_d0_server import SyntheticD0Server, fictional_payload, write_fixture


class D0BrowseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='utt-fictional-native-browse-')
        self.path = Path(self.directory.name) / 'fictional-d0.json'
        self.payload = fictional_payload()
        write_fixture(self.path, self.payload)
        self.stub = SyntheticD0Server(self.path)
        self.stub.start()
        self.client = D0Client(self.stub.api_root)
        self.session = D0BrowseSession(self.client, page_size=2)

    def tearDown(self):
        self.stub.stop()
        self.directory.cleanup()

    def assert_rejected_without_call(self, code, action):
        before = len(self.stub.requests)
        with self.assertRaises(D0BrowseError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(self.stub.requests), before)
        return caught.exception

    def test_explicit_construction_no_connect_and_argument_bounds(self):
        self.assertEqual(self.stub.requests, [])
        self.assertEqual(self.session.state, 'IDLE')
        for values in ({'page_size': 0}, {'page_size': 1001}, {'page_size': True},
                       {'max_seen': 0}, {'max_seen': True}, {'max_seen': 1000001}):
            with self.subTest(values=values), self.assertRaises(D0BrowseError):
                D0BrowseSession(self.client, **values)
        with self.assertRaises(D0BrowseError):
            D0BrowseSession(None)
        for values in ({'q': 1}, {'tag': []}, {'sort': 'id_asc'}, {'sort': []}):
            self.assert_rejected_without_call('INVALID_REQUEST', lambda: self.session.search(**values))
        self.assertEqual(self.stub.requests, [])

    def test_real_client_native_search_next_detail_and_complete_original_evidence(self):
        session = D0BrowseSession(self.client, page_size=1)
        values = dict(q='Alpha', tag='SYNTHETIC_ODD', project='synthetic-project-a',
                      source='가상 출처  A', sort='id')
        first = session.search(**values)
        self.assertEqual(first.parameters, {**values, 'offset': 0, 'limit': 1})
        self.assertEqual(first.document['results'][0]['id'], 'synthetic-d0-001')
        self.assertTrue(first.has_next)
        second = session.next_page(first)
        self.assertEqual(second.parameters, {**values, 'offset': 1, 'limit': 1})
        self.assertEqual(second.document['results'][0]['id'], 'synthetic-d0-003')
        self.assertFalse(second.has_next)
        detail = session.detail(second, 'synthetic-d0-003')
        expected = self.payload['records'][2]['detail']
        self.assertEqual(detail.document, expected)
        self.assertEqual(json.loads(detail.raw_body), expected)
        self.assertEqual(detail.inspection.document, expected)
        self.assertEqual(detail.inspection.source_metadata.value, expected['sources'])
        self.assertEqual(len(detail.inspection.entries('/sources/0/evidence')), 2)
        self.assertEqual(detail.inspection.at('/sources/0/evidence/0/locators/0').presence, Presence.NULL)
        self.assertEqual(detail.inspection.at('/sources/1/source_name').value, '')
        self.assertEqual(detail.inspection.at('/sources/0/version').presence, Presence.NULL)
        self.assertEqual(detail.inspection.at('/sources/2').presence, Presence.ABSENT)
        self.assertEqual(detail.parameters, {'id': 'synthetic-d0-003'})
        self.assertEqual(first.status, 'NATIVE_PAGE')
        self.assertEqual(detail.status, 'IDENTITY_UNVERIFIED')
        for item in (session, first, second, detail):
            self.assertEqual(item.consistency, 'UNVERIFIED')
            self.assertFalse(hasattr(item, 'snapshot_id'))
            self.assertFalse(hasattr(item, 'cursor'))
        self.assertEqual(detail.inspection.original_source_body_status, 'UNKNOWN')
        self.assertEqual(session.state, 'EXHAUSTED')
        self.assert_rejected_without_call('NO_NEXT_PAGE', lambda: session.next_page(second))

    def test_exact_null_empty_whitespace_and_native_sort_filter_parameters(self):
        for query, expected_count in ((None, 7), ('', 7), ('  ', 1), ('\u3000', 0),
                                      ('100%', 1), ('under_score', 1), ('path\\part', 1)):
            with self.subTest(query=query):
                page = self.session.search(q=query, tag='', project='', source='', sort='title')
                self.assertEqual(page.total, expected_count)
                self.assertEqual(page.parameters['q'], query)
                self.assertEqual(page.parameters['tag'], '')
                self.assertEqual(page.parameters['sort'], 'title')
                wire = self.stub.requests[-1]['query']
                if query is None:
                    self.assertNotIn('q', wire)
                else:
                    self.assertEqual(wire['q'], [query])
                self.assertEqual(wire['tag'], [''])
        for sort in ('priority', 'title', 'id', 'status'):
            page = self.session.search(q='Alpha', sort=sort)
            self.assertEqual(page.parameters['sort'], sort)
            self.assertEqual(self.stub.requests[-1]['query']['sort'], [sort])

    def test_page_binding_rejects_old_foreign_fabricated_and_wrongly_typed_ids(self):
        first = self.session.search(sort='id')
        altered = replace(first, offset=200)
        other_session = D0BrowseSession(self.client, page_size=2)
        foreign = other_session.search(sort='id')
        for page in (altered, foreign, None):
            self.assert_rejected_without_call('STALE_PAGE', lambda: self.session.next_page(page))
            self.assert_rejected_without_call('STALE_PAGE', lambda: self.session.detail(page, 'synthetic-d0-001'))
        self.assert_rejected_without_call('UNSEEN_RECORD', lambda: self.session.detail(first, 'synthetic-d0-007'))
        self.assert_rejected_without_call('UNSEEN_RECORD', lambda: self.session.detail(first, 1))
        second = self.session.next_page(first)
        self.assert_rejected_without_call('STALE_PAGE', lambda: self.session.detail(first, 'synthetic-d0-001'))
        self.assert_rejected_without_call('STALE_PAGE', lambda: self.session.next_page(first))
        self.session.search(q='100%')
        self.assert_rejected_without_call('STALE_PAGE', lambda: self.session.next_page(second))

    def test_detail_explicit_identity_conflict_requires_restart_without_coercion(self):
        for requested, returned in (('synthetic-d0-001', 'synthetic-d0-002'),
                                    ('synthetic-d0-001', 1), (1, '1')):
            with self.subTest(requested_type=type(requested), returned_type=type(returned)):
                payload = copy.deepcopy(self.payload)
                payload['records'][0]['row']['id'] = requested
                payload['records'][0]['detail']['canonical_record']['id'] = returned
                self.stub.replace_file(payload)
                page = self.session.search(sort='id')
                before = self.path.read_bytes()
                with self.assertRaises(D0BrowseError) as caught:
                    self.session.detail(page, requested)
                self.assertEqual(caught.exception.code, 'DETAIL_IDENTITY_CONFLICT')
                self.assertTrue(caught.exception.restart_required)
                self.assertEqual(self.session.state, 'RESTART_REQUIRED')
                self.assert_rejected_without_call('RESTART_REQUIRED', lambda: self.session.next_page(page))
                self.assert_rejected_without_call('RESTART_REQUIRED', lambda: self.session.detail(page, requested))
                self.assertEqual(self.path.read_bytes(), before)
                self.assertNotIn(str(requested), str(caught.exception))

    def test_detail_unknown_identity_values_preserve_raw_without_binding_claim(self):
        absent = object()
        for identity in (absent, None, '', False, [], {}):
            with self.subTest(identity_type=type(identity)):
                payload = copy.deepcopy(self.payload)
                canonical = payload['records'][0]['detail']['canonical_record']
                if identity is absent:
                    canonical.pop('id')
                else:
                    canonical['id'] = identity
                # No alternative native identity path is guessed or promoted.
                canonical['identity'] = {'record_id': 'different-unmapped-identity'}
                self.stub.replace_file(payload)
                page = self.session.search(sort='id')
                detail = self.session.detail(page, 'synthetic-d0-001')
                self.assertEqual(detail.status, 'IDENTITY_UNVERIFIED')
                self.assertEqual(detail.consistency, 'UNVERIFIED')
                self.assertEqual(detail.document, payload['records'][0]['detail'])
                native_id = detail.inspection.at('/canonical_record/id')
                self.assertEqual(native_id.presence, Presence.ABSENT if identity is absent
                                 else Presence.NULL if identity is None else Presence.VALUE)
                self.assertEqual(self.session.state, 'ACTIVE')

    def test_matching_native_detail_identity_remains_unverified(self):
        for identity in ('synthetic-d0-001', 1):
            payload = copy.deepcopy(self.payload)
            payload['records'][0]['row']['id'] = identity
            payload['records'][0]['detail']['canonical_record']['id'] = identity
            self.stub.replace_file(payload)
            page = self.session.search(sort='id')
            detail = self.session.detail(page, identity)
            self.assertEqual(detail.status, 'IDENTITY_UNVERIFIED')
            self.assertEqual(type(detail.document['canonical_record']['id']), type(identity))
            self.assertEqual(detail.document['canonical_record']['id'], identity)
            self.assertEqual(detail.inspection.mapping_status, 'UNVERIFIED')

    def test_cross_page_duplicate_requires_restart_and_never_retries_or_falls_back(self):
        first = self.session.search(sort='id')
        self.stub.set_fault('cross_page_duplicate', offset=2)
        with self.assertRaises(D0BrowseError) as caught:
            self.session.next_page(first)
        self.assertEqual(caught.exception.code, 'RESTART_REQUIRED')
        self.assertTrue(caught.exception.restart_required)
        self.assertEqual(self.session.state, 'RESTART_REQUIRED')
        self.assert_rejected_without_call('RESTART_REQUIRED', lambda: self.session.next_page(first))
        self.assert_rejected_without_call('RESTART_REQUIRED', lambda: self.session.detail(first, 'synthetic-d0-001'))
        self.stub.set_fault('normal')
        restarted = self.session.search(sort='id')
        self.assertEqual(restarted.offset, 0)
        self.assertEqual(restarted.consistency, 'UNVERIFIED')

    def test_duplicate_within_page_and_ambiguous_integer_string_ids_are_rejected(self):
        self.stub.set_fault('duplicate', offset=0)
        with self.assertRaises(D0BrowseError) as caught:
            self.session.search(sort='id')
        self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
        self.assertTrue(caught.exception.restart_required)
        self.stub.set_fault('normal')

        def ambiguous(payload):
            for index, identity in enumerate((1, '1')):
                payload['records'][index]['row']['id'] = identity
                payload['records'][index]['detail']['canonical_record']['id'] = identity

        self.stub.mutate_file(ambiguous)
        with self.assertRaises(D0BrowseError) as caught:
            self.session.search(sort='id')
        self.assertEqual(caught.exception.code, 'RESTART_REQUIRED')
        session = D0BrowseSession(self.client, page_size=1)
        first = session.search(sort='id')
        self.assertEqual(type(first.document['results'][0]['id']), int)
        self.assert_rejected_without_call('UNSEEN_RECORD', lambda: session.detail(first, '1'))
        with self.assertRaises(D0BrowseError) as caught:
            session.next_page(first)
        self.assertEqual(caught.exception.code, 'RESTART_REQUIRED')

    def test_short_empty_and_cross_page_omission_before_declared_end_require_restart(self):
        for fault in ('omit', 'cross_page_omit'):
            with self.subTest(fault=fault):
                self.stub.set_fault('normal')
                page = self.session.search(sort='id')
                if fault == 'omit':
                    self.stub.set_fault(fault, offset=2)
                else:
                    page = self.session.next_page(page)
                    page = self.session.next_page(page)
                    self.stub.set_fault(fault, offset=6)
                with self.assertRaises(D0BrowseError) as caught:
                    self.session.next_page(page)
                self.assertEqual(caught.exception.code, 'RESTART_REQUIRED')
        self.stub.set_fault('normal')
        empty = self.session.search(q='fictional-no-match')
        self.assertEqual((empty.total, empty.count, empty.has_next), (0, 0, False))

    def test_changed_total_is_detected_but_replacement_and_restore_are_not_snapshot_proof(self):
        first = self.session.search(sort='id')

        def remove_last(payload):
            payload['records'].pop()

        self.stub.mutate_file(remove_last)
        with self.assertRaises(D0BrowseError) as caught:
            self.session.next_page(first)
        self.assertEqual(caught.exception.code, 'RESTART_REQUIRED')
        self.stub.replace_file(self.payload)
        restored = self.session.search(sort='id')
        self.stub.replace_file(copy.deepcopy(self.payload))
        following = self.session.next_page(restored)
        self.assertEqual(following.consistency, 'UNVERIFIED')
        self.assertEqual(following.total, 7)

    def test_equal_size_unseen_swap_can_evade_checks_and_remains_unverified(self):
        first = self.session.search(sort='id')

        def swap_unseen(payload):
            payload['records'][2]['row']['id'] = 'synthetic-d0-008'
            payload['records'][2]['detail']['canonical_record']['id'] = 'synthetic-d0-008'

        self.stub.mutate_file(swap_unseen)
        following = self.session.next_page(first)
        self.assertEqual([row['id'] for row in following.document['results']],
                         ['synthetic-d0-004', 'synthetic-d0-005'])
        self.assertEqual(following.consistency, 'UNVERIFIED')
        self.assertEqual(self.session.state, 'ACTIVE')

    def test_response_copies_and_session_do_not_retain_previous_pages_or_detail_bodies(self):
        first = self.session.search(sort='id')
        original = copy.deepcopy(first.document)
        first.parameters['q'] = 'caller mutation'
        first.document['results'].clear()
        first.evidence.document['results'].clear()
        first.inspection.document['results'].clear()
        self.assertEqual(first.document, original)
        detail = self.session.detail(first, first.document['results'][0]['id'])
        detail_ref = weakref.ref(detail)
        detail.document['sources'].clear()
        self.assertEqual(len(detail.document['sources']), 2)
        del detail
        gc.collect()
        self.assertIsNone(detail_ref())
        prior_ref = weakref.ref(first)
        self.session.next_page(first)
        del first
        gc.collect()
        self.assertIsNone(prior_ref())
        self.assertFalse(hasattr(self.session, 'captures'))

    def test_seen_id_bound_fails_without_eviction_or_extra_fetch(self):
        session = D0BrowseSession(self.client, page_size=2, max_seen=2)
        first = session.search(sort='id')
        error = self.assert_rejected_without_call('SEEN_LIMIT_REACHED', lambda: session.next_page(first))
        self.assertTrue(error.restart_required)
        self.assertEqual(session.state, 'RESTART_REQUIRED')
        self.assertEqual(len(session._seen), 2)
        smaller = D0BrowseSession(self.client, page_size=2, max_seen=1)
        with self.assertRaises(D0BrowseError) as caught:
            smaller.search(sort='id')
        self.assertEqual(caught.exception.code, 'SEEN_LIMIT_REACHED')
        self.assertLessEqual(len(smaller._seen), 1)

    def test_large_native_ids_use_fixed_size_private_digests_and_still_detect_duplicates(self):
        payload = copy.deepcopy(self.payload)
        identities = ['fictional-large-' + 'x' * 65536 + suffix for suffix in ('-a', '-b')]
        for index, identity in enumerate(identities):
            payload['records'][index]['row']['id'] = identity
            payload['records'][index]['detail']['canonical_record']['id'] = identity
        self.stub.replace_file(payload)
        session = D0BrowseSession(self.client, page_size=1, max_seen=3)
        first = session.search(sort='id')
        self.assertEqual(first.document['results'][0]['id'], identities[0])
        second = session.next_page(first)
        self.assertEqual(second.document['results'][0]['id'], identities[1])
        self.assertEqual(len(session._seen), 2)
        self.assertTrue(all(type(value) is bytes and len(value) == 32 for value in session._seen))
        self.assertEqual(sum(map(len, session._seen)), 64)
        self.assertTrue(all(identity not in session._seen for identity in identities))
        other = D0BrowseSession(self.client, page_size=1)
        other.search(sort='id')
        self.assertTrue(other._seen.isdisjoint(session._seen))
        self.stub.set_fault('cross_page_duplicate', offset=2)
        with self.assertRaises(D0BrowseError) as caught:
            session.next_page(second)
        self.assertEqual(caught.exception.code, 'RESTART_REQUIRED')
        self.assertEqual(len(session._seen), 2)
        self.assertTrue(all(type(value) is bytes and len(value) == 32 for value in session._seen))

    def test_lone_surrogate_ids_fail_safely_without_lossy_normalization_or_exception_context(self):
        for identity in ('private-id-\ud800', 'private-id-\udfff'):
            # JSON escapes can legally reach Python's parser despite a UTF-8
            # response body. Supply only this fictional already-parsed response
            # to exercise the browse boundary independently of fixture SQLite.
            document = {'total': 1, 'results': [{'id': identity}]}
            evidence = D0Evidence('records', json.dumps(document).encode('ascii'), document)
            before = len(self.stub.requests)
            with patch.object(self.client, 'records', return_value=evidence):
                with self.assertRaises(D0BrowseError) as caught:
                    self.session.search(sort='id')
            self.assertEqual(caught.exception.code, 'INVALID_RESPONSE')
            self.assertTrue(caught.exception.restart_required)
            self.assertEqual(self.session.state, 'RESTART_REQUIRED')
            self.assertEqual(self.session._seen, set())
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn('private-id', repr(caught.exception))
            self.assertEqual(len(self.stub.requests), before)

    def test_concurrent_advance_accepts_exactly_one_request_for_current_page(self):
        first = self.session.search(sort='id')
        before = len(self.stub.requests)

        def advance():
            try:
                return self.session.next_page(first).status
            except D0BrowseError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: advance(), range(2)))
        self.assertCountEqual(outcomes, ['NATIVE_PAGE', 'STALE_PAGE'])
        self.assertEqual(len(self.stub.requests), before + 1)

    def test_source_restart_and_errors_expose_no_tokens_body_ids_or_addresses(self):
        first = self.session.search(q='Alpha', sort='id')
        detail = self.session.detail(first, first.document['results'][0]['id'])
        for item in (self.session, first, detail, detail._capture):
            display = repr(item)
            for private in ('Alpha', 'synthetic-d0-001', '가상 본문', self.stub.api_root):
                self.assertNotIn(private, display)
        self.stub.restart()
        before = len(self.stub.requests)
        with self.assertRaises(D0BrowseError) as caught:
            self.session.next_page(first)
        self.assertEqual(caught.exception.code, 'ACCESS_DENIED')
        self.assertTrue(caught.exception.restart_required)
        self.assertEqual(len(self.stub.requests), before + 1)
        for private in ('Alpha', 'synthetic-d0-001', '가상 본문', self.stub.api_root):
            self.assertNotIn(private, repr(caught.exception))
        self.client.reset_session()
        self.assertEqual(self.session.search(sort='id').offset, 0)
        before = len(self.stub.requests)
        self.assertEqual(self.client.read(request('capabilities'))['error']['code'], 'UNSUPPORTED_CONTRACT')
        self.assertEqual(len(self.stub.requests), before)
        with patch.object(self.client, 'records', side_effect=RuntimeError('private-body-token')):
            with self.assertRaises(D0BrowseError) as caught:
                self.session.search()
        self.assertNotIn('private-body-token', str(caught.exception))
        self.assertEqual(self.session.state, 'RESTART_REQUIRED')


if __name__ == '__main__':
    unittest.main()
