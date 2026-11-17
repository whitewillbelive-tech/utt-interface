"""Dropbox content-hash checks on invented in-memory byte strings only."""
import contextlib
import array
import hashlib
import io
import socket
import unittest
from unittest.mock import patch

from app.dropbox_content_hash import DropboxContentHasher, hash_stream


def expected_hash(payload):
    size = 4194304
    hashes = b''.join(hashlib.sha256(payload[i:i + size]).digest()
                      for i in range(0, len(payload), size))
    return hashlib.sha256(hashes).hexdigest()


class DropboxContentHashTests(unittest.TestCase):
    def setUp(self):
        guard = patch.object(socket, 'socket', side_effect=AssertionError('network forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_independent_known_vectors_include_block_boundaries(self):
        vectors = [
            (b'', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'),
            (b'abc', '4f8b42c22dd3729b519ba6f68d2da7cc5b2d606d05daed5ad5128cc03e6c6358'),
            (b'\0' * 4194304, 'c7e946d101855255d919ef0c70718633adf77d3dfb3adeeecf5d0cb4e951be58'),
            (b'\0' * 4194304 + b'Z', '9438757c5a030145efed09f8c7f20a7cfcc8c8e76af014ca569b50be50fa7b8d'),
        ]
        for payload, expected in vectors:
            with self.subTest(size=len(payload)):
                hasher = DropboxContentHasher()
                hasher.update(payload)
                result = hasher.digest()
                self.assertEqual(result.size, len(payload))
                self.assertTrue(result.matches_hex(expected))
                if payload:
                    self.assertFalse(result.matches_hex(hashlib.sha256(payload).hexdigest()))

    def test_fragmentation_and_multiple_snapshots_preserve_state(self):
        payload = b'A' * 4194303 + b'XYZ' + b'B' * 29
        whole = DropboxContentHasher()
        whole.update(payload)
        fragmented = DropboxContentHasher()
        for i in range(0, len(payload), 7919):
            fragmented.update(payload[i:i + 7919])
        before = fragmented.digest()
        self.assertEqual(before.size, len(payload))
        self.assertTrue(before.matches_hex(expected_hash(payload)))
        self.assertTrue(fragmented.digest().matches_hex(expected_hash(payload)))
        self.assertTrue(whole.digest().matches_hex(expected_hash(payload)))
        fragmented.update(b'C')
        self.assertEqual(before.size, len(payload))
        self.assertEqual(fragmented.digest().size, len(payload) + 1)
        self.assertTrue(before.matches_hex(expected_hash(payload)))
        self.assertTrue(fragmented.digest().matches_hex(expected_hash(payload + b'C')))

    def test_stream_helper_reads_in_bounded_chunks(self):
        payload = (b'fictional-payload-' * 2048) + b'end'
        class Limited(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 257:
                    raise AssertionError('unbounded read')
                return super().read(size)
        result = hash_stream(Limited(payload), read_size=257)
        self.assertTrue(result.matches_hex(expected_hash(payload)))
        self.assertEqual(result.size, len(payload))

    def test_short_reads_do_not_signal_eof(self):
        class Short(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 3))
        payload = b'fictional-short-stream'
        digest = hash_stream(Short(payload), read_size=64)
        self.assertTrue(digest.matches_hex(expected_hash(payload)))
        self.assertEqual(digest.size, len(payload))

    def test_bytes_like_mutable_inputs_and_strict_hash_comparison(self):
        original = bytearray(b'fictional')
        hasher = DropboxContentHasher()
        hasher.update(original)
        original[:] = b'altered!!'
        hasher.update(memoryview(b'-tail'))
        digest = hasher.digest()
        expected = expected_hash(b'fictional-tail')
        self.assertTrue(digest.matches_hex(expected))
        self.assertTrue(digest.matches_hex(expected.upper()))
        for value in (None, '', 'z' * 64, expected[:-1], expected + '0', 0):
            with self.subTest(value=type(value)):
                self.assertFalse(digest.matches_hex(value))

    def test_invalid_inputs_and_unreliable_stream_are_sanitized(self):
        hasher = DropboxContentHasher()
        for invalid in (None, 'fictional-secret', 3, memoryview(b'abcdef')[::2]):
            with self.subTest(kind=type(invalid)):
                with self.assertRaisesRegex(ValueError, '^INVALID_BYTES$'):
                    hasher.update(invalid)
        for invalid_size in (0, -1, True, 4194305, 1.5):
            with self.subTest(size=invalid_size):
                with self.assertRaisesRegex(ValueError, '^INVALID_READ_SIZE$'):
                    hash_stream(io.BytesIO(b''), read_size=invalid_size)
        class Broken:
            def read(self, size):
                raise RuntimeError('fictional-private-diagnostic')
        class NonBinary:
            def read(self, size):
                return 'fictional-private-data'
        class OverRead:
            def read(self, size):
                return b'x' * (size + 1)
        class WideView:
            def read(self, size):
                return memoryview(array.array('I', [0] * size))
        class NonContiguous:
            def read(self, size):
                return memoryview(b'abcdef')[::2]
        for stream, code in ((Broken(), 'STREAM_UNAVAILABLE'),
                             (NonBinary(), 'INVALID_STREAM'),
                             (OverRead(), 'INVALID_STREAM'),
                             (WideView(), 'INVALID_STREAM'),
                             (NonContiguous(), 'INVALID_STREAM')):
            with self.subTest(code=code):
                with self.assertRaisesRegex(ValueError, '^' + code + '$') as error:
                    hash_stream(stream, read_size=32)
                self.assertNotIn('fictional-private', str(error.exception))
                self.assertIsNone(error.exception.__cause__)

    def test_digest_repr_does_not_expose_hash_or_fictional_content(self):
        hasher = DropboxContentHasher()
        hasher.update(b'fictional-sensitive-byte-sequence')
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            digest = hasher.digest()
        rendered = captured.getvalue() + repr(digest) + str(digest) + repr(hasher)
        self.assertNotIn('fictional-sensitive-byte-sequence', rendered)
        self.assertNotIn(hashlib.sha256(b'fictional-sensitive-byte-sequence').hexdigest(), rendered)
        self.assertNotIn(hashlib.sha256(hashlib.sha256(b'fictional-sensitive-byte-sequence').digest()).hexdigest(), rendered)


if __name__ == '__main__':
    unittest.main()
