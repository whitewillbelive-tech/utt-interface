"""In-memory simulation of Dropbox upload-session sequencing, never a publisher.

The native A generation, manifest, COMMIT, approval and active-reference
formats have not been supplied. There is no network, file open, Dropbox
transport, live authorization, publication, retry or cleanup in this module.
The public entry fails closed before inspecting inputs unless a private test
gate is explicitly patched and an exact in-memory synthetic transport is used.

Even an entirely fictional finish hash match is not independent remote
re-verification, a stable SQLite snapshot, or permission to publish. Same-
process code mutation is not a security boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from app.dropbox_content_hash import DropboxContentHasher

_TEST_GATE = None  # no production issuer
_MAX_CHUNK = 8 * 1024 * 1024  # below the Dropbox per-request 150 MiB limit
_MAX_RESPONSE = 65536
_MAX_CALLS = 10000


@dataclass(frozen=True)
class UploadResult:
    """Fixed status only: never a path, ID, secret, bytes or hash."""
    status: str
    source: str = 'SYNTHETIC'
    canonical: str = 'UNKNOWN'
    snapshot: str = 'UNKNOWN'


class SyntheticUploadTransport:
    """Exact built-in in-memory fake; it cannot access network or filesystem.

    The caller must provide invented response fixtures only. ``calls`` contains
    only fixed phase names, offsets, and lengths, never request values or bytes.
    A status of None simulates an ambiguous/lost response, not a real request.
    """
    __slots__ = ('__responses', '__next', 'calls')

    def __init__(self, responses: list[tuple[int | None, bytes]]):
        if type(responses) is not list or len(responses) > _MAX_CALLS:
            raise ValueError('INVALID_FIXTURE')
        if any(type(item) is not tuple or len(item) != 2 or
               (item[0] is not None and type(item[0]) is not int) or
               type(item[1]) is not bytes or len(item[1]) > _MAX_RESPONSE
               for item in responses):
            raise ValueError('INVALID_FIXTURE')
        self.__responses = tuple(responses)
        self.__next = 0
        self.calls: list[tuple[str, int, int]] = []

    def __repr__(self) -> str:
        return '<SyntheticUploadTransport redacted>'

    def _reply(self, phase: str, offset: int, size: int):
        self.calls.append((phase, offset, size))
        if self.__next >= len(self.__responses):
            return None, b''
        result = self.__responses[self.__next]
        self.__next += 1
        return result


class _DuplicateKey(ValueError):
    pass


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey()
        value[key] = item
    return value


def _path_ok(value: Any) -> bool:
    return (type(value) is str and 2 <= len(value) <= 1024
            and value.startswith('/fictional-') and not value.endswith('/')
            and '//' not in value and '\\' not in value
            and all(part not in ('.', '..') for part in value.split('/'))
            and all(32 <= ord(c) != 127 and not 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _fictional_token(value: Any) -> bool:
    return (type(value) is str and value.startswith('fictional-') and len(value) <= 200
            and all(33 <= ord(c) <= 126 for c in value))


def _request(transport: SyntheticUploadTransport, phase: str, argument: dict[str, Any],
             payload: bytes, _guard: Any):
    """Simulate one bounded Dropbox operation, without a network transport."""
    if _TEST_GATE is None or _guard is not _TEST_GATE or type(transport) is not SyntheticUploadTransport:
        return 'BLOCKED', None
    if phase not in ('start', 'append_v2', 'finish') or type(argument) is not dict or type(payload) is not bytes:
        return 'INVALID_REQUEST', None
    offset = 0 if phase == 'start' else argument['cursor']['offset']
    status, data = transport._reply(phase, offset, len(payload))
    if status != 200:
        return ('REQUEST_REJECTED' if status in (400, 401, 403) else 'UPLOAD_UNCERTAIN'), None
    if not data:
        return 'OK', None
    try:
        return 'OK', json.loads(data.decode('utf-8'), object_pairs_hook=_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        return 'UPLOAD_UNCERTAIN', None


def _read_exact_chunk(stream, wanted: int):
    pieces = bytearray()
    while len(pieces) < wanted:
        value = stream.read(wanted - len(pieces))
        if type(value) is not bytes or len(value) > wanted - len(pieces) or not value:
            raise ValueError('INVALID_SOURCE')
        pieces.extend(value)
    return bytes(pieces)


def upload_staged(stream: Any, *, target: Any, bearer: Any, expected_size: Any,
                  chunk_size: int = 4 * 1024 * 1024, _guard: Any = None,
                  _transport: Any = None) -> UploadResult:
    """Run fictional start→append→finish; the real operation stays BLOCKED.

    There is no automatic retry or cleanup. Once a simulated start has
    succeeded, source errors return STAGING_INCOMPLETE. Any uncertain response
    may represent a staged artifact and must never be blindly retried.
    """
    if _TEST_GATE is None or _guard is not _TEST_GATE or type(_transport) is not SyntheticUploadTransport:
        return UploadResult('BLOCKED', source='LIVE_NOT_RUN')
    if (not _path_ok(target) or not _fictional_token(bearer)
            or type(expected_size) is not int or not 0 <= expected_size < 2**63
            or type(chunk_size) is not int or not 1 <= chunk_size <= _MAX_CHUNK
            or (expected_size + chunk_size - 1) // chunk_size + 1 > _MAX_CALLS):
        return UploadResult('INVALID_REQUEST')
    digest = DropboxContentHasher()
    started = False
    try:
        first_length = min(expected_size, chunk_size)
        first = _read_exact_chunk(stream, first_length)
        digest.update(first)
        status, response = _request(_transport, 'start', {'close': False}, first, _guard)
        if status != 'OK':
            return UploadResult(status)
        started = True
        if (type(response) is not dict or set(response) != {'session_id'}
                or not _fictional_token(response['session_id'])):
            return UploadResult('UPLOAD_UNCERTAIN')
        session_id = response['session_id']
        offset = first_length
        while offset < expected_size:
            piece = _read_exact_chunk(stream, min(chunk_size, expected_size - offset))
            digest.update(piece)
            status, response = _request(_transport, 'append_v2',
                                        {'cursor': {'session_id': session_id, 'offset': offset},
                                         'close': False}, piece, _guard)
            if status != 'OK':
                return UploadResult('STAGING_INCOMPLETE' if status == 'REQUEST_REJECTED' else status)
            if response is not None:
                return UploadResult('UPLOAD_UNCERTAIN')
            offset += len(piece)
        if stream.read(1) != b'':
            return UploadResult('STAGING_INCOMPLETE')
        status, metadata = _request(_transport, 'finish',
                                    {'cursor': {'session_id': session_id, 'offset': offset},
                                     'commit': {'path': target, 'mode': 'add', 'autorename': False,
                                                'mute': False, 'strict_conflict': True}}, b'', _guard)
        if status != 'OK':
            return UploadResult('STAGING_INCOMPLETE' if status == 'REQUEST_REJECTED' else status)
        # finish returns a concrete FileMetadata, not a union that requires a
        # .tag; accept the concrete form and reject contradictory tags.
        if (type(metadata) is not dict or metadata.get('.tag') not in (None, 'file')
                or not _fictional_token(metadata.get('id')) or metadata.get('path_lower') != target
                or metadata.get('symlink_info') is not None or type(metadata.get('size')) is not int
                or metadata['size'] != expected_size or type(metadata.get('content_hash')) is not str):
            return UploadResult('STAGED_UNVERIFIED')
        if not digest.digest().matches_hex(metadata['content_hash']):
            return UploadResult('STAGED_HASH_MISMATCH')
        return UploadResult('SYNTHETIC_FINISH_HASH_MATCH')
    except Exception:
        return UploadResult('STAGING_INCOMPLETE' if started else 'INVALID_SOURCE')
