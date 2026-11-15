"""Build native candidate control bytes around a preserved parent payload.

The workspace overlay is a new explicitly versioned extension, not a claim
that the old pinned A reader understands it. Callers must prove current parent
preservation, source freshness and an actual compatible reader before writing.
No file, network, log, approval history or source data is accessed here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re

from app.publisher_reader import Entry, ResolvedBaseline, ReaderError, parse_manifest

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_SAFE_INT = 2**53 - 1
_LIMIT = 16 * 1024 * 1024


class CandidateError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, repr=False)
class Candidate:
    generation_id: str = field(repr=False)
    root: str = field(repr=False)
    workspace_path: str = field(repr=False)
    entries: tuple[Entry, ...] = field(repr=False)
    manifest_bytes: bytes = field(repr=False)
    commit_bytes: bytes = field(repr=False)
    active_bytes: bytes = field(repr=False)

    def __repr__(self):
        return '<Candidate redacted>'


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError('duplicate key')
        obj[key] = value
    return obj


def _canonical(value, depth=0):
    """Legacy-compatible JSON for safe integer values and Unicode scalars."""
    if depth > 80:
        raise CandidateError('CONTROL_LIMIT')
    if value is None:
        return 'null'
    if type(value) is bool:
        return 'true' if value else 'false'
    if type(value) is int:
        if not -_SAFE_INT <= value <= _SAFE_INT:
            raise CandidateError('INVALID_NUMBER')
        return str(value)
    if type(value) is str:
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise CandidateError('INVALID_TEXT')
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    if type(value) is list:
        return '[' + ','.join(_canonical(v, depth + 1) for v in value) + ']'
    if type(value) is dict and all(type(k) is str for k in value):
        try:
            keys = sorted(value, key=lambda k: k.encode('utf-16-be'))
        except UnicodeError:
            raise CandidateError('INVALID_TEXT') from None
        return '{' + ','.join(_canonical(k, depth + 1) + ':' + _canonical(value[k], depth + 1)
                              for k in keys) + '}'
    raise CandidateError('INVALID_CONTROL_VALUE')


def _encode(value):
    try:
        raw = (_canonical(value) + '\n').encode('utf-8')
    except UnicodeError:
        raise CandidateError('INVALID_TEXT') from None
    if len(raw) > _LIMIT:
        raise CandidateError('CONTROL_LIMIT')
    return raw


def build_candidate(parent: ResolvedBaseline, parent_manifest_bytes: bytes, *,
                    workspace_size: int, workspace_sha256: str) -> Candidate:
    """Preserve all parent payload entries and add an immutable workspace overlay.

    Inputs must have been independently verified by the caller. The returned
    controls do not authorize upload or prove that a reader or parent matches.
    The overlay is addressed by its ordinary SHA-256, not Dropbox content_hash.
    """
    if (type(parent) is not ResolvedBaseline or type(parent_manifest_bytes) is not bytes
            or not 1 <= len(parent_manifest_bytes) <= _LIMIT
            or type(workspace_size) is not int or not 0 < workspace_size <= _SAFE_INT
            or type(workspace_sha256) is not str or not _HEX.fullmatch(workspace_sha256)):
        raise CandidateError('INVALID_REQUEST')
    try:
        parsed = parse_manifest(parent_manifest_bytes)
        if parsed != parent.manifest:
            raise CandidateError('PARENT_MANIFEST_CHANGED')
        value = json.loads(parent_manifest_bytes.decode('utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict or type(value.get('files')) is not list:
            raise CandidateError('INVALID_MANIFEST')
        path = 'workspace/snapshot-' + workspace_sha256 + '.sqlite'
        existing = next((entry for entry in parsed.entries if entry.path.lower() == path.lower()), None)
        if existing is not None and (existing.path != path or existing.size != workspace_size
                                     or existing.sha256 != workspace_sha256):
            raise CandidateError('WORKSPACE_PATH_CONFLICT')
        if existing is None:
            value['files'].append({'path': path, 'bytes': workspace_size, 'sha256': workspace_sha256})
        value['utt_workspace'] = {'format': 'UTT-WORKSPACE-OVERLAY/0.1',
                                  'path': path, 'sha256': workspace_sha256}
        manifest_bytes = _encode(value)
        candidate_manifest = parse_manifest(manifest_bytes)
        generation_id = hashlib.sha256(manifest_bytes).hexdigest()
        if candidate_manifest.sha256 != generation_id:
            raise CandidateError('INVALID_MANIFEST')
        commit_bytes = _encode({'format': 'utt-baseline/1', 'baseline_id': candidate_manifest.baseline_id,
                                'manifest_sha256': generation_id, 'state': 'complete'})
        total = sum(item.size for item in candidate_manifest.entries) + len(manifest_bytes) + len(commit_bytes)
        if total > _SAFE_INT:
            raise CandidateError('CONTROL_LIMIT')
        root = '/snapshots/baselines/' + generation_id + '/'
        active_bytes = _encode({'format': 'utt-active-baseline/1', 'generation_id': generation_id,
                                'manifest_sha256': generation_id, 'baseline_id': candidate_manifest.baseline_id,
                                'root': root, 'commit_sha256': hashlib.sha256(commit_bytes).hexdigest(),
                                'files': len(candidate_manifest.entries) + 2, 'bytes': total})
        return Candidate(generation_id, root, path, candidate_manifest.entries,
                         manifest_bytes, commit_bytes, active_bytes)
    except CandidateError:
        raise
    except (ReaderError, ValueError, TypeError, UnicodeError, RecursionError):
        raise CandidateError('INVALID_MANIFEST') from None
