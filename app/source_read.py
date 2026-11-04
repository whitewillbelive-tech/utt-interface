"""UTT-SOURCE-READ/0.1.0: independent, identity-bound source text reads.

Requests have exactly contract_version, dataset_id, snapshot_id, record_id,
source_id and source_version; all are nonempty strings. An unknown source
version prevents body reads, while the existing detail metadata remains usable.
The old UTT-ACCESS-READ/0.1.0 request/response schema is not extended.

An OK result echoes the five identity fields and contains unchanged ``source``
metadata, explicit ``content`` and a fresh ``locator_verification``. Content has
availability, target_kind, target_id, target_version, locator_target_version,
text and text_sha256.
SOURCE_BODY identifies source_id; RECORD_BODY identifies record_id. Its explicit
target version can differ from the source metadata version. AVAILABLE text is
complete, unnormalized Unicode with SHA-256 of its exact UTF-8 serialization.
locator_target_version separately binds the preserved locator to that content
version. Missing proof remains UNVERIFIED; a differing version is MISMATCH.
Unavailable content has null text/hash; no record-body fallback is performed.
Source metadata content_sha256 may identify original file bytes and is never
silently treated as the UTF-8 text hash.

Only exact locator target mappings SOURCE_BODY -> SOURCE_BODY, RECORD_BODY ->
RECORD_BODY and fictional SYNTHETIC_SOURCE -> SOURCE_BODY are supported. No
filename, path, B/Recovery name or hash equivalence selects a target. UTF-16
half-open code-unit ranges alone can be verified here; byte and code-point
locators remain preserved but unsupported. An inherited VERIFIED marker is not
proof: target/fragment hashes and surrogate boundaries are checked afresh.
No module import, validation or helper opens a source file or network endpoint.
"""
import copy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

VERSION = 'UTT-SOURCE-READ/0.1.0'
_SCHEMA = json.loads((Path(__file__).resolve().parents[1] /
                      'contracts/utt-source-read.schema.json').read_text())
_REQUEST = Draft202012Validator({'$ref': '#/$defs/request', '$defs': _SCHEMA['$defs']})
_RESPONSE = Draft202012Validator({'$ref': '#/$defs/response', '$defs': _SCHEMA['$defs']})
_RESULT = Draft202012Validator({'$ref': '#/$defs/result', '$defs': _SCHEMA['$defs']})
_IDENTITY = ('dataset_id', 'snapshot_id', 'record_id', 'source_id', 'source_version')
_TARGETS = {'SOURCE_BODY': 'SOURCE_BODY', 'RECORD_BODY': 'RECORD_BODY',
            'SYNTHETIC_SOURCE': 'SOURCE_BODY'}
_MESSAGES = {
    'ITEM_NOT_FOUND': '이 기준 버전에서 기록을 찾을 수 없습니다.',
    'SOURCE_MISSING': '이 기준 버전에서 출처 누락이 확인됐습니다.',
    'CONNECTION_UNAVAILABLE': '출처 읽기 공급자에 연결할 수 없습니다.',
    'VERSION_MISMATCH': '출처 또는 자료의 기준 버전이 다릅니다.',
    'UNSUPPORTED_CONTRACT': '지원하지 않는 출처 읽기 계약입니다.',
    'INVALID_REQUEST': '잘못된 출처 읽기 요청입니다.',
    'INTERNAL_ERROR': '출처 읽기 응답을 확인할 수 없습니다.',
    'NOT_SUPPORTED': '이 출처 읽기 기능은 지원하지 않습니다.',
}


class SourceReadError(Exception):
    """Safe code only; no source identity, body or provider diagnostics."""
    def __init__(self, code='INTERNAL_ERROR'):
        self.code = code if isinstance(code, str) and code in _MESSAGES else 'INTERNAL_ERROR'
        super().__init__('Source read response rejected.')


def validate_source_request(q):
    """Return None or INVALID_REQUEST/UNSUPPORTED_CONTRACT; perform no I/O."""
    if not isinstance(q, dict) or set(q) != {'contract_version', *_IDENTITY}:
        return 'INVALID_REQUEST'
    if q['contract_version'] != VERSION:
        return 'UNSUPPORTED_CONTRACT'
    if not _REQUEST.is_valid(q):
        return 'INVALID_REQUEST'
    return None


def source_error(code):
    """Return a complete error envelope with a fixed, non-sensitive message."""
    if not isinstance(code, str) or code not in _MESSAGES:
        code = 'INTERNAL_ERROR'
    return {'contract_version': VERSION, 'status': 'ERROR', 'result': None,
            'error': {'code': code, 'message': _MESSAGES[code],
                      'retryable': code == 'CONNECTION_UNAVAILABLE'}}


