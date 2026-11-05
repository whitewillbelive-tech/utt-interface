"""Pure D0 structural inspection; all documents below are fictional JSON."""
import copy
import http.client
import json
import os
import socket
import unittest
from unittest.mock import patch

from app.d0_adapter import D0Client, D0Evidence
from app.d0_projection import (
    FORMAT, D0ProjectionError, JSONKind, Presence, inspect_d0, inspect_evidence,
)


def fictional_detail():
    """Repeated source/evidence IDs deliberately describe distinct entries."""
    return {
        'canonical_record': {
            'title': '가상 D0 🧭',
            'identity': {'record_id': 1, 'original_ids': [1, '1']},
            'content': {'display_blocks': [
                {'text': '', 'original_fragment': None, 'locator': None},
                {'text': '가상 표시', 'original_fragment': '',
                 'locator': {'unit': 'native-unit', 'start': None, 'end': 0}},
            ]},
        },
        'comparison': {'body_comparison': 'SAME_SHA_AND_TEXT'},
        'stored_body': 'B에 저장된 가상 본문 🧭',
        'sources': [
            {'source_id': 's', 'source_name': None, 'availability': 'AVAILABLE',
             'verification': 'VERIFIED', 'version': None,
             'evidence': [
                 {'evidence_id': 'e', 'record_ids': [1, '1'],
                  'locators': [{'start': 0, 'end': 1}, {'start': 2, 'end': 3}],
                  'verification_history': [{'status': None}, {'status': 'native-label'}]},
                 {'evidence_id': 'e', 'record_ids': ['1'], 'locators': []},
             ]},
            {'source_id': 's', 'source_name': '', 'availability': 'MISSING',
             'version': '', 'evidence': [{'evidence_id': 'e', 'record_ids': [1]}]},
            {'source_id': 1, 'availability': 'UNKNOWN', 'evidence': None},
            {'source_id': '1', 'availability': 'INACCESSIBLE'},
        ],
        'projects': [{'project_id': 1}, {'project_id': '1'}],
        'b_relations': [
            {'project_id': 1, 'record_id': 1, 'evidence_ids': ['e', 'e']},
            {'project_id': 1, 'record_id': '1', 'evidence_ids': ['e']},
            {'project_id': 1, 'record_id': 1, 'evidence_ids': ['e', 'e']},
        ],
        'baseline_relations': [],
        'lineage': {'candidates': [{'parent': None}, {'parent': ''}]},
        'baseline_evidence': {'unknown_native_key': [None, '', False, 0]},
        'unmapped': {'a/b': {'~field': [None, '', [], {}]}, '': 'empty-key'},
    }


