"""Resolve native UTT generation controls and verify payloads without fixed pins.

Control formats are from the source-only B-2 handoff; the upstream fetcher is
supplied by the caller and must itself enforce the approved App Folder, TLS,
request limits and credential handling. This module never guesses paths,
opens local files, logs data, or declares a new workspace/legacy UI compatible.
Successful verification proves only the checked bytes and observed references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Callable, Iterable

from app.publisher_transport import PublisherTransportError
from app.publisher_read_transport import PublisherReadTransportError
from app.dropbox_auth import OAuthError

ACTIVE = '/system/active-baseline.json'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_RELATIVE = re.compile(r'[A-Za-z0-9_./-]+\Z')
_OVERLAY = re.compile(r'workspace/snapshot-([0-9a-f]{64})\.sqlite\Z')
_SAFE_INTEGER = 2**53 - 1
_CONTROL_LIMIT = 16 * 1024 * 1024
_ENTRY_LIMIT = 100_000
_CHUNK_LIMIT = 8 * 1024 * 1024
_NATIVE_REQUIRED = frozenset({'system/baseline.json', 'system/schema.json',
                              'snapshots/tables.json', 'snapshots/objects.json',
                              'sources/index.json'})


class ReaderError(Exception):
    """Fixed code; no remote path, identifier, metadata or content in errors."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class Entry:
    path: str
    size: int
    sha256: str

    def __repr__(self) -> str:
        return '<Entry redacted>'


@dataclass(frozen=True, repr=False)
class Manifest:
    baseline_id: str
    sha256: str
    raw_size: int
    entries: tuple[Entry, ...]
    workspace: Entry | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return '<Manifest redacted>'


@dataclass(frozen=True, repr=False)
class ResolvedBaseline:
    generation_id: str
    root: str
    active_bytes: bytes = field(repr=False)
    active_revision: str = field(repr=False)
    manifest: Manifest = field(repr=False)
    commit_bytes: bytes = field(repr=False)
    total_bytes: int
    file_count: int

    def __repr__(self) -> str:
        return '<ResolvedBaseline redacted>'


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def _json(raw: bytes, code: str):
    if type(raw) is not bytes or not 1 <= len(raw) <= _CONTROL_LIMIT:
        raise ReaderError(code)
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ReaderError(code) from None


def _integer(value, *, minimum=0):
    return type(value) is int and minimum <= value <= _SAFE_INTEGER