def _verification(status, reason):
    return {'status': status, 'reason': reason}


def _locator_verification(source, content):
    if content['availability'] != 'AVAILABLE':
        return _verification('NOT_AVAILABLE', 'CONTENT_UNAVAILABLE')
    location = source['location']
    if location is None:
        return _verification('UNVERIFIED', 'LOCATION_NOT_PROVIDED')
    if _TARGETS.get(location['target']) != content['target_kind']:
        return _verification('NOT_SUPPORTED', 'TARGET_NOT_SUPPORTED')
    if location['unit'] != 'UTF-16':
        return _verification('NOT_SUPPORTED', 'UNIT_NOT_SUPPORTED')
    if content['locator_target_version'] is None:
        return _verification('UNVERIFIED', 'TARGET_VERSION_UNCONFIRMED')
    if content['locator_target_version'] != content['target_version']:
        return _verification('MISMATCH', 'TARGET_VERSION_MISMATCH')
    if location['target_sha256'] is None or location['fragment_sha256'] is None:
        return _verification('UNVERIFIED', 'HASH_NOT_PROVIDED')
    if location['target_sha256'] != content['text_sha256']:
        return _verification('MISMATCH', 'TARGET_HASH_MISMATCH')
    encoded = content['text'].encode('utf-16-le')
    start, end = location['start'], location['end']
    if not 0 <= start <= end <= len(encoded) // 2:
        return _verification('MISMATCH', 'INVALID_RANGE')
    try:
        fragment = encoded[start * 2:end * 2].decode('utf-16-le')
    except UnicodeDecodeError:
        return _verification('MISMATCH', 'INVALID_RANGE')
    if hashlib.sha256(fragment.encode('utf-8')).hexdigest() != location['fragment_sha256']:
        return _verification('MISMATCH', 'FRAGMENT_HASH_MISMATCH')
    return _verification('VERIFIED', 'HASH_AND_RANGE_MATCH')


def _validate_result(result, q):
    if not _RESULT.is_valid(result):
        raise SourceReadError()
    for field in _IDENTITY:
        if result[field] != q[field]:
            raise SourceReadError('VERSION_MISMATCH' if field in
                                  ('snapshot_id', 'source_version') else 'INTERNAL_ERROR')
    source, content = result['source'], result['content']
    if source['dataset_id'] != q['dataset_id'] or source['source_id'] != q['source_id']:
        raise SourceReadError()
    if source['version'] != q['source_version']:
        raise SourceReadError('VERSION_MISMATCH')
    kind = content['target_kind']
    if kind is None:
        if content['target_id'] is not None or content['target_version'] is not None:
            raise SourceReadError()
    elif content['target_id'] != q['source_id' if kind == 'SOURCE_BODY' else 'record_id']:
        raise SourceReadError()
    if content['availability'] == 'AVAILABLE':
        try:
            actual = hashlib.sha256(content['text'].encode('utf-8')).hexdigest()
        except UnicodeError:
            raise SourceReadError() from None
        if actual != content['text_sha256']:
            raise SourceReadError()


def source_result(q, source, content):
    """Build a checked OK envelope from explicit source metadata and content.

    The provider must establish record/source membership and immutable target
    version from its own approved snapshot; this helper cannot infer either.
    It deep-copies supplied objects and leaves inherited metadata untouched.
    """
    code = validate_source_request(q)
    if code:
        raise SourceReadError(code)
    result = {field: q[field] for field in _IDENTITY}
    result.update(source=copy.deepcopy(source), content=copy.deepcopy(content),
                  locator_verification=_verification('NOT_AVAILABLE', 'CONTENT_UNAVAILABLE'))
    _validate_result(result, q)
    result['locator_verification'] = _locator_verification(result['source'], result['content'])
    envelope = {'contract_version': VERSION, 'status': 'OK', 'result': result, 'error': None}
    validate_source_response(envelope, q)
    return envelope


def validate_source_response(value, q):
    """Reject shape/scope/hash errors and fabricated verification claims.

    ERROR messages are untrusted and must be sanitized by the calling server;
    validation preserves their contract code and retry policy. A valid return
    (None) is validation, not an access grant or
    evidence that any actual source/record membership was independently checked.
    """
    code = validate_source_request(q)
    if code:
        raise SourceReadError(code)
    if not _RESPONSE.is_valid(value):
        raise SourceReadError()
    if value['status'] == 'ERROR':
        return None
    _validate_result(value['result'], q)
    expected = _locator_verification(value['result']['source'], value['result']['content'])
    if value['result']['locator_verification'] != expected:
        raise SourceReadError()
    return None
