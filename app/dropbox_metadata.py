"""User-confirmed Dropbox single-object metadata and synthetic inspection.

Live requests require a session from the fixed OAuth TLS transport, exact
scopes and deadline, its bound app key, an explicit local user confirmation,
and a single user-supplied canonical App Folder-relative ``path_lower``.
App Folder type is USER_CONFIRMED, not independently proven by Dropbox. No
token getter, account enumeration, file-content or write operation exists.

The fixture models a restricted, deliberately fictional subset of the
``/2/files/get_metadata`` response.  Its app and namespace labels are test
inputs, never evidence about a real Dropbox app.  Exact fixture sentinels also
prevent accidentally passing ordinary source metadata to this test harness.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import ssl
from typing import Any

from app.dropbox_auth import READ_SCOPES, ReadOnlySession

# This private test seam has no production issuer; private transport tests
# replace it only with an invented marker and a fake HTTPS connection. Real
# calls use a one-request permit bound to a confirmed live OAuth session.
_LIVE_AUTHORIZATION_GUARD = None


class _DuplicateKey(ValueError):
    pass


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _fictional_name(value: Any) -> bool:
    return (isinstance(value, str) and value.startswith('fictional-')
            and 11 <= len(value) <= 200
            and not any(c in '/\\' or ord(c) < 32 or ord(c) == 127 for c in value))


@dataclass(frozen=True)
class MetadataInspection:
    """Only non-sensitive state; never return a source name, ID or path."""

    status: str
    source: str = 'SYNTHETIC'
    integrity: str = 'UNVERIFIED'
    snapshot: str = 'UNVERIFIED'


class SyntheticMetadataFixture:
    """In-memory fake, not a network or account adapter.

    The caller is responsible for ensuring all fixture bytes are invented.
    No fixture values, API responses, errors, or identifiers are logged.
    """

    __slots__ = ('_body', 'http_status', 'app_identity', 'access_type', 'calls')

    def __init__(self, *, body: bytes, http_status: int = 200,
                 app_identity: str = 'fictional-utt', access_type: str = 'app_folder'):
        self._body = body
        self.http_status = http_status
        self.app_identity = app_identity
        self.access_type = access_type
        self.calls = 0

    def __repr__(self) -> str:
        return '<SyntheticMetadataFixture redacted>'

    def _get_metadata(self) -> tuple[int, bytes]:
        self.calls += 1
        return self.http_status, self._body


def _decoded(body: Any) -> Any:
    if not isinstance(body, bytes) or len(body) > 65536:
        raise ValueError('invalid synthetic response')
    # Duplicate keys are ambiguous even if both values happen to agree.
    return json.loads(body.decode('utf-8'), object_pairs_hook=_unique_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))


def _duplicate_entries(value: Any) -> bool:
    """Detect only malformed synthetic multi-object responses, not a search.

    The actual get_metadata route returns one object.  This does not establish
    whether duplicates exist elsewhere in an App Folder.
    """
    if not isinstance(value, list) or len(value) < 2:
        return False
    ids, paths, names = set(), set(), set()
    for entry in value:
        if not isinstance(entry, dict):
            return False
        identity, path, name = entry.get('id'), entry.get('path_lower'), entry.get('name')
        if not all(isinstance(x, str) for x in (identity, path, name)):
            return False
        if identity in ids or path in paths or name.lower() in names:
            return True
        ids.add(identity)
        paths.add(path)
        names.add(name.lower())
    return False


def _valid_target(target: Any) -> bool:
    return (isinstance(target, str) and 2 <= len(target) <= 1024
            and target.startswith('/') and not target.endswith('/')
            and '//' not in target and '\\' not in target
            and all(part not in ('.', '..') for part in target.split('/'))
            and all(ord(c) >= 32 and ord(c) != 127 and not 0xD800 <= ord(c) <= 0xDFFF
                    for c in target))


def inspect_synthetic_metadata(session: ReadOnlySession,
                               fixture: SyntheticMetadataFixture, *,
                               expected_name: str = 'fictional-baseline') -> MetadataInspection:
    """Inspect one explicit fictional target, returning only a fixed status.

    This exercise does not establish app identity, folder entitlement, source
    existence, original-byte integrity, or a stable snapshot in a real account.
    It does not fetch a body, enumerate entries, or call any network endpoint.
    """
    def result(status: str) -> MetadataInspection:
        return MetadataInspection(status)

    if type(session) is not ReadOnlySession:
        return result('WRONG_SCOPE')
    try:
        if not session.is_valid():
            return result('AUTH_EXPIRED')
        if session.scopes != READ_SCOPES:
            return result('WRONG_SCOPE')
    except Exception:
        return result('WRONG_SCOPE')
    if type(fixture) is not SyntheticMetadataFixture:
        return result('INVALID_RESPONSE')
    if fixture.app_identity != 'fictional-utt':
        return result('WRONG_APP')
    if fixture.access_type != 'app_folder':
        return result('WRONG_SCOPE')
    if not _fictional_name(expected_name):
        return result('INVALID_RESPONSE')

    status, body = fixture._get_metadata()
    if type(status) is not int:
        return result('INVALID_RESPONSE')
    if status == 401:
        # A remote 401 can mean invalid, revoked, suspended, or other auth
        # failure; only the local clock/session can establish local expiry.
        return result('AUTH_REQUIRED')
    if status == 403:
        return result('ACCESS_DENIED')
    if status not in (200, 409):
        return result('REQUEST_FAILED')
    try:
        data = _decoded(body)
    except _DuplicateKey:
        return result('DUPLICATE')
    except (UnicodeError, ValueError, RecursionError):
        return result('INVALID_RESPONSE')
    if status == 409:
        if (isinstance(data, dict) and isinstance(data.get('error'), dict)
                and data['error'].get('.tag') == 'path'
                and isinstance(data['error'].get('path'), dict)
                and data['error']['path'].get('.tag') == 'not_found'):
            return result('BASELINE_MISSING')
        return result('REQUEST_FAILED')

    if _duplicate_entries(data):
        return result('DUPLICATE')
    if not isinstance(data, dict):
        return result('INVALID_RESPONSE')
    if set(data) != {'.tag', 'name', 'id', 'path_lower'}:
        return result('INVALID_RESPONSE')
    name, identity, path, tag = data['name'], data['id'], data['path_lower'], data['.tag']
    if (not _fictional_name(name) or not _fictional_name(identity)
            or not isinstance(path, str) or path != '/' + name.lower()
            or name != expected_name):
        return result('INVALID_RESPONSE')
    if tag != 'file':
        return result('TYPE_MISMATCH' if tag == 'folder' else 'INVALID_RESPONSE')
    return result('FOUND_UNVERIFIED')


def inspect_live_metadata(session: Any = None, *, confirmed_app_key: str | None = None,
                          app_folder_confirmation: str | None = None,
                          target: str | None = None, **unsupported: Any) -> MetadataInspection:
    """One explicitly authorized metadata lookup; no injected live transport.

    The app-folder assertion is USER_CONFIRMED, not independently verified by
    Dropbox. The target must already be the locally confirmed canonical
    Dropbox path_lower; no data discovery or path guessing is performed.
    """
    if (unsupported or type(session) is not ReadOnlySession
            or confirmed_app_key is None or app_folder_confirmation is None or target is None):
        return MetadataInspection(status='UNSUPPORTED_CONTRACT', source='LIVE_NOT_RUN')
    return session._inspect_metadata(confirmed_app_key=confirmed_app_key,
                                     app_folder_confirmation=app_folder_confirmation,
                                     target=target)


def _request_metadata_https(*, target: str, bearer: str, _guard: Any,
                            connection_factory: Any = None) -> MetadataInspection:
    """Perform one fixed HTTPS metadata request under a session-bound permit.

    A session-bound, one-use permit is required, minted inside a valid session
    only after fixed Dropbox TLS token exchange and local user confirmation.
    Python module privacy is not a security boundary against code mutation.
    Tests can also replace the otherwise-empty private guard while using fake
    HTTPS connections; that seam is never enabled by the Windows program.
    """
    blocked = MetadataInspection('UNSUPPORTED_CONTRACT', source='LIVE_NOT_RUN')
    test_guard = _LIVE_AUTHORIZATION_GUARD is not None and _guard is _LIVE_AUTHORIZATION_GUARD
    session_guard = False
    if type(_guard) is tuple and len(_guard) == 2 and type(_guard[0]) is ReadOnlySession:
        try:
            session_guard = _guard[0]._valid_metadata_permit(_guard[1], target, bearer)
        except Exception:
            session_guard = False
    if not test_guard and not session_guard:
        return blocked

    def result(status: str) -> MetadataInspection:
        return MetadataInspection(status, source='UNVERIFIED_TRANSPORT')

    # Dropbox's get_metadata does not accept the App Folder root.  Refuse
    # path-root override, IDs, traversal, controls, alternate hosts or URLs.
    if not _valid_target(target):
        return result('INVALID_REQUEST')
    if (not isinstance(bearer, str) or not 1 <= len(bearer) <= 8192
            or any(not 33 <= ord(c) <= 126 for c in bearer)):
        return result('INVALID_REQUEST')

    conn = None
    try:
        factory = connection_factory or http.client.HTTPSConnection
        conn = factory('api.dropboxapi.com', 443, timeout=10,
                       context=ssl.create_default_context())
        payload = json.dumps({'path': target, 'include_media_info': False,
                              'include_deleted': False,
                              'include_has_explicit_shared_members': False},
                             ensure_ascii=True, separators=(',', ':')).encode('ascii')
        def send() -> None:
            conn.request('POST', '/2/files/get_metadata', body=payload, headers={
                'Authorization': 'Bearer ' + bearer,
                'Content-Type': 'application/json', 'Accept': 'application/json',
                'Cache-Control': 'no-store',
            })
        if session_guard:
            if not _guard[0]._send_metadata_if_valid(_guard[1], target, bearer, send):
                return result('AUTH_EXPIRED')
        else:
            send()
        response = conn.getresponse()
        if response.status == 401:
            return result('AUTH_REQUIRED')
        if response.status == 403:
            return result('ACCESS_DENIED')
        if response.status not in (200, 409):
            return result('REQUEST_FAILED')
        body = response.read(65537)
        data = _decoded(body)
        if response.status == 409:
            if (isinstance(data, dict) and isinstance(data.get('error'), dict)
                    and data['error'].get('.tag') == 'path'
                    and isinstance(data['error'].get('path'), dict)
                    and data['error']['path'].get('.tag') == 'not_found'):
                return result('BASELINE_MISSING')
            return result('REQUEST_FAILED')
        if _duplicate_entries(data):
            return result('DUPLICATE')
        if not isinstance(data, dict):
            return result('INVALID_RESPONSE')
        # FileMetadata may identify a symbolic link.  Never treat its target as
        # the requested baseline or try to follow it from this metadata layer.
        if data.get('symlink_info') is not None:
            return result('UNSUPPORTED_CONTRACT')
        if data.get('.tag') == 'folder':
            return result('TYPE_MISMATCH')
        if (data.get('.tag') != 'file' or not isinstance(data.get('id'), str)
                or not data['id'] or not isinstance(data.get('name'), str)
                or not data['name'] or not isinstance(data.get('path_lower'), str)):
            return result('INVALID_RESPONSE')
        # Compare exactly with the user's already-confirmed canonical value;
        # do not guess Dropbox's Unicode normalization or casing rules.
        if data['path_lower'] != target:
            return result('TARGET_MISMATCH')
        # Never interpret rev or Dropbox content_hash as proof of immutable
        # snapshot or SHA256 of file bytes; no file content is retrieved.
        return result('FOUND_UNVERIFIED')
    except _DuplicateKey:
        return result('DUPLICATE')
    except (UnicodeError, ValueError, RecursionError):
        return result('INVALID_RESPONSE')
    except Exception:
        # Never include exception strings, response content, target or token.
        return result('REQUEST_FAILED')
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