class D0ProjectionTests(unittest.TestCase):
    def test_explicit_non_utt_artifact_does_not_claim_semantics(self):
        result = inspect_d0('detail', fictional_detail())
        self.assertEqual(result.format, FORMAT)
        self.assertNotIn('UTT-ACCESS-READ', result.format)
        self.assertEqual(result.mapping_status, 'UNVERIFIED')
        for name in ('snapshot_status', 'page_consistency_status', 'relation_identity_status',
                     'history_status', 'source_access_status', 'original_source_body_status'):
            self.assertEqual(getattr(result, name), 'UNKNOWN')
        for name in ('read', 'snapshot_id', 'cursor', 'events', 'contract_version', 'status'):
            self.assertFalse(hasattr(result, name), name)
        self.assertEqual(result.issues, ())

    def test_exact_original_and_all_public_values_are_defensive_copies(self):
        document = fictional_detail()
        expected = copy.deepcopy(document)
        result = inspect_d0('detail', document)
        document['sources'][0]['evidence'][0]['locators'].clear()
        document['stored_body'] = 'caller mutation'
        returned = result.document
        returned['sources'].clear()
        result.source_metadata.value[0]['evidence'].clear()
        result.entries('/sources')[0].value['evidence'].clear()
        self.assertEqual(result.document, expected)
        self.assertEqual(result.root.value, expected)
        self.assertEqual(list(result.document), list(expected))

    def test_multiple_sources_evidence_locators_and_linkage_are_never_merged(self):
        document = fictional_detail()
        result = inspect_d0('detail', document)
        self.assertEqual([entry.pointer for entry in result.entries('/sources')],
                         ['/sources/0', '/sources/1', '/sources/2', '/sources/3'])
        self.assertEqual([entry.value['source_id'] for entry in result.entries('/sources')],
                         ['s', 's', 1, '1'])
        evidence = result.entries('/sources/0/evidence')
        self.assertEqual([entry.value['evidence_id'] for entry in evidence], ['e', 'e'])
        self.assertEqual(evidence[0].value['record_ids'], [1, '1'])
        self.assertEqual(result.at('/sources/1/evidence/0/record_ids').value, [1])
        self.assertEqual(len(result.entries('/sources/0/evidence/0/locators')), 2)
        self.assertEqual(len(result.entries('/sources/0/evidence/0/verification_history')), 2)
        self.assertEqual(result.at('/sources/0/evidence/1/locators').value, [])
        self.assertEqual(result.document, document)

    def test_missing_null_empty_and_false_values_remain_distinct(self):
        result = inspect_d0('detail', fictional_detail())
        self.assertEqual(result.at('/sources/0/source_name').presence, Presence.NULL)
        self.assertIsNone(result.at('/sources/0/source_name').value)
        self.assertEqual(result.at('/sources/1/source_name').presence, Presence.VALUE)
        self.assertEqual(result.at('/sources/1/source_name').value, '')
        self.assertEqual(result.at('/sources/2/source_name').presence, Presence.ABSENT)
        with self.assertRaises(D0ProjectionError) as caught:
            _ = result.at('/sources/2/source_name').value
        self.assertEqual(caught.exception.code, 'ABSENT_FIELD')
        self.assertEqual(result.at('/baseline_evidence/unknown_native_key').value,
                         [None, '', False, 0])
        self.assertEqual(result.at('/unmapped/a~1b/~0field').value, [None, '', [], {}])
        self.assertEqual(result.at('/unmapped/').value, 'empty-key')
        self.assertEqual(result.at('/sources/00').presence, Presence.ABSENT)

    def test_source_labels_and_stored_body_never_assert_original_source_access(self):
        for sources in (None, [], fictional_detail()['sources'],
                        [{'availability': 'AVAILABLE', 'text': 'native opaque text'}]):
            for body in (None, '', '가상 B 본문'):
                with self.subTest(sources=sources, body=body):
                    document = {'canonical_record': {}, 'comparison': {},
                                'stored_body': body, 'sources': sources}
                    result = inspect_d0('detail', document)
                    self.assertEqual(result.stored_body.value, body)
                    self.assertEqual(result.source_metadata.value, sources)
                    self.assertEqual(result.source_access_status, 'UNKNOWN')
                    self.assertEqual(result.original_source_body_status, 'UNKNOWN')
                    self.assertEqual(result.at('/source_body').presence, Presence.ABSENT)
                    self.assertEqual(result.document, document)
        absent = inspect_d0('detail', {'canonical_record': {}, 'comparison': {}})
        self.assertEqual(absent.stored_body.presence, Presence.ABSENT)
        self.assertEqual(absent.source_metadata.presence, Presence.ABSENT)
        for document in ({}, {'sources': None}, {'sources': {}}):
            result = inspect_d0('detail', document)
            with self.assertRaises(D0ProjectionError):
                result.entries('/sources')
        self.assertEqual(inspect_d0('detail', {'sources': []}).entries('/sources'), ())

    def test_native_relations_and_lineage_create_no_ids_or_decision_events(self):
        document = fictional_detail()
        result = inspect_d0('detail', document)
        relations = result.entries('/b_relations')
        self.assertEqual([item.value for item in relations], document['b_relations'])
        self.assertEqual(len(relations), 3)
        self.assertNotEqual(relations[0].pointer, relations[2].pointer)
        self.assertEqual(relations[0].value, relations[2].value)
        self.assertTrue(all('relation_id' not in item.value for item in relations))
        self.assertEqual(result.at('/lineage').value, document['lineage'])
        self.assertEqual(result.at('/events').presence, Presence.ABSENT)
        self.assertEqual(result.at('/history').presence, Presence.ABSENT)

    def test_record_native_ids_keep_type_duplicates_order_and_unknown_fields(self):
        rows = [{'id': 1, 'title': None}, {'id': '1', 'title': ''},
                {'id': 1, 'evidence': [{'id': 1}, {'id': '1'}]}]
        document = {'total': 3, 'results': rows, 'native_offset': 2,
                    'unknown_snapshot_label': 'not-a-verified-snapshot'}
        result = inspect_d0('records', document)
        self.assertEqual(result.document, document)
        self.assertEqual(result.issues, ())
        identities = [result.at(node.pointer + '/id').value for node in result.entries('/results')]
        self.assertEqual([(type(value), value) for value in identities],
                         [(int, 1), (str, '1'), (int, 1)])
        self.assertEqual(result.page_consistency_status, 'UNKNOWN')
        self.assertEqual(result.snapshot_status, 'UNKNOWN')
        self.assertEqual(result.at('/cursor').presence, Presence.ABSENT)

    def test_structural_issues_retain_malformed_native_values_without_coercion(self):
        document = {'total': -1, 'results': [None, {'id': True}, {'id': ''},
                                             {'private-secret-key': 'private-secret-body'}]}
        result = inspect_d0('records', document)
        self.assertEqual([(issue.pointer, issue.code) for issue in result.issues], [
            ('/total', 'NEGATIVE_TOTAL'), ('/results/0', 'WRONG_JSON_TYPE'),
            ('/results/1/id', 'INVALID_NATIVE_ID'), ('/results/2/id', 'INVALID_NATIVE_ID'),
            ('/results/3/id', 'MISSING_FIELD'),
        ])
        self.assertNotIn('private-secret', repr(result.issues))
        self.assertNotIn('private-secret', repr(result))
        self.assertEqual(result.document, document)
        self.assertEqual(result.entries('/results')[0].kind, JSONKind.NULL)
        result = inspect_d0('detail', {'canonical_record': None, 'stored_body': 0,
                                      'sources': {'not': 'an array'}})
        self.assertEqual({(issue.pointer, issue.code) for issue in result.issues}, {
            ('/canonical_record', 'WRONG_JSON_TYPE'), ('/comparison', 'MISSING_FIELD'),
            ('/stored_body', 'WRONG_JSON_TYPE'), ('/sources', 'WRONG_JSON_TYPE'),
        })
        self.assertEqual(result.stored_body.value, 0)

    def test_known_summary_filter_shapes_do_not_claim_support_or_completeness(self):
        for kind, document in (
            ('summary', {'stats': {'records': 0, 'unknown': None}, 'native': ''}),
            ('filters', {'tags': [], 'projects': [{'project_id': None}], 'sources': []}),
        ):
            with self.subTest(kind=kind):
                result = inspect_d0(kind, document)
                self.assertEqual(result.document, document)
                self.assertEqual(result.issues, ())
                self.assertEqual(result.mapping_status, 'UNVERIFIED')
        self.assertEqual(inspect_d0('filters', {}).issues[0].code, 'MISSING_FIELD')

    def test_existing_evidence_is_inspected_without_fetching_or_hash_snapshot_claims(self):
        document = fictional_detail()
        raw = json.dumps(document, ensure_ascii=False, indent=2).encode('utf-8')
        evidence = D0Evidence('detail', raw, document)
        with patch.object(D0Client, '_request', side_effect=AssertionError('unexpected request')):
            result = inspect_evidence(evidence)
        self.assertEqual(result.document, evidence.document)
        self.assertEqual(result.snapshot_status, 'UNKNOWN')
        self.assertNotIn(evidence.response_sha256, repr(result))
        self.assertNotIn('가상', repr(result))
        self.assertNotIn('가상', repr(result.root))
        self.assertNotIn('가상', repr(result.stored_body))
        with self.assertRaises(D0ProjectionError):
            inspect_evidence(D0Evidence('session', b'{}', {}))
        with self.assertRaises(D0ProjectionError):
            inspect_evidence(object())

    def test_pure_processing_never_reads_configuration_files_or_connects(self):
        forbidden = AssertionError('external access is not allowed')
        with patch('builtins.open', side_effect=forbidden), \
             patch.object(os, 'getenv', side_effect=forbidden), \
             patch.object(socket, 'socket', side_effect=forbidden), \
             patch.object(socket, 'create_connection', side_effect=forbidden), \
             patch.object(http.client, 'HTTPConnection', side_effect=forbidden), \
             patch.object(D0Client, '_request', side_effect=forbidden):
            result = inspect_d0('detail', fictional_detail())
            self.assertEqual(result.stored_body.value, 'B에 저장된 가상 본문 🧭')
            self.assertEqual(len(result.entries('/sources')), 4)

    def test_non_json_cycles_and_malformed_pointers_fail_without_content_leakage(self):
        class Hook:
            def __deepcopy__(self, _memo):
                raise AssertionError('untrusted deepcopy hook executed')

        cycle = []
        cycle.append(cycle)
        for document in ({'private-secret': Hook()}, {'x': float('nan')},
                         {'x': float('inf')}, {'x': cycle}, {1: 'private-secret'},
                         {'x': (1, 2)}, [], None):
            with self.subTest(document_type=type(document)), self.assertRaises(D0ProjectionError) as caught:
                inspect_d0('detail', document)
            self.assertNotIn('private-secret', str(caught.exception))
        for kind in ('session', '', None, []):
            with self.assertRaises(D0ProjectionError):
                inspect_d0(kind, {})
        result = inspect_d0('detail', fictional_detail())
        for pointer in ('sources', '/bad~', '/bad~2escape', None):
            with self.assertRaises(D0ProjectionError):
                result.at(pointer)
        self.assertEqual(result.at('/sources/' + '9' * 10000).presence, Presence.ABSENT)


if __name__ == '__main__':
    unittest.main()
