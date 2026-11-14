"""Invented data and in-memory upload-session responses only; no network."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import http.client
import io
import json
import socket
import unittest
from unittest.mock import patch

from app import dropbox_upload_session as uploader


def content_hash(data):
    block = 4194304
    parts = b''.join(hashlib.sha256(data[i:i + block]).digest()
                     for i in range(0, len(data), block))
    return hashlib.sha256(parts).hexdigest()


def response(data):
    return json.dumps(data, ensure_ascii=True, separators=(',', ':')).encode()


def start():
    return (200, response({'session_id': 'fictional-session'}))


def finish(payload, **changes):
    value = {'.tag': 'file', 'id': 'fictional-file', 'path_lower': '/fictional-target',
             'size': len(payload), 'content_hash': content_hash(payload)}
    value.update(changes)
    return (200, response(value))


class UploadSessionTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)
        self.test_gate = object()
        patcher = patch.object(uploader, '_TEST_GATE', self.test_gate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, source, transport, *, size, chunk_size=4, target='/fictional-target',
             bearer='fictional-token'):
        return uploader.upload_staged(source, target=target, bearer=bearer,
                                      expected_size=size, chunk_size=chunk_size,
                                      _guard=self.test_gate, _transport=transport)

    def test_live_gate_does_not_touch_inputs_or_network(self):
        class Poison:
            def __getattribute__(self, name):
                raise AssertionError('blocked input accessed')
        with patch.object(uploader, '_TEST_GATE', None):
            result = uploader.upload_staged(Poison(), target=Poison(), bearer=Poison(),
                                            expected_size=Poison(), _transport=Poison())
        self.assertEqual(result.status, 'BLOCKED')
        self.assertEqual((result.source, result.canonical, result.snapshot),
                         ('LIVE_NOT_RUN', 'UNKNOWN', 'UNKNOWN'))

    def test_non_fake_transport_cannot_reach_stream_or_network_even_with_test_gate(self):
        class Poison:
            def read(self, *_):
                raise AssertionError('stream read on blocked transport')
        for transport in (http.client.HTTPSConnection, lambda *_: None, object()):
            with self.subTest(transport=type(transport)):
                result = self.call(Poison(), transport, size=1)
                self.assertEqual(result.status, 'BLOCKED')

    def test_staged_chunks_use_exact_offsets_and_add_only_commit(self):
        payload = b'abcdefghijkl'
        fake = uploader.SyntheticUploadTransport([start(), (200, b''), (200, b''), finish(payload)])
        with patch.object(uploader, '_request', wraps=uploader._request) as request:
            result = self.call(io.BytesIO(payload), fake, size=len(payload))
        self.assertEqual((result.status, result.source, result.canonical, result.snapshot),
                         ('SYNTHETIC_FINISH_HASH_MATCH', 'SYNTHETIC', 'UNKNOWN', 'UNKNOWN'))
        self.assertEqual(fake.calls, [('start', 0, 4), ('append_v2', 4, 4),
                                      ('append_v2', 8, 4), ('finish', 12, 0)])
        commit = request.call_args_list[-1].args[2]['commit']
        self.assertEqual(commit['mode'], 'add')
        self.assertFalse(commit['autorename'])
        self.assertTrue(commit['strict_conflict'])

    def test_empty_exact_and_short_reads(self):
        class Short(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 2))
        for payload in (b'', b'abcd', b'abcdefghi'):
            with self.subTest(size=len(payload)):
                remaining = max(0, (len(payload) - min(4, len(payload)) + 3) // 4)
                fake = uploader.SyntheticUploadTransport([start()] + [(200, b'')] * remaining + [finish(payload)])
                self.assertEqual(self.call(Short(payload), fake, size=len(payload)).status,
                                 'SYNTHETIC_FINISH_HASH_MATCH')

    def test_truncated_or_extra_source_cannot_finish(self):
        for payload, declared, expected in ((b'abc', 4, 'INVALID_SOURCE'),
                                            (b'abcde', 4, 'STAGING_INCOMPLETE'),
                                            (b'abcde', 8, 'STAGING_INCOMPLETE')):
            with self.subTest(actual=len(payload), declared=declared):
                fake = uploader.SyntheticUploadTransport([start(), (200, b''), finish(payload)])
                self.assertEqual(self.call(io.BytesIO(payload), fake, size=declared).status, expected)
                self.assertFalse(any(call[0] == 'finish' for call in fake.calls))

    def test_ambiguous_or_rejected_append_never_retries_or_finishes(self):
        for status, expected in ((None, 'UPLOAD_UNCERTAIN'), (409, 'UPLOAD_UNCERTAIN'),
                                 (503, 'UPLOAD_UNCERTAIN'), (302, 'UPLOAD_UNCERTAIN'),
                                 (403, 'STAGING_INCOMPLETE')):
            with self.subTest(status=status):
                fake = uploader.SyntheticUploadTransport([start(), (status, b'fictional-private'), finish(b'abcdefgh')])
                self.assertEqual(self.call(io.BytesIO(b'abcdefgh'), fake, size=8).status, expected)
                self.assertEqual(fake.calls, [('start', 0, 4), ('append_v2', 4, 4)])

    def test_lost_start_or_finish_is_uncertain_and_never_retried(self):
        for responses, expected_phases in [([(None, b'')], ['start']),
                                           ([start(), (503, b'')], ['start', 'finish']),
                                           ([start(), (None, b'')], ['start', 'finish']),
                                           ([(200, b'{"session_id":"fictional-x",'
                                             b'"session_id":"fictional-y"}')], ['start'])]:
            with self.subTest(phases=expected_phases):
                fake = uploader.SyntheticUploadTransport(responses)
                result = self.call(io.BytesIO(b'abc'), fake, size=3)
                self.assertEqual(result.status, 'UPLOAD_UNCERTAIN')
                self.assertEqual([call[0] for call in fake.calls], expected_phases)

    def test_synthetic_151_mib_stream_uses_multiple_bounded_chunks(self):
        mebibyte = 1024 * 1024
        total = 151 * mebibyte
        block_size = 4 * mebibyte
        class FictionalStream:
            def __init__(self):
                self.remaining = total
            def read(self, size):
                if not 0 < size <= block_size:
                    raise AssertionError('unbounded read')
                take = min(size, self.remaining)
                self.remaining -= take
                return b'x' * take
        block_digest = hashlib.sha256(b'x' * block_size).digest()
        tail_digest = hashlib.sha256(b'x' * (3 * mebibyte)).digest()
        expected = hashlib.sha256(block_digest * 37 + tail_digest).hexdigest()
        metadata = {'.tag': 'file', 'id': 'fictional-file', 'path_lower': '/fictional-target',
                    'size': total, 'content_hash': expected}
        fake = uploader.SyntheticUploadTransport([start()] + [(200, b'')] * 37 + [(200, response(metadata))])
        result = self.call(FictionalStream(), fake, size=total, chunk_size=block_size)
        self.assertEqual(result.status, 'SYNTHETIC_FINISH_HASH_MATCH')
        self.assertEqual(len(fake.calls), 39)
        self.assertTrue(all(call[2] <= block_size for call in fake.calls))
        self.assertEqual(fake.calls[-1], ('finish', total, 0))

    def test_malformed_duplicate_or_inconsistent_finish_cannot_succeed(self):
        payload = b'abc'
        cases = [((200, b'{"size":3,"size":3}'), 'UPLOAD_UNCERTAIN'),
                 (finish(payload, size=99), 'STAGED_UNVERIFIED'),
                 (finish(payload, path_lower='/fictional-wrong'), 'STAGED_UNVERIFIED'),
                 (finish(payload, **{'.tag': 'folder'}), 'STAGED_UNVERIFIED'),
                 (finish(payload, id='real-looking-id'), 'STAGED_UNVERIFIED'),
                 (finish(payload, symlink_info={'target': '/fictional-other'}), 'STAGED_UNVERIFIED'),
                 (finish(payload, content_hash='0' * 64), 'STAGED_HASH_MISMATCH')]
        for terminal, expected in cases:
            with self.subTest(expected=expected):
                fake = uploader.SyntheticUploadTransport([start(), terminal])
                result = self.call(io.BytesIO(payload), fake, size=len(payload))
                self.assertEqual(result.status, expected)
                self.assertEqual(result.canonical, 'UNKNOWN')

    def test_finish_file_metadata_can_omit_union_tag(self):
        payload = b'abc'
        value = {'id': 'fictional-file', 'path_lower': '/fictional-target',
                 'size': len(payload), 'content_hash': content_hash(payload)}
        fake = uploader.SyntheticUploadTransport([start(), (200, response(value))])
        result = self.call(io.BytesIO(payload), fake, size=len(payload))
        self.assertEqual(result.status, 'SYNTHETIC_FINISH_HASH_MATCH')
        self.assertEqual((result.canonical, result.snapshot), ('UNKNOWN', 'UNKNOWN'))

    def test_request_limits_and_fictional_sentinels_fail_before_read(self):
        class Poison:
            def read(self, *_):
                raise AssertionError('invalid request touched stream')
        fake = uploader.SyntheticUploadTransport([])
        for kwargs in ({'size': 1, 'chunk_size': 0}, {'size': 1, 'chunk_size': 8388609},
                       {'size': True}, {'size': 1, 'target': '/real-target'},
                       {'size': 1, 'bearer': 'not-fictional'}):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(self.call(Poison(), fake, **kwargs).status, 'INVALID_REQUEST')
                self.assertEqual(fake.calls, [])

    def test_no_token_path_session_payload_or_error_details_are_exposed(self):
        captured = io.StringIO()
        fake = uploader.SyntheticUploadTransport([start(), (None, b'fictional-private-token')])
        with redirect_stdout(captured), redirect_stderr(captured):
            result = self.call(io.BytesIO(b'abcdefgh'), fake, size=8)
        rendered = captured.getvalue() + repr(result) + str(result) + repr(fake) + repr(fake.calls)
        for value in ('fictional-token', 'fictional-target', 'fictional-session', 'fictional-private', 'abcdefgh'):
            self.assertNotIn(value, rendered)


if __name__ == '__main__':
    unittest.main()
