"""Canonical filter feature responses; no operational configuration or I/O."""
import copy
import json
import unittest

from app.features import (FILTERS, VERSION, FeaturesError, features_for,
                          validate_features)
from app.provider import ReadOnlyAdapter, SyntheticProvider
from app.snapshot_provider import SnapshotProvider
from app.transport import HttpReadTransport


UNKNOWN = {'contract_version': VERSION, 'filters': {
    'query': 'UNKNOWN', 'project': 'UNKNOWN', 'document_role': 'UNKNOWN',
    'review_status': 'UNKNOWN', 'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'}}
SYNTHETIC = {'contract_version': VERSION, 'filters': {
    'query': 'SUPPORTED', 'project': 'SUPPORTED', 'document_role': 'SUPPORTED',
    'review_status': 'SUPPORTED', 'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'}}
SNAPSHOT = {'contract_version': VERSION, 'filters': {
    'query': 'SUPPORTED', 'project': 'CONDITIONAL', 'document_role': 'UNSUPPORTED',
    'review_status': 'UNSUPPORTED', 'tag': 'UNSUPPORTED', 'source': 'UNSUPPORTED'}}


class FilterFeaturesTests(unittest.TestCase):
    def test_direct_synthetic_and_actual_bound_callback_use_synthetic_semantics(self):
        provider = SyntheticProvider()
        self.addCleanup(provider.close)
        self.assertEqual(features_for(provider), SYNTHETIC)
        self.assertEqual(features_for(ReadOnlyAdapter(provider.read)), SYNTHETIC)

    def test_direct_and_wrapped_snapshot_are_conditional_without_opening_input(self):
        # Classification describes implementation, not connection readiness.
        # No constructor/file is needed to test the static type/method boundary.
        provider = object.__new__(SnapshotProvider)
        self.assertEqual(features_for(provider), SNAPSHOT)
        self.assertEqual(features_for(ReadOnlyAdapter(provider.read)), SNAPSHOT)
        self.assertEqual(features_for(ReadOnlyAdapter(provider.read_source)), UNKNOWN)

    def test_no_provider_and_unconnected_or_https_adapter_do_not_claim_support(self):
        transport = HttpReadTransport('https://fictional-read.invalid/api/read',
                                      lambda: self.fail('must not request credentials'))
        for provider in (None, object(), ReadOnlyAdapter(), ReadOnlyAdapter(transport)):
            with self.subTest(kind=type(provider).__name__):
                self.assertEqual(features_for(provider), UNKNOWN)

    def test_arbitrary_callbacks_metadata_and_subclasses_cannot_claim_support(self):
        def untrusted(*_):
            self.fail('must not call provider callback')
        untrusted.__self__ = object.__new__(SnapshotProvider)
        untrusted.features = SYNTHETIC

        class Pretender:
            @property
            def __self__(self):
                raise AssertionError('must not inspect arbitrary callback metadata')

            @property
            def capabilities(self):
                raise AssertionError('must not consult declared metadata')

            def __call__(self, *_):
                raise AssertionError('must not call transport')

        class AlteredSnapshot(SnapshotProvider):
            pass

        class AlteredSynthetic(SyntheticProvider):
            pass

        class AlteredAdapter(ReadOnlyAdapter):
            pass

        snapshot_subclass = object.__new__(AlteredSnapshot)
        synthetic_subclass = object.__new__(AlteredSynthetic)
        for provider in (Pretender(), ReadOnlyAdapter(untrusted),
                         ReadOnlyAdapter(Pretender()), snapshot_subclass,
                         synthetic_subclass, ReadOnlyAdapter(snapshot_subclass.read),
                         ReadOnlyAdapter(synthetic_subclass.read),
                         AlteredAdapter(object.__new__(SnapshotProvider).read)):
            with self.subTest(kind=type(provider).__name__):
                self.assertEqual(features_for(provider), UNKNOWN)

    def test_each_call_returns_a_fresh_exact_json_object(self):
        first, second = features_for(None), features_for(None)
        self.assertIsNot(first, second)
        self.assertIsNot(first['filters'], second['filters'])
        first['filters']['source'] = 'SUPPORTED'
        first['private_diagnostic'] = 'fictional-private-value'
        self.assertEqual(second, UNKNOWN)
        self.assertEqual(features_for(None), UNKNOWN)
        for fixture in (UNKNOWN, SYNTHETIC, SNAPSHOT):
            self.assertIsNone(validate_features(json.loads(json.dumps(fixture))))

    def test_schema_rejects_missing_extra_malformed_and_unknown_states(self):
        invalid = [None, [], {}, {**UNKNOWN, 'contract_version': 'UTT-ACCESS-READ/0.1.0'},
                   {**UNKNOWN, 'private_diagnostic': 'fictional-private-value'},
                   {**UNKNOWN, 'filters': None}]
        for field in FILTERS:
            value = copy.deepcopy(UNKNOWN)
            value['filters'].pop(field)
            invalid.append(value)
            for state in ('supported', '', 'NOT_SUPPORTED', None, True, [], {}):
                value = copy.deepcopy(UNKNOWN)
                value['filters'][field] = state
                invalid.append(value)
        value = copy.deepcopy(UNKNOWN)
        value['filters']['native_d0_tag'] = 'SUPPORTED'
        invalid.append(value)
        for value in invalid:
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(FeaturesError) as caught:
                    validate_features(value)
                self.assertNotIn('fictional-private', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
