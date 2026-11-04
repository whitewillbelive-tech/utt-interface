"""Explicit source endpoint for UI regressions; generated fiction only.

This helper never reads input paths or configured transports. The regular
synthetic server still has no source provider unless a test injects this one.
"""
import copy
import hashlib

from app.provider import SyntheticProvider, request
from app.server import make_server
from app.source_read import source_error, source_result
from fixtures.synthetic.generate import create


RECORD_TEXT = create()['sources'][0]['text']
SOURCE_TEXT = RECORD_TEXT
TARGET_VERSION = 'fictional-body-v1'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class BrowserSourceProvider:
    def __init__(self, provider):
        self.provider = provider

    def read_source(self, q):
        if q['dataset_id'] != self.provider.dataset['dataset_id']:
            return source_error('ITEM_NOT_FOUND')
        if q['snapshot_id'] != self.provider.dataset['snapshot_id']:
            return source_error('VERSION_MISMATCH')
        detail = self.provider.read(request('detail', dataset_id=q['dataset_id'],
                                            snapshot_id=q['snapshot_id'], record_id=q['record_id']))
        if detail['status'] != 'OK':
            return source_error('ITEM_NOT_FOUND')
        source = next((item for item in detail['result']['sources']
                       if item['source_id'] == q['source_id']), None)
        if source is None:
            return source_error('SOURCE_MISSING')
        if q['source_version'] != source['version']:
            return source_error('VERSION_MISMATCH')
        missing = source['availability'] == 'MISSING'
        content = {
            'availability': 'MISSING' if missing else 'AVAILABLE',
            'target_kind': 'SOURCE_BODY', 'target_id': q['source_id'],
            'target_version': TARGET_VERSION,
            'locator_target_version': None if missing else TARGET_VERSION,
            'text': None if missing else SOURCE_TEXT,
            'text_sha256': None if missing else sha(SOURCE_TEXT),
        }
        return source_result(q, copy.deepcopy(source), content)


def make_source_fixture_server():
    provider = SyntheticProvider()
    try:
        return make_server(0, provider=provider, source_provider=BrowserSourceProvider(provider))
    except Exception:
        provider.close()
        raise
