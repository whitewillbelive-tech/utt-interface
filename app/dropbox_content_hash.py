"""Dropbox content hash for a caller-supplied binary stream.

This is only a byte hashing primitive. It does not open files, contact Dropbox,
establish that a stream is an immutable SQLite snapshot, validate a database or
publish anything. A caller must independently establish the source's identity
and stability, including any SQLite auxiliary files, before using the result.
An object or its label is not proof against arbitrary code in the same process.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import BinaryIO


BLOCK_SIZE = 4 * 1024 * 1024
_HEX = re.compile(r'[0-9a-fA-F]{64}\Z')


class DropboxDigest:
    """Opaque result; its representation omits both the digest and byte count."""

    __slots__ = ('__value', 'size')

    def __init__(self, value: bytes, size: int):
        self.__value = value
        self.size = size

    def __repr__(self) -> str:
        return '<DropboxDigest redacted>'

    def matches_hex(self, expected: str) -> bool:
        """Compare against a Dropbox metadata hash without printing either value."""
        if not isinstance(expected, str) or not _HEX.fullmatch(expected):
            return False
        return hmac.compare_digest(self.__value, bytes.fromhex(expected))


class DropboxContentHasher:
    """Incremental SHA-256 of concatenated 4 MiB block SHA-256 digests."""

    __slots__ = ('__overall', '__block', '__block_bytes', '__size')

    def __init__(self):
        self.__overall = hashlib.sha256()
        self.__block = hashlib.sha256()
        self.__block_bytes = 0
        self.__size = 0

    def __repr__(self) -> str:
        return '<DropboxContentHasher redacted>'

    def update(self, data: bytes | bytearray | memoryview) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError('INVALID_BYTES')
        try:
            data_view = memoryview(data).cast('B')
        except (TypeError, ValueError):
            raise ValueError('INVALID_BYTES') from None
        position = 0
        while position < len(data_view):
            size = min(BLOCK_SIZE - self.__block_bytes, len(data_view) - position)
            self.__block.update(data_view[position:position + size])
            position += size
            self.__size += size
            self.__block_bytes += size
            if self.__block_bytes == BLOCK_SIZE:
                self.__overall.update(self.__block.digest())
                self.__block = hashlib.sha256()
                self.__block_bytes = 0

    def digest(self) -> DropboxDigest:
        """Return a snapshot; further updates do not change this result."""
        overall = self.__overall.copy()
        if self.__block_bytes:
            overall.update(self.__block.copy().digest())
        return DropboxDigest(overall.digest(), self.__size)


def hash_stream(stream: BinaryIO, *, read_size: int = 1024 * 1024) -> DropboxDigest:
    """Hash a binary stream in bounded chunks, with sanitized read errors."""
    if type(read_size) is not int or not 1 <= read_size <= BLOCK_SIZE:
        raise ValueError('INVALID_READ_SIZE')
    hasher = DropboxContentHasher()
    while True:
        try:
            chunk = stream.read(read_size)
        except Exception:
            raise ValueError('STREAM_UNAVAILABLE') from None
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise ValueError('INVALID_STREAM')
        try:
            chunk_view = memoryview(chunk).cast('B')
        except (TypeError, ValueError):
            raise ValueError('INVALID_STREAM') from None
        if len(chunk_view) > read_size:
            raise ValueError('INVALID_STREAM')
        if not chunk_view:
            return hasher.digest()
        hasher.update(chunk_view)
