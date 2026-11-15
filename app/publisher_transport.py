"""Explicit App Folder publication transport; credentials stay in the session.

Only the legacy active reference and a SHA-addressed baseline subtree are
reachable. Calls use the verified publisher session, fixed TLS endpoints,
bounded chunks and no automatic retry. Returned metadata may be sensitive;
its representation is redacted and the caller must keep it on the user PC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import io
import json
import re
import ssl
from types import MappingProxyType
from typing import Any, BinaryIO

from app.dropbox_content_hash import DropboxContentHasher

ACTIVE = '/system/active-baseline.json'
_GEN = re.compile(r'/snapshots/baselines/[0-9a-f]{64}/[A-Za-z0-9._/-]+\Z')
_REV = re.compile(r'[\x21-\x7e]{1,1024}\Z')
_CHUNK = 8 * 1024 * 1024
_MAX_JSON = 1024 * 1024
_MAX_CONTROL = 16 * 1024 * 1024
_MAX_SIZE = 1 << 40
_MISSING = object()
_PERMIT_SEAL = object()
_CODES = frozenset({'PATH_INVALID', 'REQUEST_INVALID', 'RESPONSE_INVALID', 'AUTH_REQUIRED',
                    'ACCESS_DENIED', 'CONFLICT', 'RATE_LIMITED', 'WRITE_UNCERTAIN',
                    'REQUEST_FAILED', 'SOURCE_CHANGED', 'STAGING_INCOMPLETE', 'LIMIT_EXCEEDED',
                    'SINK_FAILED', 'REMOTE_SIZE_MISMATCH', 'REMOTE_HASH_MISMATCH',
                    'PLAN_REQUIRED', 'PLAN_INVALID', 'ACTIVE_CHANGED', 'REMOTE_INCOMPLETE',
                    'COMMITTED_GENERATION_DAMAGED', 'COMMITTED_GENERATION_IMMUTABLE'})


class PublisherTransportError(Exception):
    """Fixed codes only; never include user data or remote error messages."""
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _CODES else 'REQUEST_FAILED'
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class RemoteMetadata:
    size: int
    rev: str
    content_hash: str | None = None

    def __repr__(self):
        return '<RemoteMetadata redacted>'


@dataclass(frozen=True, repr=False)
class RemoteObject:
    size: int
    rev: str
    sha256: str
    content_hash: str | None = None

    def __repr__(self):
        return '<RemoteObject redacted>'


@dataclass(frozen=True, repr=False)
class _Operation:
    kind: str
    path: str
    stream: Any = field(default=None, repr=False)
    size: int | None = None
    data: bytes | None = field(default=None, repr=False)
    rev: str | None = field(default=None, repr=False)
    permit: Any = field(default=None, repr=False)

    def __repr__(self):
        return '<PublisherOperation redacted>'


def _path(path: Any, *, active: bool = True) -> str:
    if type(path) is not str or len(path) > 1024:
        raise PublisherTransportError('PATH_INVALID')
    if path == ACTIVE and active:
        return path
    if not _GEN.fullmatch(path) or 'scrivener' in path.lower():
        raise PublisherTransportError('PATH_INVALID')
    parts = path[1:].split('/')
    if any(not part or part in ('.', '..') for part in parts):
        raise PublisherTransportError('PATH_INVALID')
    return path


class _WritePermit:
    """Immutable session-bound plan. Only the coordinator may issue it.

    Python process integrity is assumed: this is not an OS sandbox. The
    coordinator must issue a permit only after validating the candidate,
    source preservation, user action and the currently observed parent.
    """
    __slots__ = ('__session', '__files', '__active_bytes', '__previous_rev', '__previous_sha256', '__commit_path')

    def __init__(self, session, files, active_bytes, previous_rev, previous_sha256, *, _seal=None):
        if _seal is not _PERMIT_SEAL:
            raise PublisherTransportError('PLAN_REQUIRED')
        self.__session = session
        self.__files = MappingProxyType(dict(files))
        self.__active_bytes = active_bytes
        self.__previous_rev = previous_rev
        self.__previous_sha256 = previous_sha256
        self.__commit_path = _json(active_bytes)['root'] + 'system/COMMIT.json'

    def __repr__(self):
        return '<PublicationWritePermit redacted>'

    def _owns(self, session):
        return self.__session is session

    def _require_valid_session(self):
        if not self.__session.is_valid():
            raise PublisherTransportError('AUTH_REQUIRED')

    def _file(self, path):
        return self.__files.get(path)

    def _entries(self):
        return tuple(sorted(self.__files.items()))

    def _is_commit(self, path):
        return path == self.__commit_path

    def _commit_path(self):
        return self.__commit_path

    def _activation(self, data, rev):
        if data != self.__active_bytes or rev != self.__previous_rev:
            raise PublisherTransportError('PLAN_INVALID')
        return self.__previous_sha256


def _mint_write_permit(session, *, files: dict[str, tuple[int, str]], active_bytes: bytes,
                       previous_rev: str, previous_sha256: str) -> _WritePermit:
    """Bind a coordinator-validated candidate and parent to one session."""
    if (type(files) is not dict or not files or len(files) > 1_000_000
            or type(active_bytes) is not bytes or not 0 < len(active_bytes) <= _MAX_CONTROL
            or type(previous_rev) is not str or not _REV.fullmatch(previous_rev)
            or type(previous_sha256) is not str or not re.fullmatch(r'[0-9a-f]{64}', previous_sha256)):
        raise PublisherTransportError('PLAN_INVALID')
    snapshot = {}
    canonical_paths = set()
    for path, value in files.items():
        _path(path, active=False)
        # Dropbox is case-insensitive; do not silently merge two candidates.
        canonical_path = path.lower()  # _path accepts only ASCII.
        if canonical_path in canonical_paths:
            raise PublisherTransportError('PLAN_INVALID')
        canonical_paths.add(canonical_path)
        if (type(value) is not tuple or len(value) != 2 or type(value[0]) is not int
                or not 0 <= value[0] <= _MAX_SIZE or type(value[1]) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', value[1])):
            raise PublisherTransportError('PLAN_INVALID')
        snapshot[path] = value
    try:
        active = _json(active_bytes)
        fields = {'format', 'generation_id', 'manifest_sha256', 'baseline_id',
                  'root', 'commit_sha256', 'files', 'bytes'}
        if type(active) is not dict or set(active) != fields or active['format'] != 'utt-active-baseline/1':
            raise ValueError()
        generation = active['generation_id']
        if (type(generation) is not str or not re.fullmatch(r'[0-9a-f]{64}', generation)
                or active['manifest_sha256'] != generation
                or type(active['baseline_id']) is not str or not active['baseline_id']
                or len(active['baseline_id']) > 1024 or active['root'] != '/snapshots/baselines/' + generation + '/'
                or type(active['commit_sha256']) is not str
                or not re.fullmatch(r'[0-9a-f]{64}', active['commit_sha256'])
                or type(active['files']) is not int or active['files'] != len(snapshot)
                or type(active['bytes']) is not int or active['bytes'] != sum(x[0] for x in snapshot.values())
                or active['bytes'] > 2**53 - 1):
            raise ValueError()
        root = active['root']
        if (not all(path.startswith(root) for path in snapshot)
                or root + 'system/manifest.json' not in snapshot
                or root + 'system/COMMIT.json' not in snapshot
                or snapshot[root + 'system/manifest.json'][1] != generation
                or snapshot[root + 'system/COMMIT.json'][1] != active['commit_sha256']):
            raise ValueError()
        canonical = (json.dumps(active, sort_keys=True, ensure_ascii=False,
                                separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')
        if active_bytes != canonical:
            raise ValueError()
    except (PublisherTransportError, TypeError, ValueError, UnicodeError, RecursionError):
        raise PublisherTransportError('PLAN_INVALID') from None
    return _WritePermit(session, snapshot, active_bytes, previous_rev, previous_sha256, _seal=_PERMIT_SEAL)


def _unique(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate')
        out[key] = value
    return out


def _json(raw: bytes) -> Any:
    if type(raw) is not bytes or len(raw) > _MAX_JSON:
        raise PublisherTransportError('RESPONSE_INVALID')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, ValueError, RecursionError):
        raise PublisherTransportError('RESPONSE_INVALID') from None


def _metadata(value: Any, expected_path: str) -> RemoteMetadata:
    if (type(value) is not dict or value.get('.tag', 'file') != 'file'
            or value.get('symlink_info') is not None
            or value.get('path_lower') != expected_path.lower()
            or type(value.get('size')) is not int or not 0 <= value['size'] <= _MAX_SIZE
            or type(value.get('rev')) is not str or not _REV.fullmatch(value['rev'])):
        raise PublisherTransportError('RESPONSE_INVALID')
    ch = value.get('content_hash')
    if ch is not None and (type(ch) is not str or not re.fullmatch(r'[0-9a-f]{64}', ch)):
        raise PublisherTransportError('RESPONSE_INVALID')
    return RemoteMetadata(value['size'], value['rev'], ch)


def _is_missing(data: Any) -> bool:
    # Both lookup variants occur in Dropbox's route-specific error unions.
    if type(data) is not dict:
        return False
    e = data.get('error')
    if type(e) is not dict:
        return False
    tag = e.get('.tag')
    return (tag in ('path', 'path_lookup') and type(e.get(tag)) is dict
            and e[tag].get('.tag') == 'not_found')


def _request(token: str, host: str, route: str, argument: dict, payload: bytes,
             *, streaming: bool = False):
    allowed = {
        ('api.dropboxapi.com', '/2/files/get_metadata'),
        ('content.dropboxapi.com', '/2/files/download'),
        ('content.dropboxapi.com', '/2/files/upload'),
        ('content.dropboxapi.com', '/2/files/upload_session/start'),
        ('content.dropboxapi.com', '/2/files/upload_session/append_v2'),
        ('content.dropboxapi.com', '/2/files/upload_session/finish'),
    }
    if (host, route) not in allowed or type(token) is not str or not _REV.fullmatch(token):
        raise PublisherTransportError('REQUEST_INVALID')
    conn = None
    write = route in ('/2/files/upload', '/2/files/upload_session/start',
                      '/2/files/upload_session/append_v2', '/2/files/upload_session/finish')
    try:
        conn = http.client.HTTPSConnection(host, 443, timeout=30, context=ssl.create_default_context())
        conn.set_debuglevel(0)
        headers = {'Authorization': 'Bearer ' + token, 'Cache-Control': 'no-store'}
        if host == 'api.dropboxapi.com':
            headers['Content-Type'] = 'application/json'
            body = json.dumps(argument, ensure_ascii=True, separators=(',', ':')).encode('ascii')
        else:
            headers['Content-Type'] = 'application/octet-stream'
            headers['Dropbox-API-Arg'] = json.dumps(argument, ensure_ascii=True, separators=(',', ':'))
            body = payload
        conn.request('POST', route, body=body, headers=headers)
        response = conn.getresponse()
        if response.status != 200:
            # A lost success may surface as a conflict. The coordinator must
            # re-read the destination before making any subsequent write.
            data = response.read(_MAX_JSON + 1)
            parsed = None
            try:
                parsed = _json(data)
            except PublisherTransportError:
                pass
            if not write and response.status == 409 and _is_missing(parsed):
                conn.close()
                return None, _MISSING
            if response.status == 401:
                raise PublisherTransportError('AUTH_REQUIRED')
            if response.status == 403:
                raise PublisherTransportError('ACCESS_DENIED')
            if response.status == 409:
                raise PublisherTransportError('CONFLICT')
            if response.status == 429:
                raise PublisherTransportError('RATE_LIMITED')
            raise PublisherTransportError('WRITE_UNCERTAIN' if write else 'REQUEST_FAILED')
        if streaming:
            return conn, response
        data = response.read(_MAX_JSON + 1)
        conn.close()
        try:
            return None, (_json(data) if data else None)
        except PublisherTransportError:
            if write:
                raise PublisherTransportError('WRITE_UNCERTAIN') from None
            raise
    except PublisherTransportError:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        raise PublisherTransportError('WRITE_UNCERTAIN' if write else 'REQUEST_FAILED') from None


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    data = bytearray()
    try:
        while len(data) < count:
            part = stream.read(count - len(data))
            if type(part) is not bytes or not part or len(part) > count - len(data):
                raise ValueError()
            data.extend(part)
    except Exception:
        raise PublisherTransportError('SOURCE_CHANGED') from None
    return bytes(data)


def _preflight_source(stream: BinaryIO, size: int, expected_sha256: str) -> None:
    """Hash the exact candidate bytes before opening a remote upload session."""
    try:
        start = stream.tell()
        sha = hashlib.sha256()
        remaining = size
        while remaining:
            block = _read_exact(stream, min(_CHUNK, remaining))
            sha.update(block)
            remaining -= len(block)
        if stream.read(1) != b'' or sha.hexdigest() != expected_sha256:
            raise PublisherTransportError('SOURCE_CHANGED')
        stream.seek(start)
        if stream.tell() != start:
            raise PublisherTransportError('SOURCE_CHANGED')
    except PublisherTransportError:
        raise
    except Exception:
        raise PublisherTransportError('SOURCE_CHANGED') from None


class _HashSink:
    """Discard downloaded bytes after the download operation hashes them."""
    def write(self, value):
        return len(value)


def _verify_remote_catalog(token: str, permit: _WritePermit, *, include_commit: bool) -> None:
    """Independently re-download and verify each bound object; never retry."""
    for path, (expected_size, expected_sha256) in permit._entries():
        if permit._is_commit(path) and not include_commit:
            continue
        permit._require_valid_session()
        item = _dispatch(token, _Operation('download', path, stream=_HashSink(), size=expected_size))
        permit._require_valid_session()
        if item is None:
            raise PublisherTransportError('REMOTE_INCOMPLETE')
        if item.size != expected_size or item.sha256 != expected_sha256:
            raise PublisherTransportError('REMOTE_HASH_MISMATCH')


def _ensure_generation_not_committed(token: str, permit: _WritePermit, target: str) -> None:
    """Never treat a missing object in an already committed generation as repair permission."""
    permit._require_valid_session()
    commit = _dispatch(token, _Operation('metadata', permit._commit_path()))
    permit._require_valid_session()
    if commit is None:
        return
    if permit._is_commit(target):
        raise PublisherTransportError('COMMITTED_GENERATION_IMMUTABLE')
    target_meta = _dispatch(token, _Operation('metadata', target))
    permit._require_valid_session()
    if target_meta is None:
        raise PublisherTransportError('COMMITTED_GENERATION_DAMAGED')
    raise PublisherTransportError('COMMITTED_GENERATION_IMMUTABLE')


def _dispatch(token: str, operation: _Operation):
    """Only session-authorized callable; never returns the bearer token."""
    if type(operation) is not _Operation:
        raise PublisherTransportError('REQUEST_INVALID')
    kind = operation.kind
    path = _path(operation.path, active=kind in ('metadata', 'download', 'update'))
    if kind == 'metadata':
        _, obj = _request(token, 'api.dropboxapi.com', '/2/files/get_metadata',
                          {'path': path, 'include_media_info': False,
                           'include_deleted': False, 'include_has_explicit_shared_members': False}, b'')
        return None if obj is _MISSING else _metadata(obj, path)
    if kind == 'download':
        if operation.stream is None or type(operation.size) is not int or not 0 <= operation.size <= _MAX_SIZE:
            raise PublisherTransportError('REQUEST_INVALID')
        conn, response = _request(token, 'content.dropboxapi.com', '/2/files/download',
                                  {'path': path}, b'', streaming=True)
        if response is _MISSING:
            return None
        try:
            header = response.getheader('Dropbox-API-Result')
            if type(header) is not str or len(header) > _MAX_JSON:
                raise PublisherTransportError('RESPONSE_INVALID')
            meta = _metadata(_json(header.encode('utf-8')), path)
            if meta.size > operation.size:
                raise PublisherTransportError('LIMIT_EXCEEDED')
            sha, total = hashlib.sha256(), 0
            while True:
                part = response.read(min(_CHUNK, operation.size - total + 1))
                if type(part) is not bytes:
                    raise PublisherTransportError('RESPONSE_INVALID')
                if not part:
                    break
                total += len(part)
                if total > operation.size:
                    raise PublisherTransportError('LIMIT_EXCEEDED')
                sha.update(part)
                if operation.stream.write(part) != len(part):
                    raise PublisherTransportError('SINK_FAILED')
            if total != meta.size:
                raise PublisherTransportError('REMOTE_SIZE_MISMATCH')
            return RemoteObject(total, meta.rev, sha.hexdigest(), meta.content_hash)
        except PublisherTransportError:
            raise
        except Exception:
            raise PublisherTransportError('REQUEST_FAILED') from None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    if kind == 'update':
        if path != ACTIVE or type(operation.data) is not bytes or len(operation.data) > _MAX_CONTROL:
            raise PublisherTransportError('REQUEST_INVALID')
        if type(operation.rev) is not str or not _REV.fullmatch(operation.rev):
            raise PublisherTransportError('REQUEST_INVALID')
        if type(operation.permit) is not _WritePermit:
            raise PublisherTransportError('PLAN_REQUIRED')
        previous_hash = operation.permit._activation(operation.data, operation.rev)
        # Re-verify the complete committed candidate immediately before the
        # final parent read and revision-conditional activation.
        _verify_remote_catalog(token, operation.permit, include_commit=True)
        # Bind revision to exactly the bytes observed in the SAME download;
        # a later change is rejected atomically by the Dropbox revision CAS.
        previous_sink = io.BytesIO()
        previous = _dispatch(token, _Operation('download', ACTIVE, stream=previous_sink, size=_MAX_CONTROL))
        if previous is None or previous.rev != operation.rev or previous.sha256 != previous_hash:
            raise PublisherTransportError('ACTIVE_CHANGED')
        operation.permit._require_valid_session()
        _, obj = _request(token, 'content.dropboxapi.com', '/2/files/upload',
                          {'path': path, 'mode': {'.tag': 'update', 'update': operation.rev},
                           'autorename': False, 'strict_conflict': True, 'mute': False}, operation.data)
        try:
            return _metadata(obj, path)
        except PublisherTransportError:
            raise PublisherTransportError('WRITE_UNCERTAIN') from None
    if kind == 'upload':
        if (operation.stream is None or type(operation.size) is not int
                or not 0 <= operation.size <= _MAX_SIZE):
            raise PublisherTransportError('REQUEST_INVALID')
        if type(operation.permit) is not _WritePermit:
            raise PublisherTransportError('PLAN_REQUIRED')
        size = operation.size
        entry = operation.permit._file(path)
        if entry is None or entry[0] != size:
            raise PublisherTransportError('PLAN_INVALID')
        expected_sha256 = entry[1]
        _preflight_source(operation.stream, size, expected_sha256)
        operation.permit._require_valid_session()
        _ensure_generation_not_committed(token, operation.permit, path)
        if operation.permit._is_commit(path):
            # COMMIT is never created before every other bound object has
            # independently been downloaded and checked.
            _verify_remote_catalog(token, operation.permit, include_commit=False)
        first = _read_exact(operation.stream, min(size, _CHUNK))
        content = DropboxContentHasher()
        content.update(first)
        sent_sha256 = hashlib.sha256(first)
        _, started = _request(token, 'content.dropboxapi.com', '/2/files/upload_session/start',
                              {'close': False}, first)
        if (type(started) is not dict or type(started.get('session_id')) is not str
                or not _REV.fullmatch(started['session_id'])):
            raise PublisherTransportError('WRITE_UNCERTAIN')
        session_id, offset = started['session_id'], len(first)
        try:
            while offset < size:
                chunk = _read_exact(operation.stream, min(_CHUNK, size - offset))
                content.update(chunk)
                sent_sha256.update(chunk)
                operation.permit._require_valid_session()
                _, appended = _request(token, 'content.dropboxapi.com', '/2/files/upload_session/append_v2',
                                       {'cursor': {'session_id': session_id, 'offset': offset}, 'close': False}, chunk)
                if appended is not None:
                    raise PublisherTransportError('WRITE_UNCERTAIN')
                offset += len(chunk)
            if operation.stream.read(1) != b'':
                raise PublisherTransportError('SOURCE_CHANGED')
            if sent_sha256.hexdigest() != expected_sha256:
                raise PublisherTransportError('SOURCE_CHANGED')
            operation.permit._require_valid_session()
            # Recheck before finalizing: a committed generation must never be
            # repaired implicitly. Concurrent external mutation can still
            # occur after a check; add-only prevents overwriting an object.
            _ensure_generation_not_committed(token, operation.permit, path)
            _, finished = _request(token, 'content.dropboxapi.com', '/2/files/upload_session/finish',
                                   {'cursor': {'session_id': session_id, 'offset': offset},
                                    'commit': {'path': path, 'mode': {'.tag': 'add'},
                                               'autorename': False, 'strict_conflict': True, 'mute': False}}, b'')
            try:
                meta = _metadata(finished, path)
            except PublisherTransportError:
                raise PublisherTransportError('WRITE_UNCERTAIN') from None
            if meta.size != size or meta.content_hash is None or not content.digest().matches_hex(meta.content_hash):
                raise PublisherTransportError('REMOTE_HASH_MISMATCH')
            return meta
        except PublisherTransportError as exc:
            if exc.code == 'SOURCE_CHANGED':
                raise PublisherTransportError('STAGING_INCOMPLETE') from None
            raise
        except Exception:
            raise PublisherTransportError('STAGING_INCOMPLETE') from None
    raise PublisherTransportError('REQUEST_INVALID')


class PublisherTransport:
    """Narrow fixed-host transport, usable only with a publisher OAuth session."""
    __slots__ = ('__session', '__confirmed_app_key', '__app_folder_confirmation', '__permit')

    def __init__(self, session, *, confirmed_app_key: str, app_folder_confirmation: str):
        self.__session = session
        self.__confirmed_app_key = confirmed_app_key
        self.__app_folder_confirmation = app_folder_confirmation
        self.__permit = None

    def __repr__(self):
        return '<PublisherTransport redacted>'

    def _call(self, operation: _Operation):
        return self.__session._publisher_call(_dispatch, operation,
                                              confirmed_app_key=self.__confirmed_app_key,
                                              app_folder_confirmation=self.__app_folder_confirmation)

    def bind_permit(self, permit: _WritePermit):
        """Attach a coordinator-validated plan bound to this same session."""
        if type(permit) is not _WritePermit or not permit._owns(self.__session):
            raise PublisherTransportError('PLAN_REQUIRED')
        self.__permit = permit

    def metadata(self, path: str):
        return self._call(_Operation('metadata', path))

    def download(self, path: str, sink: BinaryIO, *, max_bytes: int = _MAX_SIZE):
        return self._call(_Operation('download', path, stream=sink, size=max_bytes))

    def upload_add(self, path: str, source: BinaryIO, size: int):
        if self.__permit is None:
            raise PublisherTransportError('PLAN_REQUIRED')
        return self._call(_Operation('upload', path, stream=source, size=size, permit=self.__permit))

    def update_if_revision(self, path: str, data: bytes, rev: str):
        if self.__permit is None:
            raise PublisherTransportError('PLAN_REQUIRED')
        return self._call(_Operation('update', path, data=data, rev=rev, permit=self.__permit))
