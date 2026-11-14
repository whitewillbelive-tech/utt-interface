"""Bounded exploration of an invented, in-memory baseline graph only.

The actual A reference, commit, manifest and approval wire formats are not
available here. The fixture's kinds and links are invented test facts; no
claim is made that native Dropbox metadata exposes such relationships. There
is no file-content read, filesystem access, network transport, or live
enumeration in this module. The live entry point is unconditionally blocked.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExplorationResult:
    """Fixed public states only; never carry a name, identifier or secret."""

    status: str
    source: str = 'SYNTHETIC'
    canonical: str = 'UNKNOWN'
    snapshot: str = 'UNKNOWN'


class SyntheticFolderFixture:
    """Invented relations in memory; never a real account or native contract.

    Completeness flags represent deliberately controlled test facts. They do
    not demonstrate that a real remote folder was fully enumerated.
    """

    def __init__(self):
        self.app_identity = 'fictional-utt'
        self.folder_scope = 'fictional-app-folder'
        # Explicitly model the user's scope decision; never default to consent.
        self.user_approved = False
        self.auth_state = 'VALID'
        self.root_complete = True
        self.generation_complete = True
        self.root_entries = [
            {'kind': 'reference', 'id': 'fictional-reference'},
            {'kind': 'generation', 'id': 'fictional-generation'},
        ]
        self.references = [
            {'id': 'fictional-reference', 'revision': 'fictional-revision',
             'generation': 'fictional-generation', 'commit': 'fictional-commit',
             'manifest': 'fictional-manifest'},
            {'id': 'fictional-reference', 'revision': 'fictional-revision',
             'generation': 'fictional-generation', 'commit': 'fictional-commit',
             'manifest': 'fictional-manifest'},
        ]
        self.generation_entries = [
            {'kind': 'commit', 'id': 'fictional-commit', 'generation': 'fictional-generation',
             'manifest': 'fictional-manifest', 'anchor': 'fictional-anchor'},
            {'kind': 'manifest', 'id': 'fictional-manifest', 'generation': 'fictional-generation',
             'commit': 'fictional-commit', 'anchor': 'fictional-anchor'},
        ]
        self.approval = {'generation': 'fictional-generation', 'commit': 'fictional-commit',
                         'manifest': 'fictional-manifest', 'anchor': 'fictional-anchor'}
        self.failures: dict[str, str] = {}
        self.calls: list[str] = []

    def __repr__(self) -> str:
        return '<SyntheticFolderFixture redacted>'


_ALLOWED_FAILURES = frozenset({'ACCESS_DENIED', 'AUTH_EXPIRED', 'REQUEST_FAILED'})
_HARD_MAX_REQUESTS = 4
_HARD_MAX_ENTRIES = 32


def _fictional(value: Any) -> bool:
    return (type(value) is str and value.startswith('fictional-') and 11 <= len(value) <= 128
            and all(32 <= ord(c) < 127 and c not in '/\\' for c in value))


def _check_dict(value: Any, keys: set[str]) -> bool:
    return (type(value) is dict and set(value) == keys
            and all(_fictional(value[k]) for k in keys))


def inspect_synthetic(fixture: SyntheticFolderFixture, *, max_requests: int = 4,
                      max_entries: int = 20) -> ExplorationResult:
    """Inspect four bounded synthetic observations; return no source values.

    These are conceptual synthetic facts, not a mapping of actual metadata or
    evidence that a native active chain can be reconstructed without content.
    Even a match cannot verify real canonical status, immutability, or an ABA
    replacement between the two reference observations.
    """
    def result(status: str) -> ExplorationResult:
        return ExplorationResult(status)

    if type(fixture) is not SyntheticFolderFixture:
        return result('SYNTHETIC_ONLY')
    if (type(max_requests) is not int or not 1 <= max_requests <= _HARD_MAX_REQUESTS
            or type(max_entries) is not int or not 1 <= max_entries <= _HARD_MAX_ENTRIES):
        return result('INVALID_LIMIT')
    try:
        if fixture.user_approved is not True:
            return result('SCOPE_NOT_AUTHORIZED')
        if fixture.app_identity != 'fictional-utt' or fixture.folder_scope != 'fictional-app-folder':
            return result('WRONG_SCOPE')
        if fixture.auth_state == 'EXPIRED':
            return result('AUTH_EXPIRED')
        if fixture.auth_state != 'VALID':
            return result('AUTH_REQUIRED')
        if type(fixture.failures) is not dict or type(fixture.calls) is not list:
            return result('INVALID_FIXTURE')

        calls = 0
        def step(name: str) -> str | None:
            nonlocal calls
            if calls >= max_requests:
                return 'BUDGET_EXCEEDED'
            if fixture.user_approved is not True:
                return 'SCOPE_NOT_AUTHORIZED'
            if fixture.app_identity != 'fictional-utt' or fixture.folder_scope != 'fictional-app-folder':
                return 'WRONG_SCOPE'
            if fixture.auth_state == 'EXPIRED':
                return 'AUTH_EXPIRED'
            if fixture.auth_state != 'VALID':
                return 'AUTH_REQUIRED'
            calls += 1
            fixture.calls.append(name)
            failure = fixture.failures.get(name)
            if failure is None:
                return None
            return failure if failure in _ALLOWED_FAILURES else 'REQUEST_FAILED'

        error = step('root')
        if error:
            return result(error)
        if fixture.root_complete is not True:
            return result('INCOMPLETE_SCOPE')
        if type(fixture.root_entries) is not list:
            return result('INVALID_FIXTURE')
        observed_entries = len(fixture.root_entries)
        if observed_entries > max_entries:
            return result('BUDGET_EXCEEDED')
        root_ids = set()
        candidates = []
        generations = set()
        for entry in fixture.root_entries:
            if (type(entry) is not dict or set(entry) != {'kind', 'id'}
                    or entry['kind'] not in ('reference', 'generation', 'other') or not _fictional(entry['id'])):
                return result('INVALID_FIXTURE')
            if entry['id'] in root_ids:
                return result('DUPLICATE_ENTRY')
            root_ids.add(entry['id'])
            if entry['kind'] == 'reference':
                candidates.append(entry['id'])
            elif entry['kind'] == 'generation':
                generations.add(entry['id'])
        if not candidates:
            return result('ACTIVE_REFERENCE_MISSING')
        if len(candidates) != 1:
            return result('CANDIDATES_AMBIGUOUS')

        error = step('ref_first')
        if error:
            return result(error)
        if type(fixture.references) is not list or len(fixture.references) != 2:
            return result('INVALID_FIXTURE')
        first = fixture.references[0]
        keys = {'id', 'revision', 'generation', 'commit', 'manifest'}
        if not _check_dict(first, keys):
            return result('INVALID_FIXTURE')
        # Capture values without exposing them; later observations are checked
        # against the full first reference, not its revision alone.
        first = copy.copy(first)
        if first['id'] != candidates[0] or first['generation'] not in generations:
            return result('REFERENCE_MISMATCH')

        error = step('generation')
        if error:
            return result(error)
        if fixture.generation_complete is not True:
            return result('INCOMPLETE_SCOPE')
        if type(fixture.generation_entries) is not list:
            return result('INVALID_FIXTURE')
        observed_entries += len(fixture.generation_entries)
        if observed_entries > max_entries:
            return result('BUDGET_EXCEEDED')
        nodes: dict[str, dict[str, str]] = {}
        for entry in fixture.generation_entries:
            if (type(entry) is not dict or entry.get('kind') not in ('commit', 'manifest')
                    or not _check_dict({k: v for k, v in entry.items() if k != 'kind'},
                                       {'id', 'generation', 'manifest' if entry.get('kind') == 'commit' else 'commit', 'anchor'})):
                return result('INVALID_FIXTURE')
            if entry['id'] in nodes or entry['id'] in root_ids:
                return result('DUPLICATE_ENTRY')
            nodes[entry['id']] = entry
        if sum(node['kind'] == 'commit' for node in nodes.values()) > 1 or sum(
                node['kind'] == 'manifest' for node in nodes.values()) > 1:
            return result('CANDIDATES_AMBIGUOUS')
        commit = nodes.get(first['commit'])
        manifest = nodes.get(first['manifest'])
        if (commit is None or manifest is None or commit['kind'] != 'commit' or manifest['kind'] != 'manifest'
                or commit['generation'] != first['generation'] or manifest['generation'] != first['generation']
                or commit['manifest'] != first['manifest'] or manifest['commit'] != first['commit']
                or commit['anchor'] != manifest['anchor']):
            return result('MANIFEST_MISMATCH')

        if fixture.approval is None:
            return result('APPROVAL_UNKNOWN')
        if not _check_dict(fixture.approval, {'generation', 'commit', 'manifest', 'anchor'}):
            return result('INVALID_FIXTURE')
        if fixture.approval != {'generation': first['generation'], 'commit': first['commit'],
                                'manifest': first['manifest'], 'anchor': manifest['anchor']}:
            return result('APPROVAL_MISMATCH')

        error = step('ref_last')
        if error:
            return result(error)
        last = fixture.references[1]
        if not _check_dict(last, keys) or last != first:
            return result('REFERENCE_CHANGED')
        if fixture.user_approved is not True:
            return result('SCOPE_NOT_AUTHORIZED')
        if fixture.app_identity != 'fictional-utt' or fixture.folder_scope != 'fictional-app-folder':
            return result('WRONG_SCOPE')
        if fixture.auth_state == 'EXPIRED':
            return result('AUTH_EXPIRED')
        if fixture.auth_state != 'VALID':
            return result('AUTH_REQUIRED')
        return result('SYNTHETIC_CHAIN_MATCH')
    except Exception:
        # Never propagate names, IDs, input content or exception messages.
        return result('INVALID_FIXTURE')


def inspect_live(*_args: Any, **_kwargs: Any) -> ExplorationResult:
    """Unconditionally blocked: no input inspection, environment, or network."""
    return ExplorationResult('BLOCKED', source='LIVE_NOT_RUN')
