"""Independent source contract tests with authored fiction; no external inputs."""
import copy
import hashlib
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from app.source_read import (VERSION, SourceReadError, source_error, source_result,
                             validate_source_request, validate_source_response)


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fictional_source():
    """Metadata version and locator target version intentionally differ."""
    q = dict(contract_version=VERSION, dataset_id='fiction-dataset',
             snapshot_id='fiction-snapshot', record_id='fiction-record',
             source_id='fiction-source', source_version='source-metadata-v1')
    text = '앞 🧭 동쪽\n끝'
    source = dict(source_id=q['source_id'], dataset_id=q['dataset_id'],
                  availability='AVAILABLE', version=q['source_version'],
                  content_sha256=digest('fictional original file bytes'),
                  location=dict(target='SOURCE_BODY', unit='UTF-16', start=2, end=7,
                                boundary='half_open', target_sha256=digest(text),
                                fragment_sha256=digest('🧭 동쪽'), verification='UNVERIFIED'),
                  verification='UNVERIFIED')
    content = dict(availability='AVAILABLE', target_kind='SOURCE_BODY',
                   target_id=q['source_id'], target_version='source-text-v4',
                   locator_target_version='source-text-v4', text=text,
                   text_sha256=digest(text))
    return q, source, content


class SourceReadTests(unittest.TestCase):
    def test_full_schema_and_version_do_not_widen_existing_read_contract(self):
        root = Path(__file__).resolve().parents[1]
        old = json.loads((root / 'contracts/utt-read-api.schema.json').read_text())
        new = json.loads((root / 'contracts/utt-source-read.schema.json').read_text())
        Draft202012Validator.check_schema(new)
        self.assertEqual(new['$defs']['source'], old['$defs']['source'])
        self.assertEqual(new['$defs']['location'], old['$defs']['location'])
        self.assertEqual(old['$defs']['request']['properties']['contract_version']['const'],
                         'UTT-ACCESS-READ/0.1.0')
        q, source, content = fictional_source()
        validator = Draft202012Validator(new)
        self.assertTrue(validator.is_valid(q))
        self.assertTrue(validator.is_valid(source_result(q, source, content)))
        self.assertFalse(Draft202012Validator(old).is_valid(q))

    def test_request_is_exact_identity_and_requires_known_source_version(self):
        q, _, _ = fictional_source()
        self.assertIsNone(validate_source_request(q))
        for field in ('dataset_id', 'snapshot_id', 'record_id', 'source_id', 'source_version'):
            for value in (None, '', 1, []):
                with self.subTest(field=field, value=value):
                    self.assertEqual(validate_source_request({**q, field: value}), 'INVALID_REQUEST')
        self.assertEqual(validate_source_request({**q, 'path': 'fictional-private-path'}), 'INVALID_REQUEST')
        self.assertEqual(validate_source_request({**q, 'contract_version': 'UTT-ACCESS-READ/0.1.0'}),
                         'UNSUPPORTED_CONTRACT')
        self.assertEqual(validate_source_request(None), 'INVALID_REQUEST')

    def test_text_and_locator_verify_without_rewriting_metadata(self):
        q, source, content = fictional_source()
        original = copy.deepcopy((source, content))
        response = source_result(q, source, content)
        self.assertIsNone(validate_source_response(response, q))
        self.assertEqual(response['result']['locator_verification'],
                         {'status': 'VERIFIED', 'reason': 'HASH_AND_RANGE_MATCH'})
        self.assertNotEqual(source['version'], content['target_version'])
        self.assertEqual(response['result']['source'], source)
        self.assertEqual((source, content), original)
        source['location']['verification'] = 'VERIFIED'
        self.assertEqual(response['result']['source']['location']['verification'], 'UNVERIFIED')

    def test_explicit_record_target_is_not_source_body_fallback(self):
        q, source, content = fictional_source()
        source['location']['target'] = 'RECORD_BODY'
        content.update(target_kind='RECORD_BODY', target_id=q['record_id'],
                       target_version='record-v9', locator_target_version='record-v9')
        result = source_result(q, source, content)['result']
        self.assertEqual(result['content']['target_kind'], 'RECORD_BODY')
        self.assertEqual(result['locator_verification']['status'], 'VERIFIED')
        content['target_id'] = q['source_id']
        with self.assertRaises(SourceReadError):
            source_result(q, source, content)

    def test_identity_version_body_hash_and_extra_fields_are_rejected(self):
        q, source, content = fictional_source()
        response = source_result(q, source, content)
        for field in ('dataset_id', 'snapshot_id', 'record_id', 'source_id', 'source_version'):
            with self.subTest(field=field):
                changed = copy.deepcopy(response)
                changed['result'][field] = 'fictional-other'
                with self.assertRaises(SourceReadError):
                    validate_source_response(changed, q)
        for mutate in (
            lambda r: r['source'].update(source_id='fictional-other'),
            lambda r: r['source'].update(version='fictional-other'),
            lambda r: r['content'].update(target_id='fictional-other'),
            lambda r: r['content'].update(target_version=None),
            lambda r: r['content'].update(text='fictional modified text'),
            lambda r: r['content'].update(text_sha256='0' * 64),
            lambda r: r['content'].update(text='\ud800'),
            lambda r: r.update(path='fictional-private-path'),
        ):
            changed = copy.deepcopy(response)
            mutate(changed['result'])
            with self.assertRaises(SourceReadError) as caught:
                validate_source_response(changed, q)
            self.assertNotIn('fictional', str(caught.exception))

    def test_missing_unknown_inaccessible_and_unsupported_do_not_create_text(self):
        q, source, _ = fictional_source()
        for availability in ('MISSING', 'UNKNOWN', 'INACCESSIBLE', 'NOT_SUPPORTED'):
            with self.subTest(availability=availability):
                content = dict(availability=availability, target_kind=None, target_id=None,
                               target_version=None, locator_target_version=None,
                               text=None, text_sha256=None)
                result = source_result(q, source, content)['result']
                self.assertEqual(result['content']['availability'], availability)
                self.assertIsNone(result['content']['text'])
                self.assertEqual(result['locator_verification']['status'], 'NOT_AVAILABLE')
                content['text'] = ''
                with self.assertRaises(SourceReadError):
                    source_result(q, source, content)

    def test_empty_available_text_is_distinct_from_missing(self):
        q, source, content = fictional_source()
        content.update(text='', text_sha256=digest(''))
        source['location'].update(start=0, end=0, target_sha256=digest(''),
                                  fragment_sha256=digest(''))
        result = source_result(q, source, content)['result']
        self.assertEqual(result['content']['text'], '')
        self.assertEqual(result['locator_verification']['status'], 'VERIFIED')

    def test_inherited_verification_never_bypasses_target_version_or_hash_checks(self):
        q, source, content = fictional_source()
        source['verification'] = source['location']['verification'] = 'VERIFIED'
        cases = [
            ('NOT_SUPPORTED', 'TARGET_NOT_SUPPORTED',
             lambda s, c: s['location'].update(target='Recovery-original')),
            ('NOT_SUPPORTED', 'TARGET_NOT_SUPPORTED',
             lambda s, c: s['location'].update(target='RECORD_BODY')),
            ('NOT_SUPPORTED', 'UNIT_NOT_SUPPORTED',
             lambda s, c: s['location'].update(unit='byte')),
            ('NOT_SUPPORTED', 'UNIT_NOT_SUPPORTED',
             lambda s, c: s['location'].update(unit='unicode_code_point')),
            ('UNVERIFIED', 'LOCATION_NOT_PROVIDED', lambda s, c: s.update(location=None)),
            ('UNVERIFIED', 'TARGET_VERSION_UNCONFIRMED', lambda s, c: c.update(locator_target_version=None)),
            ('MISMATCH', 'TARGET_VERSION_MISMATCH', lambda s, c: c.update(locator_target_version='prior-v1')),
            ('UNVERIFIED', 'HASH_NOT_PROVIDED', lambda s, c: s['location'].update(fragment_sha256=None)),
            ('UNVERIFIED', 'HASH_NOT_PROVIDED', lambda s, c: s['location'].update(target_sha256=None)),
            ('MISMATCH', 'TARGET_HASH_MISMATCH', lambda s, c: s['location'].update(target_sha256='0' * 64)),
            ('MISMATCH', 'FRAGMENT_HASH_MISMATCH', lambda s, c: s['location'].update(fragment_sha256='0' * 64)),
            ('MISMATCH', 'INVALID_RANGE', lambda s, c: s['location'].update(start=8, end=7)),
            ('MISMATCH', 'INVALID_RANGE', lambda s, c: s['location'].update(end=1000)),
            ('MISMATCH', 'INVALID_RANGE', lambda s, c: s['location'].update(start=3)),
            ('MISMATCH', 'INVALID_RANGE', lambda s, c: s['location'].update(end=3)),
        ]
        for status, reason, mutate in cases:
            with self.subTest(reason=reason):
                s, c = copy.deepcopy(source), copy.deepcopy(content)
                mutate(s, c)
                result = source_result(q, s, c)['result']
                self.assertEqual(result['locator_verification'], {'status': status, 'reason': reason})
                forged = copy.deepcopy(result)
                forged['locator_verification'] = {'status': 'VERIFIED', 'reason': 'HASH_AND_RANGE_MATCH'}
                with self.assertRaises(SourceReadError):
                    validate_source_response(dict(contract_version=VERSION, status='OK', result=forged, error=None), q)

    def test_error_helper_is_safe_and_upstream_errors_need_server_sanitization(self):
        q, _, _ = fictional_source()
        for code in ('ITEM_NOT_FOUND', 'SOURCE_MISSING', 'CONNECTION_UNAVAILABLE',
                     'VERSION_MISMATCH', 'UNSUPPORTED_CONTRACT', 'INVALID_REQUEST',
                     'INTERNAL_ERROR', 'NOT_SUPPORTED'):
            response = source_error(code)
            self.assertIsNone(validate_source_response(response, q))
            self.assertEqual(response['error']['retryable'], code == 'CONNECTION_UNAVAILABLE')
        self.assertNotIn('fictional-private', json.dumps(source_error('fictional-private-diagnostic')))
        upstream = source_error('NOT_SUPPORTED')
        upstream['error'].update(message='fictional-private-diagnostic', retryable=True)
        self.assertIsNone(validate_source_response(upstream, q))
        # Shape validation preserves codes/retry policy; the server must replace
        # provider messages before crossing its public response boundary.
        self.assertNotIn('fictional-private', source_error(upstream['error']['code'])['error']['message'])


if __name__ == '__main__':
    unittest.main()
