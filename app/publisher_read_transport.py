"""Fixed Dropbox downloads for the verified, read-only workspace resolver.

Only a control file in a SHA-addressed generation, the active reference, or
the declared workspace snapshot can be downloaded. No writes, folder listing,
proxy/redirect, file logging, or local path access occurs. Returned bytes and
revision are sensitive and must remain inside the approved user runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
import re
import ssl

ACTIVE = '/system/active-baseline.json'
MAX_CONTROL = 16 * 1024 * 1024
MAX_WORKSPACE = 512 * 1024 * 1024
_CONTROL_PATH = re.compile(r'/snapshots/baselines/[0-9a-f]{64}/system/(?:manifest|COMMIT)\.json\Z')
_WORKSPACE_PATH = re.compile(r'/snapshots/baselines/[0-9a-f]{64}/workspace/snapshot-[0-9a-f]{64}\.sqlite\Z')
_REV = re.compile(r'[\x21-\x7e]{1,1024}\Z')
_CODES = frozenset({'INVALID_REQUEST', 'AUTH_REQUIRED', 'ACCESS_DENIED', 'READ_FAILED',
                    'INVALID_RESPONSE', 'RESPONSE_TOO_LARGE', 'REMOTE_CHANGED'})


class PublisherReadTransportError(Exception):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else 'READ_FAILED'
        super().__init__(self.code)


@dataclass(frozen=True, repr=False)
class _ReadOperation:
    path: str = field(repr=False)
    limit: int
    expected_size: int | None = None

    def __repr__(self):
        return '<PublisherReadOperation redacted>'


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate key')
        value[key] = item
    return value


def _json(raw):
    if type(raw) is not bytes or len(raw) > 1024 * 1024:
        raise PublisherReadTransportError('INVALID_RESPONSE')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise PublisherReadTransportError('INVALID_RESPONSE') from None


def _missing(data):
    if type(data) is not dict or type(data.get('error')) is not dict:
        return False
    error = data['error']
    kind = error.get('.tag')
    return (kind in ('path', 'path_lookup') and type(error.get(kind)) is dict
            and error[kind].get('.tag') == 'not_found')


def _dispatch(token: str, operation: _ReadOperation):
    """Session-authorized download; result stays inside the local process."""
    if type(operation) is not _ReadOperation or type(token) is not str or not _REV.fullmatch(token):
        raise PublisherReadTransportError('INVALID_REQUEST')
    path, limit, expected = operation.path, operation.limit, operation.expected_size
    control = path == ACTIVE or type(path) is str and _CONTROL_PATH.fullmatch(path)
    workspace = type(path) is str and _WORKSPACE_PATH.fullmatch(path)
    if (not (control or workspace) or type(limit) is not int or limit < 1
            or limit > (MAX_CONTROL if control else MAX_WORKSPACE)
            or (expected is not None and (type(expected) is not int or not 0 <= expected <= limit))
            or (workspace and expected is None)):
        raise PublisherReadTransportError('INVALID_REQUEST')
    connection = None
    try:
        connection = http.client.HTTPSConnection('content.dropboxapi.com', 443, timeout=30,
                                                  context=ssl.create_default_context())
        connection.set_debuglevel(0)
        argument = json.dumps({'path': path}, ensure_ascii=True, separators=(',', ':'))
        connection.request('POST', '/2/files/download', body=b'', headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/octet-stream',
            'Dropbox-API-Arg': argument,
            'Cache-Control': 'no-store',
        })
        response = connection.getresponse()
        if response.status != 200:
            if response.status == 401:
                raise PublisherReadTransportError('AUTH_REQUIRED')
            if response.status == 403:
                raise PublisherReadTransportError('ACCESS_DENIED')
            if response.status == 409 and _missing(_json(response.read(1024 * 1024 + 1))):
                return None
            raise PublisherReadTransportError('READ_FAILED')
        header = response.getheader('Dropbox-API-Result')
        if type(header) is not str or len(header) > 1024 * 1024:
            raise PublisherReadTransportError('INVALID_RESPONSE')
        meta = _json(header.encode('utf-8'))
        if (type(meta) is not dict or meta.get('.tag', 'file') != 'file'
                or meta.get('symlink_info') is not None or meta.get('path_lower') != path.lower()
                or type(meta.get('size')) is not int or not 0 <= meta['size'] <= limit
                or type(meta.get('rev')) is not str or not _REV.fullmatch(meta['rev'])):
            raise PublisherReadTransportError('INVALID_RESPONSE')
        if expected is not None and meta['size'] != expected:
            raise PublisherReadTransportError('REMOTE_CHANGED')
        content = bytearray()
        while True:
            piece = response.read(min(1024 * 1024, limit - len(content) + 1))
            if type(piece) is not bytes:
                raise PublisherReadTransportError('INVALID_RESPONSE')
            if not piece:
                break
            content.extend(piece)
            if len(content) > limit:
                raise PublisherReadTransportError('RESPONSE_TOO_LARGE')
        if len(content) != meta['size']:
            raise PublisherReadTransportError('REMOTE_CHANGED')
        return bytes(content), meta['rev']
    except PublisherReadTransportError:
        raise
    except Exception:
        raise PublisherReadTransportError('READ_FAILED') from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class PublisherReadTransport:
    """Fixed transport usable only with the existing verified read-only grant."""
    __slots__ = ('__session', '__confirmed_app_key', '__app_folder_confirmation')

    def __init__(self, session, *, confirmed_app_key, app_folder_confirmation):
        self.__session = session
        self.__confirmed_app_key = confirmed_app_key
        self.__app_folder_confirmation = app_folder_confirmation

    def __repr__(self):
        return '<PublisherReadTransport redacted>'

    def control(self, path):
        return self.__session._publisher_read(_dispatch, _ReadOperation(path, MAX_CONTROL),
                                              confirmed_app_key=self.__confirmed_app_key,
                                              app_folder_confirmation=self.__app_folder_confirmation)

    def workspace(self, path, *, expected_size):
        return self.__session._publisher_read(_dispatch, _ReadOperation(path, MAX_WORKSPACE, expected_size),
                                              confirmed_app_key=self.__confirmed_app_key,
                                              app_folder_confirmation=self.__app_folder_confirmation)