def _identity(value):
    return (type(value) is str and 0 < len(value) <= 1024
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _relative(value):
    return (type(value) is str and len(value) <= 2048 and bool(_RELATIVE.fullmatch(value))
            and not value.startswith('/') and not any(p in ('', '.', '..') for p in value.split('/'))
            and 'scrivener' not in value.lower())


def parse_manifest(raw: bytes) -> Manifest:
    """Parse shape and preserve the digest of the exact supplied UTF-8 bytes."""
    data = _json(raw, 'INVALID_MANIFEST')
    if (type(data) is not dict or data.get('format') != 'utt-baseline/1'
            or not _identity(data.get('baseline_id')) or type(data.get('files')) is not list
            or len(data['files']) > _ENTRY_LIMIT):
        raise ReaderError('INVALID_MANIFEST')
    entries = []
    names = set()
    total = 0
    for item in data['files']:
        if (type(item) is not dict or not _relative(item.get('path'))
                or not _integer(item.get('bytes')) or type(item.get('sha256')) is not str
                or not _HASH.fullmatch(item['sha256'])):
            raise ReaderError('INVALID_MANIFEST')
        key = item['path'].lower()
        if key in names or key in ('system/manifest.json', 'system/commit.json'):
            raise ReaderError('INVALID_MANIFEST')
        names.add(key)
        total += item['bytes']
        if total > _SAFE_INTEGER:
            raise ReaderError('INVALID_MANIFEST')
        entries.append(Entry(item['path'], item['bytes'], item['sha256']))
    workspace = None
    if 'utt_workspace' in data:
        overlay = data['utt_workspace']
        if (type(overlay) is not dict or set(overlay) != {'format', 'path', 'sha256'}
                or overlay['format'] != 'UTT-WORKSPACE-OVERLAY/0.1'
                or type(overlay['path']) is not str or type(overlay['sha256']) is not str):
            raise ReaderError('INVALID_OVERLAY')
        match = _OVERLAY.fullmatch(overlay['path'])
        if not match or match.group(1) != overlay['sha256']:
            raise ReaderError('INVALID_OVERLAY')
        workspace = next((entry for entry in entries if entry.path == overlay['path']), None)
        if workspace is None or workspace.sha256 != overlay['sha256']:
            raise ReaderError('INVALID_OVERLAY')
    return Manifest(data['baseline_id'], hashlib.sha256(raw).hexdigest(), len(raw), tuple(entries), workspace)


_FETCH_OAUTH_CAUSES = frozenset({
    'AUTH_EXPIRED', 'AUTH_EXPIRED_AFTER_REQUEST', 'PUBLISH_AUTH_UNVERIFIED',
    'PUBLISH_TRANSPORT_UNAVAILABLE', 'PUBLISH_TRANSPORT_UNTRUSTED',
    'PUBLISH_REQUEST_UNCERTAIN', 'APP_FOLDER_UNCONFIRMED', 'APP_KEY_MISMATCH',
    'READ_AUTH_UNVERIFIED', 'READ_TRANSPORT_UNAVAILABLE',
    'READ_TRANSPORT_UNTRUSTED', 'READ_REQUEST_FAILED',
})
_FETCH_PHASES = {
    'ACTIVE_MISSING': 'ACTIVE',
    'MANIFEST_MISSING': 'MANIFEST',
    'COMMIT_MISSING': 'COMMIT',
    'ACTIVE_CHANGED': 'ACTIVE_RECHECK',
}


def _fetch(fetch: Callable[[str], tuple[bytes, str] | None], path: str, code: str):
    phase = _FETCH_PHASES[code]
    try:
        reply = fetch(path)
    except (PublisherTransportError, PublisherReadTransportError) as exc:
        # Preserve only the fixed, non-sensitive transport category and the
        # known protocol step; never include remote body/path/exception text.
        raise ReaderError(phase + '_' + exc.code) from None
    except OAuthError as exc:
        # A valid publisher grant can also fail in the local session layer.
        # OAuthError uses fixed internal codes; do not surface callback URLs.
        cause = exc.code if exc.code in _FETCH_OAUTH_CAUSES else 'AUTH_FAILED'
        raise ReaderError(phase + '_' + cause) from None
    except Exception:
        raise ReaderError(phase + '_READ_FAILED') from None
    if (type(reply) is not tuple or len(reply) != 2 or type(reply[0]) is not bytes
            or type(reply[1]) is not str or not reply[1] or len(reply[1]) > 1024
            or any(not 33 <= ord(c) <= 126 for c in reply[1])):
        raise ReaderError(code)
    if len(reply[0]) > _CONTROL_LIMIT:
        raise ReaderError(code)
    return reply


def resolve_active(fetch: Callable[[str], tuple[bytes, str] | None]) -> ResolvedBaseline:
    """Verify active, manifest, COMMIT, then exact active bytes and rev again.

    ``fetch`` is the caller's approved path-bound reader and must return
    (raw_bytes, Dropbox_revision). Only control files are read. Equality on
    re-read cannot detect an unobserved ABA or guarantee future immutability.
    Payload hashes must be verified separately at each point of use.
    """
    active_raw, active_rev = _fetch(fetch, ACTIVE, 'ACTIVE_MISSING')
    active = _json(active_raw, 'INVALID_ACTIVE')
    keys = {'format', 'generation_id', 'manifest_sha256', 'baseline_id', 'root', 'commit_sha256', 'files', 'bytes'}
    if (type(active) is not dict or set(active) != keys or active['format'] != 'utt-active-baseline/1'
            or type(active['generation_id']) is not str or not _HASH.fullmatch(active['generation_id'])
            or active['manifest_sha256'] != active['generation_id'] or not _identity(active['baseline_id'])
            or type(active['commit_sha256']) is not str or not _HASH.fullmatch(active['commit_sha256'])
            or not _integer(active['files'], minimum=2) or not _integer(active['bytes'])
            or active['root'] != '/snapshots/baselines/' + active['generation_id'] + '/'):
        raise ReaderError('INVALID_ACTIVE')
    root = active['root']
    manifest_raw, _ = _fetch(fetch, root + 'system/manifest.json', 'MANIFEST_MISSING')
    manifest = parse_manifest(manifest_raw)
    if manifest.sha256 != active['manifest_sha256'] or manifest.baseline_id != active['baseline_id']:
        raise ReaderError('MANIFEST_MISMATCH')
    commit_raw, _ = _fetch(fetch, root + 'system/COMMIT.json', 'COMMIT_MISSING')
    commit = _json(commit_raw, 'INVALID_COMMIT')
    if (hashlib.sha256(commit_raw).hexdigest() != active['commit_sha256']
            or type(commit) is not dict or commit.get('format') != 'utt-baseline/1'
            or commit.get('baseline_id') != manifest.baseline_id
            or commit.get('manifest_sha256') != manifest.sha256 or commit.get('state') != 'complete'):
        raise ReaderError('COMMIT_MISMATCH')
    total = sum(entry.size for entry in manifest.entries) + len(manifest_raw) + len(commit_raw)
    if (total > _SAFE_INTEGER or active['files'] != len(manifest.entries) + 2
            or active['bytes'] != total):
        raise ReaderError('CATALOG_MISMATCH')
    active_again, revision_again = _fetch(fetch, ACTIVE, 'ACTIVE_CHANGED')
    if active_again != active_raw or revision_again != active_rev:
        raise ReaderError('ACTIVE_CHANGED')
    return ResolvedBaseline(active['generation_id'], root, active_raw, active_rev, manifest,
                            commit_raw, total, active['files'])


def verify_payload(resolved: ResolvedBaseline, path: str,
                   fetch_chunks: Callable[[str], Iterable[bytes]]) -> None:
    """Stream and verify one declared payload; never expose content or paths."""
    if type(resolved) is not ResolvedBaseline or not _relative(path):
        raise ReaderError('INVALID_REQUEST')
    entry = next((e for e in resolved.manifest.entries if e.path == path), None)
    if entry is None:
        raise ReaderError('PAYLOAD_NOT_DECLARED')
    digest = hashlib.sha256()
    total = 0
    try:
        for chunk in fetch_chunks(resolved.root + entry.path):
            if type(chunk) is not bytes or not 0 < len(chunk) <= _CHUNK_LIMIT:
                raise ReaderError('INVALID_PAYLOAD')
            total += len(chunk)
            if total > entry.size:
                raise ReaderError('PAYLOAD_MISMATCH')
            digest.update(chunk)
    except ReaderError:
        raise
    except Exception:
        raise ReaderError('READ_FAILED') from None
    if total != entry.size or digest.hexdigest() != entry.sha256:
        raise ReaderError('PAYLOAD_MISMATCH')


def assert_parent_preserved(parent: Manifest, candidate: Manifest) -> tuple[Entry, ...]:
    """Require every parent payload entry to retain exact path, size and hash.

    Returned additions are private runtime information. This check is not a
    workspace-format or existing-reader compatibility verdict.
    """
    if type(parent) is not Manifest or type(candidate) is not Manifest:
        raise ReaderError('INVALID_REQUEST')
    new = {item.path: item for item in candidate.entries}
    for old in parent.entries:
        if new.get(old.path) != old:
            raise ReaderError('PARENT_PAYLOAD_CONFLICT')
    old_paths = {item.path for item in parent.entries}
    return tuple(item for item in candidate.entries if item.path not in old_paths)


def assert_native_payloads(manifest: Manifest) -> None:
    """Require known native catalogs; each payload still needs byte verification.

    Names establish only catalog presence, not their JSON meaning, object
    consistency, valid SQLite rows, reader usability or source preservation.
    """
    if type(manifest) is not Manifest:
        raise ReaderError('INVALID_REQUEST')
    if not _NATIVE_REQUIRED.issubset({entry.path.lower() for entry in manifest.entries}):
        raise ReaderError('NATIVE_PAYLOAD_MISSING')


def assert_legacy_pins(resolved: ResolvedBaseline, *, manifest_sha256: str, root: str,
                       file_count: int, total_bytes: int) -> None:
    """Check only the old reader's active pins; not its catalog/model counts."""
    if (type(resolved) is not ResolvedBaseline or resolved.manifest.sha256 != manifest_sha256
            or resolved.root != root or resolved.file_count != file_count
            or resolved.total_bytes != total_bytes):
        raise ReaderError('LEGACY_PIN_MISMATCH')
